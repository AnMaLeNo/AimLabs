"""Mesure du réseau seul entre deux machines (sans capture ni inférence) : le client envoie `--size` octets
à `--hz` Hz, le serveur répond 40 octets ; RTT par paquet. TCP (messages) ou UDP (chunks de 1200 octets, un
paquet « perdu » = message jeté).

  machine B :  python net_echo.py serve  --port 7800
  machine A :  python net_echo.py client --host <ip de B> --port 7800 --proto tcp|udp --size 6000 --hz 165 --seconds 10
"""
import argparse
import socket
import struct
import time

import numpy as np

CH = 1200


def serve(a):
    import select
    t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(("0.0.0.0", a.port)); t.listen(1)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("0.0.0.0", a.port))
    print("serveur prêt", flush=True)
    conn, buf, need, tcp = None, {}, 0, b""
    while True:
        rl, _, _ = select.select([t, u] + ([conn] if conn else []), [], [], 1.0)
        for s_ in rl:
            if s_ is t:
                conn, _ = t.accept(); conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1); tcp = b""
            elif s_ is u:
                d, addr = u.recvfrom(2048)
                seq, i, n = struct.unpack_from("<IBB", d, 0)
                c = buf.setdefault(seq, set()); c.add(i)
                if len(c) == n:
                    u.sendto(struct.pack("<I", seq) + b"x" * 36, addr)
                    buf.pop(seq)
                if len(buf) > 64:
                    buf.pop(min(buf))
            else:
                d = conn.recv(65536)
                if not d:
                    conn = None
                    continue
                tcp += d
                while len(tcp) >= 8:
                    n, seq = struct.unpack_from("<II", tcp, 0)
                    if len(tcp) < 8 + n:
                        break
                    tcp = tcp[8 + n:]
                    conn.sendall(struct.pack("<I", seq) + b"x" * 36)


def client(a):
    rtts, lost = [], 0
    period = 1.0 / a.hz
    if a.proto == "tcp":
        s = socket.create_connection((a.host, a.port)); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        payload = b"x" * a.size
        t_end = time.perf_counter() + a.seconds; nxt = time.perf_counter(); seq = 0
        while time.perf_counter() < t_end:
            seq += 1
            t0 = time.perf_counter()
            s.sendall(struct.pack("<II", a.size, seq) + payload)
            r = s.recv(40, socket.MSG_WAITALL)
            rtts.append((time.perf_counter() - t0) * 1e3)
            nxt += period
            while time.perf_counter() < nxt:
                pass
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((a.host, a.port)); s.settimeout(0.5)
        nch = (a.size + CH - 1) // CH
        t_end = time.perf_counter() + a.seconds; nxt = time.perf_counter(); seq = 0
        while time.perf_counter() < t_end:
            seq += 1
            t0 = time.perf_counter()
            for i in range(nch):
                s.send(struct.pack("<IBB", seq, i, nch) + b"x" * min(CH, a.size - i * CH))
            try:
                while True:
                    r = s.recv(64)
                    if struct.unpack_from("<I", r)[0] == seq:
                        break
                rtts.append((time.perf_counter() - t0) * 1e3)
            except socket.timeout:
                lost += 1
            nxt += period
            while time.perf_counter() < nxt:
                pass
    r = np.array(rtts)
    print(f"{a.proto.upper()} {a.size:6d} o @ {a.hz:.0f} Hz : n={len(r)} perdus={lost} · RTT moy {r.mean():.2f} ms · p50 {np.median(r):.2f} · p90 {np.percentile(r, 90):.2f} · p99 {np.percentile(r, 99):.2f} · max {r.max():.1f}")


ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=["serve", "client"])
ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=7800)
ap.add_argument("--proto", choices=["tcp", "udp"], default="tcp")
ap.add_argument("--size", type=int, default=6000); ap.add_argument("--hz", type=float, default=165); ap.add_argument("--seconds", type=float, default=8)
a = ap.parse_args()
serve(a) if a.mode == "serve" else client(a)
