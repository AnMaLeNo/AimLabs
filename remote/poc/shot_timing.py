"""Précision du tir dans les résultats d'un benchmark sniper du jeu (JSON de --bench-out), mesurée PAR LE JEU : vérité terrain indépendante de la chaîne.

    python shot_timing.py bench_results/live_*.json

Pour chaque passage de la cible, le jeu enregistre la position de la cible sur son trajet au moment du premier tir (`path_x`, m, 0 = réticule). Erreur de
timing signée = path_x × sens / vitesse (ms) : positive = tir en retard (la cible a dépassé le réticule), négative = en avance. La moyenne est le biais
(compensable avec --bias-ms) ; l'écart-type est la gigue de toute la chaîne (capture, réseau, décision, Pi, Bluetooth, jeu).
"""
import json
import statistics
import sys


def sweeps(path):
    d = json.load(open(path, encoding="utf-8"))
    for h in d.get("heights", []):
        for dist in h.get("distances", []):
            for s in dist.get("sweeps", []):
                yield s


def report(path):
    errs, hits, n, no_shot, extra = [], 0, 0, 0, 0
    for s in sweeps(path):
        if s.get("confirm"):
            continue
        n += 1
        rows = s.get("shot_rows") or []
        if not rows:
            no_shot += 1
            continue
        if s.get("outcome") == "extra_shots":
            extra += 1
        hits += 1 if s.get("outcome") == "hit" else 0
        errs.append(rows[0]["path_x"] * s["dir"] / s["speed"] * 1000)
    print(f"{path} : {n} passages, touches {hits}, sans tir {no_shot}, trop de balles {extra}")
    if len(errs) >= 2:
        a = [abs(e) for e in errs]
        print(f"  erreur de timing du 1er tir (ms, + = en retard) : moyenne {statistics.mean(errs):+.2f}, ecart-type {statistics.stdev(errs):.2f}, "
              f"min {min(errs):+.2f}, max {max(errs):+.2f} ; |erreur| mediane {statistics.median(a):.2f}, p90 {sorted(a)[int(len(a) * 0.9)]:.2f}")
    return errs


if __name__ == "__main__":
    for p in sys.argv[1:]:
        report(p)
