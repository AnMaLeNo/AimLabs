#!/usr/bin/env python3
"""compose.py — transforme les rendus de gen_dataset.gd en jeu de données YOLO-seg (images + polygones).

  * fonds « photo » / « uni » : le personnage (RGBA rendu par Godot, prémultiplié en linéaire) est composé en linéaire
    sur le fond, avec ombre de contact et petites dégradations de prise de vue ;
  * fonds « game » : l'image rendue du monde du jeu telle quelle (+ dégradations plus rares) ;
  * étiquettes : le masque visible de chaque personnage vient du canal alpha de SON rendu (jamais d'un modèle) ;
    avec des figurants, la partie cachée par un personnage plus proche est retirée ;
  * le réticule vert du jeu (hud.gd) est dessiné au centre (comme dans la vraie capture), 90 % des images.

Sortie : <out>/images/{train,val,test}/<id>.jpg, labels/…/<id>.txt (« 0 x1 y1 x2 y2 … » normalisés), meta.jsonl, data.yaml.
Classe 0 = « person » avec les 80 noms COCO conservés : le modèle fine-tuné garde la forme de sortie (4+80+32) que lisent
yolo_sniper / sniper-rs / sniper_b (filtre classe 0).

  python compose.py --plan plan.json --renders <dir> --backgrounds <backgrounds.json> --out <dataset dir>
"""
import argparse
import json
import math
import os
import sys
import zlib
from multiprocessing import Pool
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

MIN_AREA = 40  # px (carré 480×480) en dessous : pas d'étiquette


def s2l(x: np.ndarray) -> np.ndarray:
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def l2s(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)


def read_rgba(path: str) -> np.ndarray:
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None:
        raise FileNotFoundError(path)
    return im


def plain_bg(spec: dict, size: int) -> np.ndarray:
    """Fond uni / dégradé / bruité, float32 RGB 0–1 (sRGB)."""
    rng = np.random.default_rng(spec["seed"])
    c1, c2 = np.array(spec["c1"], np.float32), np.array(spec["c2"], np.float32)
    kind = spec["type"]
    if kind == "solid":
        t = np.zeros((size, size), np.float32)
    elif kind == "gradient":
        ang = math.radians(spec["angle"])
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) / size - 0.5
        t = np.clip((xx * math.cos(ang) + yy * math.sin(ang)) + 0.5, 0, 1)
    else:  # noise basse fréquence + grain
        k = int(rng.integers(3, 12))
        t = cv2.resize(rng.random((k, k)).astype(np.float32), (size, size), interpolation=cv2.INTER_CUBIC)
        t = np.clip(t + rng.normal(0, 0.04, (size, size)).astype(np.float32), 0, 1)
    return c1 * (1 - t[..., None]) + c2 * t[..., None]


def photo_bg(spec: dict, images_dir: str, size: int) -> np.ndarray:
    img = cv2.imread(os.path.join(spec.get("dir", images_dir), spec["file"]))
    x0, y0, side = spec["box"]
    patch = img[y0:y0 + side, x0:x0 + side]
    if spec.get("flip"):
        patch = patch[:, ::-1]
    patch = cv2.resize(patch, (size, size), interpolation=cv2.INTER_AREA if side > size else cv2.INTER_CUBIC)
    return patch[..., ::-1].astype(np.float32) / 255.0


def masks_and_order(sample: dict, renders: str) -> List[np.ndarray]:
    """Un masque booléen « visible » par agent (plan['agents'] dans le même ordre)."""
    sid, agents = sample["id"], sample["agents"]
    if len(agents) == 1:
        return [read_rgba(f"{renders}/{sid}_f.png")[..., 3] >= 128]
    raw = [read_rgba(f"{renders}/{sid}_m{k}.png")[..., 3] >= 128 for k in range(len(agents))]
    dist = [math.hypot(a["x"], a["z"]) for a in agents]
    vis = []
    for k, m in enumerate(raw):
        nearer = np.zeros_like(m)
        for j, mj in enumerate(raw):
            if j != k and dist[j] < dist[k]:
                nearer |= mj
        vis.append(m & ~nearer)
    return vis


def polygon_of(mask: np.ndarray) -> Optional[np.ndarray]:
    """Contour (n, 2) en pixels de l'image ; plusieurs morceaux → un seul polygone (comme ultralytics)."""
    from ultralytics.data.converter import merge_multi_segment

    m2 = cv2.resize(mask.astype(np.uint8) * 255, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    cnts, _ = cv2.findContours(m2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    segs = []
    biggest = max((cv2.contourArea(c) for c in cnts), default=0)
    for c in cnts:
        if cv2.contourArea(c) < max(2.0, 0.02 * biggest):
            continue
        c = cv2.approxPolyDP(c, 1.2, True)
        if len(c) >= 3:
            segs.append(((c.reshape(-1, 2).astype(np.float32) + 0.5) / 2.0))
    if not segs:
        return None
    if len(segs) == 1:
        return segs[0]
    return np.concatenate(merge_multi_segment(segs), axis=0)


def draw_reticle(img: np.ndarray, size: int) -> np.ndarray:
    """Réticule de hud.gd (espace 4× puis réduit) : 4 traits vert, contour noir 80 %, point central."""
    k = 4
    layer = np.zeros((size * k, size * k, 4), np.uint8)
    c = size * k // 2
    green, black = (102, 255, 51, 255), (0, 0, 0, 204)  # BGRA ; vert = Color(0.2, 1.0, 0.4), contour noir 80 %
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    for col, width in [(black, 4 * k), (green, 2 * k)]:  # contour (épaisseur 2+2) puis trait (2)
        for dx, dy in dirs:
            cv2.line(layer, (c + dx * 5 * k, c + dy * 5 * k), (c + dx * 14 * k, c + dy * 14 * k), col, width, cv2.LINE_8)
    cv2.circle(layer, (c, c), int(1.5 * k), green, -1)
    layer = cv2.resize(layer, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    a = layer[..., 3:4]
    return (img * (1 - a) + layer[..., :3] * a * 255.0).astype(np.uint8)  # layer BGR, img BGR


def degrade(img: np.ndarray, rng: np.random.Generator, p_scale: float) -> np.ndarray:
    """Petites dégradations de prise de vue (flou, bruit, compression, luminosité). img uint8 BGR."""
    if rng.random() < 0.35 * p_scale:
        img = cv2.GaussianBlur(img, (0, 0), float(rng.uniform(0.3, 1.0)))
    if rng.random() < 0.40 * p_scale:
        alpha, beta = float(rng.uniform(0.88, 1.12)), float(rng.uniform(-10, 10))
        img = np.clip(img.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)
    if rng.random() < 0.50 * p_scale:
        img = np.clip(img.astype(np.float32) + rng.normal(0, float(rng.uniform(1, 5)), img.shape), 0, 255).astype(np.uint8)
    if rng.random() < 0.25 * p_scale:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(60, 93))])
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return img


def contact_shadow(bg: np.ndarray, mask: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    ys, xs = np.nonzero(mask)
    if len(xs) < MIN_AREA:
        return bg
    x1, x2, y2 = xs.min(), xs.max(), ys.max()
    layer = np.zeros(mask.shape, np.float32)
    w = max(x2 - x1, 6)
    cv2.ellipse(layer, (int((x1 + x2) / 2), int(y2)), (int(w * 0.6), max(int(w * 0.14), 2)), 0, 0, 360, 1.0, -1)
    layer = cv2.GaussianBlur(layer, (0, 0), max(w * 0.06, 1.0))
    return bg * (1.0 - float(rng.uniform(0.25, 0.55)) * layer[..., None])


def build(job: Tuple[dict, str, str, str, int]) -> Optional[dict]:
    sample, renders, out, bg_dir, size = job
    sid, split = sample["id"], sample["meta"]["split"]
    rng = np.random.default_rng(zlib.crc32(sid.encode()))
    agents = sample["agents"]
    vis = masks_and_order(sample, renders) if agents else []
    if sample["mode"] == "game":
        img = cv2.imread(f"{renders}/{sid}_c.png")
        p_scale = 0.5
    else:
        bg = plain_bg(sample["bg"], size) if sample["bg"]["type"] != "photo" else photo_bg(sample["bg"], bg_dir, size)
        if sample["bg"]["type"] == "photo" and rng.random() < 0.3:
            bg = cv2.GaussianBlur(bg, (0, 0), float(rng.uniform(0.8, 2.5)))
        if agents:
            if rng.random() < 0.5:
                for k, m in enumerate(vis):
                    bg = contact_shadow(bg, m, rng)
            fg = read_rgba(f"{renders}/{sid}_f.png")
            a = (fg[..., 3:4].astype(np.float32) / 255.0)
            gain = float(rng.uniform(0.85, 1.15))
            lin = s2l(fg[..., 2::-1].astype(np.float32) / 255.0) * gain  # BGR → RGB, premultiplié linéaire
            out_lin = lin + s2l(bg) * (1.0 - a)
            rgb = l2s(out_lin)
        else:
            rgb = bg
        img = (rgb[..., ::-1] * 255.0 + 0.5).astype(np.uint8)
        p_scale = 1.0
    img = degrade(img, rng, p_scale)
    if rng.random() < 0.90:
        img = draw_reticle(img, size)
    lines, boxes = [], []
    for m in vis:
        area = int(m.sum())
        if area < MIN_AREA:
            continue
        poly = polygon_of(m)
        if poly is None:
            continue
        x1, y1 = poly.min(axis=0)
        x2, y2 = poly.max(axis=0)
        if x2 - x1 < 3 or y2 - y1 < 3:
            continue
        n = np.clip(poly / size, 0.0, 1.0).reshape(-1)
        lines.append("0 " + " ".join(f"{v:.5f}" for v in n))
        boxes.append([float(x1), float(y1), float(x2), float(y2), area])
    os.makedirs(f"{out}/images/{split}", exist_ok=True)
    os.makedirs(f"{out}/labels/{split}", exist_ok=True)
    cv2.imwrite(f"{out}/images/{split}/{sid}.jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    with open(f"{out}/labels/{split}/{sid}.txt", "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    meta = dict(sample["meta"], id=sid, mode=sample["mode"], agents=[a["slug"] for a in agents], boxes=boxes)
    if sample.get("bg"):
        meta["bg"] = sample["bg"].get("file", sample["bg"]["type"])
    return meta


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # sortie redirigée sous Windows = cp1252
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True)
    ap.add_argument("--renders", required=True)
    ap.add_argument("--backgrounds", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--exclude", nargs="*", default=["harbor"], help="personnages écartés (aucun échantillon où ils apparaissent, même en figurant)")
    ap.add_argument("--names-from", default=os.path.join(os.path.dirname(__file__), "..", "..", "algos", "yolo11n-seg.pt"))
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    bg_dir = json.load(open(a.backgrounds))["images_dir"]
    size = plan["size"]
    def rendered(s: dict) -> bool:
        need = f"{a.renders}/{s['id']}_c.png" if s["mode"] == "game" else (f"{a.renders}/{s['id']}_f.png" if s["agents"] else None)
        return need is None or os.path.exists(need)

    done = [s for s in plan["samples"] if rendered(s) and not any(x["slug"] in a.exclude for x in s["agents"])]
    print(f"{len(done)}/{len(plan['samples'])} échantillons rendus")
    jobs = [(s, a.renders, a.out, bg_dir, size) for s in done]
    metas = []
    with Pool(a.workers) as p:
        for i, m in enumerate(p.imap_unordered(build, jobs, chunksize=16)):
            if m is not None:
                metas.append(m)
            if (i + 1) % 2000 == 0:
                print(f"  {i + 1}/{len(jobs)}", flush=True)
    metas.sort(key=lambda m: m["id"])
    with open(f"{a.out}/meta.jsonl", "w") as f:
        for m in metas:
            f.write(json.dumps(m) + "\n")
    from ultralytics import YOLO
    names = YOLO(a.names_from).names
    with open(f"{a.out}/data.yaml", "w") as f:
        f.write(f"path: {os.path.abspath(a.out).replace(chr(92), '/')}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n")
        for k, v in names.items():
            f.write(f"  {k}: {v}\n")
    n_lab = sum(len(m["boxes"]) for m in metas)
    print(f"{len(metas)} images, {n_lab} étiquettes → {a.out}")


if __name__ == "__main__":
    main()
