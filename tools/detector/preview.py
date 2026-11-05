#!/usr/bin/env python3
"""preview.py — planche de contrôle : images du jeu de données avec polygones / boîtes superposés.

  python preview.py --dataset <dir> --split train --n 24 --out sheet.png [--kind photo] [--seed 0]
"""
import argparse
import json
import os
import sys
import random

import cv2
import numpy as np


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # sortie redirigée sous Windows = cp1252
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--split", default="train")
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--kind", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tile", type=int, default=320)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    metas = [json.loads(l) for l in open(os.path.join(a.dataset, "meta.jsonl"))]
    metas = [m for m in metas if m["split"] == a.split and (not a.kind or m["kind"] == a.kind)]
    random.Random(a.seed).shuffle(metas)
    tiles = []
    for m in metas[:a.n]:
        img = cv2.imread(f"{a.dataset}/images/{a.split}/{m['id']}.jpg")
        h, w = img.shape[:2]
        over = img.copy()
        for line in open(f"{a.dataset}/labels/{a.split}/{m['id']}.txt").read().splitlines():
            v = np.array(line.split()[1:], np.float32).reshape(-1, 2) * [w, h]
            cv2.fillPoly(over, [v.astype(np.int32)], (255, 0, 255))
            x1, y1 = v.min(0)
            x2, y2 = v.max(0)
            cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 255), 1)
        img = cv2.addWeighted(over, 0.35, img, 0.65, 0)
        tile = cv2.resize(img, (a.tile, a.tile), interpolation=cv2.INTER_AREA)
        cv2.putText(tile, f"{m['kind']} {m.get('dist', 0):.0f}m", (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(tile)
    while len(tiles) % a.cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + a.cols]) for i in range(0, len(tiles), a.cols)]
    cv2.imwrite(a.out, np.vstack(rows))
    print(f"{len(metas[:a.n])} tuiles → {a.out}")


if __name__ == "__main__":
    main()
