#!/usr/bin/env python3
"""eval_chain.py — mesure un modèle dans la chaîne du Mac (carré 480 → 256 plus proche voisin → Core ML → décodage de sniper_b).

Trois commandes (le décodage reprend celui de `sniper_b.py` : boîtes « person » ≥ conf après NMS, masque lu au réticule) :

  prepare  (PC)   choisit des images du jeu de test, les réduit comme `fit_crop` de sniper_b (PIL NEAREST), écrit imgs/*.png
                  et gt.json (« le centre est dans un personnage », nombre de personnages, distance, fond)
  run      (Mac ou PC)  --backend coreml --model x.mlpackage | --backend torch --model x.pt  → <out>.json : par image, les détections
                  et « le pixel du réticule est dans le masque d'une détection »
  score    (PC)   compare un ou plusieurs résultats au gt.json : TPR / FPR du réticule, faux positifs sur images sans personnage,
                  et, si deux résultats sont donnés, leur accord (même décision, mêmes boîtes) : sert à valider une conversion

  python eval_chain.py prepare --dataset <ds480> --out <dir> [--n 800]
  python eval_chain.py run --dir <dir> --backend coreml --model models/x_256.mlpackage --out <dir>/x_coreml.json
  python eval_chain.py score --dir <dir> <res1.json> [<res2.json>]
"""
import argparse
import json
import os
import random
import sys

import numpy as np

SIDE = 256


def nms(boxes, iou_thr=0.45):  # même NMS que sniper_b.py
    keep = []
    for b in sorted(boxes, key=lambda d: -d[4]):
        ok = True
        for k in keep:
            ix = max(0.0, min(b[2], k[2]) - max(b[0], k[0]))
            iy = max(0.0, min(b[3], k[3]) - max(b[1], k[1]))
            inter = ix * iy
            uni = (b[2] - b[0]) * (b[3] - b[1]) + (k[2] - k[0]) * (k[3] - k[1]) - inter
            if uni > 0 and inter / uni > iou_thr:
                ok = False
                break
        if ok:
            keep.append(b)
    return keep


def decode(out0: np.ndarray, conf: float):
    sc = out0[4]
    idx = np.nonzero(sc >= conf)[0]
    boxes = [(float(out0[0, i] - out0[2, i] / 2), float(out0[1, i] - out0[3, i] / 2), float(out0[0, i] + out0[2, i] / 2),
              float(out0[1, i] + out0[3, i] / 2), float(sc[i]), int(i)) for i in idx]
    return nms(boxes)


def reticle_in_mask(out0: np.ndarray, protos: np.ndarray, d, x_pad: float = 2.0) -> bool:
    """Comme `mask_extent` de sniper_b, au pixel central : logit du masque de la détection > 0 sur la ligne du réticule."""
    mh, mw = protos.shape[1], protos.shape[2]
    c = SIDE // 2
    py, px = min(int(c * mh / SIDE), mh - 1), min(int(c * mw / SIDE), mw - 1)
    if not (d[0] - x_pad <= c <= d[2] + x_pad and d[1] <= c <= d[3]):
        return False
    return float(out0[84:116, d[5]] @ protos[:, py, px]) > 0


def cmd_prepare(a) -> None:
    from PIL import Image
    metas = [json.loads(l) for l in open(f"{a.dataset}/meta.jsonl")]
    metas = [m for m in metas if m["split"] == a.split]
    random.Random(a.seed).shuffle(metas)
    metas = metas[:a.n]
    os.makedirs(f"{a.out}/imgs", exist_ok=True)
    gt = []
    for m in metas:
        img = Image.open(f"{a.dataset}/images/{a.split}/{m['id']}.jpg").convert("RGB")
        img.resize((SIDE, SIDE), Image.NEAREST).save(f"{a.out}/imgs/{m['id']}.png")
        polys = [np.array(l.split()[1:], np.float32).reshape(-1, 2) * SIDE for l in open(f"{a.dataset}/labels/{a.split}/{m['id']}.txt").read().splitlines()]
        import cv2
        centre = False
        for p in polys:
            mk = np.zeros((SIDE, SIDE), np.uint8)
            cv2.fillPoly(mk, [np.round(p).astype(np.int32)], 1)
            centre |= bool(mk[SIDE // 2, SIDE // 2])
        gt.append({"id": m["id"], "kind": m["kind"], "dist": m.get("dist"), "n": len(polys), "centre": centre})
    json.dump(gt, open(f"{a.out}/gt.json", "w"))
    print(f"{len(gt)} images → {a.out}  (centre dans un personnage : {sum(g['centre'] for g in gt)}, sans personnage : {sum(g['n'] == 0 for g in gt)})")


def cmd_run(a) -> None:
    gt = json.load(open(f"{a.dir}/gt.json"))
    from PIL import Image
    if a.backend == "coreml":
        import coremltools as ct
        m = ct.models.MLModel(a.model, compute_units=getattr(ct.ComputeUnit, a.units))
        spec = m.get_spec()
        inp = spec.description.input[0].name
        outs = [(o.name, list(o.type.multiArrayType.shape)) for o in spec.description.output]
        o0 = next(n for n, s in outs if len(s) == 3)
        o1 = next(n for n, s in outs if len(s) == 4)

        def infer(img):
            o = m.predict({inp: Image.fromarray(img)})
            return np.asarray(o[o0])[0], np.asarray(o[o1])[0]
    else:
        import torch
        from ultralytics import YOLO
        net = YOLO(a.model).model.to(a.device).eval().float()
        net.model[-1].export = True  # sortie (1, 116, N) + prototypes, comme l'export ONNX / Core ML

        def infer(img):
            x = torch.from_numpy(np.ascontiguousarray(img)).to(a.device).permute(2, 0, 1)[None].float() / 255.0
            with torch.no_grad():
                y, p = net(x)[:2]
            return y[0].float().cpu().numpy(), p[0].float().cpu().numpy()

    res = {}
    for g in gt:
        img = np.asarray(Image.open(f"{a.dir}/imgs/{g['id']}.png").convert("RGB"))
        out0, protos = infer(img)
        dets = decode(out0, a.conf)
        res[g["id"]] = {"dets": [[round(v, 2) for v in d[:5]] for d in dets], "reticle": any(reticle_in_mask(out0, protos, d) for d in dets)}
    json.dump({"model": a.model, "backend": a.backend, "conf": a.conf, "results": res}, open(a.out, "w"))
    print(f"{len(res)} images → {a.out}")


def cmd_score(a) -> None:
    gt = json.load(open(f"{a.dir}/gt.json"))
    rs = [json.load(open(p)) for p in a.results]
    groups = {"tous": lambda g: True, "dist ≥ 30 m": lambda g: g["dist"] is not None and g["dist"] >= 30,
              "dist < 30 m": lambda g: g["dist"] is not None and g["dist"] < 30, "sans personnage": lambda g: g["n"] == 0,
              "sans personnage difficile": lambda g: g["kind"] == "neg_hard"}
    print(f"{'groupe':28s}{'n':>6s}" + "".join(f"  {os.path.basename(r['model'])[:30]:>30s}" for r in rs))
    for name, f in groups.items():
        sel = [g for g in gt if f(g)]
        row = []
        for r in rs:
            if name.startswith("sans personnage"):
                row.append(f"FP image {np.mean([bool(r['results'][g['id']]['dets']) for g in sel]):.3f}" if sel else "-")
            else:
                pos = [g for g in sel if g["centre"]]
                neg = [g for g in sel if not g["centre"]]
                tpr = np.mean([r["results"][g["id"]]["reticle"] for g in pos]) if pos else float("nan")
                fpr = np.mean([r["results"][g["id"]]["reticle"] for g in neg]) if neg else float("nan")
                row.append(f"TPR {tpr:.3f} ({len(pos)}) FPR {fpr:.3f}")
        print(f"{name:28s}{len(sel):6d}" + "".join(f"  {c:>30s}" for c in row))
    if len(rs) == 2:
        ids = [g["id"] for g in gt]
        same_ret = np.mean([rs[0]["results"][i]["reticle"] == rs[1]["results"][i]["reticle"] for i in ids])
        same_n = np.mean([len(rs[0]["results"][i]["dets"]) == len(rs[1]["results"][i]["dets"]) for i in ids])
        diffs = []
        for i in ids:
            a_, b_ = rs[0]["results"][i]["dets"], rs[1]["results"][i]["dets"]
            if len(a_) == len(b_) and a_:
                for x, y in zip(sorted(a_), sorted(b_)):
                    diffs.append(max(abs(x[k] - y[k]) for k in range(4)))
        print(f"\naccord des deux : même décision au réticule {same_ret:.3f} ; même nombre de détections {same_n:.3f} ; "
              f"écart de boîte (px à 256) médian {np.median(diffs) if diffs else float('nan'):.2f}, max {max(diffs) if diffs else float('nan'):.2f}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--dataset", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--split", default="test")
    p.add_argument("--n", type=int, default=800)
    p.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("run")
    p.add_argument("--dir", required=True)
    p.add_argument("--backend", choices=["coreml", "torch"], required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--units", default="ALL")
    p.add_argument("--device", default="cuda", help="backend torch : cuda | cpu")
    p = sub.add_parser("score")
    p.add_argument("--dir", required=True)
    p.add_argument("results", nargs="+")
    a = ap.parse_args()
    {"prepare": cmd_prepare, "run": cmd_run, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
