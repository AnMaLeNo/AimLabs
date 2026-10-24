"""Écart entre les horloges murales de deux machines (méthode NTP : aller-retour UDP, on garde les échanges au plus petit RTT).

    # sur l'une des machines (ex. le Pi)          :  python3 clock_sync.py server [--port 7781]
    # sur l'autre (ex. le PC)                     :  python clock_sync.py client <hôte> [--port 7781] [--n 400]

Sortie : décalage (horloge du serveur − horloge du client) et son incertitude. L'erreur est bornée par RTT_min / 2 si les deux sens
sont symétriques ; sur Wi-Fi l'asymétrie peut dépasser cela : on garde donc le RTT minimal et on donne la dispersion des 20 meilleurs.
"""
import argparse
import json
import socket
import statistics
import struct
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from clocks import wall_ns  # noqa: E402

PKT = struct.Struct("<IQ")


def server(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    print(f"clock_sync serveur sur le port {port}", flush=True)
    while True:
        d, addr = s.recvfrom(64)
        t = wall_ns()
        if len(d) >= 4:
            s.sendto(PKT.pack(struct.unpack_from("<I", d)[0], t), addr)


def client(host, port, n):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    rows = []
    for i in range(n):
        t1 = wall_ns()
        s.sendto(struct.pack("<I", i), (host, port))
        try:
            d, _ = s.recvfrom(64)
        except socket.timeout:
            continue
        t4 = wall_ns()
        seq, t2 = PKT.unpack(d)
        if seq != i:
            continue
        rows.append(((t4 - t1) / 1e6, (t2 - (t1 + t4) / 2) / 1e6))     # (rtt ms, décalage serveur−client ms)
        time.sleep(0.01)
    rows.sort()
    best = rows[:20]
    offs = [o for _, o in best]
    out = {"échanges": len(rows), "rtt_min_ms": round(rows[0][0], 3), "rtt_p50_ms": round(statistics.median(r for r, _ in rows), 3),
           "décalage_ms": round(statistics.median(offs), 3), "dispersion_ms": round(max(offs) - min(offs), 3),
           "incertitude_max_ms": round(rows[0][0] / 2 + (max(offs) - min(offs)) / 2, 3)}
    print(json.dumps(out, ensure_ascii=False))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["server", "client"])
    ap.add_argument("host", nargs="?")
    ap.add_argument("--port", type=int, default=7781)
    ap.add_argument("--n", type=int, default=400)
    a = ap.parse_args()
    server(a.port) if a.mode == "server" else client(a.host, a.port, a.n)
