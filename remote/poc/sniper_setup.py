"""Fenêtre de réglage du cadrage de la capture, côté B : voir l'image qui part vers l'algo, choisir la fenêtre (ou l'écran), recadrer à la main.

    python sniper_b.py --connect listen:7777 --setup          # réglage puis l'algo tourne normalement (Entrée pour valider)
    python sniper_setup.py --connect listen:7777              # réglage seul (agent_a se connecte à B comme d'habitude)

Disposition : à gauche l'aperçu de la RÉFÉRENCE (tout l'écran, ou toute la fenêtre choisie) avec le carré qui sera découpé ; à droite l'image réellement
reçue par B (donc ce que l'algo verra), la configuration et la liste des fenêtres du PC A.

  souris : glisser sur l'aperçu = déplacer le carré · clic sur une fenêtre de la liste = la choisir · clic sur « Écran » = cadrer sur l'écran
  flèches / i j k l : déplacer d'1 px / de 10 px · + / - : taille du carré · c : recentrer · m : écran ⇄ fenêtre · [ ] : fenêtre précédente / suivante
  t : saisir le motif du titre (Entrée ferme la saisie) · r : rafraîchir · Entrée : VALIDER (écrit ~/.aimlabs/capture.json) · Échap : annuler

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
from sniper_view import BG, CYAN, DIM, GRAY, GREEN, GRID, ORANGE, PANEL, RED, WHITE, YELLOW, put  # noqa: E402

CFG_PATH = os.path.join(os.path.expanduser("~"), ".aimlabs", "capture.json")
W_UI, H_UI = 1320, 780
PREV = (12, 56, 900, 620)           # zone de l'aperçu : x, y, largeur, hauteur
KEYS = {"up": (63232, 2490368, 65362), "down": (63233, 2621440, 65364), "left": (63234, 2424832, 65361), "right": (63235, 2555904, 65363)}


def load_cfg(path=CFG_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return sanitize(json.load(f))
    except (OSError, ValueError):
        return None


def save_cfg(cfg, path=CFG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


class Setup:
    def __init__(self, client, cfg, side_net=256, cv2=None, name="Reglage de la capture"):
        import cv2 as _cv2
        self.cv2 = cv2 or _cv2
        self.c, self.side_net, self.name = client, side_net, name
        self.cfg = sanitize({**cfg, "resize": side_net})
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
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
        self.msg, self.msg_t = "", 0.0
        self.hits = []                  # zones cliquables de la liste : (x0, y0, x1, y1, action)
        self._prev_cache = (None, None, None)
        self._geom = None               # (échelle écran, x, y) de l'aperçu affiché
        self.net_ms = 0.0

    # ----------------------------------------------------------------- fil de fond : tout ce qui parle à l'agent
    def _worker(self):
        t_info = 0.0
        while not self.stop.is_set():
            self.wake.wait(0.05)
            self.wake.clear()
            with self.lock:
                want = dict(self.cfg)
            try:
                if want != self.sent:
                    t0 = time.perf_counter()
                    r = self.c.ctrl("set", config=want, timeout=3.0)
                    if r is not None:
                        with self.lock:
                            self.info.update(r[0])
                            self.net_ms = (time.perf_counter() - t0) * 1e3
                    self.sent = want
                    self.prev_t = 0.0 if self._ref_key(want) != self.prev_key else self.prev_t
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
                    if r is not None and r[1]:
                        img = self.cv2.imdecode(np.frombuffer(r[1], np.uint8), self.cv2.IMREAD_COLOR)
                        with self.lock:
                            self.prev, self.prev_key = (r[0], img), key
                    elif r is not None:
                        with self.lock:
                            self.prev, self.prev_key = (r[0], None), key
            except Exception as e:   # noqa: BLE001  une erreur réseau ne doit jamais tuer l'interface
                self.say(f"agent : {type(e).__name__}: {e}")
                time.sleep(0.3)

    @staticmethod
    def _ref_key(cfg):
        """Ce qui change la RÉFÉRENCE de l'aperçu (pas le carré, qui se dessine localement)."""
        return (cfg["mode"], cfg["title"], cfg["process"], cfg["monitor"])

    def say(self, text):
        self.msg, self.msg_t = text, time.monotonic()

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
    def mouse(self, ev, x, y, flags, _p):
        cv2 = self.cv2
        if ev == cv2.EVENT_LBUTTONDOWN:
            for x0, y0, x1, y1, act in self.hits:
                if x0 <= x < x1 and y0 <= y < y1:
                    act()
                    return
        inside = PREV[0] <= x < PREV[0] + PREV[2] and PREV[1] <= y < PREV[1] + PREV[3]
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
        if k < 0:
            return None
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
            return None
        c = self.cfg
        step = 1
        if k in KEYS["up"]:
            self.update(dy=c["dy"] - step)
        elif k in KEYS["down"]:
            self.update(dy=c["dy"] + step)
        elif k in KEYS["left"]:
            self.update(dx=c["dx"] - step)
        elif k in KEYS["right"]:
            self.update(dx=c["dx"] + step)
        elif k < 256 and chr(k) in "ijkl":
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
        elif k in (13, 10):
            return "ok"
        elif k == 27:
            return "cancel"
        return None

    def set_mode(self, mode):
        if mode == "window" and not self.cfg["title"]:
            ws = self.info.get("windows") or []
            if ws:
                self.update(mode="window", title=ws[0]["title"], process=ws[0]["process"])
                return
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
    def render(self):
        cv2 = self.cv2
        img = np.empty((H_UI, W_UI, 3), np.uint8)
        img[:] = BG
        self.hits = []
        with self.lock:
            cfg, info, prev, live = dict(self.cfg), dict(self.info), self.prev, self.live
        put(cv2, img, "REGLAGE DE LA CAPTURE (PC A)", 12, 24, WHITE, 0.7, 2)
        put(cv2, img, "Le carre vert sera decoupe, reduit a %d px et envoye a l'algo. Entree = valider · Echap = annuler" % self.side_net, 12, 44, GRAY, 0.45)
        # --- aperçu de la référence
        px, py, pw, ph = PREV
        cv2.rectangle(img, (px, py), (px + pw, py + ph), PANEL, -1)
        r = self.ref_rect()
        meta, pimg = prev if prev else ({}, None)
        stale = bool(meta) and self._ref_key(meta.get("cfg", cfg)) != self._ref_key(cfg)
        if r and pimg is not None:
            k = min(pw / r[0], ph / r[1])
            dw, dh = max(1, int(r[0] * k)), max(1, int(r[1] * k))
            gx, gy = px + (pw - dw) // 2, py + (ph - dh) // 2
            if self._prev_cache[0] is not pimg or self._prev_cache[1] != (dw, dh):
                self._prev_cache = (pimg, (dw, dh), cv2.resize(pimg, (dw, dh), interpolation=cv2.INTER_AREA))
            img[gy:gy + dh, gx:gx + dw] = self._prev_cache[2]
            if stale:
                img[gy:gy + dh, gx:gx + dw] = (img[gy:gy + dh, gx:gx + dw] * 0.45).astype(np.uint8)
            self._geom = (k, gx, gy)
            cv2.line(img, (gx + dw // 2, gy), (gx + dw // 2, gy + dh), DIM, 1)          # centre de la référence
            cv2.line(img, (gx, gy + dh // 2), (gx + dw, gy + dh // 2), DIM, 1)
            sq = self.square()
            if sq:
                sx, sy, ss = sq
                cv2.rectangle(img, (gx + int(sx * k), gy + int(sy * k)), (gx + int((sx + ss) * k), gy + int((sy + ss) * k)), GREEN, 2)
            put(cv2, img, "reference %dx%d px (echelle %.2f)" % (r[0], r[1], k), px + 6, py + ph - 8, GRAY, 0.42)
        else:
            self._geom = None
            put(cv2, img, meta.get("error") or "en attente de l'apercu de l'agent...", px + 14, py + 30, ORANGE, 0.55)
        cv2.rectangle(img, (px, py), (px + pw, py + ph), GRID, 1)
        # --- image reçue (ce que verra l'algo)
        rx = 930
        put(cv2, img, "IMAGE RECUE PAR B (ce que voit l'algo)", rx, 76, CYAN, 0.48)
        box = 300
        cv2.rectangle(img, (rx, 86), (rx + box, 86 + box), PANEL, -1)
        if live is not None and time.monotonic() - self.live_t < 2.0:
            lh, lw = live.shape[:2]
            kk = min(box / lw, box / lh)
            v = cv2.resize(live, (max(1, int(lw * kk)), max(1, int(lh * kk))), interpolation=cv2.INTER_AREA if kk < 1 else cv2.INTER_NEAREST)
            img[86:86 + v.shape[0], rx:rx + v.shape[1]] = v
            put(cv2, img, "%dx%d · %.0f img/s" % (lw, lh, self.fps), rx + 4, 86 + box - 6, YELLOW, 0.45)
        else:
            put(cv2, img, "aucune image (ecran immobile ?)", rx + 10, 86 + 24, ORANGE, 0.45)
        cv2.rectangle(img, (rx, 86), (rx + box, 86 + box), GRID, 1)
        # --- configuration
        y = 86 + box + 28
        for line, col in [
            ("mode      : %s" % ("FENETRE" if cfg["mode"] == "window" else "ECRAN (centre de l'ecran)"), WHITE),
            ("motif     : %s%s" % (cfg["title"] or "-", "_" if self.typing is not None else ""), WHITE),
            ("decalage  : dx %+d  dy %+d px" % (cfg["dx"], cfg["dy"]), WHITE),
            ("carre     : %d px -> %d px" % (cfg["crop"], self.side_net), WHITE),
        ]:
            if self.typing is not None and line.startswith("motif"):
                line, col = "motif     : %s_   (Entree = ok)" % self.typing, YELLOW
            put(cv2, img, line, rx, y, col, 0.48)
            y += 20
        if info.get("window"):
            put(cv2, img, "trouvee : %s [%s]" % (info["window"]["title"][:34], info["window"]["process"]), rx, y, GREEN, 0.42)
        elif cfg["mode"] == "window":
            put(cv2, img, info.get("error") or "fenetre introuvable", rx, y, RED, 0.45)
        y += 26
        # --- liste des fenêtres
        put(cv2, img, "CADRER SUR (clic) :", rx, y, CYAN, 0.48)
        y += 6
        items = [("Ecran (centre de l'ecran)", lambda: self.update(mode="screen"), cfg["mode"] == "screen")]
        for w in (info.get("windows") or [])[:14]:
            items.append(("%s [%s]" % (w["title"], w["process"].replace(".exe", "")), self.pick_window(w), cfg["mode"] == "window" and w["title"] == (info.get("window") or {}).get("title", cfg["title"])))
        for text, act, on in items:
            if y + 22 > H_UI - 70:
                break
            cv2.rectangle(img, (rx - 4, y + 2), (W_UI - 8, y + 20), (60, 90, 60) if on else PANEL, -1)
            put(cv2, img, text[:46], rx, y + 16, WHITE if on else GRAY, 0.43)
            self.hits.append((rx - 4, y + 2, W_UI - 8, y + 20, act))
            y += 22
        # --- pied
        lag = ""
        if info.get("found") is False:
            lag = "  ·  " + (info.get("error") or "cible introuvable")
        put(cv2, img, "Agent : %s%s" % (info.get("backend") or "?", lag), 12, H_UI - 40, RED if lag else GRAY, 0.45)
        put(cv2, img, "fleches 1 px · i j k l 10 px · + - taille · c centre · m ecran/fenetre · [ ] fenetre · t motif · r rafraichir · glisser = deplacer",
            12, H_UI - 16, DIM, 0.42)
        if self.msg and time.monotonic() - self.msg_t < 4.0:
            put(cv2, img, self.msg, 12, H_UI - 62, ORANGE, 0.5)
        return img

    # ----------------------------------------------------------------- boucle
    def run(self):
        cv2 = self.cv2
        threading.Thread(target=self._worker, daemon=True).start()
        cv2.namedWindow(self.name, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(self.name, self.mouse)
        poll = cv2.waitKeyEx
        result = None
        try:
            while result is None:
                fr = self.c.crop(timeout=0.0)               # la plus récente seulement (le client ne garde que celle-là)
                if fr is not None:
                    self.live, self.live_t = np.ascontiguousarray(fr[3][:, :, ::-1]), time.monotonic()
                    self._fid = fr[0]
                now = time.monotonic()
                if now - self._t_fps > 1.0:         # cadence = images produites par l'agent (leurs numéros), pas celles que cette fenêtre a eu le temps de dessiner
                    self.fps, self._n, self._t_fps = (self._fid - self._n) / (now - self._t_fps), self._fid, now
                cv2.imshow(self.name, self.render())
                k = poll(30)
                act = self.key(k)
                if act == "ok":
                    if self.info.get("found", True) is False or (self.cfg["mode"] == "window" and not self.info.get("window")):
                        self.say("cible introuvable : choisis une fenetre de la liste ou passe en mode ecran (m)")
                    elif self.sent != self.cfg:
                        self.say("application en cours, reessaie dans un instant")
                    else:
                        result = ("ok", dict(self.cfg))
                elif act == "cancel":
                    result = ("cancel", None)
                if self.c._closed:
                    result = ("closed", None)
        finally:
            self.stop.set()
            cv2.destroyWindow(self.name)
            for _ in range(4):
                cv2.waitKey(1)
        return result


def run_setup(client, cfg, side_net=256):
    """Ouvre la fenêtre de réglage ; retourne la configuration validée (déjà appliquée par l'agent), ou None si annulée."""
    st, c = Setup(client, cfg, side_net).run()
    if st == "ok":
        save_cfg(c)
        print(f"réglage de la capture validé et enregistré dans {CFG_PATH} : {c}", flush=True)
        return c
    print("réglage de la capture " + ("annulé : la configuration de l'agent est conservée" if st == "cancel" else "interrompu (agent déconnecté)"), flush=True)
    return None


def initial_config(client, saved=None, flags=None):
    """Configuration de départ : drapeaux > fichier enregistré > état actuel de l'agent > défaut."""
    base = dict(DEFAULT_CFG)
    base.update((client.hello.get("capture") or {}).get("config") or {})
    if saved:
        base.update(saved)
    base.update({k: v for k, v in (flags or {}).items() if v is not None})
    return sanitize(base)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--connect", default=os.environ.get("AIMLABS_REMOTE", "listen:7777"))
    ap.add_argument("--side", type=int, default=256, help="côté de l'image du réseau de B")
    ap.add_argument("--snap", default="", help="test sans écran : applique la configuration de départ, écrit une copie de la fenêtre dans ce PNG et s'arrête")
    ap.add_argument("--window", default=None, help="avec --snap : motif de titre à essayer (mode fenêtre)")
    a = ap.parse_args()
    from aimlabs.remote import RemoteClient
    with RemoteClient(a.connect, timeout=120) as c:
        if not c.has_ctrl:
            sys.exit("cet agent n'a pas de canal de réglage de la capture")
        if a.snap:
            import cv2
            st = Setup(c, initial_config(c, None, {"mode": "window" if a.window else None, "title": a.window}), a.side)
            threading.Thread(target=st._worker, daemon=True).start()
            t_end = time.monotonic() + 6
            while time.monotonic() < t_end:
                fr = c.crop(timeout=0.2)
                if fr is not None:
                    st.live, st.live_t, st._fid = np.ascontiguousarray(fr[3][:, :, ::-1]), time.monotonic(), fr[0]
                if st.prev is not None and st.sent == st.cfg and st.live is not None and time.monotonic() > t_end - 4.5:
                    break
            st.fps = 0.0
            cv2.imwrite(a.snap, st.render())
            print("écrit", a.snap)
            st.stop.set()
            return
        run_setup(c, initial_config(c, load_cfg()), a.side)


if __name__ == "__main__":
    main()
