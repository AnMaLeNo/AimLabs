"""Test du chemin de tir complet B -> Pi -> Bluetooth -> Windows, avec vérité terrain côté PC.

    # PC : python kbd_probe_win.py --seconds 60 --out probe.jsonl            (touche Pause, vk 0x13)
    # B  : python pi_fire_test.py --pi <hôte du Pi> --n 100 --out sent.jsonl --usage 72      (72 = Pause : inoffensive ; la touche doit être dans la liste blanche du Pi)
    # PC : python bt_latency_report.py sent.jsonl probe.jsonl --offset-ms 0

À lancer SUR le PC pour mesurer la latence « échéance demandée -> touche vue par Windows » : l'échéance (horloge perf_counter de B = ce PC) est convertie en
horloge murale du PC, la même que celle de la sonde. Sortie : retard d'exécution mesuré sur le Pi (ACK) et fichier au format de bt_latency_report.py.
"""
import argparse
import json
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from clocks import wall_ns  # noqa: E402
from pi_fire import STATUS, PiFire  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pi", required=True)
    ap.add_argument("--port", type=int, default=7780)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--ahead-ms", type=float, default=80.0, help="échéance demandée dans X ms (≥ latence du réseau)")
    ap.add_argument("--spacing-ms", type=float, default=300.0)
    ap.add_argument("--lead-ms", type=float, default=0.0, help="compensation de la latence Bluetooth (appuyer plus tôt)")
    ap.add_argument("--usage", type=int, default=72)
    ap.add_argument("--hold-ms", type=float, default=25.0)
    ap.add_argument("--out", default="sent.jsonl")
    a = ap.parse_args()

    fx = PiFire(a.pi, a.port, usage=a.usage, hold_ms=a.hold_ms)
    if not fx.start(wait_sync=8.0):
        sys.exit("pas de synchronisation avec le Pi (secret, pare-feu, adresse ?)")
    time.sleep(3.0)                                          # laisse la fenêtre d'échantillons se remplir
    print("synchronisation :", fx.stats(), flush=True)
    w0, m0 = wall_ns(), time.perf_counter_ns()                  # relation horloge murale / monotone du PC
    out = open(a.out, "w")
    due_list = []
    t_next = time.perf_counter() + 0.3
    for i in range(a.n):
        while time.perf_counter() < t_next - 0.0015:
            time.sleep(0.0005)
        due_b = time.perf_counter() + a.ahead_ms / 1000
        theta_ms = fx.offset_ns(int(due_b * 1e9)) / 1e6 if fx.healthy() else None     # décalage Pi - B réellement utilisé pour cet ordre
        ok = fx.fire_at(due_b, lead_s=a.lead_ms / 1000)
        due_wall = int(due_b * 1e9) + (w0 - m0)
        out.write(json.dumps({"i": i, "send_ns": due_wall, "ack_ns": due_wall, "kind": "down", "ok": ok, "theta_ms": theta_ms, "due_b_ns": int(due_b * 1e9)}) + "\n")
        out.write(json.dumps({"i": i, "send_ns": due_wall + int(a.hold_ms * 1e6), "ack_ns": due_wall, "kind": "up", "ok": ok}) + "\n")
        due_list.append(due_b)
        t_next += (a.spacing_ms + random.uniform(0, 60)) / 1000
    time.sleep(1.0)
    out.close()
    acks = list(fx.acks)
    by_status = {}
    for _, st, *_ in acks:
        by_status[STATUS.get(st, st)] = by_status.get(STATUS.get(st, st), 0) + 1
    late = [(t_pre - due) / 1e6 for _, st, due, t_pre, t_post, _ in acks if st == 0]
    inj = [(t_post - t_pre) / 1e6 for _, st, due, t_pre, t_post, _ in acks if st == 0]
    print(f"ordres : {fx.counters['fires']} envoyés, {len(acks)} ACK {by_status}")
    if late:
        print(f"exécution sur le Pi : retard sur l'échéance moy {statistics.mean(late):+.3f} ms, écart-type {statistics.stdev(late):.3f}, "
              f"min {min(late):+.3f}, max {max(late):+.3f} ; durée de l'injection {statistics.mean(inj):.3f} ms")
    print("fin :", fx.stats())
    with open(a.out + ".sync", "w") as f:                    # échantillons bruts de synchronisation : (t4 ns, rtt ns, theta ns)
        for t4, rtt, th in list(fx.samples):
            f.write(json.dumps({"t4": t4, "rtt": rtt, "theta": th}) + "\n")
    fx.close()


if __name__ == "__main__":
    main()
