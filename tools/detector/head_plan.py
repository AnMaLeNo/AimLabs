#!/usr/bin/env python3
"""head_plan.py — plan de rendu du BANC « tête » (lu par game/tools/gen_dataset.gd, comme celui de gen_plan.py).

Le jeu de test du détecteur place le personnage n'importe où dans le carré : le réticule tombe rarement près de la tête. Ici chaque image est une
situation de tir à la tête : le personnage est dans le monde du jeu (pose neutre, œil à 1,65 m, FOV 75° : ce que montre le stand de tir), à une
distance tirée de 3 à 60 m, et le RÉTICULE (centre du carré) est posé autour de sa tête — dessus, juste à côté, sur le cou, au-dessus — à moins de
`--spread` rayons de tête de son centre. Tous les personnages, toutes les orientations.

  python head_plan.py --out <plan.json> [--per-agent 100] [--dmax 60] [--spread 2.5] [--seed 7]
  godot --path game --resolution 640x360 res://tools/gen_dataset.tscn -- --plan=<plan.json> --out=<renders> --truth=<truth.jsonl>
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_plan import CROP, EYE, SCREEN_H, focal_px, project, rnd_game_env  # noqa: E402

HEAD_Y = 2.02          # hauteur du milieu de la zone « tête » du jeu (capsule de tête moins ce que le torse cache)
HEAD_R = 0.12


def place_head(d: float, ox: float, oy: float, f: float):
    """(x, z, pitch_deg) tels que le milieu de la tête d'un personnage à `d` m tombe à (ox, oy) px du centre du carré."""
    pitch = math.atan2(HEAD_Y - EYE, d)
    az = math.atan2(ox, f)
    for _ in range(12):
        px, py = project(np.array([d * math.sin(az), HEAD_Y, -d * math.cos(az)]), EYE, pitch, f)
        pitch += math.atan2(oy - py, f)
        az += math.atan2(ox - px, f)
    return d * math.sin(az), -d * math.cos(az), math.degrees(pitch)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", default=os.path.join(os.path.dirname(__file__), "..", "..", "game", "assets", "ka", "manifest.json"))
    ap.add_argument("--per-agent", type=int, default=100)
    ap.add_argument("--dmin", type=float, default=3.0)
    ap.add_argument("--dmax", type=float, default=60.0)
    ap.add_argument("--spread", type=float, default=2.5, help="le réticule est à moins de N rayons de tête du milieu de la tête")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--exclude", nargs="*", default=["harbor", "veto"], help="personnages écartés (veto : modèle abîmé, il s'affiche blanc ; harbor : écarté du jeu, world.gd)")
    ap.add_argument("--split", default="test", choices=["train", "val", "test"], help="« train » : des situations de tir à la tête pour l'ENTRAÎNEMENT (autre graine que le banc)")
    ap.add_argument("--prefix", default="h", help="préfixe des identifiants (différent d'un plan à l'autre : les images vont dans le même dossier)")
    ap.add_argument("--env-frac", type=float, default=0.0, help="part des images où ciel, soleil, brouillard et sol sont tirés au hasard (gen_plan.rnd_game_env) ; "
                    "0 pour le banc : le monde tel que le jeu l'affiche")
    a = ap.parse_args()
    slugs = sorted(k for k in json.load(open(a.manifest, encoding="utf-8"))["agents"].keys() if k not in a.exclude)
    rng = np.random.default_rng(a.seed)
    fov = 75.0
    f = focal_px(fov)
    fov_crop = math.degrees(2.0 * math.atan(math.tan(math.radians(fov) / 2.0) * CROP / SCREEN_H))
    samples = []
    for slug in slugs:
        for i in range(a.per_agent):
            d = float(math.exp(rng.uniform(math.log(a.dmin), math.log(a.dmax))))
            r_px = f * HEAD_R / d
            rr, th = a.spread * r_px * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)       # uniforme dans le disque
            ox, oy = rr * math.cos(th), rr * math.sin(th)
            x, z, pitch = place_head(d, ox, oy, f)
            az = math.atan2(x, -z)
            rel = float(rng.normal(0, 35)) if rng.random() < 0.5 else float(rng.uniform(0, 360))
            samples.append({"id": f"{a.prefix}{len(samples):06d}", "mode": "game", "kind": "game", "cam": {"eye": EYE, "pitch_deg": pitch, "fov": fov_crop},
                            "agents": [{"slug": slug, "neutral": True, "anim_t": float(rng.random()), "x": x, "z": z, "rot_deg": math.degrees(-az) + rel}],
                            "meta": {"split": a.split, "kind": "game", "eye": EYE, "fov": fov, "dist": d, "head_off_px": [ox, oy], "rel_yaw": rel % 360.0,
                                     "neutral": True},
                            "env": rnd_game_env(rng) if rng.random() < a.env_frac else {}})
    json.dump({"size": CROP, "seed": a.seed, "samples": samples}, open(a.out, "w"))
    print(f"{len(samples)} situations ({len(slugs)} personnages × {a.per_agent}) → {a.out}")


if __name__ == "__main__":
    main()
