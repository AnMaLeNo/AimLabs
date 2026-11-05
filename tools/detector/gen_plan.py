#!/usr/bin/env python3
"""gen_plan.py — plan de rendu des données d'entraînement du détecteur (lu par game/tools/gen_dataset.gd).

Une entrée = un carré de l'écran tel que le voit le réseau du sniper : carré centré sur le réticule, côté
`crop`/`screen_h` de la hauteur de l'écran (1/3 par défaut, comme `yolo_sniper --crop`). Le rendu utilise la
caméra du joueur (œil à 1,65 m, FOV vertical 75°) dont le champ est resserré sur ce carré : un pixel rendu
est un pixel de l'écran 2560×1440. Tout est tiré d'une graine : même graine, même plan.

Ce qui varie (c'est le but : « tous les personnages, tous les angles, toutes les distances, tous les fonds ») :
  * le personnage (29) et son orientation (0–360°, la moitié des tirages proches de « face au joueur » comme dans le
    stand de tir), sa pose (A-pose neutre du stand de tir, ou pose de l'animation de repos à un instant tiré) ;
  * la distance (2,5–45 m, log-uniforme) et la position dans le carré (de centré à coupé par le bord) ; hauteur
    d'œil, FOV, inclinaison de la caméra (±35° au plus : pas de plongée/contre-plongée extrême) ;
  * le fond : le vrai monde du jeu (ciel + sol quadrillé, avec ou sans variations de couleurs/soleil/brouillard),
    une photo sans personne, ou un fond uni / dégradé / bruité ; l'éclairage des personnages suit le fond ;
  * des figurants (autres personnages, 20 % des images) et des images sans personne (négatifs, ~12 %).

  python gen_plan.py --out plan.json --backgrounds <backgrounds.json> [--train 500 --val 30 --test 30] [--seed 1]
"""
import argparse
import hashlib
import json
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

EYE = 1.65
BODY_CENTER = 0.9
SCREEN_H = 1440.0
CROP = 480


def focal_px(fov_deg: float) -> float:
    return (SCREEN_H / 2.0) / math.tan(math.radians(fov_deg) / 2.0)


def project(p: np.ndarray, eye: float, pitch: float, f: float) -> Tuple[float, float]:
    """Pixel (x droite, y bas) du point monde p ; caméra en (0, eye, 0) regardant -Z, inclinée de `pitch` (rad, + = vers le haut)."""
    rel_x, rel_y, rel_z = p[0], p[1] - eye, p[2]
    c, s = math.cos(pitch), math.sin(pitch)
    y_c = rel_y * c + rel_z * s   # coordonnées caméra = Rx(-pitch) · rel
    z_c = -rel_y * s + rel_z * c
    return f * rel_x / -z_c, -f * y_c / -z_c


def place(d: float, ox: float, oy: float, eye: float, f: float) -> Optional[Tuple[float, float, float]]:
    """(x, z, pitch_deg) tels que le centre du corps d'un agent à `d` m tombe à (ox, oy) px du centre du carré."""
    pitch = math.atan2(BODY_CENTER - eye, d)
    az = math.atan2(ox, f)
    for _ in range(10):
        px, py = project(np.array([d * math.sin(az), BODY_CENTER, -d * math.cos(az)]), eye, pitch, f)
        pitch += math.atan2(oy - py, f)   # cible trop haut à l'écran → caméra vers le haut
        az += math.atan2(ox - px, f)      # cible trop à gauche → on la décale à droite
    if abs(math.degrees(pitch)) > 35.0:
        return None
    return d * math.sin(az), -d * math.cos(az), math.degrees(pitch)


def az_for_px(d: float, ox: float, eye: float, pitch_deg: float, f: float) -> float:
    az = math.atan2(ox, f)
    for _ in range(8):
        p = np.array([d * math.sin(az), BODY_CENTER, -d * math.cos(az)])
        px, _ = project(p, eye, math.radians(pitch_deg), f)
        az += math.atan2(ox - px, f)
    return az


def hsv(h: float, s: float, v: float) -> List[float]:
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(max(s, 0), 1), min(max(v, 0), 1))
    return [round(r, 3), round(g, 3), round(b, 3)]


def rnd_game_env(rng: np.random.Generator) -> Dict:
    """Variantes du monde du jeu : soleil, ciel, brouillard, sol."""
    e: Dict = {"sun_elev": float(rng.uniform(-85, -12)), "sun_az": float(rng.uniform(0, 360)),
               "sun_energy": float(rng.choice([rng.uniform(0.3, 0.9), rng.uniform(0.9, 1.8)])),
               "sun_color": [1.0, round(float(rng.uniform(0.82, 1.0)), 3), round(float(rng.uniform(0.62, 1.0)), 3)],
               "amb_energy": float(rng.uniform(0.3, 1.1)), "fog_density": float(rng.choice([0.0, 0.0012, rng.uniform(0.002, 0.006)]))}
    h = float(rng.uniform(0.52, 0.68)) if rng.random() < 0.7 else float(rng.uniform(0, 1))
    s = float(rng.uniform(0.15, 0.8))
    v = float(rng.uniform(0.35, 0.95))
    e["sky_top"] = hsv(h, s, v)
    e["sky_horizon"] = hsv(h + rng.uniform(-0.04, 0.04), s * 0.45, min(1.0, v + 0.3))
    e["sky_gb"] = hsv(float(rng.uniform(0, 1)), float(rng.uniform(0, 0.25)), float(rng.uniform(0.1, 0.4)))
    e["sky_gh"] = hsv(float(rng.uniform(0, 1)), float(rng.uniform(0, 0.2)), float(rng.uniform(0.3, 0.7)))
    e["fog_color"] = e["sky_horizon"]
    gh, gs, gv = float(rng.uniform(0, 1)), float(rng.uniform(0, 0.45)), float(rng.uniform(0.06, 0.65))
    e["g_a"] = hsv(gh, gs, gv)
    e["g_b"] = hsv(gh, gs, gv * float(rng.uniform(0.6, 1.0)))
    e["g_line"] = hsv(gh, gs * 0.5, min(1.0, gv + float(rng.uniform(0.1, 0.4))))
    e["g_cell"] = float(rng.choice([1.0, 2.0, 2.0, 3.0, 4.0]))
    e["g_strength"] = float(rng.uniform(0.15, 0.8))
    return e


def rnd_fg_env(rng: np.random.Generator, top: List[float], mid: List[float], bot: List[float], lum: float) -> Dict:
    """Éclairage d'un personnage collé sur un fond : le ciel de l'éclairage reprend les couleurs du fond."""
    sun_e = float(rng.uniform(0.15, 0.6)) if rng.random() < 0.25 else float(rng.uniform(0.7, 1.6))
    return {"sun_elev": float(rng.uniform(-80, -12)), "sun_az": float(rng.uniform(0, 360)), "sun_energy": sun_e,
            "sun_color": [1.0, round(float(rng.uniform(0.85, 1.0)), 3), round(float(rng.uniform(0.7, 1.0)), 3)],
            "amb_energy": round(min(0.3 + 0.7 * lum * float(rng.uniform(0.7, 1.3)), 0.95), 3),
            "sky_top": top, "sky_horizon": mid, "sky_gb": bot, "sky_gh": bot}


def mean_rgb(img: np.ndarray) -> List[float]:
    return [round(float(v) / 255.0, 3) for v in img.reshape(-1, 3).mean(axis=0)[::-1]]  # BGR → RGB


def split_of_bg(name: str, split: str) -> str:
    if split == "train":
        return "train"
    return "val" if int(hashlib.sha1(("t" + name).encode()).hexdigest()[:2], 16) % 2 == 0 else "test"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # sortie redirigée sous Windows = cp1252
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--backgrounds", required=True)
    ap.add_argument("--manifest", default=os.path.join(os.path.dirname(__file__), "..", "..", "game", "assets", "ka", "manifest.json"))
    ap.add_argument("--train", type=int, default=500, help="échantillons par personnage")
    ap.add_argument("--val", type=int, default=30)
    ap.add_argument("--test", type=int, default=30)
    ap.add_argument("--neg-frac", type=float, default=0.12, help="part d'images sans personne en plus")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--exclude", nargs="*", default=["harbor"], help="personnages à ne pas utiliser (harbor : textures manquantes, blanc)")
    a = ap.parse_args()
    import cv2

    slugs = sorted(k for k in json.load(open(a.manifest, encoding="utf-8"))["agents"].keys() if k not in a.exclude)
    bgs = json.load(open(a.backgrounds, encoding="utf-8"))
    bg_dir = bgs["images_dir"]
    by_split: Dict[str, List[dict]] = {"train": [], "val": [], "test": []}
    for it in bgs["items"]:
        by_split[split_of_bg(it["file"], it["split"])].append(it)
    print({k: len(v) for k, v in by_split.items()}, f"{len(slugs)} personnages", flush=True)

    samples: List[dict] = []
    for si, (split, per_agent) in enumerate([("train", a.train), ("val", a.val), ("test", a.test)]):
        rng = np.random.default_rng([a.seed, si])
        total = per_agent * len(slugs)
        n_neg = int(round(total * a.neg_frac))
        plan_agents = [slugs[i % len(slugs)] for i in range(total)] + [None] * n_neg
        rng.shuffle(plan_agents)
        for i, slug in enumerate(plan_agents):
            samples.append(make_sample(rng, f"{split[0]}{si}{i:06d}", split, slug, slugs, by_split[split], bg_dir, cv2))
    samples = [s for s in samples if s is not None]
    json.dump({"size": CROP, "seed": a.seed, "samples": samples}, open(a.out, "w"))
    kinds: Dict[str, int] = {}
    for s in samples:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    print(f"{len(samples)} échantillons → {a.out}\n  {kinds}")


def make_sample(rng, sid: str, split: str, slug: Optional[str], slugs: List[str], bgs: List[dict], bg_dir: str, cv2) -> Optional[dict]:
    fov = float(rng.choice([75.0, 75.0, 75.0, 82.0, rng.uniform(60.0, 95.0)]))
    eye = EYE if rng.random() < 0.7 else float(rng.uniform(0.8, 2.4))
    f = focal_px(fov)
    fov_crop = math.degrees(2.0 * math.atan(math.tan(math.radians(fov) / 2.0) * CROP / SCREEN_H))
    negative = slug is None
    r = rng.random()
    if negative:
        kind = "neg_game" if r < 0.35 else ("neg_photo" if r < 0.85 else "neg_plain")
    else:
        kind = "game" if r < 0.40 else ("photo" if r < 0.88 else "plain")
    mode = "game" if kind.endswith("game") or kind == "game" else "photo"
    agents: List[dict] = []
    pitch_deg = float(rng.uniform(-12, 12))
    meta: dict = {"split": split, "kind": kind, "eye": eye, "fov": fov}
    if not negative:
        for _ in range(20):
            d = float(math.exp(rng.uniform(math.log(2.5), math.log(45.0))))
            lim = 0.2 * CROP if rng.random() < 0.5 else 0.65 * CROP
            ox, oy = float(rng.uniform(-lim, lim)), float(rng.uniform(-lim, lim))
            pl = place(d, ox, oy, eye, f)
            if pl is not None:
                break
        else:
            return None
        x, z, pitch_deg = pl
        az = math.atan2(x, -z)
        rel = float(rng.normal(0, 35)) if rng.random() < 0.45 else float(rng.uniform(0, 360))
        neutral = bool(rng.random() < 0.75)
        agents.append({"slug": slug, "neutral": neutral, "anim_t": float(rng.random()), "x": x, "z": z,
                       "rot_deg": math.degrees(-az) + rel})
        meta.update(dist=d, off_px=[ox, oy], rel_yaw=rel % 360.0, neutral=neutral)
        if rng.random() < 0.20:
            for _ in range(int(rng.integers(1, 3))):
                d2 = float(np.clip(d * rng.uniform(0.6, 1.6), 2.5, 50.0))
                az2 = az_for_px(d2, float(rng.uniform(-0.5, 0.5) * CROP), eye, pitch_deg, f)
                agents.append({"slug": str(rng.choice(slugs)), "neutral": bool(rng.random() < 0.75), "anim_t": float(rng.random()),
                               "x": d2 * math.sin(az2), "z": -d2 * math.cos(az2),
                               "rot_deg": math.degrees(-az2) + (float(rng.normal(0, 35)) if rng.random() < 0.45 else float(rng.uniform(0, 360)))})
    sample = {"id": sid, "mode": mode, "kind": kind, "cam": {"eye": eye, "pitch_deg": pitch_deg, "fov": fov_crop}, "agents": agents, "meta": meta}
    if mode == "game":
        sample["env"] = rnd_game_env(rng) if rng.random() < 0.4 else {}
        return sample
    if kind.endswith("photo"):
        item = bgs[int(rng.integers(len(bgs)))]
        img = cv2.imread(os.path.join(bg_dir, item["file"]))
        h, w = img.shape[:2]
        side = int(min(h, w) * rng.uniform(0.45, 1.0))
        x0, y0 = int(rng.integers(0, w - side + 1)), int(rng.integers(0, h - side + 1))
        patch = img[y0:y0 + side, x0:x0 + side]
        flip = bool(rng.random() < 0.5)
        if flip:
            patch = patch[:, ::-1]
        third = max(side // 3, 1)
        top, mid, bot = mean_rgb(patch[:third]), mean_rgb(patch[third:2 * third]), mean_rgb(patch[2 * third:])
        lum = float(patch.mean() / 255.0)
        sample["bg"] = {"type": "photo", "file": item["file"], "box": [x0, y0, side], "flip": flip}
    else:
        c1 = hsv(float(rng.uniform(0, 1)), float(rng.uniform(0, 0.9)), float(rng.uniform(0.03, 1.0)))
        c2 = c1 if rng.random() < 0.4 else hsv(float(rng.uniform(0, 1)), float(rng.uniform(0, 0.9)), float(rng.uniform(0.03, 1.0)))
        top, mid, bot, lum = c1, [(p + q) / 2 for p, q in zip(c1, c2)], c2, float(np.mean(c1 + c2) / 2)
        sample["bg"] = {"type": str(rng.choice(["solid", "gradient", "noise"])), "c1": c1, "c2": c2, "angle": float(rng.uniform(0, 360)),
                        "seed": int(rng.integers(1 << 30))}
    sample["env"] = rnd_fg_env(rng, top, mid, bot, lum)
    return sample


if __name__ == "__main__":
    main()
