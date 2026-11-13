"""Décompose, pour les essais « coup de souris » du banc scénarisé (flick_side, flick_far, sweep), le délai entre l'arrivée du
réticule sur la cible et le tir, d'après l'enregistrement du jeu.

    python tools/behavior/flick_delay.py <partie.aimbeh> [...] [--names a,b] [--band 0.7] [--cases flick_side]

  bord → tir    : du premier instant où le réticule touche la cible (ce que case_report appelle « dessus → tir ») au tir ;
  bord → bande  : temps que met le réticule pour aller du bord de la cible à sa bande centrale (± `band` × demi-largeur) :
                  c'est le geste du joueur, pas la chaîne ; un programme prudent ne tire pas avant (il raterait au bord) ;
  bande → tir   : ce qui reste = la chaîne (affichage, capture, envoi, calcul, retour, prise en compte par le jeu) et, s'il y
                  en a une, l'attente imposée par une règle. Négatif = tir anticipé (programmé avant l'entrée dans la bande).
Pour chaque partie : médiane, moyenne, 10e et 90e centiles ; pour deux parties, l'écart des moyennes de « bande → tir » et son
intervalle à 95 % (bootstrap) : dit si une différence de quelques ms est réelle ou du bruit.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aimbeh import load  # noqa: E402
import case_report as CR  # noqa: E402


def decompose(log, cases, band):
    fr = log.frame
    ft = fr["t"].astype(np.float64) / 1e3
    out = []
    for t in CR.trials(log):
        if t["case"] not in cases or t["tir"] is None or t["dessus"] is None:
            continue
        i1, i2 = int(np.searchsorted(ft, t["dessus"])), int(np.searchsorted(ft, t["tir"] + 60.0))
        f = fr[i1:i2]
        ok = (f["near_id"] == t["subject"]) & (np.abs(f["off_x"]) <= band * f["half_w"]) & (np.abs(f["off_y"]) <= 0.9 * f["half_h"])
        k = np.nonzero(ok)[0]
        if not len(k):
            continue
        t_band = float(ft[i1 + k[0]])
        j = int(np.searchsorted(ft, t["tir"])) - 1
        out.append(dict(case=t["case"], param=t["param"], edge_to_shot=t["tir"] - t["dessus"], edge_to_band=t_band - t["dessus"],
                        band_to_shot=t["tir"] - t_band, off_at_shot=float(abs(fr["off_x"][j]) / max(fr["half_w"][j], 1e-6))))
    return out


def stats(x):
    x = np.asarray(x, float)
    return f"médiane {np.median(x):6.1f} · moyenne {x.mean():6.1f} · p10 {np.percentile(x, 10):6.1f} · p90 {np.percentile(x, 90):6.1f} ms"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--names", default="")
    ap.add_argument("--band", type=float, default=0.7)
    ap.add_argument("--cases", default="flick_side")
    a = ap.parse_args()
    names = a.names.split(",") if a.names else [os.path.basename(p)[:40] for p in a.logs]
    groups = {}
    for name, p in zip(names, a.logs):
        groups.setdefault(name, []).extend(decompose(load(p), set(a.cases.split(",")), a.band))
    for name, rows in groups.items():
        if not rows:
            print(f"{name} : aucun essai touché")
            continue
        print(f"{name} ({len(rows)} essais touchés)")
        print(f"  bord → tir    {stats([r['edge_to_shot'] for r in rows])}")
        print(f"  bord → bande  {stats([r['edge_to_band'] for r in rows])}")
        print(f"  bande → tir   {stats([r['band_to_shot'] for r in rows])}")
        print(f"  écart du réticule au centre de la cible au moment du tir : médiane {np.median([r['off_at_shot'] for r in rows]):.2f} demi-largeur")
    keys = list(groups)
    rng = np.random.default_rng(0)
    for i in range(1, len(keys)):
        x = np.array([r["band_to_shot"] for r in groups[keys[0]]])
        y = np.array([r["band_to_shot"] for r in groups[keys[i]]])
        if len(x) < 5 or len(y) < 5:
            continue
        d = [rng.choice(y, len(y)).mean() - rng.choice(x, len(x)).mean() for _ in range(4000)]
        lo, hi = np.percentile(d, [2.5, 97.5])
        print(f"« {keys[i]} » − « {keys[0]} », bande → tir : {y.mean() - x.mean():+.1f} ms (intervalle à 95 % : {lo:+.1f} à {hi:+.1f} ms)"
              + ("  → différence non établie" if lo <= 0 <= hi else "  → différence réelle"))


if __name__ == "__main__":
    sys.exit(main())
