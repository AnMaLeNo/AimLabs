"""Tir auto YOLO : ne bouge jamais la souris, tire dès qu'une personne est sous le réticule (CAPSLOCK on/off, ALT maintenu).

Même détecteur que yolo_aim.py (Ultralytics « person », moteur TensorRT FP16 sur CUDA). À chaque
frame : si le réticule (centre de l'image) est dans une boîte « person », on demande un tir au jeu
(il tire dès que la cadence de l'arme le permet). Par défaut la boîte est avancée de la vitesse
mesurée entre deux frames × la latence capture→application, pour viser là où la cible sera quand
la balle partira (--no-lead pour désactiver). --margin réduit la boîte autour de son centre
(0.6 = 60 % de la largeur) pour ne tirer que sur le torse. Démarre armé si AIMLABS_AUTOSTART=1
(posé par le jeu en mode --bench-trigger).

Banc reproductible côté jeu : `godot --path game -- --map=range --bench-trigger --algo=yolo_trigger.py`
(voir TriggerBench) — la caméra est figée, la cible traverse de plus en plus vite, puis de plus
en plus loin ; résultat JSON dans bench_results/.
"""

import argparse
import os
import sys
import time
from typing import Optional, Tuple

import numpy as np

from aimlabs import AimClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yolo_aim import Detector  # noqa: E402  (même dossier)

TOGGLE_KEY = "CAPSLOCK"
HOLD_KEY = "ALT"
STATS_EVERY = 2.0


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=os.environ.get("AIMLABS_YOLO_MODEL", "yolo11n.pt"))
    ap.add_argument("--imgsz", type=int, default=int(os.environ.get("AIMLABS_YOLO_IMGSZ", "640")))
    ap.add_argument("--device", default=os.environ.get("AIMLABS_YOLO_DEVICE", ""), help="cuda:0, cpu… (auto)")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--margin", type=float, default=1.0, help="fraction de la boîte (largeur et hauteur) autour de son centre où l'on tire")
    ap.add_argument("--no-lead", action="store_true", help="pas d'avance de la boîte selon sa vitesse × latence")
    ap.add_argument("--no-half", action="store_true", help="FP32 même sur GPU")
    ap.add_argument("--no-trt", action="store_true", help="pas de moteur TensorRT, model.predict() seulement")
    ap.add_argument("--debug", action="store_true", help="trace par frame")
    ap.add_argument("--export", action="store_true", help="construit le moteur TensorRT (jeu fermé) puis quitte")
    return ap.parse_args(argv)


def box_under(boxes, cx: float, cy: float, margin: float) -> Optional[Tuple[float, float, float, float, float]]:
    """La boîte (réduite à `margin` de sa taille autour de son centre) qui contient le réticule, la plus confiante."""
    best = None
    for x1, y1, x2, y2, c in boxes:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        hw, hh = (x2 - x1) / 2 * margin, (y2 - y1) / 2 * margin
        if abs(cx - mx) <= hw and abs(cy - my) <= hh and (best is None or c > best[4]):
            best = (x1, y1, x2, y2, c)
    return best


def main(argv) -> int:
    args = parse_args(argv)
    det = Detector(args.model, args.imgsz, args.device, args.conf, not args.no_half, not args.no_trt)
    if args.export:
        print(f"prêt : {det.name}")
        return 0
    enabled = os.environ.get("AIMLABS_AUTOSTART") == "1"  # armé d'office quand le jeu lance un benchmark
    prev = None  # (boîtes, capture_usec) de la frame précédente, pour la vitesse des boîtes
    n_frames, n_on, n_fire, ms_acc, t_stats = 0, 0, 0, 0.0, time.monotonic()

    with AimClient() as c:
        print(f"yolo_trigger connecté, mode {c.mode}, modèle {det.name}. {TOGGLE_KEY}: on/off, {HOLD_KEY} maintenu: actif", flush=True)
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
                print("tir auto", "ON" if enabled else "OFF", flush=True)
            active = enabled or HOLD_KEY in k
            now = time.monotonic()
            if now - t_stats > STATS_EVERY:
                if active:
                    print(f"[stats] {n_frames / (now - t_stats):.0f} frames/s, inférence {ms_acc / max(n_frames, 1):.1f} ms, "
                          f"réticule sur cible {n_on}/{n_frames}, tirs demandés {n_fire}", flush=True)
                n_frames, n_on, n_fire, ms_acc, t_stats = 0, 0, 0, 0.0, now
            if not active:
                prev = None
                continue

            boxes = det.boxes(np.ascontiguousarray(f.image[..., :3]))
            n_frames += 1
            ms_acc += det.last_ms
            cx, cy = f.width / 2.0, f.height / 2.0
            shifted = boxes
            if not args.no_lead and prev is not None and boxes and prev[0] and f.capture_usec > prev[1]:
                st = c.state()
                # la balle part à la prochaine frame de jeu après réception : latence capture→maintenant + 1 frame
                latency = max(st["time_usec"] - f.capture_usec, 0) / 1e6 + 1.0 / max(st["fps"], 1.0)
                dt = (f.capture_usec - prev[1]) / 1e6
                shifted = []
                for b in boxes:
                    # boîte précédente la plus proche (même cible) → vitesse écran
                    pb = min(prev[0], key=lambda q: abs((q[0] + q[2]) / 2 - (b[0] + b[2]) / 2))
                    vx = ((b[0] + b[2]) - (pb[0] + pb[2])) / 2 / dt
                    vy = ((b[1] + b[3]) - (pb[1] + pb[3])) / 2 / dt
                    if abs(vx) * dt < 2 * (b[2] - b[0]) and dt < 0.5:  # suivi plausible
                        shifted.append((b[0] + vx * latency, b[1] + vy * latency, b[2] + vx * latency, b[3] + vy * latency, b[4]))
                    else:
                        shifted.append(b)
            prev = (boxes, f.capture_usec)
            hit = box_under(shifted, cx, cy, args.margin)
            fired = False
            if hit is not None:
                n_on += 1
                fired = c.fire(frame_id=f.id)
                n_fire += int(fired)
            if args.debug:
                print(f"[dbg] frame {f.id} boîtes {len(boxes)} {'TIR ' if fired else ''}inf {det.last_ms:.0f} ms"
                      + (f" boîte x[{hit[0]:.0f},{hit[2]:.0f}] y[{hit[1]:.0f},{hit[3]:.0f}] conf {hit[4]:.2f}" if hit else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
