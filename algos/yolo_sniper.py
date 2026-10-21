#!/usr/bin/env python3
"""yolo_sniper.py — tir auto « une balle » (sniper) : YOLO sur un carré autour du réticule, tir anticipé.

Même principe que yolo_trigger.py (ne bouge jamais la souris, ne fait que tirer) mais pensé pour
une arme à cadence très lente (Operator) : **si ça tire, ça doit toucher**, et le plus tôt possible.
- Le modèle n'analyse qu'un **carré centré sur le réticule** (côté = --crop × hauteur de l'écran,
  1/3 par défaut) en pleine résolution : pas de réduction d'image, inférence plus courte.
- La **vitesse de la cible en pixels** est mesurée sur les dernières frames (régression linéaire
  sur la position de la boîte) ; la position au moment où la balle partira (latence capture →
  maintenant + 1 frame de jeu) est **prédite**, et l'on tire quand cette position prédite met le
  réticule dans la bande centrale du corps (--band) — donc parfois *avant* que l'image ne montre
  la cible sous le réticule (pre-shot).
- Conditions de sûreté : suivi stable (≥ --track frames, mouvement cohérent), confiance ≥ --conf,
  et en --mode seg le pixel du réticule (ramené à la frame vue) est dans le masque de segmentation.
  Une seule balle par passage : après un tir, silence pendant --cooldown s et tant que la cible
  reste sous le réticule.
- mark(frame_id) est envoyé comme yolo_trigger quand l'image montre la cible sous le réticule
  (statistiques du benchmark) ; fire() une seule fois.
CAPSLOCK : on/off ; ALT maintenu : actif. Options : --mode box|seg (seg par défaut), --crop, --imgsz
(côté du réseau, 256 par défaut → moteur yolo11n-seg_256x256.engine, `--export` jeu fermé), --band,
--lead-scale, --cooldown, --debug.
"""
import argparse
import os
import sys
import time
from typing import List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sdk", "python"))
from aimlabs import AimClient  # noqa: E402
from yolo_aim import Detector  # noqa: E402

TOGGLE_KEY = "CAPSLOCK"
HOLD_KEY = "ALT"
STATS_EVERY = 5.0


def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["box", "seg"], default=os.environ.get("AIMLABS_SNIPER_MODE", "seg"))
    ap.add_argument("--model", default=os.environ.get("AIMLABS_YOLO_MODEL", ""), help="défaut : yolo11n-seg.pt (seg) / yolo11n.pt (box)")
    ap.add_argument("--imgsz", type=int, default=int(os.environ.get("AIMLABS_SNIPER_IMGSZ", "256")), help="côté du réseau (carré)")
    ap.add_argument("--crop", type=float, default=float(os.environ.get("AIMLABS_SNIPER_CROP", "0.3333")),
                    help="côté du carré analysé, en fraction de la hauteur de l'écran")
    ap.add_argument("--device", default=os.environ.get("AIMLABS_YOLO_DEVICE", ""))
    ap.add_argument("--conf", type=float, default=0.5, help="confiance minimale de la boîte pour tirer")
    ap.add_argument("--band", type=float, default=0.5, help="bande centrale de la boîte (fraction de la largeur) où le tir est sûr")
    ap.add_argument("--vband", type=float, default=0.9, help="idem en hauteur")
    ap.add_argument("--track", type=int, default=3, help="frames de suivi minimales avant de tirer")
    ap.add_argument("--lead-scale", type=float, default=1.0, help="facteur sur l'avance prédite (0 = pas d'anticipation)")
    ap.add_argument("--cooldown", type=float, default=1.0, help="s sans tir après un tir")
    ap.add_argument("--no-half", action="store_true")
    ap.add_argument("--no-trt", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--export", action="store_true", help="construit le moteur TensorRT (jeu fermé) puis quitte")
    return ap.parse_args(argv)


def crop_square(img: np.ndarray, frac: float) -> Tuple[np.ndarray, int, int]:
    """Carré centré sur le réticule ; retourne (crop, x0, y0) — x0/y0 = coin dans la frame."""
    h, w = img.shape[:2]
    side = int(min(h, w, max(32, round(h * frac))))
    x0, y0 = (w - side) // 2, (h - side) // 2
    return np.ascontiguousarray(img[y0:y0 + side, x0:x0 + side, :3]), x0, y0


class Track:
    """Boîte de la cible sur les dernières frames → vitesse en px/s.

    La vitesse est mesurée sur les **bords** de la boîte, pas sur son centre : quand la cible entre
    dans le carré analysé, le bord côté bordure est tronqué et le centre avance à la moitié de la
    vitesse réelle (c'est ce qui faisait rater les tirs rapides). Un bord collé à la bordure du
    crop (< 2 px) est ignoré ; il faut ≥ 2 mesures d'un même bord libre.
    """

    def __init__(self, side: int, horizon_s: float = 0.12) -> None:
        self.hist: List[Tuple[float, float, float, float, float]] = []  # (t, x1, x2, y1, y2)
        self.horizon = horizon_s
        self.side = side

    def push(self, t: float, box) -> None:
        x1, y1, x2, y2 = box[:4]
        self.hist.append((t, x1, x2, y1, y2))
        self.hist = [p for p in self.hist if t - p[0] <= self.horizon]

    def reset(self) -> None:
        self.hist = []

    def _fit(self, pts: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
        if len(pts) < 2:
            return None
        t = np.array([p[0] for p in pts])
        v = np.array([p[1] for p in pts])
        if float(np.ptp(t)) < 1e-4:
            return None
        a, b = np.polyfit(t - t[-1], v, 1)
        res = float(np.sqrt(np.mean((a * (t - t[-1]) + b - v) ** 2)))
        return float(a), res

    def velocity(self) -> Optional[Tuple[float, float, float]]:
        """(vx, vy, résidu px) ; None si aucun bord libre n'a ≥ 2 mesures."""
        m = 2.0
        left = [(p[0], p[1]) for p in self.hist if p[1] > m]
        right = [(p[0], p[2]) for p in self.hist if p[2] < self.side - m]
        top = [(p[0], p[3]) for p in self.hist if p[3] > m]
        bottom = [(p[0], p[4]) for p in self.hist if p[4] < self.side - m]
        fx = [f for f in (self._fit(left), self._fit(right)) if f is not None]
        fy = [f for f in (self._fit(top), self._fit(bottom)) if f is not None]
        if not fx:
            return None
        vx, rx = min(fx, key=lambda f: f[1])  # le bord le mieux ajusté
        vy, ry = min(fy, key=lambda f: f[1]) if fy else (0.0, 0.0)
        return vx, vy, max(rx, ry)


def main(argv) -> int:
    args = parse_args(argv)
    if not args.model:
        args.model = "yolo11n-seg.pt" if args.mode == "seg" else "yolo11n.pt"
    det = Detector(args.model, args.imgsz, args.device, min(args.conf, 0.25), not args.no_half, not args.no_trt, square=True)
    if args.export:
        print(f"prêt : {det.name}")
        return 0
    seg = det if args.mode == "seg" else None
    enabled = os.environ.get("AIMLABS_AUTOSTART") == "1"
    track = None
    last_fire_t = -1e9
    locked = False  # après un tir : attendre que la cible quitte le réticule
    n_frames, n_fire, n_pre, ms_acc, t_stats = 0, 0, 0, 0.0, time.monotonic()

    with AimClient() as c:
        print(f"yolo_sniper connecté, modèle {det.name}, crop {args.crop:.2f}×h → {det.in_w}px, critère {args.mode}, "
              f"bande {args.band:g}. {TOGGLE_KEY}: on/off, {HOLD_KEY} maintenu: actif", flush=True)
        while True:
            f = c.frame(timeout=2.0)
            if f is None:
                if not c.game_alive():
                    print("jeu fermé", flush=True)
                    return 0
                continue
            k = c.keys()
            if TOGGLE_KEY in k.just_pressed:
                enabled = not enabled
                print("sniper", "ON" if enabled else "OFF", flush=True)
            active = enabled or HOLD_KEY in k
            now = time.monotonic()
            if now - t_stats > STATS_EVERY:
                if active:
                    print(f"[stats] {n_frames / (now - t_stats):.0f} frames/s, inférence {ms_acc / max(n_frames, 1):.1f} ms, "
                          f"tirs {n_fire} (dont anticipés {n_pre})", flush=True)
                n_frames, n_fire, n_pre, ms_acc, t_stats = 0, 0, 0, 0.0, now
            if not active:
                if track is not None:
                    track.reset()
                continue

            crop, x0, y0 = crop_square(f.image, args.crop)
            if track is None:
                track = Track(crop.shape[0])
            boxes = det.boxes(crop)  # coordonnées du crop ; le réticule est en son centre
            n_frames += 1
            ms_acc += det.last_ms
            cx, cy = crop.shape[1] / 2.0, crop.shape[0] / 2.0
            t_cap = f.capture_usec / 1e6
            if not boxes:
                track.reset()
                locked = False
                continue
            # la boîte la plus proche du réticule
            b = min(boxes, key=lambda q: abs((q[0] + q[2]) / 2 - cx) + abs((q[1] + q[3]) / 2 - cy))
            bi = boxes.index(b)
            track.push(t_cap, b)

            # marque : l'image montre la cible sous le réticule (vérité à la capture, pour le benchmark)
            under_now = b[0] <= cx <= b[2] and b[1] <= cy <= b[3] and (seg is None or seg.mask_at(bi, cx, cy) >= 0.5)
            if under_now:
                c.mark(frame_id=f.id)
            else:
                locked = False

            if locked or now - last_fire_t < args.cooldown:
                continue
            # prédiction : où sera la cible quand la balle partira ?
            st = c.state()
            latency = max(st["time_usec"] - f.capture_usec, 0) / 1e6 + 1.0 / max(st["fps"], 1.0)
            vel = track.velocity()
            dx, dy = 0.0, 0.0
            if vel is not None and args.lead_scale > 0.0:
                vx, vy, res = vel
                bw = b[2] - b[0]
                if res < 0.15 * bw + 2.0:  # mouvement cohérent (sinon : pas d'anticipation)
                    dx, dy = vx * latency * args.lead_scale, vy * latency * args.lead_scale
            px1, py1, px2, py2 = b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy
            mx, my = (px1 + px2) / 2, (py1 + py2) / 2
            hw, hh = (px2 - px1) / 2 * args.band, (py2 - py1) / 2 * args.vband
            in_band = abs(cx - mx) <= hw and abs(cy - my) <= hh
            sure = in_band and b[4] >= args.conf and len(track.hist) >= args.track
            if sure and seg is not None:
                # le pixel du réticule au moment du tir correspond au pixel (cx - dx, cy - dy) de la frame vue ;
                # il doit être dans la bande centrale (--band) de la largeur du MASQUE sur cette ligne (pas de la
                # boîte : à hauteur de tête la boîte fait tout le corps)
                ext = seg.mask_extent(bi, cy - dy, b[0], b[2])
                if ext is None:
                    sure = False
                else:
                    ex_c, ex_hw = (ext[0] + ext[1]) / 2, (ext[1] - ext[0]) / 2 * args.band
                    sure = abs((cx - dx) - ex_c) <= max(ex_hw, 1.0)
            if sure:
                if c.fire(frame_id=f.id):
                    n_fire += 1
                    n_pre += int(not under_now)
                    last_fire_t = now
                    locked = True
                    if args.debug:
                        print(f"[tir] frame {f.id} {'anticipé ' if not under_now else ''}conf {b[4]:.2f} avance ({dx:+.0f},{dy:+.0f}) px "
                              f"v {vel[0] if vel else 0:+.0f} px/s res {vel[2] if vel else 0:.1f} boîte x[{b[0]:.0f},{b[2]:.0f}] "
                              f"latence {latency * 1000:.0f} ms inf {det.last_ms:.1f} ms", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
