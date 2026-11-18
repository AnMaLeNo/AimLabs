#!/usr/bin/env python3
"""head_parse_check.py — la zone « tête » du jeu colle-t-elle à la tête DESSINÉE ? Avis d'un modèle indépendant qui découpe les visages.

La zone où le jeu compte un tir à la tête est une capsule posée sur le squelette (enemy.gd) ; rien ne dit qu'elle recouvre ce que l'œil appelle la tête.
Ici un modèle public de découpage du visage (« face parsing », entraîné sur des photos de vraies personnes : peau, cheveux, chapeau, cou, vêtements…)
regarde chaque personnage du jeu de près (2,5 m ; face, côté, dos) et dit quels pixels sont la tête. On compare à la zone du jeu :

  * menton et sommet : hauteur (m) du bas du visage et du haut de la tête d'après le modèle, à côté du bas et du haut de la zone du jeu ;
  * part de la tête vue par le modèle qui tombe dans la zone du jeu, et part du personnage dans la zone que le modèle appelle « tête ».

Le modèle n'a jamais vu ces personnages : ce n'est pas une vérité, c'est un second avis à lire avec les images (`--out`).

  python head_parse_check.py plan --out <plan.json>
  godot --path game --resolution 640x360 res://tools/gen_dataset.tscn -- --plan=<plan.json> --out=<rendus> --truth=<truth.jsonl>
  python head_parse_check.py run --renders <rendus> --truth <truth.jsonl> --out <dossier> [--model jonathandinu/face-parsing] [--libs <dossier pip --target>]

Dépendances hors de l'environnement habituel : `pip install --no-deps --target <dossier> transformers huggingface_hub tokenizers safetensors regex tqdm …`
puis `--libs <dossier>` (rien n'est ajouté à l'environnement du détecteur).
"""
import argparse
import json
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import head_truth as HT  # noqa: E402
from gen_plan import CROP, EYE, SCREEN_H, focal_px  # noqa: E402
from head_plan import place_head  # noqa: E402

VIEWS = [("face", 0.0), ("côté", 90.0), ("dos", 180.0)]
# classes de jonathandinu/face-parsing (CelebAMask-HQ) tenues pour « la tête » ; le cou, les colliers et les vêtements n'en font pas partie
FACE = {"skin", "nose", "eye_g", "l_eye", "r_eye", "l_brow", "r_brow", "l_ear", "r_ear", "mouth", "u_lip", "l_lip", "ear_r"}
HAIR = {"hair", "hat"}


def cmd_plan(a) -> None:
    slugs = sorted(k for k in json.load(open(a.manifest, encoding="utf-8"))["agents"].keys() if k not in a.exclude)
    fov = 75.0
    f = focal_px(fov)
    fov_crop = math.degrees(2.0 * math.atan(math.tan(math.radians(fov) / 2.0) * CROP / SCREEN_H))
    samples = []
    for slug in slugs:
        for vi, (_, rel) in enumerate(VIEWS):
            x, z, pitch = place_head(a.dist, 0.0, 0.0, f)
            az = math.atan2(x, -z)
            samples.append({"id": f"{slug}_{vi}", "mode": "game", "kind": "game", "cam": {"eye": EYE, "pitch_deg": pitch, "fov": fov_crop},
                            "agents": [{"slug": slug, "neutral": True, "anim_t": 0.0, "x": x, "z": z, "rot_deg": math.degrees(-az) + rel}],
                            "meta": {"split": "test", "kind": "game", "eye": EYE, "fov": fov, "dist": a.dist, "rel_yaw": rel, "neutral": True}, "env": {}})
    json.dump({"size": CROP, "seed": 0, "samples": samples}, open(a.out, "w"))
    print(f"{len(samples)} vues de près ({len(slugs)} personnages × {len(VIEWS)}) → {a.out}")


def world_y(s: dict, px: float, py: float, z_world: float) -> float:
    """Hauteur (m) du point du plan vertical z = z_world vu au pixel (px, py)."""
    o, d = HT.rays(s, np.array([[px, py]]))
    t = (z_world - o[2]) / d[0][2]
    return float(o[1] + t * d[0][1])


def cmd_run(a) -> None:
    if a.libs:
        sys.path.insert(0, a.libs)
    import torch
    from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor

    os.makedirs(a.out, exist_ok=True)
    proc = SegformerImageProcessor.from_pretrained(a.model)
    model = SegformerForSemanticSegmentation.from_pretrained(a.model).eval().to("cuda" if torch.cuda.is_available() else "cpu")
    id2 = {int(k): v for k, v in model.config.id2label.items()}
    face_ids = [k for k, v in id2.items() if v in FACE]
    hair_ids = [k for k, v in id2.items() if v in HAIR]
    print(f"modèle {a.model} : classes {list(id2.values())}")
    rows = []
    for s in HT.read_truth(a.truth):
        slug, vi = s["id"].rsplit("_", 1)
        img = cv2.imread(f"{a.renders}/{s['id']}_c.png")
        alpha = cv2.imread(f"{a.renders}/{s['id']}_f.png", cv2.IMREAD_UNCHANGED)[..., 3] >= 128
        size = s["size"]
        xs, ys, zm, who = HT.zone_map(s, None, 1.0)
        zone = (zm == HT.HEAD) & (who == 0)
        zr, zc = np.nonzero(zone.any(axis=1))[0], np.nonzero(zone.any(axis=0))[0]
        # le modèle a appris sur des portraits cadrés serré : on lui donne un carré autour de la tête (--crop × la hauteur de la zone du jeu), pas l'image entière
        side = int(a.crop * (zr.max() - zr.min() + 1)) if a.crop > 0 else size
        cx, cy = (zc.min() + zc.max()) // 2, (zr.min() + zr.max()) // 2
        x0, y0 = int(np.clip(cx - side // 2, 0, size - side)), int(np.clip(cy - side // 2, 0, size - side))
        with torch.no_grad():
            inp = proc(images=img[y0:y0 + side, x0:x0 + side, ::-1].copy(), return_tensors="pt").to(model.device)
            logits = model(**inp).logits
            part = torch.nn.functional.interpolate(logits, size=(side, side), mode="bilinear", align_corners=False).argmax(1)[0].cpu().numpy()
        lab = np.zeros((size, size), part.dtype)
        lab[y0:y0 + side, x0:x0 + side] = part
        face = np.isin(lab, face_ids) & alpha
        head = (face | np.isin(lab, hair_ids)) & alpha
        b = s["agents"][0]["bones"]
        hp = HT.project(s, np.array([b["Head"]]))[0]
        zrows = np.nonzero(zone.any(axis=1))[0]
        r = {"slug": slug, "vue": VIEWS[int(vi)][0], "zone_bas": world_y(s, hp[0], zrows.max() + 1, b["Head"][2]), "zone_haut": world_y(s, hp[0], zrows.min(), b["Head"][2])}
        # tête d'après le modèle : seulement le morceau qui touche la colonne de la tête (pas une main ni une épaule étiquetée « peau »)
        col = slice(max(0, int(hp[0]) - 6), int(hp[0]) + 7)
        hrows = np.nonzero(head[:, col].any(axis=1))[0]
        frows = np.nonzero(face[:, col].any(axis=1))[0]
        r["tête_haut"] = world_y(s, hp[0], hrows.min(), b["Head"][2]) if len(hrows) else float("nan")
        r["menton"] = world_y(s, hp[0], frows.max() + 1, b["Head"][2]) if len(frows) and int(vi) == 0 else float("nan")
        r["dans_zone"] = float((head & zone).sum() / max(1, head.sum()))
        r["zone_tête"] = float((head & zone).sum() / max(1, (zone & alpha).sum()))
        r["tête_px"] = int(head.sum())
        rows.append(r)
        ov = img.copy()
        ov[face] = (0.45 * ov[face] + 0.55 * np.array([0, 150, 255])).astype(np.uint8)                          # visage : orange (BGR)
        hm = head & ~face
        ov[hm] = (0.45 * ov[hm] + 0.55 * np.array([255, 120, 0])).astype(np.uint8)                              # cheveux / chapeau : bleu
        cv2.rectangle(ov, (x0, y0), (x0 + side - 1, y0 + side - 1), (255, 255, 255), 1)                         # ce que le modèle a reçu
        cnts, _ = cv2.findContours(zone.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(ov, cnts, -1, (255, 0, 255), 2)                                                        # zone du jeu : violet
        cv2.imwrite(os.path.join(a.out, f"{s['id']}.jpg"), ov, [cv2.IMWRITE_JPEG_QUALITY, 92])
    json.dump(rows, open(os.path.join(a.out, "mesures.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = [r for r in rows if r["tête_px"] > 800]
    print(f"{len(rows)} vues, dont {len(ok)} où le modèle voit une tête (plus de 800 px)")
    for vue in [v for v, _ in VIEWS]:
        q = [r for r in ok if r["vue"] == vue]
        if not q:
            continue
        f = lambda k: np.nanmedian([r[k] for r in q]) if any(r[k] == r[k] for r in q) else float("nan")
        print(f"  {vue:5s} n={len(q):2d} · zone du jeu {f('zone_bas'):.2f} → {f('zone_haut'):.2f} m · modèle : menton {f('menton'):.2f} m, sommet {f('tête_haut'):.2f} m · "
              f"tête du modèle dans la zone {100 * f('dans_zone'):.0f} % · personnage dans la zone appelé « tête » {100 * f('zone_tête'):.0f} %")
    fr = [r for r in ok if r["vue"] == "face" and r["menton"] == r["menton"]]
    if fr:
        d = np.array([r["menton"] - r["zone_bas"] for r in fr])
        t = np.array([r["tête_haut"] - r["zone_haut"] for r in fr])
        print(f"  de face, par personnage ({len(fr)}) : menton du modèle − bas de la zone : médiane {100 * np.median(d):+.1f} cm (de {100 * d.min():+.1f} à {100 * d.max():+.1f}) ; "
              f"sommet du modèle − haut de la zone : médiane {100 * np.median(t):+.1f} cm (de {100 * t.min():+.1f} à {100 * t.max():+.1f})")
    print(f"images (orange : visage, bleu : cheveux / chapeau, violet : zone du jeu) et mesures.json → {a.out}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--out", required=True)
    p.add_argument("--manifest", default=os.path.join(os.path.dirname(__file__), "..", "..", "game", "assets", "ka", "manifest.json"))
    p.add_argument("--dist", type=float, default=2.5)
    p.add_argument("--exclude", nargs="*", default=["veto"])
    r = sub.add_parser("run")
    r.add_argument("--renders", required=True)
    r.add_argument("--truth", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--model", default="jonathandinu/face-parsing")
    r.add_argument("--crop", type=float, default=2.2, help="carré donné au modèle = N × la hauteur de la zone tête du jeu, centré sur elle (0 : l'image entière)")
    r.add_argument("--libs", default="", help="dossier où transformers est installé (pip install --target)")
    a = ap.parse_args()
    {"plan": cmd_plan, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
