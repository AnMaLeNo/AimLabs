#!/usr/bin/env python3
"""head_bench.py — banc RAPIDE « où est la tête ? » : compare des façons de trouver la tête, sans lancer de partie (quelques secondes par modèle).

Pourquoi : juger une idée sur la détection de la tête (un autre modèle, un seuil, une règle) demandait de faire jouer le stand de tir sur la chaîne
complète (PC + Mac, 5 à 10 minutes par réglage). Ici les images sont rendues UNE fois par le jeu (`head_plan.py` : 2 800 situations de tir à la tête,
tous les personnages, 3 à 60 m, réticule posé autour de la tête) avec la vérité du moteur (`gen_dataset.gd --truth` : quels pixels sont un tir à la
tête, vérifié contre les vrais rayons du jeu par `head_truth.py check`). Chaque candidat est ensuite jugé hors ligne sur les mêmes images, réduites à
256 px comme le fait l'agent de la chaîne deux machines (bilinéaire, `--interp`), avec le réticule du jeu dessiné au centre (`--reticle`, défaut « small » : celui que le jeu dessine
aujourd'hui ; « classic » : l'ancien, qui couvre une tête au-delà de 40 m ; voir compose.draw_reticle).

Trois variantes de chaque image : « libre » (personnage entier), « muret » (un mur cache le bas du corps, il reste la tête et plus ou moins de torse),
« angle » (un mur cache un côté ; la tête dépasse en entier ou en partie). Les murs sont dessinés (`head_truth.draw_wall`), pas rendus par le jeu.

Mesures, par variante et par distance :
  * trouvée   : part des têtes (au moins en partie visibles) pour lesquelles le candidat donne une tête ;
  * erreur    : distance entre le centre annoncé et le centre de la zone « tête » du jeu, en pixels de l'image 256 (médiane, 9e décile) ;
  * viser     : si on tirait SUR le centre annoncé, part de tirs à la tête d'après le jeu (parmi toutes les têtes visibles : une tête non trouvée compte 0) ;
  * décision  : ce que fait le tir automatique « uniquement sur la tête ». Le réticule est au centre de l'image. `tire` = quand le réticule est vraiment
                sur la tête, part des images où le candidat le dit ; `à tort` = quand il n'y est pas, part des images où le candidat dit que si
                (tir au corps ou à côté) ; `juste` = parmi les « oui » du candidat, part de vrais tirs à la tête.

Candidats (`nom=type,modèle[,paramètre=valeur…]`) :
  seg   modèle de segmentation actuel : tête = haut de la boîte (`frac`, défaut 0,08), largeur lue dans le masque (`band`, défaut 0,7) — la règle de sniper_b
  coco  modèle YOLO-pose tel que livré (COCO) : tête = nez, yeux, oreilles (ceux dont la confiance dépasse `kconf`)
  jeu   modèle YOLO-pose réentraîné par `pose_labels.py` : points 0–4 = centre et bords de la zone tête ; `shrink` réduit la zone annoncée (défaut 1) ;
        `shift` la descend d'une part de sa hauteur (0,2 : la zone du jeu monte au-dessus des cheveux, la tête dessinée est plus bas) ; `shape=ellipse`

  python head_bench.py --renders <bench_renders> --truth <bench_truth.jsonl> --plan <bench_plan.json> \\
      --models seg8=seg,algos/yolo11n-seg-ft2.pt coco-n=coco,algos/yolo11n-pose.pt jeu-n=jeu,<runs>/pose_n/weights/best.pt [--out res.json]
"""
import argparse
import json
import os
import sys
import time
import zlib
from typing import Dict, List, Optional

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import head_truth as HT  # noqa: E402
from compose import draw_reticle  # noqa: E402

VARIANTS = ["libre", "muret", "angle"]
# (le banc d'origine s'arrête à 60 m : sa dernière tranche s'appelait « ≥ 40 m » dans les résultats d'avant le 2026-11-18 ; au-delà : banc lointain, head_plan --dmin 60)
DIST_BINS = [(0, 10, "< 10 m"), (10, 20, "10–20 m"), (20, 40, "20–40 m"), (40, 60.5, "40–60 m"), (60.5, 90, "60–90 m"), (90, 1e9, "≥ 90 m")]
N = 256


def build_cases(a) -> List[dict]:
    """Une entrée par (image, variante) : mur, zone tête restante, zone du jeu sous le réticule. Mis en cache à côté de la vérité."""
    cache = os.path.splitext(a.truth)[0] + ".cases.json"
    if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(a.truth) and not a.rebuild:
        saved = json.load(open(cache, encoding="utf-8"))
        if saved.get("exclude") == sorted(a.exclude):
            return saved["cases"]
    dist = {s["id"]: s["meta"]["dist"] for s in json.load(open(a.plan))["samples"]}
    cases = []
    for s in HT.read_truth(a.truth):
        if s["agents"][0]["slug"] in a.exclude:
            continue
        size = s["size"]
        free = HT.head_info(s, 0)
        for v in VARIANTS:
            wall = None
            if v != "libre":
                if free is None:
                    continue
                wall = HT.pick_wall(np.random.default_rng(zlib.crc32((v + s["id"]).encode())), s, 0, free, kind=v)
            blockers = [wall["rect"]] if wall else None
            hi = HT.head_info(s, 0, blockers=blockers) if wall else free
            c = np.array([[size / 2.0, size / 2.0]])
            z, _ = HT.zones_at(s, c)
            zone = -2 if (blockers and HT.blocked(c, blockers)[0]) else int(z[0])           # -2 : le réticule est sur le mur
            cases.append({"id": s["id"], "variant": v, "dist": dist[s["id"]], "wall": wall, "head": hi, "zone": zone, "size": size})
    json.dump({"exclude": sorted(a.exclude), "cases": cases}, open(cache, "w", encoding="utf-8"))
    return cases


CHROMA = {"444": cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444, "422": cv2.IMWRITE_JPEG_SAMPLING_FACTOR_422, "420": cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420}


def shrink(img: np.ndarray, n: int, interp) -> np.ndarray:
    """Réduit l'image au côté du réseau SANS déplacer son contenu. `interp` None = plus proche voisin pris au centre de chaque pixel d'arrivée, comme PIL
    (`fit_crop` de sniper_b). cv2.INTER_NEAREST, lui, prend le coin : le contenu glisse de (rapport − 1) / 2 pixel de départ vers le bas et la droite, soit
    0,33 px d'arrivée de 384 à 256 px et 0,47 px de 480 à 256 px (mesuré le 2026-11-18 sur le barycentre d'une tache). Le banc l'a utilisé jusqu'à cette
    date : ses chiffres « image réduite » portaient ce décalage, que la chaîne réelle n'a pas (agent_a.py réduit en bilinéaire, sniper_b avec PIL)."""
    if interp is not None:
        return cv2.resize(img, (n, n), interpolation=interp)
    idx = ((np.arange(n) + 0.5) * img.shape[0] / n).astype(np.int64)
    return np.ascontiguousarray(img[idx][:, idx])


def load_images(a, cases: List[dict]) -> np.ndarray:
    """Les images telles que le réseau les reçoit (mur, réticule, réduction à 256 px), dans l'ordre de `cases`. Préparées une fois puis relues d'un
    fichier .npy à côté de la vérité (1,6 Go pour 8 400 cas) : c'est ce qui rend le banc rapide."""
    interp = {"bilinear": cv2.INTER_LINEAR, "area": cv2.INTER_AREA}.get(a.interp)
    vs = [v for v in VARIANTS if any(c["variant"] == v for c in cases)]
    # (un fichier par jeu de variantes : `--variants libre` réécrivait sinon, en 4 minutes, le fichier des trois variantes, et inversement)
    # (« nearestc » : les fichiers « nearest » d'avant le 2026-11-18 ont été réduits par cv2.INTER_NEAREST, dont le contenu est décalé : voir shrink)
    cache = (os.path.splitext(a.truth)[0] + f".{'nearestc' if a.interp == 'nearest' else a.interp}.{a.reticle}" + ("" if vs == VARIANTS else "." + "-".join(vs))
             + ("" if N == 256 else f".n{N}") + ("" if a.zoom == 1.0 else f".z{a.zoom:g}") + (f".j{a.jpeg}" + ("" if a.jpeg_chroma == "420" else "c" + a.jpeg_chroma) if a.jpeg else "") + ".npy")
    key = [c["id"] + c["variant"] for c in cases]
    if os.path.exists(cache) and os.path.exists(cache + ".json") and json.load(open(cache + ".json")) == key and not a.rebuild:
        return np.load(cache, mmap_mode="r")
    out = np.lib.format.open_memmap(cache, mode="w+", dtype=np.uint8, shape=(len(cases), N, N, 3))
    last_id, base = None, None
    for i, c in enumerate(cases):
        if c["id"] != last_id:
            last_id, base = c["id"], cv2.imread(f"{a.renders}/{c['id']}_c.png")
        img = HT.draw_wall(base, c["wall"], c["size"]) if c["wall"] else base
        img = draw_reticle(img, c["size"], a.reticle)
        if a.zoom != 1.0:                                    # le réseau ne reçoit que le carré central (côté / zoom) : moins de champ, plus de pixels sur la tête
            side = int(round(c["size"] / a.zoom))
            o = (c["size"] - side) // 2
            img = img[o:o + side, o:o + side]
        img = img if img.shape[0] == N else shrink(img, N, interp)
        if a.jpeg:                                           # la chaîne deux machines envoie l'image en JPEG (agent_a.py --jpeg 60 --jpeg-chroma 444)
            img = cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, a.jpeg, cv2.IMWRITE_JPEG_SAMPLING_FACTOR, CHROMA[a.jpeg_chroma]])[1], cv2.IMREAD_COLOR)
        out[i] = img
    out.flush()
    json.dump(key, open(cache + ".json", "w"))
    return out


# ---------------------------------------------------------------------------------------------------------------- candidats
# Chaque candidat rend, par détection : (confiance, boîte, centre de tête ou None, « le point (x, y) est-il sur la tête ? »)

def heads_seg(res, p) -> list:
    frac, band, margin = float(p.get("frac", 0.08)), float(p.get("band", 0.7)), 2.0
    out = []
    if res.masks is None:
        return out
    masks = res.masks.data.cpu().numpy() > 0.5
    for b, cf, m in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy(), masks):
        if m.shape[0] != N:
            m = cv2.resize(m.astype(np.uint8), (N, N), interpolation=cv2.INTER_NEAREST).astype(bool)
        if b[1] <= margin:                                   # haut coupé par le bord du carré : tête inconnue (sniper_b.head_band)
            out.append((cf, b, None, lambda x, y: False))
            continue
        y0, y1 = b[1], b[1] + frac * (b[3] - b[1])

        def extent(y, m=m):
            xs = np.nonzero(m[int(np.clip(y, 0, N - 1))])[0]
            return (float(xs.min()), float(xs.max() + 1)) if len(xs) else None

        def on(x, y, y0=y0, y1=y1, extent=extent):
            if not (y0 <= y <= y1):
                return False
            e = extent(y)
            return e is not None and abs(x - (e[0] + e[1]) / 2) <= band * (e[1] - e[0]) / 2

        e = extent((y0 + y1) / 2)
        out.append((cf, b, ((e[0] + e[1]) / 2, (y0 + y1) / 2) if e else None, on))
    return out


def heads_coco(res, p) -> list:
    kconf = float(p.get("kconf", 0.5))
    out = []
    if res.keypoints is None or res.keypoints.conf is None:
        return out
    for b, cf, k, kc in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy(), res.keypoints.xy.cpu().numpy(), res.keypoints.conf.cpu().numpy()):
        sel = k[:5][kc[:5] > kconf]
        if not len(sel):
            out.append((cf, b, None, lambda x, y: False))
            continue
        c = sel.mean(axis=0)
        rad = max(1.5, float(np.linalg.norm(sel - c, axis=1).max()))
        out.append((cf, b, (float(c[0]), float(c[1])), lambda x, y, c=c, rad=rad: (x - c[0]) ** 2 + (y - c[1]) ** 2 <= rad * rad))
    return out


def heads_jeu(res, p) -> list:
    kconf, shrink, shape, shift = float(p.get("kconf", 0.5)), float(p.get("shrink", 1.0)), p.get("shape", "box"), float(p.get("shift", 0.0))
    out = []
    if res.keypoints is None or res.keypoints.conf is None:
        return out
    for b, cf, k, kc in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy(), res.keypoints.xy.cpu().numpy(), res.keypoints.conf.cpu().numpy()):
        if kc[0] <= kconf:
            out.append((cf, b, None, lambda x, y: False))
            continue
        c = (float(k[0][0]), float(k[0][1]))
        if (kc[1:5] > kconf).all():
            down = shift * (k[4][1] - k[3][1])                      # zone descendue de `shift` × sa hauteur (du point 3 au point 4)
            c = (c[0], c[1] + float(down))
            ex, ey = shrink * (k[2][0] - k[1][0]) / 2, shrink * (k[4][1] - k[3][1]) / 2
            mx, my = (k[1][0] + k[2][0]) / 2, (k[3][1] + k[4][1]) / 2 + down
            if shape == "ellipse":
                on = lambda x, y, ex=ex, ey=ey, mx=mx, my=my: ex > 0 and ey > 0 and ((x - mx) / ex) ** 2 + ((y - my) / ey) ** 2 <= 1.0
            else:
                on = lambda x, y, ex=ex, ey=ey, mx=mx, my=my: abs(x - mx) <= ex and abs(y - my) <= ey
        else:
            on = lambda x, y: False
        out.append((cf, b, c, on))
    return out


KINDS = {"seg": heads_seg, "coco": heads_coco, "jeu": heads_jeu}


# ---------------------------------------------------------------------------------------------------------------- mesures
class Acc:
    def __init__(self) -> None:
        self.n = self.vis = self.found = self.aim = 0
        self.err: List[float] = []
        self.err_r: List[float] = []
        self.dy: List[float] = []                            # milieu annoncé − vrai milieu, en hauteurs de tête (positif : annoncé trop bas)
        self.pos = self.pos_yes = self.neg = self.neg_yes = self.neg_yes_body = 0

    def row(self) -> Dict:
        f = lambda x, y: (x / y) if y else float("nan")
        e = np.array(self.err) if self.err else np.array([np.nan])
        yes = self.pos_yes + self.neg_yes
        return {"n": self.n, "têtes": self.vis, "trouvée": f(self.found, self.vis), "err_med": float(np.median(e)), "err_p90": float(np.percentile(e, 90)),
                "err_rayons": float(np.median(self.err_r)) if self.err_r else float("nan"), "biais_y": float(np.median(self.dy)) if self.dy else float("nan"),
                "viser": f(self.aim, self.vis),
                "n_sur_tête": self.pos, "tire": f(self.pos_yes, self.pos), "n_hors_tête": self.neg, "à_tort": f(self.neg_yes, self.neg),
                "à_tort_corps": f(self.neg_yes_body, self.neg), "juste": f(self.pos_yes, yes)}


def judge(case: dict, truth: dict, dets: list, conf: float, accs: List[Acc], zoom: float = 1.0) -> None:
    k = N * zoom / case["size"]                              # px du rendu -> px du réseau (autour du centre : le réticule reste au milieu)
    half = case["size"] / 2.0
    dets = [d for d in dets if d[0] >= conf]
    hi = case["head"]
    hit, err = False, None
    for acc in accs:
        acc.n += 1
    if hi is not None and hi["in_frac"] >= 0.5:
        gx, gy = (hi["cx"] - half) * k + N / 2.0, (hi["cy"] - half) * k + N / 2.0
        cand = [d for d in dets if d[2] is not None and d[1][0] - 3 <= gx <= d[1][2] + 3 and d[1][1] - 3 <= gy <= d[1][3] + 3]
        if cand:
            c = max(cand, key=lambda d: d[0])[2]
            err = float(np.hypot(c[0] - gx, c[1] - gy))
            p = np.array([[(c[0] - N / 2.0) / k + half, (c[1] - N / 2.0) / k + half]])
            z, w = HT.zones_at(truth, p)
            hit = bool(z[0] == HT.HEAD and w[0] == 0 and not (case["wall"] and HT.blocked(p, [case["wall"]["rect"]])[0]))
        for acc in accs:
            acc.vis += 1
            if err is not None:
                acc.found += 1
                acc.err.append(err)
                acc.err_r.append(err / (hi["r_px"] * k))
                acc.dy.append((c[1] - gy) / max(1e-6, (hi["y1"] - hi["y0"]) * k))
                acc.aim += int(hit)
    says = any(d[3](N / 2.0, N / 2.0) for d in dets)
    out = {"vis": hi is not None and hi["in_frac"] >= 0.5, "found": bool(hi is not None and hi["in_frac"] >= 0.5 and err is not None),
           "hit": bool(hi is not None and hi["in_frac"] >= 0.5 and hit), "says": bool(says), "head_any": any(d[2] is not None for d in dets)}
    for acc in accs:
        if case["zone"] == HT.HEAD:
            acc.pos += 1
            acc.pos_yes += int(says)
        else:
            acc.neg += 1
            acc.neg_yes += int(says)
            acc.neg_yes_body += int(says and case["zone"] in (HT.BODY, HT.ARM))
    return out


def pct(v: float) -> str:
    return "   –  " if v != v else f"{100 * v:5.1f}%"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--renders", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--models", nargs="+", required=True, help="nom=type,modèle[,paramètre=valeur…] ; type : seg, coco, jeu")
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--interp", default="bilinear", choices=["nearest", "bilinear", "area"],
                    help="réduction au côté du réseau : bilinear = celle de l'agent de la chaîne deux machines (agent_a.py --interp linear, défaut) ; nearest = "
                    "plus proche voisin centré, celle de sniper_b quand il réduit lui-même")
    ap.add_argument("--reticle", default="small", choices=["small", "classic", "dot", "none"],
                    help="réticule dessiné au centre (défaut : celui du jeu aujourd'hui ; « classic » : l'ancien, gros, qui cache une tête lointaine)")
    ap.add_argument("--jpeg", type=int, default=0, help="qualité JPEG appliquée à l'image donnée au réseau (60 : ce qu'envoie la chaîne deux machines ; 0 : pixels intacts). "
                    "Constaté le 2026-11-18 : sur une tête de 4 px le JPEG déplace la zone annoncée d'un demi-pixel vers le bas, et le banc sans JPEG ne le voyait pas")
    ap.add_argument("--jpeg-chroma", default="444", choices=["444", "422", "420"], help="couleur du JPEG : 444 = entière, ce qu'envoie l'agent depuis le 2026-11-18 ; 420 = "
                    "sous-échantillonnée, ce qu'il envoyait avant. ATTENTION : sur les rendus de ce banc le 420 ne déplace pas la tête d'un modèle entraîné avec ; sur les vraies "
                    "images du jeu il la déplace de 0,22 px (dépôt aimlabs-pipeline, tools/head_session_check.py --jpeg). Le banc ne remplace pas cette mesure-là")
    ap.add_argument("--net", type=int, default=256, help="côté de l'image donnée au réseau (256 : la chaîne d'aujourd'hui, qui réduit le carré de 480 px de l'écran)")
    ap.add_argument("--zoom", type=float, default=1.0, help="ne donner au réseau que le carré central de côté 480 / zoom (1,875 avec --net 256 : pixels de l'écran, sans réduction)")
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--variants", nargs="*", default=VARIANTS)
    ap.add_argument("--exclude", nargs="*", default=["veto"], help="personnages écartés du banc (veto : modèle abîmé, il s'affiche blanc)")
    ap.add_argument("--rebuild", action="store_true", help="recalculer le cache des cas (après un changement de head_truth.py)")
    ap.add_argument("--out", default="")
    ap.add_argument("--cases-out", default="", help="écrit aussi le verdict cas par cas (tête trouvée, viser = tête, décision) : pour combiner plusieurs carrés (crop_study.py)")
    a = ap.parse_args()
    from ultralytics import YOLO

    global N
    N = a.net
    t0 = time.time()
    cases = [c for c in build_cases(a) if c["variant"] in a.variants]
    if a.limit:
        keep = set(sorted({c["id"] for c in cases})[:a.limit])
        cases = [c for c in cases if c["id"] in keep]
    truth = {s["id"]: s for s in HT.read_truth(a.truth)}
    imgs = load_images(a, cases)
    print(f"{len(cases)} cas ({len({c['id'] for c in cases})} images × {len(a.variants)} variantes) prêts en {time.time() - t0:.1f} s", flush=True)
    results = {}
    for spec in a.models:
        name, rest = spec.split("=", 1)
        parts = rest.split(",")
        kind, path = parts[0], parts[1]
        params = dict(x.split("=", 1) for x in parts[2:])
        t1 = time.time()
        model = YOLO(path)
        accs: Dict[tuple, Acc] = {}
        per_case = []
        for i in range(0, len(cases), a.batch):
            res = model.predict([np.ascontiguousarray(x) for x in imgs[i:i + a.batch]], imgsz=N, conf=min(a.conf, 0.25), classes=[0], verbose=False, device=0)
            for c, r in zip(cases[i:i + a.batch], res):
                dbin = next(n for lo, hi, n in DIST_BINS if lo <= c["dist"] < hi)
                keys = [(c["variant"], "toutes distances"), (c["variant"], dbin)]
                v = judge(c, truth[c["id"]], KINDS[kind](r, params), a.conf, [accs.setdefault(k, Acc()) for k in keys], a.zoom)
                per_case.append(dict(v, id=c["id"], variant=c["variant"], dist=c["dist"], on_head=c["zone"] == HT.HEAD))
        rows = {f"{v} · {d}": acc.row() for (v, d), acc in accs.items()}
        results[name] = {"kind": kind, "model": path, "params": params, "rows": rows}
        if a.cases_out:
            json.dump(per_case, open(a.cases_out.replace("{name}", name), "w", encoding="utf-8"))
        print(f"\n=== {name}  ({kind}, {os.path.basename(path)}{', ' + ', '.join(parts[2:]) if parts[2:] else ''})  — {time.time() - t1:.1f} s")
        print(f"{'':28s}{'têtes':>6s} {'trouvée':>8s} {'erreur px (méd / p90)':>22s} {'biais y':>8s} {'viser':>7s} │ {'sur tête':>8s} {'tire':>7s} {'hors tête':>9s} {'à tort':>7s} {'juste':>7s}")
        for v in a.variants:
            for d in ["toutes distances"] + [n for _, _, n in DIST_BINS]:
                r = rows.get(f"{v} · {d}")
                if r is None:
                    continue
                print(f"{(v if d == 'toutes distances' else '') :8s}{d:20s}{r['têtes']:6d} {pct(r['trouvée']):>8s} {r['err_med']:10.2f} / {r['err_p90']:5.2f}    {100 * r['biais_y']:+7.1f}% {pct(r['viser']):>7s} │ "
                      f"{r['n_sur_tête']:8d} {pct(r['tire']):>7s} {r['n_hors_tête']:9d} {pct(r['à_tort']):>7s} {pct(r['juste']):>7s}")
    if a.out:
        json.dump(results, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n→ {a.out}")
    print(f"total {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
