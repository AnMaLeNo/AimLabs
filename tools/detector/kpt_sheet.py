#!/usr/bin/env python3
"""kpt_sheet.py — planches de contrôle : où sont posés les points « pose » sur chaque personnage du jeu, de face, de côté et de dos.

Les étiquettes d'entraînement du modèle pose sont calculées, pas dessinées à la main : une erreur dans le calcul fausserait tout l'entraînement sans
qu'aucun chiffre ne le montre. Ici le jeu rend chaque personnage (pose neutre, 6 m, trois orientations) et les points sont dessinés par LA MÊME fonction
que celle qui écrit les étiquettes (`pose_labels.points`) : ce qu'on voit sur la planche est ce que le modèle apprend. À regarder à l'œil.

  python kpt_sheet.py plan --out <plan.json>
  godot --path game --resolution 640x360 res://tools/gen_dataset.tscn -- --plan=<plan.json> --out=<rendus> --truth=<truth.jsonl>
  python kpt_sheet.py draw --renders <rendus> --truth <truth.jsonl> --out <dossier>          # une planche PNG par personnage + ensemble.png

Chaque planche : en haut le personnage entier dans les trois orientations, en bas la tête agrandie. Violet : la zone où le jeu compte un tir à la tête
(contour) et ses 5 points (0 centre, 1 bord gauche, 2 bord droit, 3 haut, 4 bas). Bleu clair : nez (5) et yeux (6, 7), absents de dos. Jaune : articulations
(8 à 19). Gauche / droite = celles du personnage. `--shift 0.2` : en vert, la zone tête descendue de 20 % de sa hauteur (la zone du jeu monte au-dessus des
cheveux ; le décalage est un réglage appliqué à la décision de tir, les étiquettes d'entraînement restent la zone du jeu).
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
import pose_labels as PL  # noqa: E402
from gen_plan import CROP, EYE, SCREEN_H, focal_px, place  # noqa: E402

VIEWS = [("face", 0.0), ("côté", 90.0), ("dos", 180.0)]
ZONE, FACE, BODY = (255, 0, 255), (255, 220, 0), (0, 230, 255)        # BGR


def cmd_plan(a) -> None:
    slugs = sorted(json.load(open(a.manifest, encoding="utf-8"))["agents"].keys())
    fov = 75.0
    f = focal_px(fov)
    fov_crop = math.degrees(2.0 * math.atan(math.tan(math.radians(fov) / 2.0) * CROP / SCREEN_H))
    samples = []
    for slug in slugs:
        for vi, (_, rel) in enumerate(VIEWS):
            x, z, pitch = place(a.dist, 0.0, 45.0, EYE, f)                 # milieu du corps un peu sous le centre : la tête entière reste dans le carré
            az = math.atan2(x, -z)
            samples.append({"id": f"{slug}_{vi}", "mode": "game", "kind": "game", "cam": {"eye": EYE, "pitch_deg": pitch, "fov": fov_crop},
                            "agents": [{"slug": slug, "neutral": True, "anim_t": 0.0, "x": x, "z": z, "rot_deg": math.degrees(-az) + rel}],
                            "meta": {"split": "test", "kind": "game", "eye": EYE, "fov": fov, "dist": a.dist, "rel_yaw": rel, "neutral": True}, "env": {}})
    json.dump({"size": CROP, "seed": 0, "samples": samples}, open(a.out, "w"))
    print(f"{len(samples)} vues ({len(slugs)} personnages × {len(VIEWS)}) → {a.out}")


def text(img: np.ndarray, items) -> np.ndarray:
    """Écrit [(x, y, texte, couleur BGR, taille), …] avec une police qui a les accents."""
    from PIL import Image, ImageDraw, ImageFont
    pil = Image.fromarray(img[..., ::-1])
    d = ImageDraw.Draw(pil)
    fonts = {}
    for x, y, s, col, size in items:
        if size not in fonts:
            try:
                fonts[size] = ImageFont.truetype("arial.ttf", size)
            except OSError:
                fonts[size] = ImageFont.load_default()
        d.text((x + 1, y + 1), s, font=fonts[size], fill=(0, 0, 0))
        d.text((x, y), s, font=fonts[size], fill=tuple(int(c) for c in col[::-1]))
    return np.array(pil)[..., ::-1].copy()


SHIFTED = (80, 255, 80)                                               # BGR : la zone descendue (--shift)


def draw_view(img: np.ndarray, s: dict, x0: float, y0: float, side: float, out_px: int, numbers: bool, radius: int, shift: float = 0.0):
    """Le carré [x0, x0 + side] × [y0, y0 + side] de l'image, agrandi à `out_px`, avec la zone tête et les points. Retourne (image, étiquettes à écrire)."""
    k = out_px / side
    M = np.array([[k, 0, -x0 * k], [0, k, -y0 * k]], np.float32)
    view = cv2.warpAffine(img, M, (out_px, out_px), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(40, 40, 40))
    step = 1.0 / max(1.0, k / 2)
    xs, ys, zm, who = HT.zone_map(s, (int(x0) - 1, int(y0) - 1, int(x0 + side) + 1, int(y0 + side) + 1), step, shown=True)
    mask = ((zm == HT.HEAD) & (who == 0)).astype(np.uint8)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    labels = []
    P = PL.points(s, 0)
    down = shift * (P[4][1] - P[3][1]) * k if shift and P[3][2] > 0 and P[4][2] > 0 else 0.0
    for c in cnts:
        pts = np.stack([(xs[c[:, 0, 0]] - x0) * k, (ys[c[:, 0, 1]] - y0) * k], axis=1)
        cv2.polylines(view, [np.round(pts).astype(np.int32)], True, ZONE, 1, cv2.LINE_AA)
        if down:                                                   # la même zone, descendue : ce que le tir « uniquement sur la tête » viserait avec ce réglage
            cv2.polylines(view, [np.round(pts + [0, down]).astype(np.int32)], True, SHIFTED, 2, cv2.LINE_AA)
    if down and P[0][2] > 0:
        u, w = (P[0][0] - x0) * k, (P[0][1] - y0) * k + down
        cv2.drawMarker(view, (int(round(u)), int(round(w))), SHIFTED, cv2.MARKER_CROSS, 4 * radius, 2, cv2.LINE_AA)
    for i, (px, py, v) in enumerate(P):
        if v <= 0:
            continue
        col = ZONE if i < 5 else (FACE if i < 8 else BODY)
        u, w = (px - x0) * k, (py - y0) * k
        if not (0 <= u < out_px and 0 <= w < out_px):
            continue
        cv2.circle(view, (int(round(u)), int(round(w))), radius + 1, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.circle(view, (int(round(u)), int(round(w))), radius, col, -1, cv2.LINE_AA)
        if numbers:
            labels.append((u + radius + 2, w - 8, str(i), col, 15))
    return view, labels


def cmd_draw(a) -> None:
    os.makedirs(a.out, exist_ok=True)
    truth = {s["id"]: s for s in HT.read_truth(a.truth)}
    slugs = sorted({i.rsplit("_", 1)[0] for i in truth} - set(a.exclude))
    W = a.size
    thumbs = []
    for slug in slugs:
        tops, heads, labels = [], [], []
        for vi, (vname, _) in enumerate(VIEWS):
            s = truth[f"{slug}_{vi}"]
            img = cv2.imread(f"{a.renders}/{slug}_{vi}_c.png")
            top, lt = draw_view(img, s, 0, 0, s["size"], W, numbers=False, radius=3, shift=a.shift)
            hi = HT.head_info(s, 0, shown=True)
            side = 5.2 * hi["r_px"]
            head, lh = draw_view(img, s, hi["cx"] - side / 2, hi["cy"] - side / 2 + 0.25 * hi["r_px"], side, W, numbers=True, radius=4, shift=a.shift)
            # numéros des articulations sur la vue entière (ceux de la tête sont lisibles sur la vue agrandie)
            P = PL.points(s, 0)
            kf = W / s["size"]
            lt = [(P[i][0] * kf + 6, P[i][1] * kf - 8, str(i), BODY, 15) for i in range(8, PL.NK) if P[i][2] > 0]
            labels += [(x + vi * W, y, t, c, sz) for x, y, t, c, sz in lt] + [(x + vi * W, y + W, t, c, sz) for x, y, t, c, sz in lh]
            labels.append((vi * W + 12, 10, f"{slug} · {vname}", (255, 255, 255), 26))
            tops.append(top)
            heads.append(head)
        sheet = np.concatenate([np.concatenate(tops, axis=1), np.concatenate(heads, axis=1)], axis=0)
        leg = np.full((150, sheet.shape[1], 3), 28, np.uint8)
        sheet = np.concatenate([sheet, leg], axis=0)
        y = 2 * W + 10
        labels.append((12, y, "Violet : zone où le jeu compte un tir à la tête (contour) et ses points — 0 centre · 1 bord gauche · 2 bord droit · 3 haut · 4 bas", ZONE, 19))
        labels.append((12, y + 30, "Bleu clair : 5 nez · 6 œil gauche · 7 œil droit (absents quand le visage est de dos)", FACE, 19))
        labels.append((12, y + 60, "Jaune : 8 épaule G · 9 épaule D · 10 coude G · 11 coude D · 12 main G · 13 main D · 14 hanche G · 15 hanche D · 16 genou G · 17 genou D · 18 pied G · 19 pied D",
                       BODY, 19))
        if a.shift:
            labels.append((12, y + 122, f"Vert : la zone tête descendue de {100 * a.shift:.0f} % de sa hauteur (du point 3 au point 4), et son centre (croix) : la zone visée avec ce réglage.",
                           SHIFTED, 17))
        labels.append((12, y + 95, "Gauche / droite = celles du personnage. En haut : personnage entier (face, côté, dos). En bas : la tête agrandie. Mêmes points que les étiquettes d'entraînement.",
                       (200, 200, 200), 17))
        sheet = text(sheet, labels)
        cv2.imwrite(os.path.join(a.out, f"{slug}.png"), sheet)
        thumbs.append(cv2.resize(sheet[:2 * W, :W], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA))
        thumbs[-1] = text(thumbs[-1], [(8, 6, slug, (255, 255, 255), 22)])
    cols = 8
    while len(thumbs) % cols:
        thumbs.append(np.full_like(thumbs[0], 28))
    cv2.imwrite(os.path.join(a.out, "ensemble_face.png"), np.concatenate([np.concatenate(thumbs[i:i + cols], axis=1) for i in range(0, len(thumbs), cols)], axis=0))
    print(f"{len(slugs)} planches → {a.out} (une par personnage, plus ensemble_face.png : tous les personnages de face)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--out", required=True)
    p.add_argument("--manifest", default=os.path.join(os.path.dirname(__file__), "..", "..", "game", "assets", "ka", "manifest.json"))
    p.add_argument("--dist", type=float, default=6.0)
    d = sub.add_parser("draw")
    d.add_argument("--renders", required=True)
    d.add_argument("--truth", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--size", type=int, default=720, help="côté (px) de chaque vue de la planche")
    d.add_argument("--exclude", nargs="*", default=["veto"], help="personnages sans planche (veto : modèle abîmé, écarté des données)")
    d.add_argument("--shift", type=float, default=0.0, help="dessine aussi (en vert) la zone tête descendue de cette part de sa hauteur : le réglage de décision "
                   "(head_bench `shift=`), pas une étiquette d'entraînement")
    a = ap.parse_args()
    {"plan": cmd_plan, "draw": cmd_draw}[a.cmd](a)


if __name__ == "__main__":
    main()
