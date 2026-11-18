#!/usr/bin/env python3
"""evaluate.py — compare des modèles de détection sur les images de test, avec les critères du sniper.

Les images 480×480 sont réduites comme le fait le pipeline (bilinéaire sans anti-crénelage sur le GPU, comme
`Detector` de yolo_aim.py ; `--interp nearest` = chaîne Mac), puis passées au modèle (classe 0 seulement). Mesures,
par défaut à confiance ≥ 0.5 (le seuil de yolo_sniper) :
  * rappel / précision des personnages (appariement par IoU de masque ≥ 0.5, cibles ≥ --min-area px à 256) ;
  * IoU moyen des masques appariés ;
  * « décision réticule » = ce que fait le tir : un pixel au centre est dans le masque détecté. TPR = le centre est
    dans un personnage et le modèle le dit ; FPR = le centre n'est dans aucun personnage et le modèle dit que si
    (c'est le tir à côté) ;
  * fausses détections sur les images sans personnage.
Détail par fond (game / photo / uni), distance, orientation (face / côté / dos) et personnage.

  python evaluate.py --dataset <dataset 480> --split test --models algos/yolo11n-seg.pt runs/v1/weights/best.pt [--out res.json]
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List

import cv2
import numpy as np


def gt_masks(path: str, size: int) -> List[np.ndarray]:
    out = []
    for line in open(path).read().splitlines():
        v = np.array(line.split()[1:], np.float32).reshape(-1, 2) * size
        m = np.zeros((size, size), np.uint8)
        cv2.fillPoly(m, [np.round(v).astype(np.int32)], 1)
        out.append(m.astype(bool))
    return out


def bucket_dist(d: float) -> str:
    for hi, name in [(5, "<5 m"), (10, "5-10 m"), (20, "10-20 m"), (30, "20-30 m")]:
        if d < hi:
            return name
    return ">=30 m"


def bucket_yaw(r: float) -> str:
    r = r % 360
    if r < 45 or r >= 315:
        return "face"
    return "dos" if 135 <= r < 225 else "côté"


class Acc:
    def __init__(self) -> None:
        self.gt = self.gt_hit = self.pred = self.pred_ok = 0
        self.iou_sum = 0.0
        self.c_pos = self.c_pos_hit = self.c_neg = self.c_neg_hit = 0
        self.neg_img = self.neg_img_fp = 0

    def row(self) -> Dict[str, float]:
        f = lambda a, b: (a / b) if b else float("nan")
        return {"rappel": f(self.gt_hit, self.gt), "précision": f(self.pred_ok, self.pred), "IoU": f(self.iou_sum, self.gt_hit),
                "TPR_réticule": f(self.c_pos_hit, self.c_pos), "FPR_réticule": f(self.c_neg_hit, self.c_neg),
                "neg_FP": f(self.neg_img_fp, self.neg_img), "n_gt": self.gt, "n_centre+": self.c_pos}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--split", default="test")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--imgsz", type=int, default=256)
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--interp", default="bilinear", choices=["bilinear", "nearest", "area"])
    ap.add_argument("--min-area", type=int, default=30, help="surface minimale d'une cible (px à imgsz) pour compter dans le rappel")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    import torch
    import torch.nn.functional as F
    from ultralytics import YOLO

    metas = [json.loads(l) for l in open(f"{a.dataset}/meta.jsonl")]
    metas = [m for m in metas if m["split"] == a.split]
    if a.limit:
        metas = metas[:a.limit]
    S = a.imgsz
    gts = [gt_masks(f"{a.dataset}/labels/{a.split}/{m['id']}.txt", S) for m in metas]
    results = {}
    for mp in a.models:
        model = YOLO(mp)
        groups: Dict[str, Dict[str, Acc]] = defaultdict(lambda: defaultdict(Acc))
        for i in range(0, len(metas), a.batch):
            chunk = metas[i:i + a.batch]
            imgs = [cv2.imread(f"{a.dataset}/images/{a.split}/{m['id']}.jpg")[..., ::-1] for m in chunk]
            x = torch.from_numpy(np.ascontiguousarray(np.stack(imgs))).cuda().permute(0, 3, 1, 2).float() / 255.0
            kw = {"mode": "bilinear", "align_corners": False} if a.interp == "bilinear" else {"mode": a.interp}
            x = F.interpolate(x, size=(S, S), **kw)
            res = model.predict(x, imgsz=S, conf=a.conf, iou=0.45, classes=[0], verbose=False)
            for j, (m, r) in enumerate(zip(chunk, res)):
                gt = gts[i + j]
                big = [g for g in gt if g.sum() >= a.min_area]
                pm = [(r.masks.data[k].cpu().numpy() > 0.5) for k in range(len(r.boxes))] if r.masks is not None else []
                if pm and pm[0].shape != (S, S):
                    pm = [cv2.resize(p.astype(np.uint8), (S, S), interpolation=cv2.INTER_NEAREST).astype(bool) for p in pm]
                taken = set()
                matched_pred = 0
                accs = [groups["tous"]["tous"], groups["fond"][m["kind"].replace("neg_", "") if not m["kind"].startswith("neg") else "sans personnage" + (" difficile" if m["kind"] == "neg_hard" else "")]]
                if big:
                    accs.append(groups["distance"][bucket_dist(m["dist"])])
                    accs.append(groups["orientation"][bucket_yaw(m["rel_yaw"])])
                    accs.append(groups["personnage"][m["agents"][0]])
                for g in big:
                    best, bj = 0.0, -1
                    for k, p in enumerate(pm):
                        if k in taken:
                            continue
                        iou = (g & p).sum() / max((g | p).sum(), 1)
                        if iou > best:
                            best, bj = iou, k
                    for ac in accs:
                        ac.gt += 1
                    if best >= 0.5:
                        taken.add(bj)
                        for ac in accs:
                            ac.gt_hit += 1
                            ac.iou_sum += best
                for ac in accs:
                    ac.pred += len(pm)
                # une détection compte comme juste si elle recouvre (IoU ≥ 0.5) un personnage étiqueté quelconque
                ok = 0
                for p in pm:
                    if any((g & p).sum() / max((g | p).sum(), 1) >= 0.5 for g in gt):
                        ok += 1
                for ac in accs:
                    ac.pred_ok += ok
                cgt = any(g[S // 2, S // 2] for g in gt)
                cpr = any(p[S // 2, S // 2] for p in pm)
                for ac in accs:
                    if cgt:
                        ac.c_pos += 1
                        ac.c_pos_hit += int(cpr)
                    else:
                        ac.c_neg += 1
                        ac.c_neg_hit += int(cpr)
                if not gt:
                    for ac in accs:
                        ac.neg_img += 1
                        ac.neg_img_fp += int(len(pm) > 0)
        results[mp] = {g: {k: v.row() for k, v in d.items()} for g, d in groups.items()}
        print(f"{mp}: {len(metas)} images", flush=True)
    if a.out:
        json.dump(results, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print_tables(results)


def print_tables(results: Dict) -> None:
    names = list(results)
    cols = ["rappel", "précision", "IoU", "TPR_réticule", "FPR_réticule", "neg_FP"]
    print()
    for group in ["tous", "fond", "distance", "orientation", "personnage"]:
        keys = sorted({k for n in names for k in results[n].get(group, {})})
        if group == "personnage":
            keys = sorted(keys, key=lambda k: results[names[-1]][group][k]["rappel"])[:10]  # les 10 plus faibles du dernier modèle
        print(f"== {group}" + (" (10 plus faibles du dernier modèle)" if group == "personnage" else ""))
        print(f"{'':16s}{'modèle':>14s}" + "".join(f"{c:>14s}" for c in cols) + f"{'n_gt':>8s}")
        for k in keys:
            for n in names:
                r = results[n][group].get(k)
                if r is None:
                    continue
                print(f"{k:16s}{os.path.basename(os.path.dirname(os.path.dirname(n))) if n.endswith('best.pt') else os.path.basename(n):>14s}"
                      + "".join(f"{r[c]:14.3f}" for c in cols) + f"{r['n_gt']:8d}")
        print()


if __name__ == "__main__":
    main()
