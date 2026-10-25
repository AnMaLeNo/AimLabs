"""Page « Capture » de la fenêtre de réglages (aimlabs.setup) : voir l'image qui part vers l'algo, choisir la fenêtre (ou l'écran), recadrer à la main.

C'est une PAGE ACTIVE de la fenêtre générique : elle a besoin du lien avec l'agent du PC A (le `RemoteClient` de B), donc c'est l'hôte (sniper_b.py, ou ce fichier lancé seul)
qui la fournit : `run(schema, plugins={"capture": CapturePage(client, cfg, side_net)})`.

    python sniper_b.py --connect listen:7777 --setup          # réglages (capture + touches) puis l'algo tourne normalement (Entrée pour valider)
    python sniper_setup.py --connect listen:7777              # la seule page Capture (agent_a se connecte à B comme d'habitude)

À gauche l'aperçu de la RÉFÉRENCE (tout l'écran, ou toute la fenêtre choisie) avec le carré qui sera découpé ; à droite l'image réellement reçue par B (donc ce que l'algo verra),
la configuration et la liste des fenêtres du PC A.

  souris : glisser sur l'aperçu = déplacer le carré · clic sur une fenêtre de la liste = la choisir · clic sur « Écran » = cadrer sur l'écran
  flèches / i j k l : déplacer d'1 px / de 10 px · + / - : taille du carré · c : recentrer · m : écran ⇄ fenêtre · [ ] : fenêtre précédente / suivante
  t : saisir le motif du titre (Entrée ferme la saisie) · r : rafraîchir

L'agent applique chaque changement tout de suite (la capture est reciblée à chaud, ~100 ms) : l'image de droite montre toujours le résultat réel.
Tout ce qui parle à l'agent passe par un fil de fond ; l'interface ne bloque jamais sur le réseau.
"""
import argparse
import json
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk", "python"))
from capture_cfg import DEFAULT_CFG, region_in, sanitize  # noqa: E402

CFG_PATH = os.path.join(os.path.expanduser("~"), ".aimlabs", "capture.json")      # ancien emplacement (avant la fenêtre de réglages générique)
KEYS = {"up": (63232, 2490368, 65362), "down": (63233, 2621440, 65364), "left": (63234, 2424832, 65361), "right": (63235, 2555904, 65363)}


def load_cfg(path=CFG_PATH):
    """Cadrage enregistré : un fichier de valeurs de la fenêtre de réglages (entrée « capture ») ou l'ancien fichier de cadrage seul."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return sanitize(d["capture"] if isinstance(d.get("capture"), dict) else d)
    except (OSError, ValueError, AttributeError):
        return None


def initial_config(client, saved=None, flags=None):
    """Configuration de départ : drapeaux > fichier enregistré > état actuel de l'agent > défaut."""
    base = dict(DEFAULT_CFG)
    base.update((client.hello.get("capture") or {}).get("config") or {})
    if saved:
        base.update(saved)
    base.update({k: v for k, v in (flags or {}).items() if v is not None})
    return sanitize(base)


class CapturePage:
    id, title = "capture", "Capture"

    def __init__(self, client, cfg, side_net=256):
        self.c, self.side_net = client, side_net
        self.cfg = sanitize({**cfg, "resize": side_net})
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.sent = None                # dernière configuration appliquée par l'agent
        self.info = {}                  # dernière réponse de l'agent (config, found, error, region, windows, monitors…)
        self.prev = None                # (meta, image BGR)
        self.prev_key = None
        self.prev_t = 0.0
        self.live = None                # dernière image reçue (BGR, ce que verra l'algo)
        self.live_t = 0.0
        self.fps = 0.0
        self._n, self._fid, self._t_fps = 0, 0, time.monotonic()
        self.drag = False
        self.typing = None              # None ou texte en cours de saisie
        self._prev_cache = (None, None, None)
        self._geom = None               # (échelle écran, x, y) de l'aperçu affiché
        self._area = (0, 0, 1, 1)
        self.win = None
        self.cv2 = None

    # ----------------------------------------------------------------- cycle de vie
    def start(self, win):
        import cv2
        self.win, self.cv2 = win, cv2
        threading.Thread(target=self._worker, daemon=True, name="capture-page").start()

    def stop(self):
        self.stopped.set()
        self.wake.set()

    def busy(self):
        return self.sent != self.cfg

    def validate(self):
        if self.cfg["mode"] == "window" and not self.info.get("window"):
            return "Capture : fenêtre « %s » introuvable : choisis-en une dans la liste ou passe en mode écran (m)" % (self.cfg["title"] or "?")
        if self.info.get("found", True) is False:
            return "Capture : " + (self.info.get("error") or "cible introuvable")
        return None

    def value(self):
        return dict(self.cfg)

    # ----------------------------------------------------------------- fil de fond : tout ce qui parle à l'agent
    @staticmethod
    def _ref_key(cfg):
        """Ce qui change la RÉFÉRENCE de l'aperçu (pas le carré, qui se dessine localement)."""
        return (cfg["mode"], cfg["title"], cfg["process"], cfg["monitor"])

    def _worker(self):
        t_info = 0.0
        while not self.stopped.is_set():
            self.wake.wait(0.05)
            self.wake.clear()
            with self.lock:
                want = dict(self.cfg)
            try:
                if want != self.sent:
                    r = self.c.ctrl("set", config=want, timeout=3.0)
                    if r is not None:
                        with self.lock:
                            self.info.update(r[0])
                    self.sent = want
                    if self._ref_key(want) != self.prev_key:
                        self.prev_t = 0.0
                if time.monotonic() - t_info > 2.0:
                    r = self.c.ctrl("get_info", timeout=3.0)
                    if r is not None:
                        with self.lock:
                            self.info.update(r[0])
                    t_info = time.monotonic()
                if time.monotonic() - self.prev_t > 2.0 or self.prev is None:
                    key = self._ref_key(want)
                    r = self.c.ctrl("get_preview", target=want, max_w=1000, timeout=4.0)
                    self.prev_t = time.monotonic()
                    if r is not None:
                        img = self.cv2.imdecode(np.frombuffer(r[1], np.uint8), self.cv2.IMREAD_COLOR) if r[1] else None
                        with self.lock:
                            self.prev, self.prev_key = (r[0], img), key
            except Exception as e:   # noqa: BLE001  une erreur réseau ne doit jamais tuer l'interface
                self.win.say("agent : %s: %s" % (type(e).__name__, e))
                time.sleep(0.3)

    def update(self, **kw):
        with self.lock:
            self.cfg = sanitize({**self.cfg, **kw, "resize": self.side_net})
        self.wake.set()

    # ----------------------------------------------------------------- géométrie
    def ref_rect(self):
        """(rw, rh) de la référence courante, d'après l'aperçu."""
        meta = self.prev[0] if self.prev else None
        if meta and meta.get("ref") and meta.get("cfg") and self._ref_key(meta["cfg"]) == self._ref_key(self.cfg):
            return meta["ref"][2], meta["ref"][3]
        ref = self.info.get("ref")
        return (ref[2], ref[3]) if ref else None

    def square(self):
        r = self.ref_rect()
        if r is None:
            return None
        x, y, s, _ = region_in((0, 0, r[0], r[1]), self.cfg)
        return x, y, s

    # ----------------------------------------------------------------- entrées
    def mouse(self, ev, x, y, flags):
        cv2 = self.cv2
        px, py, pw, ph = self._area
        inside = px <= x < px + pw and py <= y < py + ph
        if ev == cv2.EVENT_LBUTTONDOWN and inside:
            self.drag = True
        elif ev == cv2.EVENT_LBUTTONUP:
            self.drag = False
        if self.drag and self._geom and ev in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_MOUSEMOVE):
            k, gx, gy = self._geom
            r = self.ref_rect()
            if r:
                self.update(dx=round((x - gx) / k - r[0] / 2), dy=round((y - gy) / k - r[1] / 2))

    def key(self, k):
        if self.typing is not None:
            if k in (13, 10):
                self.update(mode="window", title=self.typing)
                self.typing = None
            elif k == 27:
                self.typing = None
            elif k in (8, 127):
                self.typing = self.typing[:-1]
            elif 32 <= k < 127:
                self.typing += chr(k)
            return True
        c = self.cfg
        if k in KEYS["up"]:
            self.update(dy=c["dy"] - 1)
        elif k in KEYS["down"]:
            self.update(dy=c["dy"] + 1)
        elif k in KEYS["left"]:
            self.update(dx=c["dx"] - 1)
        elif k in KEYS["right"]:
            self.update(dx=c["dx"] + 1)
        elif 0 <= k < 256 and chr(k) in "ijkl":
            d = {"i": (0, -10), "k": (0, 10), "j": (-10, 0), "l": (10, 0)}[chr(k)]
            self.update(dx=c["dx"] + d[0], dy=c["dy"] + d[1])
        elif k in (ord("+"), ord("=")):
            self.update(crop=c["crop"] + 16)
        elif k in (ord("-"), ord("_")):
            self.update(crop=c["crop"] - 16)
        elif k == ord("c"):
            self.update(dx=0, dy=0)
        elif k == ord("m"):
            self.set_mode("screen" if c["mode"] == "window" else "window")
        elif k == ord("t"):
            self.typing = c["title"]
        elif k == ord("r"):
            self.prev_t = 0.0
            self.wake.set()
        elif k in (ord("["), ord("]")):
            self.step_window(-1 if k == ord("[") else 1)
        else:
            return False
        return True

    def set_mode(self, mode):
        if mode == "window" and not self.cfg["title"]:
            ws = self.info.get("windows") or []
            if ws:
                self.update(mode="window", title=ws[0]["title"], process=ws[0]["process"])
            else:
                self.typing = ""
            return
        self.update(mode=mode)

    def step_window(self, d):
        ws = self.info.get("windows") or []
        if not ws:
            return
        titles = [w["title"] for w in ws]
        i = titles.index(self.cfg["title"]) if self.cfg["title"] in titles and self.cfg["mode"] == "window" else -1
        w = ws[(i + d) % len(ws)]
        self.update(mode="window", title=w["title"], process=w["process"])

    def pick_window(self, w):
        return lambda: self.update(mode="window", title=w["title"], process=w["process"])

    # ----------------------------------------------------------------- rendu
    def tick(self):
        fr = self.c.crop(timeout=0.0)               # la plus récente seulement (le client ne garde que celle-là)
        if fr is not None:
            self.live, self.live_t = np.ascontiguousarray(fr[3][:, :, ::-1]), time.monotonic()
            self._fid = fr[0]
        now = time.monotonic()
        if now - self._t_fps > 1.0:                 # cadence = images produites par l'agent (leurs numéros), pas celles que cette fenêtre a eu le temps de dessiner
            self.fps, self._n, self._t_fps = (self._fid - self._n) / (now - self._t_fps), self._fid, now

    def render(self, cv, rect):
        from aimlabs.setup import ui
        cv2 = self.cv2 or __import__("cv2")
        x0, y0, w0, h0 = rect
        with self.lock:
            cfg, info, prev, live = dict(self.cfg), dict(self.info), self.prev, self.live
        px, py, pw, ph = x0 + 16, y0 + 40, 850, h0 - 80
        self._area = (px, py, pw, ph)
        cv.text("Le carré vert sera découpé, réduit à %d px et envoyé à l'algo." % self.side_net, px, y0 + 12, 14, ui.DIM)
        cv.fill(px, py, pw, ph, ui.PANEL)
        r = self.ref_rect()
        meta, pimg = prev if prev else ({}, None)
        stale = bool(meta) and self._ref_key(meta.get("cfg", cfg)) != self._ref_key(cfg)
        if r and pimg is not None:
            k = min(pw / r[0], ph / r[1])
            dw, dh = max(1, int(r[0] * k)), max(1, int(r[1] * k))
            gx, gy = px + (pw - dw) // 2, py + (ph - dh) // 2
            if self._prev_cache[0] is not pimg or self._prev_cache[1] != (dw, dh):
                self._prev_cache = (pimg, (dw, dh), cv2.resize(pimg, (dw, dh), interpolation=cv2.INTER_AREA))
            cv.img[gy:gy + dh, gx:gx + dw] = self._prev_cache[2]
            if stale:
                cv.img[gy:gy + dh, gx:gx + dw] = (cv.img[gy:gy + dh, gx:gx + dw] * 0.45).astype(np.uint8)
            self._geom = (k, gx, gy)
            cv2.line(cv.img, (gx + dw // 2, gy), (gx + dw // 2, gy + dh), ui.FAINT, 1)          # centre de la référence
            cv2.line(cv.img, (gx, gy + dh // 2), (gx + dw, gy + dh // 2), ui.FAINT, 1)
            sq = self.square()
            if sq:
                sx, sy, ss = sq
                cv2.rectangle(cv.img, (gx + int(sx * k), gy + int(sy * k)), (gx + int((sx + ss) * k), gy + int((sy + ss) * k)), ui.OK, 2)
            cv.text("référence %d×%d px (échelle %.2f)" % (r[0], r[1], k), px + 8, py + ph - 22, 12, ui.DIM)
        else:
            self._geom = None
            cv.text(meta.get("error") or "en attente de l'aperçu de l'agent…", px + 16, py + 16, 15, ui.WARN)
        cv.frame(px, py, pw, ph, ui.LINE)
        cv.text("Glisser dans l'aperçu = déplacer le carré · flèches 1 px · i j k l 10 px · + / - taille · c centre · m écran/fenêtre · [ ] fenêtre · t motif · r rafraîchir",
                px, py + ph + 10, 12, ui.FAINT, max_w=pw)
        # --- image reçue (ce que verra l'algo)
        rx, box = px + pw + 24, 300
        cv.text("IMAGE REÇUE PAR B — ce que voit l'algo", rx, py - 2, 13, ui.ACCENT, bold=True)
        by = py + 24
        cv.fill(rx, by, box, box, ui.PANEL)
        if live is not None and time.monotonic() - self.live_t < 2.0:
            lh, lw = live.shape[:2]
            kk = min(box / lw, box / lh)
            v = cv2.resize(live, (max(1, int(lw * kk)), max(1, int(lh * kk))), interpolation=cv2.INTER_AREA if kk < 1 else cv2.INTER_NEAREST)
            cv.img[by:by + v.shape[0], rx:rx + v.shape[1]] = v
            cv.text("%d×%d · %.0f img/s" % (lw, lh, self.fps), rx + 6, by + box - 20, 12, ui.WARN, bold=True)
        else:
            cv.text("aucune image (écran immobile ?)", rx + 12, by + 14, 14, ui.WARN)
        cv.frame(rx, by, box, box, ui.LINE)
        # --- configuration
        y = by + box + 14
        mode = "FENÊTRE" if cfg["mode"] == "window" else "ÉCRAN (centre de l'écran)"
        for label, val in (("mode", mode), ("motif", (self.typing + "|  (Entrée = ok)") if self.typing is not None else cfg["title"] or "—"),
                           ("décalage", "dx %+d  dy %+d px" % (cfg["dx"], cfg["dy"])), ("carré", "%d px, réduit à %d px" % (cfg["crop"], self.side_net))):
            cv.text(label, rx, y, 14, ui.DIM)
            cv.text(val, rx + 76, y, 14, ui.WARN if (self.typing is not None and label == "motif") else ui.TXT, max_w=box - 80)
            y += 21
        if info.get("window"):
            cv.text("trouvée : %s [%s]" % (info["window"]["title"], info["window"]["process"]), rx, y, 12, ui.OK, max_w=box)
        elif cfg["mode"] == "window":
            cv.text(info.get("error") or "fenêtre introuvable", rx, y, 12, ui.ERR, max_w=box)
        y += 26
        # --- liste des fenêtres
        cv.text("CADRER SUR (clic) :", rx, y, 13, ui.ACCENT, bold=True)
        y += 22
        items = [("Écran (centre de l'écran)", lambda: self.update(mode="screen"), cfg["mode"] == "screen")]
        shown = (info.get("window") or {}).get("title", cfg["title"])
        for wd in (info.get("windows") or [])[:14]:
            items.append(("%s  [%s]" % (wd["title"], wd["process"].replace(".exe", "")), self.pick_window(wd), cfg["mode"] == "window" and wd["title"] == shown))
        for text, act, on in items:
            if y + 24 > y0 + h0 - 8:
                break
            cv.fill(rx, y, 380, 22, (96, 78, 40) if on else ui.PANEL)
            cv.text(text, rx + 8, y + 3, 13, ui.TXT if on else ui.DIM, max_w=364)
            cv.hit(rx, y, 380, 22, act)
            y += 24


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--connect", default=os.environ.get("AIMLABS_REMOTE", "listen:7777"))
    ap.add_argument("--side", type=int, default=256, help="côté de l'image du réseau de B")
    ap.add_argument("--snap", default="", help="test sans écran : écrit une copie de la fenêtre dans ce PNG et s'arrête")
    ap.add_argument("--window", default=None, help="avec --snap : motif de titre à essayer (mode fenêtre)")
    a = ap.parse_args()
    import aimlabs.setup as S
    from aimlabs.remote import RemoteClient
    with RemoteClient(a.connect, timeout=120) as c:
        if not c.has_ctrl:
            sys.exit("cet agent n'a pas de canal de réglage de la capture")
        cfg = initial_config(c, None if a.snap else load_cfg(), {"mode": "window" if a.window else None, "title": a.window})
        schema = {"setup": 1, "app": "capture", "title": "Capture", "pages": [{"id": "capture", "title": "Capture", "type": "capture"}]}
        vals = S.run(schema, values={}, plugins={"capture": CapturePage(c, cfg, a.side)}, save=not a.snap, snap=a.snap)
        if a.snap:
            print("écrit", a.snap)
        elif vals:
            print("réglage validé :", vals["capture"])


if __name__ == "__main__":
    main()
