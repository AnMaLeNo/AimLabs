#!/usr/bin/env python3
"""make_view.py — vue 256×256 d'un jeu de données 480×480 (ce que voit réellement le réseau du sniper).

Le réseau ne reçoit jamais le carré en pleine résolution : yolo_sniper (PC) le réduit en bilinéaire sur le GPU,
sniper_b (Mac) en « plus proche voisin » (`fit_crop`). Pour que le modèle y soit robuste, chaque image est réduite avec
une interpolation tirée au hasard (graine fixe) : plus proche voisin 25 %, bilinéaire 35 %, aire 40 % ; l'interpolation
est notée dans meta256.jsonl. Les étiquettes (normalisées) sont identiques. Les splits val / test sont réduits avec
une seule méthode (--eval-interp, bilinéaire par défaut = chaîne PC) pour que les mesures soient comparables.

  python make_view.py --src <dataset 480> --dst <dataset 256> [--size 256]
"""
import argparse
import json
import os
import shutil
import sys
import zlib
from multiprocessing import Pool

import cv2
import numpy as np

METHODS = {"nearest": cv2.INTER_NEAREST, "bilinear": cv2.INTER_LINEAR, "area": cv2.INTER_AREA}


def work(job):
    src, dst, split, name, size, method = job
    img = cv2.imread(f"{src}/images/{split}/{name}.jpg")
    out = cv2.resize(img, (size, size), interpolation=METHODS[method])
    cv2.imwrite(f"{dst}/images/{split}/{name}.jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    shutil.copyfile(f"{src}/labels/{split}/{name}.txt", f"{dst}/labels/{split}/{name}.txt")
    return name, method


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--eval-interp", default="bilinear", choices=list(METHODS))
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    jobs, used = [], {}
    for split in ("train", "val", "test"):
        d = f"{a.src}/images/{split}"
        if not os.path.isdir(d):
            continue
        os.makedirs(f"{a.dst}/images/{split}", exist_ok=True)
        os.makedirs(f"{a.dst}/labels/{split}", exist_ok=True)
        for f in sorted(os.listdir(d)):
            name = os.path.splitext(f)[0]
            if split == "train":
                r = np.random.default_rng(zlib.crc32(name.encode())).random()
                method = "nearest" if r < 0.25 else ("bilinear" if r < 0.60 else "area")
            else:
                method = a.eval_interp
            jobs.append((a.src, a.dst, split, name, a.size, method))
    with Pool(a.workers) as p:
        for name, method in p.imap_unordered(work, jobs, chunksize=64):
            used[name] = method
    with open(f"{a.dst}/meta{a.size}.jsonl", "w") as f:
        for l in open(f"{a.src}/meta.jsonl"):
            m = json.loads(l)
            m["interp"] = used.get(m["id"])
            f.write(json.dumps(m) + "\n")
    shutil.copyfile(f"{a.src}/meta.jsonl", f"{a.dst}/meta.jsonl") if not os.path.exists(f"{a.dst}/meta.jsonl") else None
    yaml = open(f"{a.src}/data.yaml").read().replace(os.path.abspath(a.src).replace(chr(92), "/"), os.path.abspath(a.dst).replace(chr(92), "/"))
    open(f"{a.dst}/data.yaml", "w").write(yaml)
    print(f"{len(jobs)} images → {a.dst}")


if __name__ == "__main__":
    main()
