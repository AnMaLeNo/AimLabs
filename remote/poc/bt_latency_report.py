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
    a_hist = a.hist
    pi, win = load(a.pi), load(a.win)
    off = a.offset_ms * 1e6
    for kind, wdown in (("down", True), ("up", False)):
        ps = [r for r in pi if r["kind"] == kind]
        ws = sorted(r["wall_ns"] for r in win if r["down"] == wdown)
        # appariement PAR LE TEMPS (pas par l'ordre : une frappe perdue décalerait tout) : premier évènement du PC dans [envoi − 10 ms, envoi + 200 ms]
        lat, lost, j, by_gap = [], 0, 0, {}
        for r in ps:
            t = r["send_ns"] - off
            while j < len(ws) and ws[j] < t - 10e6:
                j += 1
            if j < len(ws) and ws[j] <= t + 200e6:
                lat.append((ws[j] - t) / 1e6)
                by_gap.setdefault(r.get("gap_ms"), []).append(lat[-1])
                j += 1
            else:
                lost += 1
        n = len(lat)
        if n < 2:
            print(f"{kind:4s} : {n} appariées sur {len(ps)} envoyées")
            continue
        ack = [(r["ack_ns"] - r["send_ns"]) / 1e6 for r in ps]
        print(f"{kind:4s} n={n} (envoyées {len(ps)}, perdues {lost} = {100 * lost / len(ps):.1f} %, vues par le PC {len(ws)}) : latence moy {statistics.mean(lat):6.2f} ms, "
              f"écart-type {statistics.stdev(lat):5.2f}, min {min(lat):6.2f}, p50 {pct(lat, 50):6.2f}, p90 {pct(lat, 90):6.2f}, p99 {pct(lat, 99):6.2f}, max {max(lat):6.2f}")
        if kind == "down" and len(by_gap) > 1:
            for g in sorted(k for k in by_gap if k is not None):
                v = by_gap[g]
                if len(v) > 1:
                    print(f"     après {g:7.0f} ms d'inactivité : n={len(v):3d} moy {statistics.mean(v):6.2f} ms, écart-type {statistics.stdev(v):5.2f}, min {min(v):6.2f}, max {max(v):6.2f}")
        if a_hist and kind == "down":
            lo = int(min(lat))
            for bn in range(lo, int(max(lat)) + 2):
                c = sum(1 for x in lat if bn <= x < bn + 1)
                if c:
                    print(f"  {bn:4d}-{bn + 1:<4d} ms {c:4d} " + "#" * (c * 60 // max(n, 1)))


if __name__ == "__main__":
    main()
