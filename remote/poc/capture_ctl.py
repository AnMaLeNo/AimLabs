"""Cadrage de la capture d'écran du PC A, réglable à chaud depuis la machine B (voir sniper_setup.py).

Un cadrage = une référence (l'ÉCRAN, ou une FENÊTRE trouvée par son titre) + un carré de `crop` pixels centré sur la référence, décalé de (dx, dy).
Par défaut : le centre de l'écran principal (aucune fenêtre). Configuration :

    {"mode": "screen" | "window", "title": "AimLabs", "process": "", "monitor": 0, "dx": 0, "dy": 0, "crop": 384, "resize": 256}

  * mode « window » : `title` est un motif (casse ignorée) ; identique > commence par > contient, puis la plus grande ; `process` (« godot ») départage deux
    fenêtres de même titre. Si la fenêtre bouge, la capture la suit (test de position de quelques µs, toutes les 250 ms) ; si elle disparaît, la capture
    garde sa dernière zone et `found` passe à False dans `info()` jusqu'à ce qu'elle revienne.
  * `resize` : côté de l'image envoyée à l'algo (celui du réseau, fixé par B).

`CaptureController.latest()` est appelé par la boucle de capture (le seul thread qui touche à la capture) ; `set_config`, `info` et `preview` sont thread-safe
(appelés depuis le thread de contrôle).
"""
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "level1"))
import screencap as SC  # noqa: E402
from capture_cfg import DEFAULT_CFG, region_in, sanitize  # noqa: E402,F401


def resolve(cfg: dict, windows=None, mons=None) -> dict:
    """{found, ref, region, window, monitor, error} : où capturer, pour une configuration donnée."""
    out = {"found": False, "ref": None, "region": None, "window": None, "monitor": cfg["monitor"], "error": ""}
    if cfg["mode"] == "window":
        w = SC.find_window(cfg["title"], cfg["process"], windows)
        if w is None:
            out["error"] = f"fenêtre « {cfg['title']} » introuvable" if cfg["title"] else "aucun motif de titre"
            return out
        out["ref"] = tuple(w["rect"])
        out["window"] = {"title": w["title"], "process": w["process"], "hwnd": w["hwnd"]}
    else:
        ms = mons if mons is not None else SC.monitors()
        m = ms[cfg["monitor"]] if cfg["monitor"] < len(ms) else ms[0]
        out["ref"], out["monitor"] = tuple(m["rect"]), m["index"]
    out["region"] = region_in(out["ref"], cfg)
    out["found"] = True
    return out


class CaptureController:
    def __init__(self, cfg=None, backend="auto"):
        self.cfg = sanitize(cfg or {})
        self.backend = backend
        self.cap = None
        self.res = None                 # dernière résolution appliquée
        self.found = True
        self.error = ""
        self.seq = 0                    # incrémenté à chaque configuration appliquée
        self._pending = None
        self._lock = threading.Lock()
        self._t_track = 0.0
        self._applied_region = None

    # ----------------------------------------------------------------- thread de capture
    def start(self):
        res = resolve(self.cfg)
        if res["region"] is None:       # fenêtre demandée mais absente au démarrage : on capture le centre de l'écran en attendant qu'elle apparaisse
            self.found, self.error = False, res["error"]
            res = resolve(dict(self.cfg, mode="screen"))
        self.res = res
        self._applied_region = res["region"]
        self.cap = SC.ScreenCapture(region=res["region"], crop=None, backend=self.backend, color="BGR").start()
        return self

    def stop(self):
        if self.cap is not None:
            self.cap.stop()
            self.cap = None

    def _retarget(self, region):
        if region != self._applied_region:
            self.cap.retarget(region)
            self._applied_region = region

    def latest(self, timeout=0.2):
        with self._lock:
            pend, self._pending = self._pending, None
        if pend is not None:
            self.cfg = pend
            res = resolve(pend)
            self.found, self.error = res["found"], res["error"]
            if res["found"]:
                self.res = res
                self._retarget(res["region"])
            self.seq += 1
        elif self.cfg["mode"] == "window":
            self._track_window()
        c = self.cap.latest(timeout)
        if c is not None and self.res is not None:
            rx, ry, rw, rh = self.res["ref"]
            x, y = self._applied_region[0], self._applied_region[1]
            c.window, c.origin = (rw, rh), (x - rx, y - ry)       # géométrie de l'en-tête des images : référence (écran ou fenêtre) et position du carré dedans
        return c

    def _track_window(self):
        """Suit la fenêtre si elle bouge (test bon marché sur le handle) ; si elle a disparu, nouvelle recherche toutes les 250 ms."""
        now = time.monotonic()
        if now - self._t_track < 0.25:
            return
        self._t_track = now
        w = (self.res or {}).get("window")
        rect = SC.window_rect(w["hwnd"]) if w else None
        if rect is not None and self.found:
            if tuple(rect) != tuple(self.res["ref"]):
                self.res = dict(self.res, ref=tuple(rect), region=region_in(tuple(rect), self.cfg))
                self._retarget(self.res["region"])
            return
        res = resolve(self.cfg)
        self.found, self.error = res["found"], res["error"]
        if res["found"]:
            self.res = res
            self._retarget(res["region"])

    # ----------------------------------------------------------------- thread de contrôle
    def set_config(self, cfg: dict):
        """Applique `cfg` (champs partiels acceptés) à la prochaine itération de la boucle de capture."""
        with self._lock:
            base = self._pending if self._pending is not None else self.cfg
            self._pending = sanitize({**base, **{k: v for k, v in cfg.items() if k in DEFAULT_CFG}})

    def info(self, full=False) -> dict:
        res = self.res or {}
        d = {"config": self.cfg, "found": self.found, "error": self.error, "seq": self.seq, "backend": getattr(self.cap, "backend_name", None),
             "ref": res.get("ref"), "region": self._applied_region, "window": res.get("window")}
        if full:
            d["windows"] = [{"title": w["title"], "process": w["process"], "rect": w["rect"]} for w in SC.list_windows()][:80]
            d["monitors"] = SC.monitors()
        return d

    def preview(self, target=None, max_w=960, quality=70):
        """Aperçu (JPEG) de la RÉFÉRENCE d'un cadrage — le courant ou `target` (champs partiels) — pour que B y dessine le carré à découper.
        Retourne (meta, jpeg|None). Capture par GDI (mss) : n'interfère pas avec la duplication d'écran de la capture en cours."""
        import cv2
        import mss
        cfg = sanitize({**self.cfg, **(target or {})})
        res = resolve(cfg)
        meta = {"found": res["found"], "error": res["error"], "ref": res["ref"], "region": res["region"], "window": res["window"], "cfg": cfg}
        if not res["found"]:
            return meta, None
        x, y, w, h = res["ref"]
        with mss.mss() as sct:
            shot = np.asarray(sct.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3]
        scale = min(1.0, max_w / w)
        img = cv2.resize(shot, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1.0 else shot
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        meta["scale"], meta["size"] = scale, (img.shape[1], img.shape[0])
        return meta, (buf.tobytes() if ok else None)
