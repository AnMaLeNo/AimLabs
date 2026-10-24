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
HDR = struct.Struct("<BBI")
P_PING = struct.Struct("<Q")
P_PONG = struct.Struct("<QQQ")
P_FIRE = struct.Struct("<QHBBQI")
P_ACK = struct.Struct("<BQQQ")
MAC_LEN = 8
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

    # ------------------------------------------------------------------ réseau
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        threading.Thread(target=self._rx_loop, daemon=True, name="fire-rx").start()
        threading.Thread(target=self._sched_loop, daemon=True, name="fire-sched").start()
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
                elif typ == T_CANCEL:
                    with self.cv:
                        if self.pending is not None:
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
        if due > now + 2_000_000_000 or due < now - 1_000_000_000:       # trop lointain / trop ancien : refus
            self.stats["late"] += 1
            self._send(T_ACK, seq, P_ACK.pack(ST_LATE, due, 0, 0), addr)
            return
        hold_ms = max(5, min(int(hold_ms), self.max_hold_ms))
        with self.cv:
            self.pending = (due, hold_ms, usage, mods, seq, tag, late_us or self.late_us, addr)
            self.cv.notify_all()

    # ------------------------------------------------------------ exécution
    def _sched_loop(self):
        try:
            if self.rt_prio:
                os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(self.rt_prio))   # 0 = ce thread (Linux)
        except OSError as e:
            log("SCHED_FIFO refusé :", e)
        try:
            if self.cpu is not None:
                os.sched_setaffinity(0, {int(self.cpu)})
        except OSError as e:
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
        return {"port": self.port, "usages": sorted(self.usages), "stats": dict(self.stats)}
