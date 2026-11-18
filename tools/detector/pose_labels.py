#!/usr/bin/env python3
"""pose_labels.py — étiquettes « pose » (boîte + 20 points) pour un jeu de données déjà rendu, à partir de la vérité du moteur.

Entraîner un modèle YOLO-pose à donner LA TÊTE telle que le jeu la compte, pas seulement une silhouette. Les images sont celles du jeu de données
de segmentation (rien n'est re-rendu) ; les points viennent de `gen_dataset.gd --truth --no-render` (capsules de tir + os du squelette) :

  0  tête : centre de la zone « tête » (barycentre des pixels où un tir est un tir à la tête d'après le jeu)
  1  bord gauche de cette zone (à l'image)      2  bord droit      3  haut de la zone      4  bas de la zone
  5  nez      6  œil gauche      7  œil droit                       (os M_Nose, L_Eyeball, R_Eyeball ; gauche / droite = ceux du personnage)
  8–19  épaules, coudes, mains, hanches, genoux, pieds (gauche puis droite du personnage)          (os L_Shoulder … R_Foot, dans la pose affichée)

Les points 0 à 4 ne sont pas des repères anatomiques : ils décrivent la zone où le jeu compte un tir à la tête (capsule de tête de enemy.gd, moins ce
que le torse, un autre personnage ou un mur cache), c'est-à-dire ce que le tir automatique « uniquement sur la tête » doit viser. Le nez et les yeux
ne sont étiquetés que si le visage est tourné vers la caméra (moins de 100° entre le regard et la caméra). Un point hors de l'image ou derrière un mur
est marqué absent (v = 0). `flip_idx` du data.yaml : 1↔2, 6↔7 et les paires gauche / droite du corps. `kpt_shape` [20, 3] : `train_pose.py` donne à la
perte les tolérances par point (OKS) que ultralytics ne connaît que pour les 17 points de COCO.

`--walls 0.25` : sur cette part des images d'entraînement avec personnage, un MUR est dessiné devant lui (muret qui cache le bas du corps, ou angle
qui cache un côté : head_truth.pick_wall) ; boîte, points et zone tête sont recalculés pour ce qui reste visible. Le jeu de données d'origine ne
contient aucun personnage à moitié caché par un décor.

`kpt_sheet.py` dessine ces mêmes points (même fonction `points`) sur chaque personnage, de face, de côté et de dos : à vérifier à l'œil.

  python pose_labels.py --src <ds256> --truth <truth.jsonl> --dst <ds256p> [--walls 0.25]
"""
import argparse
import json
import os
import shutil
import sys
import zlib
from multiprocessing import Pool
from typing import List, Optional

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import head_truth as HT  # noqa: E402

FACE_BONES = ["M_Nose", "L_Eyeball", "R_Eyeball"]
BODY_BONES = ["L_Shoulder", "R_Shoulder", "L_Elbow", "R_Elbow", "L_Hand", "R_Hand", "L_Hip", "R_Hip", "L_Knee", "R_Knee", "L_Foot", "R_Foot"]
NAMES = ["tête (centre de la zone)", "zone tête : bord gauche", "zone tête : bord droit", "zone tête : haut", "zone tête : bas", "nez", "œil gauche", "œil droit",
         "épaule gauche", "épaule droite", "coude gauche", "coude droit", "main gauche", "main droite", "hanche gauche", "hanche droite",
         "genou gauche", "genou droit", "pied gauche", "pied droit"]
NK = len(NAMES)
FLIP_IDX = [0, 2, 1, 3, 4, 5, 7, 6, 9, 8, 11, 10, 13, 12, 15, 14, 17, 16, 19, 18]
# tolérance de chaque point dans la perte et la mesure (OKS) : celles de COCO pour le nez, les yeux et les articulations ; les points de la zone tête comme le nez
SIGMAS = [0.026, 0.026, 0.026, 0.026, 0.026, 0.026, 0.025, 0.025, 0.079, 0.079, 0.072, 0.072, 0.062, 0.062, 0.107, 0.107, 0.087, 0.087, 0.089, 0.089]
FACE_MAX_DEG = 100.0   # nez et yeux étiquetés si le regard fait moins que cet angle avec la direction de la caméra
MIN_AREA = 12          # px² (à 480) de silhouette visible en dessous desquels un personnage caché par un mur n'est plus étiqueté


def match_agents(sample: dict, boxes: List[list]) -> List[int]:
    """Indice du personnage de chaque étiquette. compose.py écrit les étiquettes dans l'ordre des personnages en sautant ceux qu'on ne voit pas :
    on retrouve lesquels en comparant chaque boîte à la projection des os."""
    n = len(sample["agents"])
    if len(boxes) == n:
        return list(range(n))
    cent = []
    for ag in sample["agents"]:
        pts = HT.project(sample, np.array(list(ag["bones"].values())))
        cent.append((pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()))
    out, start = [], 0
    for j, b in enumerate(boxes):
        best, bi = -1.0, start
        for k in range(start, n - (len(boxes) - 1 - j)):
            c = cent[k]
            iw = min(b[2], c[2]) - max(b[0], c[0])
            ih = min(b[3], c[3]) - max(b[1], c[1])
            inter = max(0.0, iw) * max(0.0, ih)
            iou = inter / max(1e-6, (b[2] - b[0]) * (b[3] - b[1]) + (c[2] - c[0]) * (c[3] - c[1]) - inter)
            if iou > best:
                best, bi = iou, k
        out.append(bi)
        start = bi + 1
    return out


def points(sample: dict, k: int, wall=None) -> np.ndarray:
    """Les 20 points du personnage `k`, (x, y, v) en pixels du carré ; v = 2 point présent, 0 absent (hors de l'image, derrière le mur, visage de dos)."""
    size = sample["size"]
    blockers = [wall["rect"]] if wall else None
    hi = HT.head_info(sample, k, shown=True, blockers=blockers)
    pts = np.zeros((NK, 3))
    if hi is not None:
        pts[0:5, :2] = [(hi["cx"], hi["cy"]), (hi["x0"], hi["cy"]), (hi["x1"], hi["cy"]), (hi["cx"], hi["y0"]), (hi["cx"], hi["y1"])]
        pts[0:5, 2] = 2
    b = sample["agents"][k]["bones"]
    names = FACE_BONES + BODY_BONES
    have = [i for i, n in enumerate(names) if n in b]
    if have:
        pr = HT.project(sample, np.array([b[names[i]] for i in have]))
        for i, p in zip(have, pr):
            pts[5 + i] = (p[0], p[1], 2)
    if all(n in b for n in ("Head", "M_Nose")):                    # visage tourné vers la caméra ? regard = de l'os de la tête vers le nez, à l'horizontale
        head, nose = np.array(b["Head"]), np.array(b["M_Nose"])
        fwd = (nose - head) * np.array([1.0, 0.0, 1.0])
        cam = (np.array([0.0, sample["cam"]["eye"], 0.0]) - head) * np.array([1.0, 0.0, 1.0])
        cosang = float(fwd @ cam) / max(1e-9, float(np.linalg.norm(fwd) * np.linalg.norm(cam)))
        if cosang < np.cos(np.radians(FACE_MAX_DEG)):
            pts[5:8] = 0
    out_img = (pts[:, 0] < 0) | (pts[:, 0] >= size) | (pts[:, 1] < 0) | (pts[:, 1] >= size)
    if blockers:
        out_img[5:] |= HT.blocked(pts[5:, :2], blockers)
    pts[out_img] = 0
    return pts


def label_line(sample: dict, k: int, box, wall, size: int) -> Optional[str]:
    """« 0 cx cy w h » + 20 × « x y v », normalisés. `box` = boîte visible (x1, y1, x2, y2) en pixels du carré."""
    pts = points(sample, k, wall)
    x1, y1, x2, y2 = box
    vals = [(x1 + x2) / 2 / size, (y1 + y2) / 2 / size, (x2 - x1) / size, (y2 - y1) / size]
    return "0 " + " ".join(f"{v:.5f}" for v in vals) + " " + " ".join(f"{p[0] / size:.5f} {p[1] / size:.5f} {int(p[2])}" for p in pts)


def build(job):
    src, dst, split, meta, tline, walls = job
    sid = meta["id"]
    sample = json.loads(tline) if tline else None
    size = sample["size"] if sample else 480
    img_src, img_dst = f"{src}/images/{split}/{sid}.jpg", f"{dst}/images/{split}/{sid}.jpg"
    boxes = meta.get("boxes", [])
    lines, wall = [], None
    if boxes and sample:
        who = match_agents(sample, boxes)
        rng = np.random.default_rng(zlib.crc32(("mur" + sid).encode()))
        if split == "train" and rng.random() < walls:
            hi = HT.head_info(sample, who[0], shown=True)
            if hi is not None:
                wall = HT.pick_wall(rng, sample, who[0], hi)
        polys = []
        if wall:
            for l in open(f"{src}/labels/{split}/{sid}.txt").read().splitlines():
                polys.append(np.array(l.split()[1:], np.float32).reshape(-1, 2) * size)
        for j, (k, b) in enumerate(zip(who, boxes)):
            box = b[:4]
            if wall:
                m = np.zeros((size, size), np.uint8)
                cv2.fillPoly(m, [np.round(polys[j]).astype(np.int32)], 1)
                x0, y0, x1, y1 = [int(round(v)) for v in wall["rect"]]
                m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
                if int(m.sum()) < MIN_AREA:
                    continue
                ys, xs = np.nonzero(m)
                box = [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]
                if box[2] - box[0] < 3 or box[3] - box[1] < 3:
                    continue
            lines.append(label_line(sample, k, box, wall, size))
    if wall:
        cv2.imwrite(img_dst, HT.draw_wall(cv2.imread(img_src), wall, size), [cv2.IMWRITE_JPEG_QUALITY, 95])
    else:
        try:
            if os.path.exists(img_dst):
                os.remove(img_dst)
            os.link(img_src, img_dst)                   # même disque : aucune copie
        except OSError:
            shutil.copyfile(img_src, img_dst)
    with open(f"{dst}/labels/{split}/{sid}.txt", "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return sid, len(lines), wall["kind"] if wall else None


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True, help="jeu de données de segmentation (vue 256)")
    ap.add_argument("--truth", required=True, help="vérité du moteur (gen_dataset.gd --truth) pour le même plan")
    ap.add_argument("--dst", required=True)
    ap.add_argument("--walls", type=float, default=0.25)
    ap.add_argument("--exclude", nargs="*", default=["veto"], help="personnages écartés : aucune image où ils apparaissent, même en figurant "
                    "(veto : modèle abîmé, il s'affiche blanc)")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    truth = {}
    with open(a.truth, encoding="utf-8") as f:
        for line in f:
            i = line.find('"id":"')
            truth[line[i + 6:line.find('"', i + 6)]] = line
    jobs = []
    skipped = 0
    for line in open(f"{a.src}/meta.jsonl", encoding="utf-8"):
        m = json.loads(line)
        if any(s in a.exclude for s in m.get("agents", [])):
            skipped += 1
            continue
        jobs.append((a.src, a.dst, m["split"], m, truth.get(m["id"]), a.walls))
    for split in ("train", "val", "test"):
        os.makedirs(f"{a.dst}/images/{split}", exist_ok=True)
        os.makedirs(f"{a.dst}/labels/{split}", exist_ok=True)
    n_lab, kinds = 0, {}
    with Pool(a.workers) as p:
        for _, n, kind in p.imap_unordered(build, jobs, chunksize=64):
            n_lab += n
            if kind:
                kinds[kind] = kinds.get(kind, 0) + 1
    root = os.path.abspath(a.dst).replace(chr(92), "/")
    with open(f"{a.dst}/data.yaml", "w", encoding="utf-8") as f:
        f.write(f"path: {root}\ntrain: images/train\nval: images/val\ntest: images/test\nkpt_shape: [{NK}, 3]\nflip_idx: {FLIP_IDX}\nnames:\n  0: person\n")
    print(f"{len(jobs)} images, {n_lab} étiquettes, murs : {kinds} ; {skipped} images écartées ({', '.join(a.exclude)}) → {a.dst}")


if __name__ == "__main__":
    main()
