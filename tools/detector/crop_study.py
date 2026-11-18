#!/usr/bin/env python3
"""crop_study.py — quel carré donner au réseau pour trouver la tête : quelle taille à l'écran, quelle taille pour le réseau, un carré ou deux ?

La chaîne deux machines découpe un carré de l'écran autour du réticule et le réduit avant de le donner au réseau (384 px de l'écran -> 256 px dans les
réglages d'Antoine du 2026-11-18) : une tête lointaine y perd un tiers de sa taille. Ce banc rejoue les situations de `head_bench.py` pour une grille de
couples « carré à l'écran : côté donné au réseau » et pour des paires de carrés (un large pour le champ, un petit pour la tête), et range les résultats
par distance. Il ne mesure pas le temps de calcul (il dépend de la machine : `--times` l'ajoute au tableau).

  python crop_study.py --bench <rendus>,<vérité.jsonl>,<plan.json> [--bench …] --model <pose.pt> --out <dossier> \\
      --configs 384:256 384:384 256:256 192:256 128:256 --pairs 384:256+128:256 [--times 256=0.37,384=0.62]

« C:N » : le carré central de C px du rendu (= pixels d'un écran 1440p) donné au réseau en N × N. Paire « large+petit » : on écoute le petit carré quand il
voit une tête, sinon le large. Les colonnes : tête trouvée, viser le centre annoncé = tir à la tête, tire quand le réticule est sur la tête, tire à tort.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from head_bench import DIST_BINS  # noqa: E402

RENDER = 480


def run(bench, cfg, a, tag):
    crop, net = cfg
    out = os.path.join(a.out, f"{tag}_c{crop}_n{net}" + (f"_j{a.jpeg}" if a.jpeg else "") + ".cases.json")
    if not os.path.exists(out) or a.redo:
        renders, truth, plan = bench
        spec = f"m=jeu,{a.model}" + ("," + a.params if a.params else "")
        cmd = [sys.executable, os.path.join(HERE, "head_bench.py"), "--renders", renders, "--truth", truth, "--plan", plan, "--reticle", a.reticle, "--variants", *a.variants,
               "--net", str(net), "--zoom", f"{RENDER / crop:g}", "--jpeg", str(a.jpeg), "--batch", "64", "--models", spec, "--cases-out", out]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            sys.exit(r.stdout[-2000:] + r.stderr[-2000:])
    return json.load(open(out, encoding="utf-8"))


def rates(cases):
    """Par tranche de distance : (n, tête trouvée, viser = tête, n sur tête, tire, n hors tête, à tort)."""
    rows = {}
    for lo, hi, name in DIST_BINS:
        q = [c for c in cases if lo <= c["dist"] < hi]
        if not q:
            continue
        vis = [c for c in q if c["vis"]]
        pos, neg = [c for c in q if c["on_head"]], [c for c in q if not c["on_head"]]
        f = lambda x, y: (100.0 * x / y) if y else float("nan")
        rows[name] = (len(q), f(sum(c["found"] for c in vis), len(vis)), f(sum(c["hit"] for c in vis), len(vis)),
                      f(sum(c["says"] for c in pos), len(pos)), f(sum(c["says"] for c in neg), len(neg)))
    return rows


def pair(wide, small):
    """Deux carrés : le petit quand il voit une tête, sinon le large."""
    by = {(c["id"], c["variant"]): c for c in wide}
    out = []
    for s in small:
        w = by[(s["id"], s["variant"])]
        out.append(s if s["head_any"] else w)
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="append", required=True, help="rendus,vérité,plan (répétable : banc d'origine 3–60 m, banc lointain 60–120 m)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--params", default="shrink=0.9,shape=ellipse")
    ap.add_argument("--configs", nargs="+", required=True, help="C:N — carré de C px à l'écran donné au réseau en N px")
    ap.add_argument("--pairs", nargs="*", default=[], help="large+petit, ex. 384:256+128:256")
    ap.add_argument("--variants", nargs="+", default=["libre"])
    ap.add_argument("--reticle", default="small")
    ap.add_argument("--jpeg", type=int, default=60, help="qualité JPEG de l'image donnée au réseau, comme dans la chaîne (0 : pixels intacts)")
    ap.add_argument("--times", default="", help="temps d'inférence par côté de réseau, ex. 256=0.37,384=0.62 (ms, mesurés sur la machine B)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    times = dict((int(k), float(v)) for k, v in (x.split("=") for x in a.times.split(","))) if a.times else {}
    benches = [tuple(b.split(",")) for b in a.bench]
    parse = lambda s: tuple(int(x) for x in s.split(":"))
    cfgs = [parse(c) for c in a.configs]
    for p in a.pairs:
        for c in p.split("+"):
            if parse(c) not in cfgs:
                cfgs.append(parse(c))
    cases = {}
    for cfg in cfgs:
        cases[cfg] = []
        for bi, b in enumerate(benches):
            cases[cfg] += run(b, cfg, a, f"b{bi}")
        print(f"  {cfg[0]}:{cfg[1]} fait ({len(cases[cfg])} cas)", flush=True)
    names = [n for _, _, n in DIST_BINS if any(n in rates(c) for c in cases.values())]
    table = [(f"{c}:{n}", f"×{n / c:.2f}", times.get(n), rates(cases[(c, n)])) for c, n in cfgs if f"{c}:{n}" in a.configs]
    for p in a.pairs:
        w, s = (parse(x) for x in p.split("+"))
        t = times.get(w[1], float("nan")) + times.get(s[1], float("nan")) if times else None
        table.append((p, "deux carrés", t, rates(pair(cases[w], cases[s]))))
    for title, col in (("TÊTE TROUVÉE (%)", 1), ("VISER LE CENTRE ANNONCÉ = TIR À LA TÊTE (%)", 2), ("TIRE QUAND LE RÉTICULE EST SUR LA TÊTE (%)", 3), ("TIRE À TORT (%)", 4)):
        print(f"\n{title}\n{'carré:réseau':22s}{'échelle':>12s}{'calcul ms':>10s}" + "".join(f"{n:>10s}" for n in names))
        for name, scale, t, r in table:
            print(f"{name:22s}{scale:>12s}{(f'{t:.2f}' if t is not None and t == t else '–'):>10s}" + "".join(f"{r[n][col]:10.1f}" if n in r else f"{'–':>10s}" for n in names))
    json.dump([{"config": name, "scale": scale, "ms": t, "rows": r} for name, scale, t, r in table], open(os.path.join(a.out, "study.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {os.path.join(a.out, 'study.json')}")


if __name__ == "__main__":
    main()
