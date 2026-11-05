#!/usr/bin/env python3
"""prepare_backgrounds.py — choisit les photos de fond (COCO val2017) qui ne contiennent aucune personne.

Pourquoi : les personnages du jeu sont collés sur des photos ; une vraie personne présente sur la photo mais
non étiquetée apprendrait au modèle à ignorer les gens, ce que l'on ne veut pas. Deux filtres :
  1. les annotations COCO (aucune annotation « person », crowd compris) ;
  2. un détecteur COCO (yolo11m.pt, seuil bas) qui doit ne trouver aucune personne dans l'image restante.
Écrit <out>/backgrounds.json : [{"file", "w", "h", "split"}], split train/val décidé par un hash du nom
(les photos de val ne servent jamais à l'entraînement).

  python prepare_backgrounds.py --images <dir val2017> --annotations <instances_val2017.json> --out <dir>

Pour ajouter des photos (fetch_coco_negatives.py) à une liste existante : `--only-existing` (seules les images présentes
dans --images comptent), `--force-split train` (elles ne servent qu'à l'entraînement : val / test restent les photos de
val2017) et `--extend <backgrounds.json>` (chaque photo porte alors son dossier, "dir").
"""
import argparse
import hashlib
import json
import os
import sys

import cv2


def split_of(name: str, val_frac: float) -> str:
    h = int(hashlib.sha1(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "val" if h < val_frac else "train"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # sortie redirigée sous Windows = cp1252
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--images", required=True)
    ap.add_argument("--annotations", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--detector", default=os.path.join(os.path.dirname(__file__), "..", "..", "yolo11m.pt"))
    ap.add_argument("--conf", type=float, default=0.10, help="seuil du détecteur : bas = on écarte au moindre doute")
    ap.add_argument("--min-side", type=int, default=360)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--only-existing", action="store_true")
    ap.add_argument("--force-split", default="", choices=["", "train", "val"])
    ap.add_argument("--extend", default="", help="backgrounds.json existant auquel ajouter les photos retenues")
    a = ap.parse_args()

    coco = json.load(open(a.annotations, encoding="utf-8"))
    person_cat = next(c["id"] for c in coco["categories"] if c["name"] == "person")
    with_person = {x["image_id"] for x in coco["annotations"] if x["category_id"] == person_cat}
    cands = [im for im in coco["images"] if im["id"] not in with_person and min(im["width"], im["height"]) >= a.min_side
             and (not a.only_existing or os.path.exists(os.path.join(a.images, im["file_name"])))]
    print(f"{len(coco['images'])} images, {len(with_person)} avec personne (annotations), {len(cands)} candidates", flush=True)

    from ultralytics import YOLO
    det = YOLO(a.detector)
    kept, dropped = [], 0
    for i in range(0, len(cands), 64):
        batch = cands[i:i + 64]
        paths = [os.path.join(a.images, im["file_name"]) for im in batch]
        res = det.predict(paths, conf=a.conf, classes=[0], imgsz=640, verbose=False, device=0)
        for im, r in zip(batch, res):
            if len(r.boxes):
                dropped += 1
                continue
            kept.append({"file": im["file_name"], "w": im["width"], "h": im["height"], "dir": os.path.abspath(a.images),
                         "split": a.force_split or split_of(im["file_name"], a.val_frac)})
    os.makedirs(a.out, exist_ok=True)
    images_dir, items = os.path.abspath(a.images), kept
    if a.extend:
        old = json.load(open(a.extend, encoding="utf-8"))
        images_dir = old["images_dir"]
        items = [dict(it, dir=it.get("dir", images_dir)) for it in old["items"]] + kept
    json.dump({"images_dir": images_dir, "items": items}, open(os.path.join(a.out, "backgrounds.json"), "w"), indent=0)
    n_val = sum(1 for k in kept if k["split"] == "val")
    print(f"écartées par le détecteur : {dropped} ; retenues : {len(kept)} (train {len(kept) - n_val}, val {n_val})")


if __name__ == "__main__":
    main()
