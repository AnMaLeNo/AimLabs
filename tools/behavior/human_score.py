"""Score « humain » d'une partie, à partir de l'enregistrement de comportement du jeu (.aimbeh).

    python tools/behavior/human_score.py features <partie.aimbeh> [--json]         # ce que le jeu a mesuré, sans jugement
    python tools/behavior/human_score.py fit <humain1.aimbeh> [...] -o ref.json    # référence = parties jouées par un humain
    python tools/behavior/human_score.py score <partie.aimbeh> --ref ref.json      # score 0–100 + détail par mesure

Principe : pour chaque appui sur le tir, le jeu sait ce que le joueur ne lui dit pas : depuis combien de temps le réticule
était sur la cible, à quelle vitesse la cible et le réticule se croisaient, combien de temps le bouton est resté enfoncé, où le
réticule était sur la cible… Une partie est découpée en fenêtres de N appuis ; chaque fenêtre donne une valeur par mesure
(médiane, dispersion). La référence garde, pour chaque mesure, la médiane et la dispersion de ces valeurs sur des parties
HUMAINES. Le score d'une fenêtre dit à quel point ses mesures sortent de cette référence :

    z_i = (valeur_i − médiane_humaine_i) / dispersion_humaine_i          (dispersion = 1,4826 × écart absolu médian)
    D   = moyenne des min(z_i², 25)                                       (≈ 1 pour un humain de la référence)
    score = 100 × exp(−max(0, D − 1) / 2)

La formule est PROVISOIRE : elle n'a de sens qu'avec une référence faite de vraies parties humaines, et elle doit être réglée
sur des données réelles (humain et algorithme). Sans référence, seul `features` est utilisable.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aimbeh import load  # noqa: E402

WINDOW = 15                # appuis par fenêtre
GAP_MAX_MS = 2000.0        # deux appuis plus espacés ne font pas partie de la même rafale
# mesure -> description (une valeur par fenêtre)
FEATURES = {
    "dur_med": "durée d'un appui, médiane (ms)",
    "dur_spread": "durée d'un appui, dispersion relative (écart interquartile / médiane)",
    "gap_med": "intervalle entre deux appuis rapprochés, médiane (ms)",
    "gap_spread": "intervalle entre deux appuis rapprochés, dispersion relative",
    "tot_med": "temps réticule-sur-cible avant l'appui, médiane (ms)",
    "tot_p10": "temps réticule-sur-cible avant l'appui, 10e centile (ms)",
    "tot_spread": "temps réticule-sur-cible avant l'appui, écart interquartile (ms)",
    "on_frac": "part des appuis faits avec le réticule sur la cible",
    "pos_spread": "position du réticule sur la cible à l'appui (en demi-largeurs), écart interquartile",
    "cross_ms": "durée du passage du réticule sur la cible pendant lequel l'appui a lieu, médiane (ms)",
    "slow_ratio": "vitesse de la vue à l'appui / vitesse 50–150 ms avant, médiane (1 = pas de ralentissement)",
}


def _iqr(x):
    return float(np.percentile(x, 75) - np.percentile(x, 25))


def presses(log):
    """Un dict par appui sur le tir (appui → relâchement), avec ce que le jeu voyait à cet instant."""
    fr = log.frame
    out = []
    if len(fr) < 3:
        return out
    ft = fr["t"].astype(np.float64) / 1e3                           # ms
    yaw = np.degrees(np.unwrap(np.radians(fr["yaw"].astype(np.float64))))
    pitch = fr["pitch"].astype(np.float64)
    step = np.hypot(np.diff(yaw), np.diff(pitch))                   # degrés parcourus par la vue entre deux images
    cum = np.concatenate([[0.0], np.cumsum(step)])
    on = fr["on_id"]

    def view_speed(t0, t1):
        """Vitesse moyenne de la vue (°/s) entre t0 et t1 (ms)."""
        i0, i1 = np.searchsorted(ft, t0), np.searchsorted(ft, t1)
        i0, i1 = max(i0 - 1, 0), min(i1, len(ft) - 1)
        if i1 <= i0 or ft[i1] <= ft[i0]:
            return float("nan")
        return float((cum[i1] - cum[i0]) / (ft[i1] - ft[i0]) * 1e3)

    bt = log.button
    shots_t = log.shot["t"].astype(np.float64) / 1e3
    down = None
    for row in bt:
        t = float(row["t"]) / 1e3
        if row["pressed"]:
            down = t
            continue
        if down is None:
            continue
        t0, down = down, None
        i = int(np.searchsorted(ft, t0, side="right")) - 1          # dernière image vue avant l'appui
        if i < 1:
            continue
        p = dict(t_ms=t0, dur_ms=t - t0, key=int(row["key"]), on_target=bool(on[i] >= 0))
        p["tot_ms"] = p["cross_ms"] = float("nan")
        if p["on_target"]:
            a = i
            while a > 0 and on[a - 1] == on[i]:
                a -= 1
            b = i
            while b < len(on) - 1 and on[b + 1] == on[i]:
                b += 1
            p["tot_ms"] = t0 - ft[a]                                # réticule sur la cible depuis…
            p["cross_ms"] = ft[b] - ft[a]                           # …et durée totale de ce passage
        hw = float(fr["half_w"][i])
        p["pos"] = float(fr["off_x"][i]) / hw if fr["near_id"][i] >= 0 and hw > 0 else float("nan")
        p["speed_click"] = view_speed(t0 - 20.0, t0 + 20.0)
        p["speed_before"] = view_speed(t0 - 150.0, t0 - 50.0)
        j = np.searchsorted(shots_t, t0 - 0.5)
        p["shot"] = bool(j < len(shots_t) and shots_t[j] - t0 < 50.0)
        out.append(p)
    return out


def window_features(ps):
    """Les mesures d'une fenêtre d'appuis (dict mesure -> valeur ; NaN quand la fenêtre ne permet pas de la calculer)."""
    f = dict.fromkeys(FEATURES, float("nan"))
    dur = np.array([p["dur_ms"] for p in ps])
    f["dur_med"] = float(np.median(dur))
    f["dur_spread"] = _iqr(dur) / max(f["dur_med"], 1e-6)
    t = np.array([p["t_ms"] for p in ps])
    gaps = np.diff(t)
    gaps = gaps[gaps < GAP_MAX_MS]
    if len(gaps) >= 4:
        f["gap_med"] = float(np.median(gaps))
        f["gap_spread"] = _iqr(gaps) / max(f["gap_med"], 1e-6)
    f["on_frac"] = float(np.mean([p["on_target"] for p in ps]))
    tot = np.array([p["tot_ms"] for p in ps if p["on_target"]])
    if len(tot) >= 4:
        f["tot_med"] = float(np.median(tot))
        f["tot_p10"] = float(np.percentile(tot, 10))
        f["tot_spread"] = _iqr(tot)
        f["cross_ms"] = float(np.median([p["cross_ms"] for p in ps if p["on_target"]]))
    pos = np.array([p["pos"] for p in ps if p["on_target"] and not math.isnan(p["pos"])])
    if len(pos) >= 4:
        f["pos_spread"] = _iqr(pos)
    ratio = np.array([p["speed_click"] / p["speed_before"] for p in ps
                      if p["speed_before"] == p["speed_before"] and p["speed_before"] > 5.0 and p["speed_click"] == p["speed_click"]])
    if len(ratio) >= 4:
        f["slow_ratio"] = float(np.median(ratio))
    return f


def windows(ps, n=WINDOW):
    return [ps[i:i + n] for i in range(0, len(ps) - n + 1, n)]


def session_summary(log):
    ps = presses(log)
    fr = log.frame
    dt = np.diff(fr["t"].astype(np.float64)) / 1e3 if len(fr) > 1 else np.array([float("nan")])
    dt = dt[dt < 100.0] if len(dt) else dt
    shots = log.shot
    return dict(
        path=log.path, mode=log.header.get("mode"), map=log.header.get("map"), weapon=log.header.get("weapon"),
        duration_s=float((fr["t"][-1] - fr["t"][0]) / 1e6) if len(fr) else 0.0,
        frame_ms=float(np.median(dt)) if len(dt) else float("nan"),
        presses=len(ps), shots=int(len(shots)), hits=int(shots["hit"].sum()) if len(shots) else 0,
        truncated=log.truncated,
        features=window_features(ps) if len(ps) >= 4 else None,
    )


def fit(logs, n=WINDOW):
    vals = {k: [] for k in FEATURES}
    n_win = 0
    for log in logs:
        for w in windows(presses(log), n):
            n_win += 1
            for k, v in window_features(w).items():
                if v == v:
                    vals[k].append(v)
    ref = dict(version=1, window=n, windows=n_win, sources=[os.path.basename(lg.path) for lg in logs], features={})
    for k, v in vals.items():
        if len(v) < 3:
            continue
        v = np.array(v)
        med = float(np.median(v))
        mad = float(np.median(np.abs(v - med)) * 1.4826)
        ref["features"][k] = dict(median=med, spread=max(mad, 0.05 * abs(med), 1e-3), n=int(len(v)),
                                  min=float(v.min()), max=float(v.max()))
    return ref


def score_window(feats, ref):
    """(score 0–100, {mesure: z}) pour les mesures d'une fenêtre. None si aucune mesure n'est comparable."""
    z = {}
    for k, r in ref["features"].items():
        v = feats.get(k, float("nan"))
        if v == v:
            z[k] = (v - r["median"]) / r["spread"]
    if not z:
        return None, z
    d = float(np.mean([min(x * x, 25.0) for x in z.values()]))
    return 100.0 * math.exp(-max(0.0, d - 1.0) / 2.0), z


def score(log, ref):
    ps = presses(log)
    n = int(ref.get("window", WINDOW))
    ws = windows(ps, n) or ([ps] if len(ps) >= 4 else [])
    rows = []
    for w in ws:
        f = window_features(w)
        s, z = score_window(f, ref)
        if s is not None:
            rows.append(dict(score=s, z=z, features=f))
    if not rows:
        return dict(score=None, windows=0, presses=len(ps))
    zs = {k: float(np.median([r["z"][k] for r in rows if k in r["z"]])) for k in ref["features"] if any(k in r["z"] for r in rows)}
    fs = {k: float(np.nanmedian([r["features"][k] for r in rows])) for k in zs}
    return dict(score=float(np.median([r["score"] for r in rows])), windows=len(rows), presses=len(ps),
                window_scores=[round(r["score"], 1) for r in rows], z=zs, features=fs)


def _print_features(s):
    print(f"{s['path']}")
    print(f"  mode {s['mode']}, carte {s['map']}, arme {s['weapon']} ; {s['duration_s']:.0f} s en jeu, image {s['frame_ms']:.2f} ms ; "
          f"{s['presses']} appuis, {s['shots']} tirs, {s['hits']} touches" + (" ; FICHIER TRONQUÉ" if s["truncated"] else ""))
    if not s["features"]:
        print("  moins de 4 appuis : aucune mesure")
        return
    for k, v in s["features"].items():
        print(f"  {k:<11} {v:>9.3f}   {FEATURES[k]}" if v == v else f"  {k:<11} {'—':>9}   {FEATURES[k]}")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("features")
    a.add_argument("logs", nargs="+")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("fit")
    a.add_argument("logs", nargs="+")
    a.add_argument("-o", "--out", required=True)
    a.add_argument("--window", type=int, default=WINDOW)
    a = sub.add_parser("score")
    a.add_argument("logs", nargs="+")
    a.add_argument("--ref", required=True)
    a.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "features":
        out = [session_summary(load(p)) for p in args.logs]
        if args.json:
            print(json.dumps(out, indent=1))
        else:
            for s in out:
                _print_features(s)
    elif args.cmd == "fit":
        ref = fit([load(p) for p in args.logs], args.window)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(ref, f, indent=1)
        print(f"référence : {ref['windows']} fenêtres de {ref['window']} appuis, {len(ref['features'])} mesures -> {args.out}")
        for k, r in ref["features"].items():
            print(f"  {k:<11} médiane {r['median']:>9.3f}  dispersion {r['spread']:>8.3f}  (n {r['n']}, {r['min']:.3f} à {r['max']:.3f})")
        if ref["windows"] < 10:
            print("  ATTENTION : moins de 10 fenêtres, la référence est fragile")
    else:
        with open(args.ref, encoding="utf-8") as f:
            ref = json.load(f)
        out = []
        for p in args.logs:
            r = score(load(p), ref)
            r["path"] = p
            out.append(r)
            if args.json:
                continue
            if r["score"] is None:
                print(f"{p} : pas assez d'appuis ({r['presses']}) pour un score")
                continue
            print(f"{p} : score humain {r['score']:.0f}/100  ({r['presses']} appuis, {r['windows']} fenêtres : {r['window_scores']})")
            for k, z in sorted(r["z"].items(), key=lambda kv: -abs(kv[1])):
                rr = ref["features"][k]
                print(f"  {k:<11} {r['features'][k]:>9.3f}  (humain {rr['median']:.3f} ± {rr['spread']:.3f})  z {z:+6.1f}   {FEATURES[k]}")
        if args.json:
            print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
