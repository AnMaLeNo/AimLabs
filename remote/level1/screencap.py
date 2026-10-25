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
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

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


_dpi_done = False


def _win32():
    """user32 / kernel32 / dwmapi avec des signatures explicites (les handles font 64 bits : sans cela ctypes les tronque) ; process « DPI aware »
    pour que toutes les coordonnées soient en pixels physiques, comme celles de dxcam."""
    global _dpi_done
    from ctypes import wintypes
    u32, k32, dwm = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.dwmapi
    if not _dpi_done:
        u32.SetProcessDPIAware()
        _dpi_done = True
    u32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return u32, k32, dwm, wintypes


def list_windows(min_w: int = 160, min_h: int = 120) -> List[dict]:
    """Fenêtres de premier niveau visibles, avec un titre, non réduites, non « cloaked » (applications UWP cachées) :
    [{hwnd, title, process (nom de l'exécutable), rect = (x, y, w, h) de la zone cliente en pixels écran}]."""
    u32, k32, dwm, wt = _win32()
    out: List[dict] = []

    def cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        cloaked = wt.BOOL(0)
        dwm.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))   # DWMWA_CLOAKED
        if cloaked.value:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        rc, pt = wt.RECT(), wt.POINT(0, 0)
        u32.GetClientRect(hwnd, ctypes.byref(rc))
        u32.ClientToScreen(hwnd, ctypes.byref(pt))
        if rc.right < min_w or rc.bottom < min_h:
            return True
        pid, proc = wt.DWORD(0), ""
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            sz = wt.DWORD(520)
            pb = ctypes.create_unicode_buffer(520)
            if k32.QueryFullProcessImageNameW(h, 0, pb, ctypes.byref(sz)):
                proc = os.path.basename(pb.value)
            k32.CloseHandle(h)
        out.append({"hwnd": int(hwnd), "title": buf.value, "process": proc, "rect": (pt.x, pt.y, rc.right, rc.bottom)})
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    keep = proto(cb)                                            # référence gardée vivante pendant l'énumération
    u32.EnumWindows(keep, 0)
    return out


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Zone cliente (x, y, w, h) d'une fenêtre DÉJÀ identifiée : quelques microsecondes, donc utilisable pour la suivre pendant la capture
    (une énumération complète de toutes les fenêtres coûte plusieurs ms). None si elle n'existe plus ou est réduite."""
    u32, _k32, _dwm, wt = _win32()
    u32.IsWindow.argtypes = [wt.HWND]
    if not u32.IsWindow(hwnd) or u32.IsIconic(hwnd):
        return None
    rc, pt = wt.RECT(), wt.POINT(0, 0)
    u32.GetClientRect(hwnd, ctypes.byref(rc))
    u32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, rc.right, rc.bottom) if rc.right > 0 and rc.bottom > 0 else None


def window_rank(pattern: str, title: str) -> Optional[int]:
    """0 = titre identique, 1 = commence par le motif, 2 = le contient (sans tenir compte de la casse) ; None = ne correspond pas."""
    p, t = pattern.casefold().strip(), title.casefold()
    if not p:
        return None
    if t == p:
        return 0
    if t.startswith(p):
        return 1
    return 2 if p in t else None


def find_window(pattern: str, process: str = "", windows: Optional[List[dict]] = None) -> Optional[dict]:
    """La fenêtre qui correspond le mieux au motif de titre : d'abord le meilleur rang (identique > commence par > contient), puis la plus grande.
    `process` (sous-chaîne du nom de l'exécutable, ex. « godot ») départage deux fenêtres de même titre (l'éditeur de code d'un projet « AimLabs »
    porte aussi « AimLabs » dans son titre)."""
    best = None
    for w in (windows if windows is not None else list_windows()):
        r = window_rank(pattern, w["title"])
        if r is None or (process and process.casefold() not in w["process"].casefold()):
            continue
        key = (r, -w["rect"][2] * w["rect"][3])
        if best is None or key < best[0]:
            best = (key, w)
    return best[1] if best else None


def monitors() -> List[dict]:
    """Écrans : [{index, rect = (x, y, w, h) en pixels du bureau virtuel, primary}], l'écran principal en premier (index 0)."""
    u32, _k32, _dwm, wt = _win32()

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    out: List[dict] = []

    def cb(hmon, _hdc, _rc, _lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        u32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        out.append({"rect": (r.left, r.top, r.right - r.left, r.bottom - r.top), "primary": bool(mi.dwFlags & 1)})
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    keep = proto(cb)
    u32.EnumDisplayMonitors(None, None, keep, 0)
    out.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))
    for i, m in enumerate(out):
        m["index"] = i
    return out


class ScreenCapture:
    def __init__(self, title: str = WINDOW_TITLE, crop: Optional[int] = 240, backend: str = "auto",
                 region: Optional[Tuple[int, int, int, int]] = None, color: str = "RGB"):
        """`crop` : côté (px) du carré centré sur la fenêtre, None = fenêtre entière. `region` = (x, y, w, h)
        en pixels écran pour ignorer la recherche de fenêtre (autres systèmes que Windows). `color` : « RGB »
        ou « BGR » (ordre natif de l'écran et d'OpenCV : évite une conversion si on encode ensuite en JPEG)."""
        self.color = color
        self.title, self.crop, self.backend_name, self.manual_region = title, crop, backend, region
        self._requested = backend      # `backend_name` devient « dxcam » ou « mss » une fois démarré : on garde la demande d'origine pour recadrer
        self._cam_obj = None           # caméra dxcam, créée une seule fois (une seule duplication d'écran possible par sortie)
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
        backend = self._requested
        if backend in ("auto", "dxcam"):
            try:
                import dxcam
                if self._cam_obj is None:
                    self._cam_obj = dxcam.create(output_color=self.color)
                cam = self._cam_obj
                if x >= 0 and y >= 0 and x + w <= cam.width and y + h <= cam.height:
                    cam.start(region=(x, y, x + w, y + h), target_fps=0, video_mode=False)
                    self._cam = cam
                    self.backend_name = "dxcam"
                    return self
                if backend == "dxcam":
                    raise RuntimeError("la zone n'est pas sur l'écran principal : utilise --backend mss")
            except ImportError:
                if backend == "dxcam":
                    raise
        import mss
        if self._sct is None:
            self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.backend_name = "mss"
        return self

    def retarget(self, region: Tuple[int, int, int, int]) -> "ScreenCapture":
        """Change la zone capturée (x, y, w, h en pixels écran) sans recréer la capture : arrêt et redémarrage de dxcam (≈ 100 ms) sur la même caméra."""
        self.manual_region = region
        self.stop(keep=True)
        return self.start()

    def stop(self, keep: bool = False) -> None:
        """`keep=True` : on arrête seulement la capture en cours (avant de la redémarrer ailleurs) ; sinon on libère tout."""
        if self._cam is not None:
            self._cam.stop()
            self._cam = None
        if not keep:
            if self._sct is not None:
                self._sct.close()
                self._sct = None
            self._cam_obj = None

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
