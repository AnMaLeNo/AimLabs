"""YOLO aim : détection « person » (Ultralytics, GPU) → vise la tête de la cible la plus proche du réticule. CAPSLOCK on/off, ALT maintenu, V tir auto, H tête/corps."""

# Fonctionnement
#   1. frame RGBA du pont → YOLO (classe COCO 0 = person) sur GPU si dispo (CUDA, FP16) ;
#   2. la boîte la plus proche du réticule est la cible ; point visé = tête (12 % sous le haut
#      de la boîte) ou centre du corps (touche H) ;
#   3. l'erreur en pixels est convertie en degrés avec le FOV de la caméra, et ajoutée au yaw/pitch
#      que la caméra avait AU MOMENT DE LA CAPTURE (fournis avec la frame) → direction absolue de
#      la cible ; la commande souris est la différence avec le yaw/pitch ACTUEL du joueur (état
#      partagé) divisée par la sensibilité du jeu. Les commandes déjà envoyées mais pas encore
#      visibles dans l'image ne sont jamais comptées deux fois : une seule commande en vol, la
#      suivante attend une image qui en montre l'effet (pas d'oscillation même avec 100+ ms de
#      latence), et il n'y a aucun gain à régler ;
#   4. la direction de la cible est extrapolée de sa vitesse angulaire × latence capture→commande
#      (compensation du temps d'inférence), désactivable avec --no-lead ; le tir auto part quand la
#      prédiction précédente s'est vérifiée (résidu < tolérance), le mouvement étant appliqué par le
#      jeu avant le tir.
#
# Réglages par variables d'environnement (le menu du jeu lance le script sans argument) ou
# en ligne de commande :  AIMLABS_YOLO_MODEL  (yolo11n.pt | yolo11s.pt | ... | mon_modele.engine)
#                         AIMLABS_YOLO_IMGSZ  (640 par défaut)   AIMLABS_YOLO_DEVICE (cuda:0 | cpu)
#
# Banc d'essai hors jeu (vitesse + détections sur des captures PNG) :
#     python algos/yolo_aim.py --bench dossier_de_png [--model yolo11s.pt --imgsz 640]

import argparse
import glob
import math
import os
import sys
import time
from typing import List, Optional, Tuple

import numpy as np

from aimlabs import AimClient

TOGGLE_KEY = "CAPSLOCK"
HOLD_KEY = "ALT"
AUTOFIRE_KEY = "V"
HEADBODY_KEY = "H"

PERSON_CLASS = 0
HEAD_RATIO = 0.12          # position de la tête sous le haut de la boîte (fraction de la hauteur)
FIRE_INTERVAL = 0.10       # s entre deux tirs auto
FIRE_TOLERANCE = 0.5       # tir auto si le résidu de suivi < 50 % de la demi-largeur de la boîte
MAX_STEP_PX = 600.0        # borne d'une commande souris (42° à sens 0,07 : un flick, pas un tour)
STATS_EVERY = 2.0          # s


def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=os.environ.get("AIMLABS_YOLO_MODEL", "yolo11n.pt"))
    ap.add_argument("--imgsz", type=int, default=int(os.environ.get("AIMLABS_YOLO_IMGSZ", "640")))
    ap.add_argument("--device", default=os.environ.get("AIMLABS_YOLO_DEVICE", ""), help="cuda:0, cpu… (auto)")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--smooth", type=float, default=1.0, help="fraction de l'erreur corrigée par frame (1 = instantané)")
    ap.add_argument("--no-lead", action="store_true", help="pas d'extrapolation du mouvement de la cible")
    ap.add_argument("--no-half", action="store_true", help="FP32 même sur GPU")
    ap.add_argument("--no-trt", action="store_true", help="pas de moteur TensorRT, model.predict() seulement")
    ap.add_argument("--debug", action="store_true", help="trace par frame (boîtes, erreur, commande)")
    ap.add_argument("--bench", metavar="DIR", help="mesure hors jeu sur les PNG d'un dossier")
    ap.add_argument("--export", action="store_true", help="construit le moteur TensorRT (jeu fermé) puis quitte")
    return ap.parse_args(argv)


ALGOS_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_model(model: str) -> str:
    """Un nom nu (`yolo11n.pt`) est rangé dans algos/ (poids téléchargés et moteurs exportés y
    restent, quel que soit le dossier de lancement) ; un chemin explicite est gardé tel quel."""
    if os.path.dirname(model) or os.path.exists(model):
        return model
    return os.path.join(ALGOS_DIR, model)


class Detector:
    """Détecteur « person ». Sur CUDA : moteur TensorRT FP16 (exporté et mis en cache au premier
    lancement : `<modèle>_<h>x<w>.engine`), pré-traitement sur GPU, NMS — mesuré 5–7 ms par frame
    sur RTX 3080 contre 17–23 ms pour `model.predict()` (surcoût Python/launchs CUDA sous Windows,
    indépendant de la taille du modèle). Sinon (CPU, --no-trt, export impossible) : `model.predict()`."""

    def __init__(self, model: str, imgsz: int, device: str, conf: float, half: bool, trt: bool = True, square: bool = False) -> None:
        import torch
        from ultralytics import YOLO

        import ultralytics

        self.torch = torch
        self.device = device or ("cuda:0" if torch.cuda.is_available() else "cpu")
        self._has_quantize = tuple(int(v) for v in ultralytics.__version__.split(".")[:2]) >= (8, 4)  # half → quantize
        self.half = half and self.device.startswith("cuda")
        self.imgsz, self.conf = imgsz, conf
        self.last_ms = 0.0
        self.engine = None
        model = resolve_model(model)
        self.seg = "-seg" in os.path.basename(model)  # yolo11n-seg : masques de segmentation en plus des boîtes
        self._protos = None   # (32, mh, mw) de la dernière frame (moteur TensorRT)
        self._coefs = []      # un vecteur de 32 coefficients par boîte retournée par boxes()
        self._masks = None    # predict() : masques (n, h, w) de la dernière frame
        # entrée réseau : frame 16:9 → largeur imgsz, hauteur au multiple de 32 supérieur ; carré (crop) → imgsz × imgsz
        self.in_w, self.in_h = imgsz, (imgsz if square else int(math.ceil(imgsz * 9 / 16 / 32) * 32))
        if self.device.startswith("cuda") and (trt or model.endswith(".engine")):
            try:
                self._load_engine(model)
            except Exception as e:  # noqa: BLE001 — on retombe sur predict()
                print(f"TensorRT indisponible ({e.__class__.__name__}: {e}), repli sur model.predict()", flush=True)
        if self.engine is None:
            self.model = YOLO(model)
            self.name = f"{os.path.basename(model)} @{imgsz} {self.device}{' fp16' if self.half else ''} predict()"
            self.model.predict(np.zeros((self.in_h, self.in_w, 3), np.uint8), **self._kw())  # warm-up
        self.boxes(np.zeros((self.in_h, self.in_w, 3) if square else (720, 1280, 3), np.uint8))  # autotune à la vraie forme

    def _load_engine(self, model: str) -> None:
        from ultralytics import YOLO
        from ultralytics.nn.autobackend import AutoBackend

        try:
            from ultralytics.utils.nms import non_max_suppression
        except ImportError:  # ultralytics < 8.3.2xx
            from ultralytics.utils.ops import non_max_suppression
        self._nms = non_max_suppression
        engine = model
        if not model.endswith(".engine"):
            engine = f"{os.path.splitext(model)[0]}_{self.in_h}x{self.in_w}.engine"
            if not os.path.exists(engine):
                print(f"export TensorRT {engine} : une seule fois, 2–4 min GPU libre — bien plus long si le jeu "
                      f"tourne (préférer `python algos/yolo_aim.py --export` avant de jouer)…", flush=True)
                out = YOLO(model).export(format="engine", half=True, imgsz=(self.in_h, self.in_w), device=0, verbose=False)
                os.replace(out, engine)
        self.engine = AutoBackend(engine, device=self.torch.device(self.device), fp16=True, verbose=False)
        self.name = f"{os.path.basename(engine)} TensorRT fp16 {self.device}{' seg' if self.seg else ''}"

    def _kw(self) -> dict:
        kw = dict(imgsz=self.imgsz, conf=self.conf, classes=[PERSON_CLASS], device=self.device, verbose=False)
        if self.half:
            kw["quantize" if self._has_quantize else "half"] = 16 if self._has_quantize else True
        return kw

    def _raw_boxes(self, rgb: np.ndarray) -> List[Tuple[float, float, float, float, float]]:
        if self.engine is None:
            r = self.model.predict(rgb, **self._kw())[0]
            self._masks = r.masks.data if (self.seg and r.masks is not None) else None
            self._raw_coefs = None
            return [(*b, c) for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())]
        torch, h, w = self.torch, rgb.shape[0], rgb.shape[1]
        with torch.inference_mode():
            x = torch.from_numpy(rgb).to(self.device, non_blocking=True).permute(2, 0, 1)[None].float() / 255.0
            x = torch.nn.functional.interpolate(x, size=(self.in_h, self.in_w), mode="bilinear", align_corners=False)
            y = self.engine(x)  # le moteur attend du fp32 en entrée (il calcule en fp16)
            protos = None
            if isinstance(y, (list, tuple)):
                if self.seg and len(y) > 1:
                    protos = y[1][0] if y[1].dim() == 4 else y[1]
                y = y[0]
            det = self._nms(y.float(), self.conf, 0.45, classes=[PERSON_CLASS], nc=80)[0]
            self._protos = protos.float() if protos is not None else None
            self._raw_coefs = det[:, 6:] if self.seg and det.shape[1] > 6 else None
            det = det[:, :6].cpu()
        sx, sy = w / self.in_w, h / self.in_h
        return [(x1 * sx, y1 * sy, x2 * sx, y2 * sy, c) for x1, y1, x2, y2, c, _cls in det.tolist()]

    def boxes(self, rgb: np.ndarray) -> List[Tuple[float, float, float, float, float]]:
        """[(x1, y1, x2, y2, conf)] sans le viewmodel (boîte large collée au bas de l'image)."""
        t0 = time.perf_counter()
        raw = self._raw_boxes(rgb)
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        h = rgb.shape[0]
        self._frame_hw = rgb.shape[:2]
        out = []
        self._coefs = []
        for i, (x1, y1, x2, y2, c) in enumerate(raw):
            bw, bh = x2 - x1, y2 - y1
            if y2 > 0.97 * h and bh < 1.2 * bw:  # bras + arme du joueur : large, tronqué par le bas
                continue
            out.append((x1, y1, x2, y2, c))
            self._coefs.append(i)
        return out

    def mask_at(self, box_index: int, x: float, y: float) -> float:
        """Probabilité (0–1) que le pixel (x, y) de la frame appartienne au masque de la boîte
        `box_index` (index dans la liste retournée par boxes()). 1.0 sans modèle de segmentation."""
        if not self.seg or box_index >= len(self._coefs):
            return 1.0
        raw_i = self._coefs[box_index]
        fh, fw = self._frame_hw
        if self._protos is not None and self._raw_coefs is not None:
            mh, mw = self._protos.shape[1], self._protos.shape[2]
            px = min(max(int(x * mw / fw), 0), mw - 1)
            py = min(max(int(y * mh / fh), 0), mh - 1)
            logit = (self._raw_coefs[raw_i] * self._protos[:, py, px]).sum()
            return float(self.torch.sigmoid(logit))
        if self._masks is not None and raw_i < self._masks.shape[0]:
            mh, mw = self._masks.shape[1], self._masks.shape[2]
            px = min(max(int(x * mw / fw), 0), mw - 1)
            py = min(max(int(y * mh / fh), 0), mh - 1)
            return float(self._masks[raw_i, py, px])
        return 1.0


def pick_target(boxes, cx: float, cy: float, head: bool) -> Optional[Tuple[float, float, float, float]]:
    """(aim_x, aim_y, half_w, half_h) de la boîte dont le point visé est le plus proche du réticule."""
    best = None
    for x1, y1, x2, y2, _c in boxes:
        ax = (x1 + x2) / 2
        ay = y1 + (y2 - y1) * HEAD_RATIO if head else (y1 + y2) / 2
        d = (ax - cx) ** 2 + (ay - cy) ** 2
        if best is None or d < best[0]:
            best = (d, ax, ay, (x2 - x1) / 2, (y2 - y1) / 2)
    return None if best is None else best[1:]


def px_to_deg(ex: float, ey: float, height: int, fov_deg: float) -> Tuple[float, float]:
    """Erreur écran (px, x à droite, y en bas) → angles (deg) via le FOV vertical de la caméra."""
    focal = (height / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return math.degrees(math.atan2(ex, focal)), math.degrees(math.atan2(ey, focal))


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def bench(args: argparse.Namespace) -> int:
    files = sorted(glob.glob(os.path.join(args.bench, "*.png")))
    if not files:
        print(f"aucun PNG dans {args.bench}", file=sys.stderr)
        return 1
    from PIL import Image
    det = Detector(args.model, args.imgsz, args.device, args.conf, not args.no_half, not args.no_trt)
    imgs = [np.asarray(Image.open(f).convert("RGB")) for f in files]
    found = 0
    for p in range(3):  # 3 passes : la dernière reflète le régime établi (horloges GPU montées)
        times, found = [], 0
        for img in imgs:
            b = det.boxes(img)
            times.append(det.last_ms)
            found += bool(b)
        print(f"  passe {p + 1} : {np.mean(times):.1f} ms/frame (médiane {np.median(times):.1f}, "
              f"min {min(times):.1f}, max {max(times):.1f})")
    print(f"{det.name}: {len(files)} images, détection sur {found}/{len(files)}, "
          f"inférence {np.mean(times):.1f} ms/frame en régime établi")
    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if args.bench:
        return bench(args)
    if args.export:
        det = Detector(args.model, args.imgsz, args.device, args.conf, not args.no_half, True)
        print(f"prêt : {det.name}" if det.engine is not None else "pas de moteur TensorRT (CPU ou tensorrt absent)")
        return 0 if det.engine is not None else 1

    det = Detector(args.model, args.imgsz, args.device, args.conf, not args.no_half, not args.no_trt)
    enabled, autofire, head = False, False, True
    prev: Optional[Tuple[float, float, float]] = None  # (aim_x, aim_y, capture_usec) pour la vitesse écran
    last_fire = 0.0
    n_frames, n_det, t_stats, ms_acc = 0, 0, time.monotonic(), 0.0

    with AimClient() as c:
        # Une seule commande en vol : on attend que le jeu l'ait consommée (compteur
        # cmds_applied + cmds_dropped) puis une frame capturée après (f.id > tick), avant de
        # mesurer à nouveau — pas de double comptage possible, quelle que soit la latence.
        st0 = c.state()
        n_sent = st0["cmds_applied"] + st0["cmds_dropped"]
        waiting_idx: Optional[int] = None
        applied_tick: Optional[int] = None
        print(f"yolo_aim connecté, mode {c.mode}, modèle {det.name}. {TOGGLE_KEY}: on/off, "
              f"{HOLD_KEY} maintenu: vise, {AUTOFIRE_KEY}: tir auto, {HEADBODY_KEY}: tête/corps", flush=True)
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
                print("aim", "ON" if enabled else "OFF", flush=True)
            if AUTOFIRE_KEY in k.just_pressed:
                autofire = not autofire
                print("tir auto", "ON" if autofire else "OFF", flush=True)
            if HEADBODY_KEY in k.just_pressed:
                head = not head
                print("vise :", "tête" if head else "corps", flush=True)
            active = enabled or HOLD_KEY in k
            if active and waiting_idx is not None:
                st = c.state()
                if applied_tick is None and st["cmds_applied"] + st["cmds_dropped"] >= waiting_idx:
                    applied_tick = st["tick"]
                if applied_tick is None or f.id <= applied_tick:
                    continue  # cette image ne montre pas encore l'effet de la commande précédente
                waiting_idx = applied_tick = None

            boxes = det.boxes(np.ascontiguousarray(f.image[..., :3])) if active else []
            n_frames += 1
            ms_acc += det.last_ms if active else 0.0
            now = time.monotonic()
            if now - t_stats > STATS_EVERY:
                if active:
                    print(f"[stats] {n_frames / (now - t_stats):.0f} frames/s, inférence {ms_acc / max(n_frames, 1):.1f} ms, "
                          f"cible vue {n_det}/{n_frames}", flush=True)
                n_frames, n_det, ms_acc, t_stats = 0, 0, 0.0, now
            if not active:
                prev = None
                continue

            cx, cy = f.width / 2.0, f.height / 2.0
            tgt = pick_target(boxes, cx, cy, head)
            if tgt is None:
                prev = None
                continue
            n_det += 1
            ax, ay, half_w, _half_h = tgt
            # direction absolue de la cible : angles de la caméra à la capture + erreur écran
            # (le jeu fait yaw -= dx·sens et pitch -= dy·sens, x écran à droite, y en bas)
            yaw_err, pitch_err = px_to_deg(ax - cx, ay - cy, f.height, f.fov)
            tgt_yaw, tgt_pitch = wrap180(f.yaw - yaw_err), f.pitch - pitch_err
            half_w_deg = px_to_deg(half_w, 0.0, f.height, f.fov)[0]

            st = c.state()
            aim_yaw, aim_pitch = tgt_yaw, tgt_pitch
            if not args.no_lead:
                # latence capture→maintenant (horloge du jeu) + une frame de jeu pour l'application
                latency_s = max(st["time_usec"] - f.capture_usec, 0) / 1e6 + 1.0 / max(st["fps"], 1.0)
                if prev is not None and f.capture_usec > prev[2]:
                    dt = (f.capture_usec - prev[2]) / 1e6
                    dyaw, dpitch = wrap180(tgt_yaw - prev[0]), tgt_pitch - prev[1]
                    jump = math.hypot(dyaw, dpitch)
                    # suivi continu (dt court) et saut plausible (< 4 demi-largeurs, sinon autre
                    # cible / faux positif) ; avance bornée à une largeur de boîte
                    if dt < 0.6 and jump < 4.0 * half_w_deg:
                        lead = min(latency_s / dt, 2.0 * half_w_deg / max(jump, 1e-6))
                        aim_yaw, aim_pitch = wrap180(tgt_yaw + dyaw * lead), tgt_pitch + dpitch * lead
                prev = (tgt_yaw, tgt_pitch, f.capture_usec)

            # rien en vol : l'état du jeu est la caméra réelle (souris humaine comprise)
            cam_yaw, cam_pitch = st["yaw"], st["pitch"]
            # résidu = écart entre où la caméra est et où la cible est prédite : petit ⇒ suivi calé
            res_yaw, res_pitch = wrap180(cam_yaw - aim_yaw), cam_pitch - aim_pitch
            dx = res_yaw / st["sens"] * args.smooth
            dy = res_pitch / st["sens"] * args.smooth
            dx = max(-MAX_STEP_PX, min(MAX_STEP_PX, dx))
            dy = max(-MAX_STEP_PX, min(MAX_STEP_PX, dy))
            if c.move(dx, dy, frame_id=f.id):
                n_sent += 1
                waiting_idx = n_sent
            tol_deg = max(FIRE_TOLERANCE * half_w_deg, 0.1)
            fired = autofire and abs(res_yaw) < tol_deg and abs(res_pitch) < tol_deg and now - last_fire > FIRE_INTERVAL
            if fired and c.fire(frame_id=f.id):
                n_sent += 1
                waiting_idx = n_sent
                last_fire = now
            if args.debug:
                print(f"[dbg] frame {f.id} boîtes {len(boxes)} cible ({tgt_yaw:.2f},{tgt_pitch:.2f})° "
                      f"visée ({aim_yaw:.2f},{aim_pitch:.2f})° cam ({cam_yaw:.2f},{cam_pitch:.2f})° "
                      f"résidu ({res_yaw:.2f},{res_pitch:.2f})° tol {tol_deg:.2f} souris ({dx:.0f},{dy:.0f}) "
                      f"{'TIR ' if fired else ''}inf {det.last_ms:.0f} ms", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
