#!/usr/bin/env python3
"""train.py — fine-tuning de yolo11n-seg sur le jeu de données synthétique (personnages du jeu).

Départ : les poids COCO déjà utilisés par le sniper (algos/yolo11n-seg.pt). Toutes les couches sont entraînées, learning
rate bas (AdamW), 80 classes COCO conservées (seule « person » est étiquetée) : la forme de sortie reste celle que
lisent yolo_sniper / sniper-rs / sniper_b. Augmentations : retournement horizontal, mosaïque, échelle, teinte ; pas de
rotation ni de retournement vertical (les personnages sont debout, l'horizon est horizontal).

  python train.py --data <dataset 256>/data.yaml --name v1 [--epochs 60] [--batch 128]
"""
import argparse
import os
import sys


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--weights", default=os.path.join(here, "..", "..", "algos", "yolo11n-seg.pt"))
    ap.add_argument("--project", default=os.path.join(os.path.expanduser("~"), "aimlabs-data", "detector", "runs"))
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--imgsz", type=int, default=256)
    ap.add_argument("--lr0", type=float, default=0.0008)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--patience", type=int, default=20)
    ap.add_argument("--cache", action="store_true", help="images en RAM : sous Windows chaque worker en garde une copie (8 x 3 Go), à éviter")
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    from ultralytics import YOLO

    model = YOLO(a.weights)
    model.train(data=a.data, imgsz=a.imgsz, epochs=a.epochs, batch=a.batch, device=0, workers=a.workers,
                project=a.project, name=a.name, exist_ok=True, optimizer="AdamW", lr0=a.lr0, lrf=0.05, cos_lr=True,
                warmup_epochs=1, patience=a.patience, close_mosaic=8, mosaic=1.0, fliplr=0.5, flipud=0.0, degrees=0.0,
                scale=0.5, translate=0.1, hsv_h=0.02, hsv_s=0.5, hsv_v=0.35, mixup=0.0, cache="ram" if a.cache else False, amp=True,
                overlap_mask=True, plots=True, seed=0, deterministic=False, resume=a.resume)


if __name__ == "__main__":
    main()
