"""Serveur de tir UDP de btkbd : la machine B (Mac) demande au Pi d'appuyer sur la touche de tir du jeu, à un instant précis.

Chemin : B --UDP--> Pi --Bluetooth HID--> PC Windows --> jeu (le jeu ne voit qu'un clavier). Le Pi exécute l'instant demandé avec sa propre
horloge (SCHED_FIFO, attente active sur les dernières ms) : la gigue du réseau entre B et le Pi disparaît tant que l'ordre arrive avant l'échéance.

Sécurité (ce service injecte des frappes dans le PC) :
  * chaque paquet porte un HMAC-SHA256 (8 octets) calculé avec un secret partagé (fichier de configuration, mode 0600) ;
  * SEULES les touches de la liste blanche (`usages`) sont injectées, appui limité à `max_hold_ms` ;
  * un FIRE déjà vu (même `seq`) est ignoré (le client l'envoie deux fois) ; un FIRE trop lointain ou trop ancien est refusé.

Configuration : /etc/btkbd/fire.json  {"port": 7780, "key_hex": "…", "usages": [9], "max_hold_ms": 100, "late_us": 3000, "rt_prio": 40, "cpu": 3}

Protocole (little-endian ; tout paquet = en-tête + charge + HMAC[:8]) :
  en-tête   <BBI>      magic 0xA7, type, seq
  PING  1   <Q>        t1 = horloge monotone de B à l'envoi
  PONG  2   <QQQ>      t1 (écho), t2 = réception sur le Pi, t3 = envoi depuis le Pi   (horloge monotone du Pi, ns)
  FIRE  3   <QHBBQI>   échéance (horloge monotone du Pi, ns ; 0 = tout de suite), appui (ms), touche (usage HID), modificateurs, étiquette, tolérance de retard (µs)
  CANCEL 4  <>         annule le tir en attente
  ACK   5   <BQQQ>     statut (0 ok, 1 trop tard, 2 pas d'hôte, 3 touche refusée, 4 annulé), échéance (écho), t_avant, t_après l'injection (monotone du Pi)

Flux des touches du clavier vers B (2026-10-25 : le Pi est le seul à voir le clavier physique ; B s'en sert pour ses propres commandes : bascule / maintien du tir auto…).
  KSUB  6   B -> Pi, à renouveler toutes les ~1 s (abonnement : 5 s sans renouvellement = fin) : octet drapeaux (bit0 = toutes les touches, bit1 = ne rien transmettre au PC : tout est avalé ; pour l'assignation d'une touche),
            n puis n touches À SURVEILLER (usages HID ; 0xE0-0xE7 = modificateurs), n puis n touches À AVALER (non transmises au PC tant que B est abonné ; 8 au plus)
  KEYS  7   <QIBB> + ...  Pi -> B : t (horloge monotone du Pi), numéro du dernier évènement, n touches enfoncées, m évènements récents ; suivis de n usages puis de
            m × <IBB> (numéro, usage, 1 = appui / 0 = relâché). Envoyé à chaque évènement (deux fois, 3 ms d'écart) et toutes les 200 ms (battement) :
            B recompose les évènements perdus par leurs numéros et corrige un appui « coincé » par la liste des touches enfoncées.
  CAPS  9   B -> Pi (charge vide) ; réponse CAPS (9) : json {"usages": touches de tir autorisées, "keys_all": bool}
Confidentialité : seules les touches demandées sont transmises (sauf le drapeau « toutes », qui sert à assigner une touche dans la fenêtre de réglage).
"""
import collections
import hashlib
import hmac
import json
import os
import socket
import struct
import threading
import time

CONF_PATH = "/etc/btkbd/fire.json"
MAGIC = 0xA7
T_PING, T_PONG, T_FIRE, T_CANCEL, T_ACK = 1, 2, 3, 4, 5
T_KSUB, T_KEYS, T_CAPS = 6, 7, 9
HDR = struct.Struct("<BBI")
P_PING = struct.Struct("<Q")
P_PONG = struct.Struct("<QQQ")
P_FIRE = struct.Struct("<QHBBQI")
P_ACK = struct.Struct("<BQQQ")
P_KEYS = struct.Struct("<QIBB")
P_EV = struct.Struct("<IBB")
MAC_LEN = 8
SUB_TTL_S = 5.0
MAX_SUBS, MAX_SWALLOW, KEYS_REPEAT_S, KEYS_BEAT_S = 4, 8, 0.003, 0.2
ST_OK, ST_LATE, ST_NO_HOST, ST_DENIED, ST_CANCELLED = 0, 1, 2, 3, 4

mono_ns = time.monotonic_ns


def log(*a):
    import sys
    print("fire:", *a, file=sys.stderr, flush=True)


def mac(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, body, hashlib.sha256).digest()[:MAC_LEN]


class FireServer:
    def __init__(self, relay, hid, conf_path=CONF_PATH):
        with open(conf_path) as f:
            c = json.load(f)
        self.relay, self.hid = relay, hid
        self.key = bytes.fromhex(c["key_hex"])
        self.port = int(c.get("port", 7780))
        self.usages = {int(u) for u in c["usages"]}
        self.max_hold_ms = int(c.get("max_hold_ms", 100))
        self.late_us = int(c.get("late_us", 3000))
        self.rt_prio = int(c.get("rt_prio", 40))
        self.cpu = c.get("cpu")
        self.keepalive_ms = float(c.get("keepalive_ms", 100))      # 0 = désactivé
        self.sock = None
        self.cv = threading.Condition()
        self.pending = None            # (due_ns, hold_ms, usage, mods, seq, tag, late_us, addr)
        self.seen = collections.deque(maxlen=512)
        self.seen_set = set()
        self.stats = {"pings": 0, "fires": 0, "executed": 0, "late": 0, "no_host": 0, "denied": 0, "bad_mac": 0, "dup": 0, "cancelled": 0}
        self.log = collections.deque(maxlen=200)   # (due, t_pre, t_post, hold, usage, seq, tag) : mesures du Pi
        self.rx_log = collections.deque(maxlen=4000)   # (t_rx, seq, tag, due, avance à l'arrivée) de chaque ordre programmé reçu
        # flux des touches
        self.keys_all = bool(c.get("keys_all", True))
        self.subs = {}                 # addr -> {"t", "all", "watch", "swallow", "seq", "ev": deque[(seq, usage, down)], "resend": instant|None}
        self.phys = set()              # usages physiquement enfoncés (indépendamment de ce qui est relayé au PC)
        self.kq = collections.deque()  # (usage, down, t_ns) en attente de distribution
        self.kev = threading.Event()
        self.klock = threading.Lock()
        relay.on_key = self.on_key

    # ------------------------------------------------------------------ réseau
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        threading.Thread(target=self._rx_loop, daemon=True, name="fire-rx").start()
        threading.Thread(target=self._sched_loop, daemon=True, name="fire-sched").start()
        threading.Thread(target=self._keys_loop, daemon=True, name="fire-keys").start()
        if self.keepalive_ms > 0:
            threading.Thread(target=self._keepalive_loop, daemon=True, name="fire-keepalive").start()
        log("serveur de tir sur UDP %d, touches autorisées %s, keep-alive %g ms" % (self.port, sorted(self.usages), self.keepalive_ms))

    def _keepalive_loop(self):
        while True:
            time.sleep(self.keepalive_ms / 1000)
            try:
                self.relay.keepalive()
            except Exception as e:
                log("keep-alive :", repr(e))

    def _send(self, typ, seq, payload, addr):
        body = HDR.pack(MAGIC, typ, seq) + payload
        try:
            self.sock.sendto(body + mac(self.key, body), addr)
        except OSError:
            pass

    def _rx_loop(self):
        while True:
            try:
                d, addr = self.sock.recvfrom(512)
            except OSError:
                return
            t2 = mono_ns()
            if len(d) < HDR.size + MAC_LEN:
                continue
            body, tag = d[:-MAC_LEN], d[-MAC_LEN:]
            if not hmac.compare_digest(tag, mac(self.key, body)):
                self.stats["bad_mac"] += 1
                continue
            magic, typ, seq = HDR.unpack_from(body)
            if magic != MAGIC:
                continue
            p = body[HDR.size:]
            try:
                if typ == T_PING and len(p) == P_PING.size:
                    self.stats["pings"] += 1
                    (t1,) = P_PING.unpack(p)
                    self._send(T_PONG, seq, P_PONG.pack(t1, t2, mono_ns()), addr)
                elif typ == T_FIRE and len(p) == P_FIRE.size:
                    self._on_fire(seq, P_FIRE.unpack(p), addr, t2)
                elif typ == T_KSUB:
                    self._on_ksub(p, addr)
                elif typ == T_CAPS:
                    self._send(T_CAPS, seq, json.dumps({"usages": sorted(self.usages), "keys_all": self.keys_all}).encode(), addr)
                elif typ == T_CANCEL:
                    with self.cv:
                        # n'annule que les ordres PLUS ANCIENS que l'annulation (numéro de séquence) : un tir envoyé juste après elle, ou
                        # qui la doublerait sur le réseau, n'est jamais perdu
                        if self.pending is not None and self.pending[4] < seq:
                            self.pending = None
                            self.stats["cancelled"] += 1
                            self.cv.notify_all()
            except Exception as e:
                log("paquet ignoré :", repr(e))

    def _on_fire(self, seq, f, addr, t_rx):
        due, hold_ms, usage, mods, tag, late_us = f
        if seq in self.seen_set:                       # le client envoie chaque ordre deux fois
            self.stats["dup"] += 1
            return
        if len(self.seen) == self.seen.maxlen:
            self.seen_set.discard(self.seen[0])
        self.seen.append(seq)
        self.seen_set.add(seq)
        self.stats["fires"] += 1
        if usage not in self.usages or mods:           # liste blanche : jamais d'autre touche, jamais de modificateur
            self.stats["denied"] += 1
            self._send(T_ACK, seq, P_ACK.pack(ST_DENIED, due, 0, 0), addr)
            return
        now = mono_ns()
        if due == 0:
            due = now
        else:
            self.rx_log.append((t_rx, seq, tag, due, due - t_rx))      # avance de l'ordre sur son échéance À L'ARRIVÉE (< 0 : arrivé en retard) : mesure du réseau B -> Pi
        if due > now + 2_000_000_000 or due < now - 1_000_000_000:       # trop lointain / trop ancien : refus
            self.stats["late"] += 1
            self._send(T_ACK, seq, P_ACK.pack(ST_LATE, due, 0, 0), addr)
            return
        tol_us = late_us or self.late_us
        if due < now - tol_us * 1000:
            # Déjà trop tard : ne REMPLACE PAS l'ordre en attente (un ordre plus ancien, encore valable, partirait à l'heure). B renvoie un ordre mis à jour
            # à chaque image ; sur Wi-Fi certains arrivent après leur échéance et faisaient perdre le précédent.
            self.stats["late"] += 1
            self._send(T_ACK, seq, P_ACK.pack(ST_LATE, due, 0, 0), addr)
            return
        hold_ms = max(5, min(int(hold_ms), self.max_hold_ms))
        with self.cv:
            self.pending = (due, hold_ms, usage, mods, seq, tag, tol_us, addr)
            self.cv.notify_all()

    # ------------------------------------------------------------ flux des touches
    def on_key(self, usage, down):
        """Appelé par le relais (fil du clavier) APRÈS l'envoi du rapport au PC : ne fait que ranger l'évènement (quelques µs)."""
        self.kq.append((usage, down, mono_ns()))
        self.kev.set()

    def _on_ksub(self, p, addr):
        try:
            flags, nw = p[0], p[1]
            watch = set(p[2:2 + nw])
            ns = p[2 + nw]
            swallow = set(p[3 + nw:3 + nw + ns])
        except IndexError:
            return
        if len(swallow) > MAX_SWALLOW or len(watch) > 32:
            return
        with self.klock:
            new = addr not in self.subs
            if new and len(self.subs) >= MAX_SUBS:
                return
            sub = self.subs.setdefault(addr, {"seq": 0, "ev": collections.deque(maxlen=6), "resend": None})
            sub.update(t=time.monotonic(), all=bool(flags & 1) and self.keys_all, watch=watch, swallow=swallow, swallow_all=bool(flags & 2))
            self._apply_swallow()
        self._send_keys(addr, sub)
        if new:
            log("abonné aux touches :", addr[0], "surveille", sorted(watch), "avale", sorted(swallow))

    def _apply_swallow(self):
        """Touches non transmises au PC : union de celles des abonnés VIVANTS (un B disparu rend aussitôt le clavier entier au PC)."""
        self.relay.swallow = frozenset(u for s in self.subs.values() for u in s["swallow"])
        self.relay.swallow_all = any(s.get("swallow_all") for s in self.subs.values())

    def _send_keys(self, addr, sub):
        held = sorted(u for u in self.phys if sub["all"] or u in sub["watch"])[:14]
        evs = list(sub["ev"])
        pl = P_KEYS.pack(mono_ns(), sub["seq"], len(held), len(evs)) + bytes(held) + b"".join(P_EV.pack(*e) for e in evs)
        self._send(T_KEYS, sub["seq"], pl, addr)

    def _keys_loop(self):
        beat = 0.0
        while True:
            self.kev.wait(KEYS_REPEAT_S if any(s["resend"] for s in self.subs.values()) else KEYS_BEAT_S)
            self.kev.clear()
            now = time.monotonic()
            try:
                while self.kq:
                    usage, down, _t = self.kq.popleft()
                    with self.klock:
                        if down == (usage in self.phys):
                            continue                                     # répétition / doublon : pas un changement
                        (self.phys.add if down else self.phys.discard)(usage)
                        subs = list(self.subs.items())
                    for addr, sub in subs:
                        if sub["all"] or usage in sub["watch"]:
                            sub["seq"] += 1
                            sub["ev"].append((sub["seq"], usage, int(down)))
                            self._send_keys(addr, sub)
                            sub["resend"] = now + KEYS_REPEAT_S
                for addr, sub in list(self.subs.items()):
                    if now - sub["t"] > SUB_TTL_S:
                        with self.klock:
                            self.subs.pop(addr, None)
                            self._apply_swallow()
                        log("abonnement terminé :", addr[0])
                    elif sub["resend"] and now >= sub["resend"]:
                        sub["resend"] = None
                        self._send_keys(addr, sub)
                if now - beat >= KEYS_BEAT_S:
                    beat = now
                    for addr, sub in list(self.subs.items()):
                        self._send_keys(addr, sub)
            except Exception as e:
                log("flux des touches :", repr(e))

    # ------------------------------------------------------------ exécution
    def _sched_loop(self):
        try:
            if self.rt_prio:
                os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(self.rt_prio))   # 0 = ce thread (Linux)
        except (OSError, AttributeError) as e:     # AttributeError : pas Linux (tests sur un autre système)
            log("SCHED_FIFO refusé :", e)
        try:
            if self.cpu is not None:
                os.sched_setaffinity(0, {int(self.cpu)})
        except (OSError, AttributeError) as e:
            log("affinité refusée :", e)
        while True:
            with self.cv:
                while self.pending is None:
                    self.cv.wait()
                p = self.pending
            due = p[0]
            while True:                                 # sommeil jusqu'à ~2 ms avant l'échéance, puis attente active
                d = due - mono_ns()
                if d <= 0:
                    break
                if d > 3_000_000:
                    with self.cv:
                        self.cv.wait((d - 2_000_000) / 1e9)
                        if self.pending is not p:
                            break
                else:
                    while mono_ns() < due:
                        pass
                    break
            with self.cv:
                if self.pending is not p:
                    continue                            # remplacé ou annulé pendant l'attente
                self.pending = None
            self._execute(p)

    def _execute(self, p):
        due, hold_ms, usage, mods, seq, tag, late_us, addr = p
        t_pre = mono_ns()
        if t_pre - due > late_us * 1000:
            self.stats["late"] += 1
            self._send(T_ACK, seq, P_ACK.pack(ST_LATE, due, t_pre, t_pre), addr)
            return
        if self.hid.peer is None:
            self.stats["no_host"] += 1
            self._send(T_ACK, seq, P_ACK.pack(ST_NO_HOST, due, t_pre, t_pre), addr)
            return
        self.relay.inject(usage, mods, True)
        t_post = mono_ns()
        self.stats["executed"] += 1
        self.log.append((due, t_pre, t_post, hold_ms, usage, seq, tag))
        self._send(T_ACK, seq, P_ACK.pack(ST_OK, due, t_pre, t_post), addr)
        time.sleep(hold_ms / 1000)
        self.relay.inject(usage, mods, False)

    def status(self):
        return {"port": self.port, "usages": sorted(self.usages), "stats": dict(self.stats),
                "subscribers": [a[0] for a in self.subs], "swallow": sorted(self.relay.swallow), "swallow_all": self.relay.swallow_all}
