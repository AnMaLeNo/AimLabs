#!/usr/bin/env python3
"""head_truth.py — « quels pixels sont un tir à la tête ? », d'après le moteur du jeu, pour chaque image du jeu de données.

`game/tools/gen_dataset.gd --truth=<fichier.jsonl>` écrit par image la caméra, les capsules de tir de chaque personnage (celles que
`player.gd` vise d'un rayon : la plus proche gagne) et la position de quelques os. Ici, le même calcul rayon → capsule est refait en numpy
pour n'importe quel pixel : zone du jeu sous ce pixel (-1 rien, 0 corps, 1 tête, 2 bras) et personnage touché. C'est la vérité du banc
rapide `head_bench.py` (aucune partie à lancer) et la source des étiquettes « tête » d'entraînement (`pose_labels.py`).

  python head_truth.py check --truth <probe.jsonl>      # compare aux vrais rayons du moteur (gen_dataset.gd --probe=N)
  python head_truth.py show --truth <t.jsonl> --images <ds480/images/test> --out <dossier> [--n 12]   # planche de contrôle : zone tête sur l'image

Repère des pixels : (x vers la droite, y vers le bas), en pixels du carré rendu (480 par défaut) ; le pixel d'indice (i, j) a son centre en
(i + 0,5 ; j + 0,5). Le réticule est au centre du carré, (size / 2, size / 2).
"""
import argparse
import json
import math
import os
import sys
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

BODY, HEAD, ARM = 0, 1, 2


def read_truth(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def focal(sample: dict) -> float:
    return (sample["size"] / 2.0) / math.tan(math.radians(sample["cam"]["fov"]) / 2.0)


def rays(sample: dict, px: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Origine (3,) et directions unitaires (N, 3) des rayons passant par les pixels `px` (N, 2)."""
    f, half = focal(sample), sample["size"] / 2.0
    p = math.radians(sample["cam"]["pitch_deg"])
    c, s = math.cos(p), math.sin(p)
    xc, yc, zc = (px[:, 0] - half) / f, -(px[:, 1] - half) / f, -np.ones(len(px))
    d = np.stack([xc, yc * c - zc * s, yc * s + zc * c], axis=1)        # monde = Rx(pitch) · caméra
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    return np.array([0.0, sample["cam"]["eye"], 0.0]), d


def project(sample: dict, pts: np.ndarray) -> np.ndarray:
    """Pixels (N, 2) des points monde (N, 3)."""
    f, half = focal(sample), sample["size"] / 2.0
    p = math.radians(sample["cam"]["pitch_deg"])
    c, s = math.cos(p), math.sin(p)
    rel = pts - np.array([0.0, sample["cam"]["eye"], 0.0])
    yc = rel[:, 1] * c + rel[:, 2] * s
    zc = -rel[:, 1] * s + rel[:, 2] * c
    return np.stack([half + f * rel[:, 0] / -zc, half - f * yc / -zc], axis=1)


def _sphere(o: np.ndarray, d: np.ndarray, c: np.ndarray, r: float) -> np.ndarray:
    m = o - c
    b = d @ m
    h = b * b - (m @ m - r * r)
    t = np.where(h >= 0, -b - np.sqrt(np.maximum(h, 0)), np.inf)
    return np.where(t > 0, t, np.inf)


def _capsule(o: np.ndarray, d: np.ndarray, a: np.ndarray, b: np.ndarray, r: float) -> np.ndarray:
    """Distance le long de chaque rayon jusqu'à la capsule (segment a–b, rayon r), inf si manquée."""
    t = np.minimum(_sphere(o, d, a, r), _sphere(o, d, b, r))
    ba, oa = b - a, o - a
    baba = ba @ ba
    if baba < 1e-12:
        return t
    bard, baoa, rdoa, oaoa = d @ ba, ba @ oa, d @ oa, oa @ oa
    A = baba - bard * bard
    B = baba * rdoa - baoa * bard
    C = baba * oaoa - baoa * baoa - r * r * baba
    h = B * B - A * C
    ok = (h >= 0) & (np.abs(A) > 1e-12)
    tc = np.where(ok, (-B - np.sqrt(np.maximum(h, 0))) / np.where(ok, A, 1.0), np.inf)
    y = baoa + tc * bard
    tc = np.where(ok & (y > 0) & (y < baba) & (tc > 0), tc, np.inf)
    return np.minimum(t, tc)


# dimensions de game/scripts/enemy.gd (HEAD_AXIS_LOW, HEAD_AXIS_HIGH, HEAD_RADIUS, TORSO_NECK_DROP) : à tenir identiques
HEAD_AXIS_LOW, HEAD_AXIS_HIGH, HEAD_RADIUS, TORSO_NECK_DROP, TORSO_RADIUS = 0.03, 0.10, 0.12, 0.12, 0.17


def shown_caps(ag: dict) -> List[list]:
    """Capsules de la tête et du torse là où le personnage est DESSINÉ. En pose neutre (la seule que le jeu utilise, world.gd) le jeu construit ses capsules
    sur la pose affichée : elles sont rendues telles quelles. Dans les poses « sélection de personnage » du jeu de données (25 % des images d'entraînement)
    le jeu les construit sur la pose de repos et la tête affichée est ailleurs (médiane 0,12 m, jusqu'à 1 m) : pour étiqueter ce qu'une image MONTRE, on
    reconstruit alors tête et torse sur les os animés, avec les dimensions de enemy.gd."""
    b = ag["bones"]
    if not all(k in b for k in ("Head", "Neck", "Pelvis")):
        return ag["caps"]
    h, n, pv = np.array(b["Head"]), np.array(b["Neck"]), np.array(b["Pelvis"])
    hc = [c for c in ag["caps"] if c[0] == HEAD]
    if hc and float(np.linalg.norm(h - (np.array(hc[0][1:4]) - np.array([0.0, HEAD_AXIS_LOW, 0.0])))) < 0.01:
        return ag["caps"]
    u = (h - n) / max(1e-6, float(np.linalg.norm(h - n)))
    t = (n - pv) / max(1e-6, float(np.linalg.norm(n - pv)))
    return [[HEAD] + list(h + HEAD_AXIS_LOW * u) + list(h + HEAD_AXIS_HIGH * u) + [HEAD_RADIUS], [BODY] + list(pv) + list(n - TORSO_NECK_DROP * t) + [TORSO_RADIUS]]


def zones_at(sample: dict, px: np.ndarray, shown: bool = False) -> Tuple[np.ndarray, np.ndarray]:
    """Pour chaque pixel : (zone du jeu, indice du personnage touché), -1 / -1 si le rayon ne touche personne.
    `shown` : capsules là où les personnages sont dessinés (voir shown_caps) au lieu de celles du jeu."""
    o, d = rays(sample, np.asarray(px, np.float64))
    best = np.full(len(d), np.inf)
    zone = np.full(len(d), -1, np.int8)
    who = np.full(len(d), -1, np.int16)
    for k, ag in enumerate(sample["agents"]):
        for cap in (shown_caps(ag) if shown else ag["caps"]):
            t = _capsule(o, d, np.array(cap[1:4]), np.array(cap[4:7]), float(cap[7]))
            m = t < best
            best[m], zone[m], who[m] = t[m], int(cap[0]), k
    return zone, who


def zone_map(sample: dict, box: Optional[Tuple[int, int, int, int]] = None, step: float = 1.0, shown: bool = False) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Carte des zones sur la grille des centres de pixels de `box` (x0, y0, x1, y1 ; défaut : tout le carré), pas `step` px.
    Retourne (xs, ys, zone[y, x], who[y, x])."""
    size = sample["size"]
    x0, y0, x1, y1 = box or (0, 0, size, size)
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    gx, gy = np.meshgrid(xs, ys)
    z, w = zones_at(sample, np.stack([gx.ravel(), gy.ravel()], axis=1), shown)
    return xs, ys, z.reshape(gy.shape), w.reshape(gy.shape)


def blocked(px: np.ndarray, blockers) -> np.ndarray:
    """Pixels (N, 2) cachés par un des rectangles `blockers` [(x0, y0, x1, y1), …] (un mur devant le personnage : on ne tire pas à travers)."""
    out = np.zeros(len(px), bool)
    for x0, y0, x1, y1 in blockers or ():
        out |= (px[:, 0] >= x0) & (px[:, 0] < x1) & (px[:, 1] >= y0) & (px[:, 1] < y1)
    return out


def head_info(sample: dict, k: int, shown: bool = False, blockers=None, cells: float = 15.0) -> Optional[Dict]:
    """La zone « tête » du personnage `k` telle que le jeu la compte (capsule de tête, moins ce que le torse, un autre personnage ou un mur
    `blockers` cache) : centre (barycentre), boîte englobante, surface en px², part de la zone qui tombe dans le carré. None si aucun pixel n'est
    un tir à la tête. Coordonnées en pixels du carré ; la zone peut déborder du carré (personnage coupé par le bord). La grille de calcul compte
    `cells` points par rayon de tête, quelle que soit la distance."""
    caps = shown_caps(sample["agents"][k]) if shown else sample["agents"][k]["caps"]
    hc = [c for c in caps if c[0] == HEAD]
    if not hc:
        return None
    ends = project(sample, np.array([hc[0][1:4], hc[0][4:7]]))
    r_px = focal(sample) * hc[0][7] / max(1e-6, _depth(sample, np.array(hc[0][1:4])))
    pad = 1.3 * r_px + 1
    box = (ends[:, 0].min() - pad, ends[:, 1].min() - pad, ends[:, 0].max() + pad, ends[:, 1].max() + pad)
    st = r_px / cells
    xs, ys, z, w = zone_map(sample, box, st, shown)
    gx, gy = np.meshgrid(xs, ys)
    m = (z == HEAD) & (w == k)
    full = int(m.sum())
    if blockers:
        m &= ~blocked(np.stack([gx.ravel(), gy.ravel()], axis=1), blockers).reshape(gy.shape)
    if not m.any():
        return None
    size = sample["size"]
    inside = m & (gx >= 0) & (gx < size) & (gy >= 0) & (gy < size)
    return {"cx": float(gx[m].mean()), "cy": float(gy[m].mean()), "x0": float(gx[m].min() - st / 2), "x1": float(gx[m].max() + st / 2),
            "y0": float(gy[m].min() - st / 2), "y1": float(gy[m].max() + st / 2), "area": float(m.sum() * st * st), "r_px": float(r_px),
            "in_frac": float(inside.sum() / m.sum()), "wall_frac": 1.0 - float(m.sum() / max(1, full))}


def _depth(sample: dict, p: np.ndarray) -> float:
    pr = math.radians(sample["cam"]["pitch_deg"])
    rel = p - np.array([0.0, sample["cam"]["eye"], 0.0])
    return float(-(-rel[1] * math.sin(pr) + rel[2] * math.cos(pr)))


def pick_wall(rng: np.random.Generator, sample: dict, k: int, hi: Dict, kind: Optional[str] = None) -> Dict:
    """Un mur tiré au hasard devant le personnage `k` (dont `hi` est la zone tête sans mur), en pixels du carré : soit un muret qui cache le bas du
    corps (il reste la tête, plus ou moins d'épaules et de torse), soit un angle de mur qui cache un côté (de « tête et une épaule visibles » à
    « un cinquième de la tête dépasse »). `kind` impose « muret » ou « angle ». Retourne {"kind", "rect": (x0, y0, x1, y1), "look": …} ; à dessiner avec draw_wall."""
    size = sample["size"]
    r = hi["r_px"]
    look = {"hue": float(rng.uniform(0, 1)), "sat": float(rng.uniform(0, 0.35)), "val": float(rng.uniform(0.12, 0.8)), "grad": float(rng.uniform(-0.15, 0.15)),
            "noise": float(rng.uniform(1.0, 6.0)), "edge": bool(rng.random() < 0.5), "seed": int(rng.integers(1 << 30))}
    if (rng.random() < 0.5) if kind is None else (kind == "muret"):
        b = sample["agents"][k]["bones"]
        pelvis_y = float(project(sample, np.array([b["Pelvis"]]))[0][1]) if "Pelvis" in b else hi["y1"] + 6 * r
        ycut = hi["y1"] + float(rng.uniform(0.05, 0.9)) * max(2.0, pelvis_y - hi["y1"])
        return {"kind": "muret", "rect": (0.0, ycut, float(size), float(size)), "look": look}
    t = float(rng.uniform(-2.5, 0.6))
    if rng.random() < 0.5:
        return {"kind": "angle", "rect": (0.0, 0.0, hi["cx"] + t * r, float(size)), "look": look}
    return {"kind": "angle", "rect": (hi["cx"] - t * r, 0.0, float(size), float(size)), "look": look}


def draw_wall(img: np.ndarray, wall: Dict, size: int) -> np.ndarray:
    """Dessine le mur sur `img` (BGR, n'importe quelle taille : le rectangle est donné en pixels d'un carré de côté `size`)."""
    import colorsys
    h, w = img.shape[:2]
    k = w / float(size)
    x0, y0, x1, y1 = [int(round(v * k)) for v in wall["rect"]]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return img
    lk = wall["look"]
    rgb = np.array(colorsys.hsv_to_rgb(lk["hue"], lk["sat"], lk["val"]))[::-1] * 255.0
    rng = np.random.default_rng(lk["seed"])
    ramp = 1.0 + lk["grad"] * np.linspace(-1, 1, y1 - y0)[:, None, None]
    patch = rgb[None, None, :] * ramp + rng.normal(0, lk["noise"], (y1 - y0, x1 - x0, 1))
    out = img.copy()
    out[y0:y1, x0:x1] = np.clip(patch, 0, 255).astype(np.uint8)
    if lk["edge"]:
        dark = np.clip(rgb * 0.55, 0, 255).astype(np.uint8)
        if wall["kind"] == "muret":
            out[y0:min(h, y0 + max(1, int(k * 2))), x0:x1] = dark
        elif x0 == 0:
            out[y0:y1, max(0, x1 - max(1, int(k * 2))):x1] = dark
        else:
            out[y0:y1, x0:min(w, x0 + max(1, int(k * 2)))] = dark
    return out


def cmd_check(a) -> None:
    n = bad = 0
    by = {}
    worst = []
    for s in read_truth(a.truth):
        for k, ag in enumerate(s["agents"]):
            pr = np.array(ag.get("probe", []), np.float64).reshape(-1, 3)
            if not len(pr):
                continue
            z, w = zones_at(s, pr[:, :2])
            z = np.where(w == k, z, np.where(z >= 0, z, -1))            # le moteur donne la zone du premier personnage touché, quel qu'il soit
            for got, want in zip(z, pr[:, 2].astype(int)):
                by[(int(want), int(got))] = by.get((int(want), int(got)), 0) + 1
            miss = z != pr[:, 2]
            n += len(pr)
            bad += int(miss.sum())
            if miss.any() and len(worst) < 8:
                worst.append((s["id"], k, pr[miss][:3].tolist(), z[miss][:3].tolist()))
            if "head_px" in ag:
                hp = project(s, np.array([ag["bones"]["Head"]]))[0]
                assert np.abs(hp - np.array(ag["head_px"])).max() < 0.02, (s["id"], hp, ag["head_px"])
    print(f"{n} rayons du moteur comparés : {bad} désaccords ({100.0 * bad / max(n, 1):.3f} %)")
    print("  (zone du moteur, zone calculée) : nombre  — -1 rien, 0 corps, 1 tête, 2 bras")
    for key in sorted(by):
        print(f"  {key}: {by[key]}")
    for w in worst:
        print("  désaccord :", w)
    print("projection de l'os de la tête : identique à celle du moteur (< 0,02 px) sur toutes les images")


def cmd_show(a) -> None:
    import cv2
    os.makedirs(a.out, exist_ok=True)
    done = 0
    for s in read_truth(a.truth):
        img = cv2.imread(os.path.join(a.images, s["id"] + ".jpg"))
        if img is None or not s["agents"]:
            continue
        infos = [head_info(s, k) for k in range(len(s["agents"]))]
        if a.near and not any(i and i["r_px"] > a.near for i in infos):
            continue
        z = 4
        big = cv2.resize(img, None, fx=z, fy=z, interpolation=cv2.INTER_NEAREST)
        xs, ys, zm, _ = zone_map(s, None, 1.0)
        ov = big.copy()
        for zone, col in ((HEAD, (255, 0, 255)), (BODY, (0, 200, 0)), (ARM, (0, 160, 255))):
            m = cv2.resize((zm == zone).astype(np.uint8), None, fx=z, fy=z, interpolation=cv2.INTER_NEAREST).astype(bool)
            ov[m] = col
        big = cv2.addWeighted(big, 0.65, ov, 0.35, 0)
        for k, ag in enumerate(s["agents"]):
            pts = project(s, np.array([ag["bones"][b] for b in ("Head", "M_Nose", "L_Eyeball", "R_Eyeball") if b in ag["bones"]]))
            for p in pts:
                cv2.circle(big, (int(p[0] * z), int(p[1] * z)), 3, (0, 255, 255), -1)
            if infos[k]:
                cv2.drawMarker(big, (int(infos[k]["cx"] * z), int(infos[k]["cy"] * z)), (255, 255, 255), cv2.MARKER_CROSS, 14, 2)
        cv2.imwrite(os.path.join(a.out, s["id"] + ".png"), big)
        done += 1
        if done >= a.n:
            break
    print(f"{done} planches → {a.out}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--truth", required=True)
    c = sub.add_parser("show")
    c.add_argument("--truth", required=True)
    c.add_argument("--images", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--n", type=int, default=12)
    c.add_argument("--near", type=float, default=0.0, help="seulement les images dont une tête a un rayon de plus de N px")
    a = ap.parse_args()
    {"check": cmd_check, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
