"""Client du serveur de tir du Pi (remote/pi/fire_server.py), côté machine B.

  * synchronise l'horloge de B (`time.perf_counter()` : ATTENTION, `time.monotonic()` n'a que 15,6 ms de résolution sous Windows avec Python <= 3.12)
    avec l'horloge monotone du Pi (échanges PING/PONG, méthode NTP : 4 horodatages, on garde les échanges au plus petit
    RTT et on ajuste une droite pour suivre la dérive des deux quartz) ;
  * demande un tir à une échéance donnée dans l'horloge de B : le Pi l'exécute avec sa propre horloge (la gigue du réseau B -> Pi disparaît tant que
    l'ordre arrive avant l'échéance) ;
  * chaque ordre est signé (HMAC) et envoyé deux fois (le Pi ignore le doublon) ; les ACK donnent le retard réel d'exécution mesuré sur le Pi.

    fx = PiFire("100.72.6.43"); fx.start()
    fx.fire_at(time.perf_counter() + 0.050)       # tir dans 50 ms (horloge de B = perf_counter)
    fx.fire_now(); fx.cancel(); fx.stats()

Secret : variable AIMLABS_PI_KEY (hex) ou fichier ~/.aimlabs/pi_fire.key (jamais dans le dépôt).
"""
import collections
import hashlib
import hmac
import os
import socket
import struct
import threading
import time

MAGIC = 0xA7
T_PING, T_PONG, T_FIRE, T_CANCEL, T_ACK = 1, 2, 3, 4, 5
HDR = struct.Struct("<BBI")
P_PING = struct.Struct("<Q")
P_PONG = struct.Struct("<QQQ")
P_FIRE = struct.Struct("<QHBBQI")
P_ACK = struct.Struct("<BQQQ")
MAC_LEN = 8
STATUS = {0: "ok", 1: "trop tard", 2: "pas d'hôte", 3: "touche refusée", 4: "annulé"}


def load_key(key=None) -> bytes:
    if key:
        return key if isinstance(key, bytes) else bytes.fromhex(key)
    h = os.environ.get("AIMLABS_PI_KEY")
    if not h:
        with open(os.path.expanduser("~/.aimlabs/pi_fire.key")) as f:
            h = f.read().strip()
    return bytes.fromhex(h)


class PiFire:
    def __init__(self, host, port=7780, key=None, usage=10, hold_ms=25, late_ms=3.0, redundancy=2, sync_hz=10.0, window_s=30.0):
        self.addr = (host, port)
        self.key = load_key(key)
        self.usage, self.hold_ms, self.late_us, self.redundancy = usage, hold_ms, int(late_ms * 1000), redundancy
        self.sync_period, self.window_s = 1.0 / sync_hz, window_s
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.2)
        self.seq = 0
        self.samples = collections.deque(maxlen=int(sync_hz * window_s) + 8)   # (t4_ns, rtt_ns, theta_ns)
        self.fit = None                                                       # (t_ref_ns, theta_ref_ns, drift ns/ns, rtt_best_ns, n)
        self.acks = collections.deque(maxlen=256)                              # (seq, status, due_pi, t_pre, t_post, t_rx_b)
        self.acks_new = collections.deque(maxlen=256)                          # idem, vidé par drain_acks()
        self.sent = {}                                                         # seq -> (due_pi_ns, due_b_ns) des ordres récents
        self.counters = {"pings": 0, "pongs": 0, "fires": 0, "acks": 0, "bad_mac": 0}
        self.stop = threading.Event()
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ cycle de vie
    def start(self, wait_sync=5.0):
        threading.Thread(target=self._rx_loop, daemon=True, name="pifire-rx").start()
        threading.Thread(target=self._sync_loop, daemon=True, name="pifire-sync").start()
        t_end = time.monotonic() + wait_sync
        while time.monotonic() < t_end:
            if self.healthy():
                return True
            time.sleep(0.05)
        return False

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ paquets
    def _pack(self, typ, seq, payload=b""):
        body = HDR.pack(MAGIC, typ, seq) + payload
        return body + hmac.new(self.key, body, hashlib.sha256).digest()[:MAC_LEN]

    def _sync_loop(self):
        while not self.stop.is_set():
            self.seq += 1
            t1 = time.perf_counter_ns()
            try:
                self.sock.sendto(self._pack(T_PING, self.seq, P_PING.pack(t1)), self.addr)
                self.counters["pings"] += 1
            except OSError:
                pass
            time.sleep(self.sync_period)

    def _rx_loop(self):
        while not self.stop.is_set():
            try:
                d, _ = self.sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                return
            t4 = time.perf_counter_ns()
            if len(d) < HDR.size + MAC_LEN:
                continue
            body, tag = d[:-MAC_LEN], d[-MAC_LEN:]
            if not hmac.compare_digest(tag, hmac.new(self.key, body, hashlib.sha256).digest()[:MAC_LEN]):
                self.counters["bad_mac"] += 1
                continue
            magic, typ, seq = HDR.unpack_from(body)
            p = body[HDR.size:]
            if magic != MAGIC:
                continue
            if typ == T_PONG and len(p) == P_PONG.size:
                t1, t2, t3 = P_PONG.unpack(p)
                self.counters["pongs"] += 1
                rtt = (t4 - t1) - (t3 - t2)
                theta = ((t2 - t1) + (t3 - t4)) // 2                  # horloge du Pi − horloge de B
                with self.lock:
                    self.samples.append((t4, rtt, theta))
                self._refit()
            elif typ == T_ACK and len(p) == P_ACK.size:
                st, due, t_pre, t_post = P_ACK.unpack(p)
                self.counters["acks"] += 1
                self.acks.append((seq, st, due, t_pre, t_post, t4))
                self.acks_new.append((seq, st, due, t_pre, t_post, t4))

    def _refit(self):
        """Droite θ(t) sur les échanges au plus petit RTT de la fenêtre (le meilleur RTT borne l'erreur d'asymétrie à RTT/2)."""
        now = time.perf_counter_ns()
        with self.lock:
            s = [x for x in self.samples if now - x[0] <= self.window_s * 1e9]
        if len(s) < 3:
            return
        best = sorted(s, key=lambda x: x[1])[:max(3, len(s) // 5)]
        n = len(best)
        t0 = sum(x[0] for x in best) / n
        th0 = sum(x[2] for x in best) / n
        sxx = sum((x[0] - t0) ** 2 for x in best)
        slope = sum((x[0] - t0) * (x[2] - th0) for x in best) / sxx if sxx > 1e12 else 0.0     # ns/ns ; dérive plausible : ±1e-4
        slope = max(-2e-4, min(2e-4, slope))
        self.fit = (t0, th0, slope, best[0][1], n)

    # ------------------------------------------------------------------ horloge
    def healthy(self, max_age_s=2.0):
        return self.fit is not None and bool(self.samples) and time.perf_counter_ns() - self.samples[-1][0] < max_age_s * 1e9

    def offset_ns(self, at_b_ns=None):
        """Horloge du Pi − horloge de B (ns) à l'instant `at_b_ns` (horloge de B)."""
        t0, th0, slope, _, _ = self.fit
        t = time.perf_counter_ns() if at_b_ns is None else at_b_ns
        return int(th0 + slope * (t - t0))

    def rtt_best_ms(self):
        return self.fit[3] / 1e6 if self.fit else None

    # ------------------------------------------------------------------ tirs
    def fire_at(self, due_b_s, lead_s=0.0, usage=None, hold_ms=None, tag=0):
        """Tir à `due_b_s` (secondes, horloge `time.perf_counter()` de B) ; `lead_s` : on appuie ce temps AVANT (latence Bluetooth moyenne à compenser)."""
        if not self.healthy():
            return False
        due_b_ns = int(due_b_s * 1e9)
        due_pi = due_b_ns + self.offset_ns(due_b_ns) - int(lead_s * 1e9)
        return self._send_fire(due_pi, due_b_ns, usage, hold_ms, tag)

    def fire_now(self, usage=None, hold_ms=None, tag=0):
        return self._send_fire(0, time.perf_counter_ns(), usage, hold_ms, tag)

    def _send_fire(self, due_pi, due_b_ns, usage, hold_ms, tag):
        self.seq += 1
        seq = self.seq
        pkt = self._pack(T_FIRE, seq, P_FIRE.pack(due_pi, int(hold_ms or self.hold_ms), usage or self.usage, 0, tag, self.late_us))
        self.sent[seq] = (due_pi, due_b_ns)
        if len(self.sent) > 512:
            self.sent.pop(next(iter(self.sent)))
        try:
            for _ in range(self.redundancy):
                self.sock.sendto(pkt, self.addr)
        except OSError:
            return False
        self.counters["fires"] += 1
        return True

    def cancel(self):
        self.seq += 1
        try:
            self.sock.sendto(self._pack(T_CANCEL, self.seq), self.addr)
            return True
        except OSError:
            return False

    def drain_acks(self):
        """ACK reçus depuis le dernier appel : (nombre, statuts, retard moyen et maximal d'exécution sur le Pi en ms pour les tirs exécutés)."""
        items = []
        while self.acks_new:
            items.append(self.acks_new.popleft())
        late = [(t_pre - due) / 1e6 for _, st, due, t_pre, _, _ in items if st == 0 and due]
        by = {}
        for _, st, *_ in items:
            by[STATUS.get(st, st)] = by.get(STATUS.get(st, st), 0) + 1
        return len(items), by, (sum(late) / len(late) if late else None), (max(late) if late else None)

    def stats(self):
        f = self.fit
        return {"healthy": self.healthy(), "rtt_best_ms": None if f is None else round(f[3] / 1e6, 3), "drift_ppm": None if f is None else round(f[2] * 1e6, 2),
                "points": None if f is None else f[4], **self.counters}
