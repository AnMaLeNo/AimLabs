"""Lecture, cas par cas, d'un enregistrement du banc scénarisé du jeu (`--cases`, game/scripts/bench_cases.gd).

    python tools/behavior/case_report.py <partie.aimbeh> [<autre.aimbeh> …] [--names sans,carré,large] [--json out.json] [--trials]

Pour chaque essai marqué par le jeu, à partir de ce que le JEU a mesuré (pas de ce que dit le programme testé) :
  * `vu`    : instant où la cible de l'essai est devenue visible (premier point en vue) ;
  * `dessus`: instant où le réticule est arrivé sur elle ;
  * le premier tir qui la touche. Délai `vu → tir` (ce qu'un humain ne peut pas faire en moins de 100 ms, et rarement en moins
    de 150) et délai `dessus → tir` (ce que l'autorisation de tir coûte au joueur : 0 = tir dès que le réticule y est).
Par cas : essais, cibles touchées, médiane et minimum de `vu → tir`, nombre de tirs sous 100 ms et entre 100 et 150 ms, médiane
de `dessus → tir`, tirs à côté. Plusieurs enregistrements = plusieurs colonnes du même tableau (configurations à comparer).
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aimbeh import load  # noqa: E402

CASES = ["flick_side", "peek_preaim", "cross_preaim", "two_far", "two_near", "sweep", "peek_far", "flick_far", "mix"]
WHAT = {
    "flick_side": "cible qui apparaît sur le côté, coup de souris humain",
    "peek_preaim": "réticule posé d'avance, la cible sort du mur dessous",
    "cross_preaim": "réticule posé dans un passage, la cible le traverse",
    "two_far": "comme peek_preaim, une autre personne visible loin à l'opposé",
    "two_near": "comme peek_preaim, une autre personne visible à 1 m",
    "sweep": "cible visible depuis longtemps, le réticule la balaie",
    "peek_far": "peek_preaim à 30 m",
    "flick_far": "flick_side à 30 m",
    "mix": "arène aléatoire, joueur simulé seul",
}


def trials(log):
    """Un dict par essai : cas, numéro, paramètre, sujet, et les instants (ms) mesurés par le jeu."""
    mk = log.mark
    out = []
    if not len(mk):
        return out
    fr = log.frame
    ft = fr["t"].astype(np.float64) / 1e3
    shots = log.shot
    st = shots["t"].astype(np.float64) / 1e3
    vis = log.visible
    keys = sorted({(int(m["case"]), int(m["trial"])) for m in mk}, key=lambda k: min(int(m["t"]) for m in mk if (int(m["case"]), int(m["trial"])) == k))
    for case, trial in keys:
        ms = {int(m["phase"]): m for m in mk if int(m["case"]) == case and int(m["trial"]) == trial}
        if 1 not in ms or 2 not in ms:
            continue
        t1, t2 = float(ms[1]["t"]) / 1e3, float(ms[2]["t"]) / 1e3
        subj = int(ms[1]["subject"])
        d = dict(case=CASES[case - 1] if 1 <= case <= len(CASES) else str(case), trial=trial, param=float(ms[1]["param"]), subject=subj,
                 t_stim=t1, t_end=t2, vu=None, dessus=None, tir=None, react=None, tot=None, hit=False, shots=0, misses=0, wrong=0)
        # visibilité du sujet : l'épisode en cours au stimulus (cible déjà visible : sweep), sinon le premier qui commence après
        v = vis[vis["id"] == subj]
        vt = v["t"].astype(np.float64) / 1e3
        on_at_stim = False
        k = int(np.searchsorted(vt, t1 - 1.0, side="right")) - 1
        if k >= 0 and v["visible"][k]:
            on_at_stim, d["vu"] = True, float(vt[k])
        else:
            later = [float(t) for t, x in zip(vt, v["visible"]) if x and t1 - 1.0 <= t <= t2]
            if later:
                d["vu"] = later[0]
        i1, i2 = int(np.searchsorted(ft, t1)), int(np.searchsorted(ft, t2 + 100.0))
        on = np.nonzero(fr["on_id"][i1:i2] == subj)[0]
        if len(on):
            d["dessus"] = float(ft[i1 + on[0]])
        m = (st >= t1) & (st <= t2 + 100.0)
        d["shots"] = int(m.sum())
        d["misses"] = int((m & (shots["hit"] == 0)).sum())
        d["wrong"] = int((m & (shots["hit"] == 1) & (shots["id"] != subj)).sum())
        h = m & (shots["hit"] == 1) & (shots["id"] == subj)
        if h.any():
            d["hit"] = True
            d["tir"] = float(st[h][0])
            if d["vu"] is not None:
                d["react"] = d["tir"] - d["vu"]
            if d["dessus"] is not None:
                d["tot"] = d["tir"] - d["dessus"]
        d["visible_at_stim"] = on_at_stim
        out.append(d)
    return out


def label(t):
    """Nom du cas d'un essai ; le balayage est détaillé par vitesse (le paramètre de l'essai)."""
    return f"sweep {t['param']:.0f} °/s" if t["case"] == "sweep" else t["case"]


def summarize(ts):
    out = {}
    for name in dict.fromkeys(label(t) for t in ts):
        rows = [t for t in ts if label(t) == name]
        r = np.array([t["react"] for t in rows if t["react"] is not None])
        tot = np.array([t["tot"] for t in rows if t["tot"] is not None])
        out[name] = dict(n=len(rows), hit=sum(t["hit"] for t in rows), seen=sum(t["vu"] is not None for t in rows),
                         reached=sum(t["dessus"] is not None for t in rows),
                         react_med=float(np.median(r)) if len(r) else None, react_min=float(r.min()) if len(r) else None,
                         lt100=int((r < 100).sum()), lt150=int(((r >= 100) & (r < 150)).sum()),
                         tot_med=float(np.median(tot)) if len(tot) else None,
                         misses=sum(t["misses"] for t in rows), wrong=sum(t["wrong"] for t in rows), shots=sum(t["shots"] for t in rows))
    return out


def fmt(v, unit=""):
    return "—" if v is None else f"{v:.0f}{unit}"


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--names", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--trials", action="store_true", help="une ligne par essai")
    a = ap.parse_args(argv)
    names = a.names.split(",") if a.names else [os.path.splitext(os.path.basename(p))[0] for p in a.logs]
    res = {}
    for name, p in zip(names, a.logs):
        ts = trials(load(p))
        res[name] = dict(path=p, trials=ts, summary=summarize(ts))
        if a.trials:
            print(f"== {name}")
            for t in ts:
                print(f"  {t['case']:<13} #{t['trial']:<2} param {t['param']:6.1f} · vu→tir {fmt(t['react'], ' ms'):>7} · dessus→tir {fmt(t['tot'], ' ms'):>7} · "
                      f"tirs {t['shots']} (à côté {t['misses']}, autre cible {t['wrong']})" + ("" if t["vu"] is not None else " · JAMAIS VISIBLE")
                      + ("" if t["dessus"] is not None else " · réticule jamais dessus"))
    cases = list(dict.fromkeys(c for r in res.values() for c in r["summary"]))
    for c in cases:
        print(f"\n{c} — {WHAT.get(c.split()[0], '')}")
        print(f"  {'configuration':<14} {'essais':>6} {'touchées':>8} {'vu→tir méd.':>12} {'min':>6} {'<100 ms':>8} {'100–150':>8} {'dessus→tir méd.':>16} {'à côté':>7} {'autre cible':>12}")
        for name, r in res.items():
            s = r["summary"].get(c)
            if s:
                print(f"  {name:<14} {s['n']:>6} {s['hit']:>8} {fmt(s['react_med'], ' ms'):>12} {fmt(s['react_min']):>6} {s['lt100']:>8} {s['lt150']:>8} {fmt(s['tot_med'], ' ms'):>16} {s['misses']:>7} {s['wrong']:>12}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
