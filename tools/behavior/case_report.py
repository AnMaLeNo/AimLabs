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

CASES = ["flick_side", "peek_preaim", "cross_preaim", "two_far", "two_near", "sweep", "peek_far", "flick_far", "mix", "sweep_acc", "flick_pass"]
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
    "sweep_acc": "balayage dont la vitesse change sur les 10 derniers degrés avant la cible (vitesse avant → vitesse sur la cible)",
    "flick_pass": "coup de souris humain qui TRAVERSE la cible : à 30 % de sa durée (il accélère), 50 % (vitesse maximale), 70 % (il freine)",
}
PASSING = ("sweep", "sweep_acc", "flick_pass")       # cas où le réticule traverse la cible : on y mesure l'écart du tir à son passage sur l'axe


def shot_times_ms(log):
    """Instant de chaque tir (ms, horloge du jeu). Les journaux écrits AVANT le 2026-11-16 datent un tir MANQUÉ jusqu'à 9 ms trop tard : le jeu horodatait
    le tir après avoir créé l'impact sur le décor, ce qui lui prenait 8 à 16 ms quand plus aucun impact n'existait (ils sont supprimés après 8 s).
    Mesuré sur 5 passes du banc : 0 tir touché sur 590 daté en retard, 60 tirs manqués sur 147. Quand le tir répond à une demande d'un programme
    (bouton, périphérique 2) reçue 2 à 20 ms plus tôt, on le date de cette demande + 0,3 ms (l'écart normal, celui des tirs touchés). Sans effet sur
    les journaux récents (écart de 0,2 à 0,3 ms) ni sur les tirs d'un joueur (pas de demande)."""
    st = log.shot["t"].astype(np.float64) / 1e3
    b = log.button
    req = b["t"][(b["pressed"] == 1) & (b["key"] == 2)].astype(np.float64) / 1e3
    if not len(req) or not len(st):
        return st
    q = np.clip(np.searchsorted(req, st + 1e-4) - 1, 0, len(req) - 1)
    lag = st - req[q]
    fix = (lag > 2.0) & (lag < 20.0)
    st = st.copy()
    st[fix] = req[q][fix] + 0.3
    return st


def trials(log):
    """Un dict par essai : cas, numéro, paramètre, sujet, et les instants (ms) mesurés par le jeu."""
    mk = log.mark
    out = []
    if not len(mk):
        return out
    fr = log.frame
    ft = fr["t"].astype(np.float64) / 1e3
    shots = log.shot
    st = shot_times_ms(log)
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
        # Réticule qui TRAVERSE la cible : instant où il passe sur son axe vertical (l'écart horizontal change de signe), vitesse angulaire à cet
        # instant, et écart du tir le plus proche à ce passage (ms ; positif = tir en retard). `half_ms` : temps que met le réticule à parcourir la
        # demi-largeur de la cible à cette vitesse — un tir écarté de plus que cela est à côté.
        d["cross"] = d["err"] = d["cross_speed"] = d["half_ms"] = None
        if d["case"] in PASSING and i2 - i1 > 3:
            seg = fr[i1:i2]
            ok = np.nonzero(seg["near_id"] == subj)[0]
            ox = seg["off_x"].astype(np.float64)
            for a_, b_ in zip(ok[:-1], ok[1:]):
                if b_ == a_ + 1 and ox[a_] * ox[b_] <= 0.0 and ox[a_] != ox[b_]:
                    u = ox[a_] / (ox[a_] - ox[b_])
                    tc = ft[i1 + a_] + u * (ft[i1 + b_] - ft[i1 + a_])
                    lo, hi = max(i1 + a_ - 3, 0), min(i1 + b_ + 3, len(fr) - 1)
                    dyaw = (float(fr["yaw"][hi]) - float(fr["yaw"][lo]) + 180.0) % 360.0 - 180.0
                    spd = abs(dyaw) / max((ft[hi] - ft[lo]) / 1e3, 1e-6)
                    d["cross"], d["cross_speed"] = float(tc), float(spd)
                    d["half_ms"] = float(seg["half_w"][a_]) / max(spd, 1e-6) * 1e3
                    near = st[m]
                    if len(near):
                        d["err"] = float(near[np.argmin(np.abs(near - tc))] - tc)
                    break
        out.append(d)
    return out


def label(t):
    """Nom du cas d'un essai ; les cas à paramètre sont détaillés (le paramètre de l'essai)."""
    if t["case"] == "sweep":
        return f"sweep {t['param']:.0f} °/s"
    if t["case"] == "sweep_acc":
        v0, v1 = int(round(t["param"])) // 1000, int(round(t["param"])) % 1000
        acc = (v1 * v1 - v0 * v0) / 20.0
        return f"sweep_acc {v0}→{v1} °/s ({acc:+.0f} °/s²)"
    if t["case"] == "flick_pass":
        amp = int(t["param"])
        return f"flick_pass {amp}° à {round((t['param'] - amp) * 100):.0f} %"
    return t["case"]


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
        err = np.array([t["err"] for t in rows if t.get("err") is not None])
        half = np.array([t["half_ms"] for t in rows if t.get("half_ms") is not None])
        spd = np.array([t["cross_speed"] for t in rows if t.get("cross_speed") is not None])
        out[name].update(crossed=sum(t.get("cross") is not None for t in rows), fired=int(len(err)),
                         err_med=float(np.median(err)) if len(err) else None, err_mean=float(err.mean()) if len(err) else None,
                         err_sd=float(err.std()) if len(err) else None, err_p90=float(np.percentile(np.abs(err), 90)) if len(err) else None,
                         half_ms=float(np.median(half)) if len(half) else None, speed=float(np.median(spd)) if len(spd) else None)
    return out


def fmt(v, unit=""):
    return "—" if v is None else f"{v:.0f}{unit}"


def fmt1(v, unit=""):
    return "—" if v is None else f"{v:+.1f}{unit}"


def passing_table(res, out=print):
    """Cas où le réticule TRAVERSE la cible : pour chaque configuration, taux de touches et écart du tir au passage du réticule sur l'axe de la cible.
    `res` : {nom: {"summary": …}} (ou {"summary", "trials"}). Un tir écarté de plus que la demi-largeur (en temps) est à côté."""
    cases = [c for c in dict.fromkeys(c for r in res.values() for c in r["summary"]) if c.split()[0] in PASSING]
    if not cases:
        return
    out("\nRéticule qui traverse la cible : touches et écart du tir à son passage sur l'axe de la cible (ms ; + = tir en retard)")
    out(f"  {'cas':<38} {'configuration':<16} {'essais':>6} {'touchées':>9} {'tirs':>5} {'à côté':>7} {'vitesse':>9} {'½ largeur':>10} {'écart méd.':>11} {'moyen':>7} {'écart-type':>11} {'|écart| p90':>12}")
    for c in cases:
        for name, r in res.items():
            x = r["summary"].get(c)
            if x:
                out(f"  {c:<38} {name:<16} {x['n']:>6} {x['hit']:>5} ({100 * x['hit'] / max(x['n'], 1):3.0f}%) {x['shots']:>5} {x['misses']:>7} {fmt(x.get('speed'), ' °/s'):>9} "
                    f"{('—' if x.get('half_ms') is None else format(x['half_ms'], '.1f') + ' ms'):>10} {fmt1(x.get('err_med'), ' ms'):>11} {fmt1(x.get('err_mean')):>7} "
                    f"{('—' if x.get('err_sd') is None else format(x['err_sd'], '.1f') + ' ms'):>11} {('—' if x.get('err_p90') is None else format(x['err_p90'], '.1f') + ' ms'):>12}")


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
    passing_table(res)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
