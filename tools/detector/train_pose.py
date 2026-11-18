#!/usr/bin/env python3
"""train_pose.py — fine-tuning d'un modèle YOLO-pose sur les étiquettes de `pose_labels.py` (personnages du jeu, 20 points dont la zone « tête »).

Départ : les poids COCO `yolo11n-pose.pt` (ou s / m / l / x). Mêmes réglages que `train.py` (AdamW, taux bas, mosaïque, pas de rotation).
Une différence qui compte : la perte des points (OKS) pondère chaque point par une tolérance, et ultralytics ne connaît que celles des 17 points de
COCO ; pour toute autre disposition il met 1 / nombre de points à tous (0,05 ici, deux fois la tolérance du nez), ce qui laisse la tête imprécise.
Ce script donne à la perte ET à la mesure de validation les tolérances de `pose_labels.SIGMAS` (zone tête, nez, yeux : celles du nez et des yeux de COCO).

  python train_pose.py --data <ds256p>/data.yaml --name pose_n [--weights algos/yolo11n-pose.pt] [--epochs 40]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def patch_sigmas(sigmas) -> None:
    """Tolérances par point pour une disposition qui n'est pas celle de COCO (perte d'entraînement et mAP de validation)."""
    import numpy as np
    import torch
    from ultralytics.models.yolo.pose import val as pose_val
    from ultralytics.utils import loss as L

    init = L.v8PoseLoss.__init__

    def loss_init(self, model, *args, **kw):
        init(self, model, *args, **kw)
        if self.kpt_shape[0] == len(sigmas):
            self.keypoint_loss.sigmas = torch.tensor(sigmas, dtype=torch.float32, device=self.device)

    L.v8PoseLoss.__init__ = loss_init
    init_metrics = pose_val.PoseValidator.init_metrics

    def val_init_metrics(self, model):
        init_metrics(self, model)
        if self.kpt_shape[0] == len(sigmas):
            self.sigma = np.array(sigmas, dtype=np.float32)

    pose_val.PoseValidator.init_metrics = val_init_metrics


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--weights", default=os.path.join(here, "..", "..", "algos", "yolo11n-pose.pt"))
    ap.add_argument("--project", default=os.path.join(os.path.expanduser("~"), "aimlabs-data", "detector", "runs"))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--imgsz", type=int, default=256)
    ap.add_argument("--lr0", type=float, default=0.0008)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--patience", type=int, default=20)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()
    import pose_labels as PL
    from ultralytics import YOLO

    patch_sigmas(PL.SIGMAS)
    model = YOLO(a.weights)
    model.train(data=a.data, imgsz=a.imgsz, epochs=a.epochs, batch=a.batch, device=0, workers=a.workers,
                project=a.project, name=a.name, exist_ok=True, optimizer="AdamW", lr0=a.lr0, lrf=0.05, cos_lr=True,
                warmup_epochs=1, patience=a.patience, close_mosaic=8, mosaic=1.0, fliplr=0.5, flipud=0.0, degrees=0.0,
                scale=0.5, translate=0.1, hsv_h=0.02, hsv_s=0.5, hsv_v=0.35, mixup=0.0, cache=False, amp=True,
                plots=True, seed=0, deterministic=False, resume=a.resume)


if __name__ == "__main__":
    main()
