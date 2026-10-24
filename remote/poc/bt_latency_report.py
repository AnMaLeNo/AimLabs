"""Latence du chemin « Pi -> Bluetooth HID -> Windows » : apparie les frappes envoyées par le Pi (bt_probe_send.py) et vues par la
sonde Windows (kbd_probe_win.py), en recalant les horloges avec le décalage mesuré par clock_sync.py (serveur − client, en ms).

    python bt_latency_report.py bench_results/bt_probe_pi_1.jsonl bench_results/bt_probe_win_1.jsonl --offset-ms -19.2

Latence = (réception sur le PC) − (envoi sur le Pi, ramené à l'horloge du PC). Incertitude de la moyenne : celle du décalage (± RTT_min/2) ;
la gigue (écart-type, percentiles) n'en dépend pas.
"""
import argparse
import json
import statistics


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8")]


def pct(a, q):
    a = sorted(a)
    return a[min(int(len(a) * q / 100), len(a) - 1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pi")
    ap.add_argument("win")
    ap.add_argument("--offset-ms", type=float, required=True, help="horloge du Pi − horloge du PC (clock_sync.py client <Pi>)")
    ap.add_argument("--hist", action="store_true")
    a = ap.parse_args()
    pi, win = load(a.pi), load(a.win)
    off = a.offset_ms * 1e6
    for kind, wdown in (("down", True), ("up", False)):
        ps = [r for r in pi if r["kind"] == kind]
        ws = [r for r in win if r["down"] == wdown]
        n = min(len(ps), len(ws))
        lat = [(ws[i]["wall_ns"] - (ps[i]["send_ns"] - off)) / 1e6 for i in range(n)]
        ack = [(ps[i]["ack_ns"] - ps[i]["send_ns"]) / 1e6 for i in range(n)]
        print(f"{kind:4s} n={n} (Pi {len(ps)}, PC {len(ws)}) : latence moy {statistics.mean(lat):6.2f} ms, écart-type {statistics.stdev(lat):5.2f}, "
              f"min {min(lat):6.2f}, p50 {pct(lat, 50):6.2f}, p90 {pct(lat, 90):6.2f}, p99 {pct(lat, 99):6.2f}, max {max(lat):6.2f} | "
              f"appel du relais côté Pi : moy {statistics.mean(ack):.2f} ms")
        if a.hist and kind == "down":
            lo = int(min(lat))
            for b in range(lo, int(max(lat)) + 2):
                c = sum(1 for x in lat if b <= x < b + 1)
                print(f"  {b:4d}-{b + 1:<4d} ms {c:4d} " + "#" * (c * 60 // max(n, 1)))


if __name__ == "__main__":
    main()
