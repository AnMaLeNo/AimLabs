"""Niveau 1 du mode « deux machines » : capturer l'image du jeu depuis l'ÉCRAN.

Ce programme n'est PAS connecté au jeu : il regarde la fenêtre comme le ferait une caméra et rend des
tableaux numpy. C'est le point de départ fourni aux joueurs ; ce qu'on fait de l'image (la compresser, la
rogner, l'envoyer à un autre ordinateur…) est leur affaire — voir remote/README.md.

    from screencap import ScreenCapture
    with ScreenCapture(crop=240) as cap:        # carré de 240 px autour du réticule (None = fenêtre entière)
        while True:
            c = cap.latest()                    # bloque jusqu'à une image plus récente que la précédente
            c.image                             # (h, w, 3) uint8 RGB
            c.t_us                              # horloge locale (perf_counter, µs) au moment où on l'a reçue

    python screencap.py --bench [--crop 240] [--save out.png] [--backend dxcam|mss]

Backends : « dxcam » (DXGI Desktop Duplication, quasi gratuit, une image par rafraîchissement de l'écran) ou
« mss » (GDI, ~6 ms par image, fonctionne partout). Limite physique de ce niveau : la capture ne voit que ce
que l'écran affiche, donc au mieux la fréquence de rafraîchissement du moniteur (164 Hz ici), alors que le
pont interne du jeu publie chaque image rendue (~500/s).
"""
import argparse
import ctypes
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

WINDOW_TITLE = "AimLabs (DEBUG)"  # titre de la fenêtre du jeu (build de développement)


@dataclass
class Capture:
    index: int           # compteur d'images de ce programme
    t_us: int            # horloge locale (time.perf_counter, µs) à la réception
    image: np.ndarray    # (h, w, 3) uint8, RGB (ou BGR si ScreenCapture(color="BGR"))
    window: Tuple[int, int]   # taille de la zone cliente de la fenêtre (w, h)
    origin: Tuple[int, int]   # coin haut-gauche de `image` dans la fenêtre


def _window_client_rect(title: str) -> Tuple[int, int, int, int]:
    """(x, y, w, h) de la zone cliente de la fenêtre, en pixels écran (Windows)."""
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        raise RuntimeError(f"fenêtre « {title} » introuvable (le jeu tourne-t-il, fenêtre non minimisée ?)")
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, rc.right, rc.bottom


class ScreenCapture:
    def __init__(self, title: str = WINDOW_TITLE, crop: Optional[int] = 240, backend: str = "auto",
                 region: Optional[Tuple[int, int, int, int]] = None, color: str = "RGB"):
        """`crop` : côté (px) du carré centré sur la fenêtre, None = fenêtre entière. `region` = (x, y, w, h)
        en pixels écran pour ignorer la recherche de fenêtre (autres systèmes que Windows). `color` : « RGB »
        ou « BGR » (ordre natif de l'écran et d'OpenCV : évite une conversion si on encode ensuite en JPEG)."""
        self.color = color
        self.title, self.crop, self.backend_name, self.manual_region = title, crop, backend, region
        self._cam = None
        self._sct = None
        self._count = 0
        self._region = None
        self._win = (0, 0)
        self._origin = (0, 0)

    # -- géométrie ---------------------------------------------------------------------------
    def _compute_region(self) -> Tuple[int, int, int, int]:
        if self.manual_region:
            x, y, w, h = self.manual_region
        else:
            x, y, w, h = _window_client_rect(self.title)
        self._win = (w, h)
        if self.crop:
            side = min(self.crop, w, h)
            ox, oy = (w - side) // 2, (h - side) // 2
            self._origin = (ox, oy)
            return x + ox, y + oy, side, side
        self._origin = (0, 0)
        return x, y, w, h

    def start(self) -> "ScreenCapture":
        x, y, w, h = self._compute_region()
        self._region = (x, y, w, h)
        backend = self.backend_name
        if backend in ("auto", "dxcam"):
            try:
                import dxcam
                cam = dxcam.create(output_color=self.color)
                if x >= 0 and y >= 0 and x + w <= cam.width and y + h <= cam.height:
                    cam.start(region=(x, y, x + w, y + h), target_fps=0, video_mode=False)
                    self._cam = cam
                    self.backend_name = "dxcam"
                    return self
                if backend == "dxcam":
                    raise RuntimeError("la fenêtre n'est pas sur l'écran principal : utilise --backend mss")
            except ImportError:
                if backend == "dxcam":
                    raise
        import mss
        self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.backend_name = "mss"
        return self

    def stop(self) -> None:
        if self._cam is not None:
            self._cam.stop()
            self._cam = None
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def __enter__(self) -> "ScreenCapture":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- images ------------------------------------------------------------------------------
    def latest(self, timeout: float = 1.0) -> Optional[Capture]:
        """Prochaine image plus récente que la précédente (dxcam : au rythme de l'écran ; mss : en continu)."""
        t_end = time.perf_counter() + timeout
        img = None
        while img is None and time.perf_counter() < t_end:
            if self._cam is not None:
                img = self._cam.get_latest_frame()
            else:
                x, y, w, h = self._region
                shot = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
                raw = np.asarray(shot)                                   # BGRA
                img = np.ascontiguousarray(raw[:, :, :3] if self.color == "BGR" else raw[:, :, 2::-1])
        if img is None:
            return None
        self._count += 1
        return Capture(self._count, time.perf_counter_ns() // 1000, img, self._win, self._origin)


def _bench(args) -> None:
    with ScreenCapture(crop=args.crop or None, backend=args.backend) as cap:
        print(f"backend {cap.backend_name}, fenêtre {cap._win[0]}x{cap._win[1]}, région {cap._region}")
        n, gaps, t_prev, last, uniq, h_prev = 0, [], None, None, 0, None
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < args.seconds:
            c = cap.latest()
            if c is None:
                continue
            n += 1
            h = hash(c.image.tobytes())
            uniq += h != h_prev
            h_prev = h
            if t_prev is not None:
                gaps.append((c.t_us - t_prev) / 1000)
            t_prev, last = c.t_us, c
        el = time.perf_counter() - t0
        g = np.array(gaps)
        print(f"{n} images en {el:.1f} s = {n / el:.0f}/s (dont {uniq} différentes de la précédente = {uniq / el:.0f}/s) · écart entre images : moyen {g.mean():.2f} ms, "
              f"p50 {np.median(g):.2f}, p99 {np.percentile(g, 99):.2f} · image {last.image.shape}")
        if args.save:
            import cv2
            cv2.imwrite(args.save, last.image[:, :, ::-1])
            print("dernière image ->", args.save)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true", help="mesure le débit de capture")
    ap.add_argument("--crop", type=int, default=240, help="côté du carré central (0 = fenêtre entière)")
    ap.add_argument("--backend", choices=["auto", "dxcam", "mss"], default="auto")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--save", default="")
    a = ap.parse_args()
    if not a.bench:
        ap.error("rien à faire sans --bench ; utilise ce fichier comme module (voir la docstring)")
    _bench(a)
