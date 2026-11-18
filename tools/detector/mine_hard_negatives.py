#!/usr/bin/env python3
"""mine_hard_negatives.py — trouve, dans les photos sans personne, les carrés où un détecteur voit « une personne ».

Pour chaque photo, `--k` carrés tirés comme le fait gen_plan.py (côté 45–100 % du petit côté) sont passés dans le pipeline du
sniper (480 → 256 bilinéaire, classe 0 seulement). Ceux où un modèle répond avec une confiance ≥ `--conf` sont des
négatifs difficiles : porte vitrée, poteau, plante, bouche d'incendie… Ils sont écrits dans backgrounds.json, champ
"hard" de la photo : [[x0, y0, côté, confiance max, modèle], …].

Deux jeux de modèles, pour que le test reste équitable :
  * photos d'entraînement : `--train-models` (le modèle courant ET le modèle COCO d'origine) ;
  * photos de validation / test : `--eval-models` seulement (le modèle COCO d'origine : ni le modèle courant ni le futur
    modèle n'ont choisi ces carrés, donc comparer leurs fausses détections dessus est juste).

  python mine_hard_negatives.py --backgrounds b.json --out b_hard.json --train-models algos/yolo11n-seg-ft1.pt algos/yolo11n-seg.pt \\
         --eval-models algos/yolo11n-seg.pt
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np


def patches(rng: np.random.Generator, w: int, h: int, k: int):
    out = []
    for _ in range(k):
        side = int(min(h, w) * rng.uniform(0.45, 1.0))
        out.append((int(rng.integers(0, w - side + 1)), int(rng.integers(0, h - side + 1)), side))
    return out


def view(img: np.ndarray, box, size: int = 480, view_size: int = 256) -> np.ndarray:
    x0, y0, side = box
    p = img[y0:y0 + side, x0:x0 + side]
    p = cv2.resize(p, (size, size), interpolation=cv2.INTER_AREA if side > size else cv2.INTER_CUBIC)
    return cv2.resize(p, (view_size, view_size), interpolation=cv2.INTER_LINEAR)[..., ::-1]  # RGB


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backgrounds", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--train-models", nargs="+", required=True)
    ap.add_argument("--eval-models", nargs="+", required=True)
    ap.add_argument("--k-train", type=int, default=8)
    ap.add_argument("--k-eval", type=int, default=24)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    import torch
    from ultralytics import YOLO

    bgs = json.load(open(a.backgrounds, encoding="utf-8"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gen_plan import split_of_bg
    models = {p: YOLO(p) for p in set(a.train_models) | set(a.eval_models)}
    n_hard = {"train": 0, "val": 0, "test": 0}
    n_tot = {"train": 0, "val": 0, "test": 0}
    jobs = []  # (item, split, boxes)
    for i, it in enumerate(bgs["items"]):
        sp = split_of_bg(it["file"], it["split"])
        rng = np.random.default_rng([a.seed, i])
        jobs.append((it, sp, patches(rng, it["w"], it["h"], a.k_train if sp == "train" else a.k_eval)))
        it["hard"] = []
    B = 8  # photos par lot
    for s in range(0, len(jobs), B):
        chunk = jobs[s:s + B]
        views, owners = [], []
        for ji, (it, sp, boxes) in enumerate(chunk):
            img = cv2.imread(os.path.join(it.get("dir", bgs["images_dir"]), it["file"]))
            if img is None:
                continue
            for b in boxes:
                views.append(view(img, b))
                owners.append((ji, b))
        if not views:
            continue
        x = torch.from_numpy(np.ascontiguousarray(np.stack(views))).cuda().permute(0, 3, 1, 2).float() / 255.0
        best = [[0.0, ""] for _ in views]
        used = {p for (_, sp, _) in chunk for p in (a.train_models if sp == "train" else a.eval_models)}
        for p in used:
            res = models[p].predict(x, imgsz=256, conf=a.conf, iou=0.45, classes=[0], verbose=False)
            for vi, r in enumerate(res):
                sp = chunk[owners[vi][0]][1]
                if p not in (a.train_models if sp == "train" else a.eval_models):
                    continue
                if len(r.boxes):
                    c = float(r.boxes.conf.max())
                    if c > best[vi][0]:
                        best[vi] = [c, os.path.basename(p)]
        for vi, (ji, b) in enumerate(owners):
            it, sp, _ = chunk[ji]
            n_tot[sp] += 1
            if best[vi][0] >= a.conf:
                it["hard"].append([b[0], b[1], b[2], round(best[vi][0], 3), best[vi][1]])
                n_hard[sp] += 1
        if (s // B) % 100 == 0:
            print(f"  {s}/{len(jobs)} photos  difficiles {n_hard}", flush=True)
    json.dump(bgs, open(a.out, "w"))
    print(f"carrés testés {n_tot} ; difficiles {n_hard} → {a.out}")


if __name__ == "__main__":
    main()
