"""Fenêtre de réglages (OpenCV + Pillow pour le texte) : pages à onglets, champs générés depuis le schéma (voir aimlabs/setup/__init__.py), pages actives fournies par l'hôte.

Page active (`plugins={"type": objet}`) : un objet qui sait se dessiner dans la zone de contenu ; interface (tous les membres sont facultatifs sauf `render`) :

    title: str                             titre de l'onglet (défaut : celui du schéma)
    start(win) / stop()                    avant la première image / à la fermeture (fils de fond, connexions)
    tick()                                 une fois par image, avant `render` (jamais bloquant)
    render(cv, rect)                       dessine dans rect = (x, y, w, h) avec le `Canvas` cv (cv.fill / cv.frame / cv.text / cv.button / cv.hit, cv.img = pixels BGR)
    mouse(ev, x, y, flags)                 évènements souris (cv2.EVENT_*) en coordonnées de la fenêtre
    key(k) -> bool                         touche du clavier de CETTE machine ; True si la page l'a utilisée
    value() -> dict                        ce que la page ajoute aux valeurs (clé = id de la page)
    validate() -> str | None               message d'erreur qui BLOQUE la validation (ex. cible introuvable)
    busy() -> bool                         vrai tant qu'un changement n'est pas encore appliqué (la validation attend)

Le texte est dessiné avec Pillow (accents, anti-crénelage) si disponible, sinon avec la police Hershey d'OpenCV (accents retirés).
"""
import os
import sys
import time
import unicodedata

import numpy as np

from . import KEY_MODES, coerce  # noqa: E402

W, H = 1280, 800
HEAD, TABS, FOOT = 64, 44, 76
BG, PANEL, PANEL2, LINE = (28, 25, 24), (44, 40, 38), (58, 53, 50), (78, 72, 68)
TXT, DIM, FAINT = (240, 238, 236), (176, 170, 165), (122, 116, 112)
ACCENT, ACCENT_D, OK, WARN, ERR = (255, 160, 70), (160, 100, 40), (110, 210, 120), (60, 170, 255), (90, 90, 240)

KEY_CODES = {"up": (63232, 2490368, 65362), "down": (63233, 2621440, 65364), "left": (63234, 2424832, 65361), "right": (63235, 2555904, 65363)}
MODE_LABELS = {"toggle": "Bascule", "hold": "Maintien", "press": "Impulsion"}
MODE_HELP = {"toggle": "chaque appui inverse l'état", "hold": "actif tant que la touche est enfoncée", "press": "une impulsion par appui"}


def _ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


# ---------------------------------------------------------------------------------------------------- dessin
class Canvas:
    """Image BGR + liste de textes dessinés à la fin (une seule conversion vers Pillow par image) + zones cliquables."""

    FONTS = {"win32": ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"],
             "darwin": ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/HelveticaNeue.ttc", "/Library/Fonts/Arial.ttf"],
             "linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]}
    BOLD = {"win32": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"], "darwin": ["/System/Library/Fonts/Helvetica.ttc"],
            "linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]}

    def __init__(self, cv2):
        self.cv2 = cv2
        self.img = np.zeros((H, W, 3), np.uint8)
        self.texts, self.hits = [], []
        self.mouse = (-1, -1)
        self._fonts = {}
        try:
            from PIL import Image, ImageDraw, ImageFont
            self.pil = (Image, ImageDraw, ImageFont)
        except ImportError:
            self.pil = None

    def font(self, size, bold):
        if self.pil is None:
            return None
        k = (size, bold)
        if k not in self._fonts:
            f = None
            plat = "linux" if sys.platform.startswith("linux") else sys.platform
            for path in (self.BOLD if bold else self.FONTS).get(plat, []):
                try:
                    f = self.pil[2].truetype(path, size, index=1 if bold and path.endswith(".ttc") else 0)
                    break
                except (OSError, ValueError):
                    try:
                        f = self.pil[2].truetype(path, size)
                        break
                    except OSError:
                        continue
            self._fonts[k] = f
        return self._fonts[k]

    def begin(self):
        self.img[:] = BG
        self.texts.clear()
        self.hits.clear()

    def measure(self, s, size=15, bold=False):
        f = self.font(size, bold)
        if f is not None:
            return int(f.getlength(s)) if hasattr(f, "getlength") else f.getsize(s)[0]
        return self.cv2.getTextSize(_ascii(s), self.cv2.FONT_HERSHEY_SIMPLEX, size / 30.0, 1)[0][0]

    def fit(self, s, max_w, size=15, bold=False):
        if max_w is None or self.measure(s, size, bold) <= max_w:
            return s
        while s and self.measure(s + "…", size, bold) > max_w:
            s = s[:-1]
        return s + "…"

    def wrap(self, s, max_w, size=13, lines=2):
        words, out, cur = s.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if self.measure(t, size) <= max_w or not cur:
                cur = t
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
        if len(out) > lines:
            out = out[:lines]
            out[-1] = self.fit(out[-1] + " …", max_w, size)
        return out

    def fill(self, x, y, w, h, color):
        self.cv2.rectangle(self.img, (int(x), int(y)), (int(x + w - 1), int(y + h - 1)), color, -1)

    def frame(self, x, y, w, h, color, t=1):
        self.cv2.rectangle(self.img, (int(x), int(y)), (int(x + w - 1), int(y + h - 1)), color, t)

    def text(self, s, x, y, size=15, color=TXT, bold=False, align="l", max_w=None):
        s = self.fit(s, max_w, size, bold)
        w = self.measure(s, size, bold)
        x = x - w if align == "r" else x - w // 2 if align == "c" else x
        self.texts.append((s, int(x), int(y), size, color, bold))
        return w

    def hit(self, x, y, w, h, fn):
        self.hits.append((x, y, x + w, y + h, fn))

    def hovered(self, x, y, w, h):
        return x <= self.mouse[0] < x + w and y <= self.mouse[1] < y + h

    def button(self, label, x, y, w, h, fn=None, on=False, primary=False, disabled=False, size=14):
        hov = self.hovered(x, y, w, h) and not disabled
        fill = ACCENT if (primary and not disabled) else (ACCENT_D if on else (PANEL2 if hov else PANEL))
        if primary and hov:
            fill = (255, 185, 110)
        self.fill(x, y, w, h, fill)
        self.frame(x, y, w, h, ACCENT if on else LINE)
        col = (30, 24, 20) if primary and not disabled else (FAINT if disabled else TXT)
        self.text(label, x + w // 2, y + (h - size - 4) // 2, size, col, bold=on or primary, align="c", max_w=w - 8)
        if fn is not None and not disabled:
            self.hit(x, y, w, h, fn)

    def finish(self):
        """Applique les textes et retourne l'image BGR finale."""
        if self.pil is not None and self.font(15, False) is not None:
            Image, ImageDraw, _ = self.pil
            im = Image.fromarray(self.img[:, :, ::-1])
            d = ImageDraw.Draw(im)
            for s, x, y, size, color, bold in self.texts:
                d.text((x, y), s, font=self.font(size, bold), fill=color[::-1])
            return np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
        for s, x, y, size, color, bold in self.texts:
            self.cv2.putText(self.img, _ascii(s), (x, y + size), self.cv2.FONT_HERSHEY_SIMPLEX, size / 30.0, color, 2 if bold else 1, self.cv2.LINE_AA)
        return self.img


# ---------------------------------------------------------------------------------------------------- page de champs
class FormPage:
    ROW = 86

    def __init__(self, spec, values, win):
        self.spec, self.win = spec, win
        self.title = spec.get("title", spec["id"])
        self.id = spec["id"]
        self.fields = spec.get("fields", [])
        self.vals = {f["id"]: values[f["id"]] for f in self.fields if f["type"] != "label" and f["id"] in values}
        self.edit = None            # (id, tampon)
        self.listen = None          # id du champ en attente d'une touche
        self.scroll = 0
        self.notes = {}             # id -> (texte, couleur) sous le champ

    # -- valeurs
    def value(self):
        return {}                   # les valeurs des champs sont collectées à plat par la fenêtre

    def set(self, fid, v):
        self.vals[fid] = v

    def validate(self):
        for f in self.fields:
            if f["type"] == "key_out" and not self.vals.get(f["id"]):
                return "« %s » : choisis une touche" % f.get("label", f["id"])
            if f["type"] == "key_out" and not self.win.out_ok(f, self.vals[f["id"]]):
                return "« %s » : la touche %s n'est pas autorisée" % (f.get("label", f["id"]), self.vals[f["id"]])
        return None

    # -- clavier de cette machine
    def key(self, k):
        if self.edit is None:
            return False
        fid, buf = self.edit
        f = next(f for f in self.fields if f["id"] == fid)
        if k in (13, 10):
            self.commit(f, buf)
        elif k == 27:
            self.edit = None
        elif k in (8, 127):
            self.edit = (fid, buf[:-1])
        elif 32 <= k < 127:
            self.edit = (fid, buf + chr(k))
        return True

    def commit(self, f, buf):
        self.edit = None
        if f["type"] in ("int", "float"):
            try:
                float(buf)
            except ValueError:
                return                                          # tampon vide ou invalide : on garde la valeur
        self.vals[f["id"]] = coerce(f, buf)

    # -- dessin
    def render(self, cv, rect):
        x0, y0, w, h = rect
        lx, lw = x0 + 36, 560
        wx = x0 + 660
        y = y0 + 14 - self.scroll
        names = {}
        for f in self.fields:                       # touches déjà prises, pour signaler les doublons
            if f["type"] == "label":
                continue
            v = self.vals.get(f["id"])
            key = v.get("key") if f["type"] == "key_in" and isinstance(v, dict) else v if f["type"] == "key_out" else None
            if key:
                names.setdefault(key, []).append(f.get("label", f["id"]))
        for f in self.fields:
            t = f["type"]
            if t == "label":
                for line in cv.wrap(f.get("text", ""), w - 72, 14, 3):
                    cv.text(line, lx, y + 4, 14, DIM)
                    y += 22
                y += 8
                continue
            if y + self.ROW > y0 and y < y0 + h:
                self._row(cv, f, lx, lw, wx, y, names)
            y += self.ROW
        total = y + self.scroll - y0
        self.max_scroll = max(0, total - h + 8)
        self.scroll = max(0, min(self.scroll, self.max_scroll))

    def _row(self, cv, f, lx, lw, wx, y, names):
        fid, t = f["id"], f["type"]
        v = self.vals.get(fid)
        cv.frame(lx - 16, y - 4, W - 40, self.ROW - 8, (46, 42, 40))
        cv.text(f.get("label", fid), lx, y + 6, 16, TXT, bold=True, max_w=lw)
        for i, line in enumerate(cv.wrap(f.get("help", ""), lw, 13, 2)):
            cv.text(line, lx, y + 30 + i * 18, 13, DIM)
        cy = y + 10
        note = self.notes.pop(fid, None)
        if t == "bool":
            on = bool(v)
            cv.fill(wx, cy, 56, 28, ACCENT if on else PANEL2)
            cv.frame(wx, cy, 56, 28, LINE)
            cv.fill(wx + (30 if on else 4), cy + 4, 22, 20, TXT)
            cv.hit(wx, cy, 56, 28, lambda f=f: self.set(f["id"], not self.vals.get(f["id"])))
            cv.text("activé" if on else "désactivé", wx + 70, cy + 5, 14, TXT if on else DIM)
        elif t in ("int", "float"):
            step = f.get("step", 1)
            dec = lambda f=f, s=step: self.set(f["id"], self._num(f, self.vals[f["id"]] - s))
            inc = lambda f=f, s=step: self.set(f["id"], self._num(f, self.vals[f["id"]] + s))
            cv.button("-", wx, cy, 36, 32, dec)
            editing = self.edit is not None and self.edit[0] == fid
            shown = ((self.edit[1] or "%g" % v) + "|") if editing else ("%g" % v)        # tampon vide : l'ancienne valeur reste lisible, la frappe la remplace
            cv.fill(wx + 42, cy, 130, 32, PANEL2 if editing else PANEL)
            cv.frame(wx + 42, cy, 130, 32, ACCENT if editing else LINE)
            cv.text(shown, wx + 107, cy + 6, 16, TXT, align="c", max_w=120)
            cv.hit(wx + 42, cy, 130, 32, lambda fid=fid: self._start_edit(fid, ""))
            cv.button("+", wx + 178, cy, 36, 32, inc)
            if f.get("unit"):
                cv.text(f["unit"], wx + 226, cy + 7, 14, DIM)
            if "min" in f and "max" in f:
                cv.text("%g – %g" % (f["min"], f["max"]), wx + 226 + (cv.measure(f.get("unit", ""), 14) + 12 if f.get("unit") else 0), cy + 8, 12, FAINT)
        elif t == "text":
            editing = self.edit is not None and self.edit[0] == fid
            cv.fill(wx, cy, 380, 32, PANEL2 if editing else PANEL)
            cv.frame(wx, cy, 380, 32, ACCENT if editing else LINE)
            cv.text((self.edit[1] + "|") if editing else str(v), wx + 10, cy + 6, 15, TXT, max_w=360)
            cv.hit(wx, cy, 380, 32, lambda fid=fid, v=v: self._start_edit(fid, str(v)))
        elif t == "choice":
            opts = [(o["value"], o.get("label", str(o["value"]))) if isinstance(o, dict) else (o, str(o)) for o in f["options"]]
            bw = min(130, 560 // max(1, len(opts)))
            for i, (val, label) in enumerate(opts):
                cv.button(label, wx + i * (bw + 6), cy, bw, 32, lambda f=f, val=val: self.set(f["id"], val), on=(v == val))
        elif t in ("key_in", "key_out"):
            key = v.get("key") if t == "key_in" else v
            listening = self.listen == fid
            label = "Appuie sur une touche…" if listening else (key or "— non assignée —")
            cv.fill(wx, cy, 190, 32, ACCENT_D if listening else PANEL)
            cv.frame(wx, cy, 190, 32, ACCENT if listening else LINE)
            cv.text(label, wx + 95, cy + 6, 15, TXT if key or listening else FAINT, bold=bool(key) and not listening, align="c", max_w=180)
            cv.hit(wx, cy, 190, 32, lambda f=f: self.win.listen_for(self, f))
            if t == "key_in":
                cv.button("Effacer", wx + 196, cy, 78, 32, lambda f=f: self.set(f["id"], dict(self.vals[f["id"]], key=None)), disabled=not key)
                modes = f.get("modes", list(KEY_MODES))
                if len(modes) > 1:
                    for i, m in enumerate(modes):
                        cv.button(MODE_LABELS[m], wx + 292 + i * 92, cy, 88, 32, lambda f=f, m=m: self.set(f["id"], dict(self.vals[f["id"]], mode=m)), on=(v.get("mode") == m))
                else:
                    cv.text(MODE_LABELS[modes[0]], wx + 292, cy + 7, 14, DIM)
                mode = v.get("mode")
                if mode in MODE_HELP and note is None:
                    note = ("%s : %s" % (MODE_LABELS[mode], MODE_HELP[mode]), FAINT)
                if self.win.keys is not None:           # sans Pi, rien ne peut empêcher une touche d'arriver au PC
                    sw = bool(v.get("swallow"))
                    cv.button("Bloquée côté PC" if sw else "Transmise au PC", wx + 292, cy + 38, 170, 24,
                              lambda f=f: self.set(f["id"], dict(self.vals[f["id"]], swallow=not self.vals[f["id"]].get("swallow"))), on=sw, size=12)
                    cv.text("n'atteint pas le jeu" if sw else "agit aussi dans le jeu", wx + 470, cy + 42, 11, FAINT, max_w=130)
            else:
                ok = self.win.out_ok(f, key) if key else True
                if not ok:
                    note = ("cette touche n'est pas dans la liste autorisée par le Pi", ERR)
            if key and len(names.get(key, [])) > 1:
                others = [n for n in names[key] if n != f.get("label", fid)]
                note = ("même touche que « %s »" % (others[0] if others else "?"), WARN)
        if note:
            cv.text(note[0], wx, cy + 40, 12, note[1], max_w=280 if (t == "key_in" and self.win.keys is not None) else 580)

    def _num(self, f, x):
        return coerce(f, round(x, 6))

    def _start_edit(self, fid, buf):
        self.edit = (fid, buf)
        self.listen = None

    def mouse(self, ev, x, y, flags):
        import cv2
        if ev == cv2.EVENT_MOUSEWHEEL:
            self.scroll = max(0, min(getattr(self, "max_scroll", 0), self.scroll - (1 if flags > 0 else -1) * 40))


class MissingPage:
    def __init__(self, spec):
        self.title, self.id = spec.get("title", spec["id"]), spec["id"]
        self.kind = spec.get("type")

    def render(self, cv, rect):
        x, y, w, h = rect
        cv.text("Page « %s » non disponible" % self.kind, x + 36, y + 30, 18, WARN, bold=True)
        cv.text("Le programme qui a ouvert cette fenêtre ne fournit pas ce type de page.", x + 36, y + 62, 14, DIM)


# ---------------------------------------------------------------------------------------------------- fenêtre
class SetupWindow:
    def __init__(self, schema, values, keys=None, plugins=None):
        import cv2
        self.cv2 = cv2
        self.schema, self.keys, self.msg, self.msg_t = schema, keys, "", 0.0
        self.cv = Canvas(cv2)
        self.name = "AimLabs - Reglages : %s" % _ascii(schema.get("title", schema["app"]))      # titre de fenêtre : ASCII (Cocoa déforme le reste)
        self.values = dict(values)
        self.pages = []
        for p in schema["pages"]:
            if p.get("type") in (None, "form"):
                self.pages.append(FormPage(p, values, self))
            else:
                page = (plugins or {}).get(p["type"]) or MissingPage(p)
                page.id = getattr(page, "id", p["id"])
                if not getattr(page, "title", None):
                    page.title = p.get("title", p["id"])
                self.pages.append(page)
        self.tab = 0
        self.listening = None       # (page, champ)
        self._prev_all = None
        self.out_allowed = None
        if keys is not None:
            try:
                self.out_allowed = keys.allowed_output_keys(1.0)
            except Exception:       # noqa: BLE001
                self.out_allowed = None
            keys.add_listener(self._on_pi_key)
        self.result = None
        self._started = False

    # -- touches : assignation en appuyant la touche (clavier du Pi) ou, à défaut, au clavier de cette machine
    def out_ok(self, f, key):
        allowed = f.get("allowed")
        if allowed is None:
            allowed = self.out_allowed
        return True if allowed is None else str(key).upper() in {a.upper() for a in allowed}

    def listen_for(self, page, f):
        if self.listening and self.listening[1]["id"] == f["id"]:
            return self._stop_listen()
        self._stop_listen()
        page.edit = None
        self.listening = (page, f)
        page.listen = f["id"]
        if self.keys is not None:
            self._prev_all = getattr(self.keys, "_all", False)
            self.keys.set(all_keys=True, swallow_all=True)      # pendant l'assignation, aucune touche ne part vers le PC
        self.say("Appuie sur la touche voulue" + (" (clavier du joueur)" if self.keys is not None else " (clavier de cette machine)") + ". Clique de nouveau pour annuler.")

    def _stop_listen(self):
        if self.listening:
            self.listening[0].listen = None
            self.listening = None
            if self.keys is not None:
                self.keys.set(all_keys=bool(self._prev_all), swallow_all=False)

    def _assign(self, name):
        if not self.listening:
            return
        page, f = self.listening
        if f["type"] == "key_out" and not self.out_ok(f, name):
            self.say("« %s » n'est pas dans la liste des touches que le Pi accepte d'injecter." % name)
            return
        if f["type"] == "key_in":
            page.vals[f["id"]] = dict(page.vals[f["id"]], key=name)
        else:
            page.vals[f["id"]] = name
        self._stop_listen()
        self.say("")

    def _on_pi_key(self, name, down):
        if down and self.listening:
            self._assign(name)

    def say(self, text, secs=6.0):
        self.msg, self.msg_t = text, time.monotonic() + secs

    # -- cycle de vie
    def _start(self):
        if self._started:
            return
        self._started = True
        for p in self.pages:
            if hasattr(p, "start"):
                p.start(self)

    def _stop(self):
        self._stop_listen()
        for p in self.pages:
            if hasattr(p, "stop"):
                p.stop()
        if self.keys is not None and hasattr(self.keys, "remove_listener"):
            self.keys.remove_listener(self._on_pi_key)

    def collect(self):
        out = dict(self.values)
        for p in self.pages:
            if isinstance(p, FormPage):
                out.update(p.vals)
            elif hasattr(p, "value"):
                out[p.id] = p.value()
        return out

    def _validate(self):
        for i, p in enumerate(self.pages):
            if hasattr(p, "busy") and p.busy():
                return "application en cours, réessaie dans un instant"
            err = p.validate() if hasattr(p, "validate") else None
            if err:
                self.tab = i
                return err
        return None

    def _ok(self):
        err = self._validate()
        if err:
            self.say(err)
        else:
            self.result = self.collect()

    def _cancel(self):
        self.result = False

    # -- rendu
    def render(self):
        cv = self.cv
        cv.begin()
        cv.fill(0, 0, W, HEAD, PANEL)
        cv.text(self.schema.get("title", self.schema["app"]), 36, 14, 24, TXT, bold=True)
        cv.text("Réglages avant le lancement · distinct de la fenêtre de debug · rien ne tourne ici pendant que l'algorithme travaille", 36, 44, 12, FAINT)
        x = 24
        for i, p in enumerate(self.pages):
            w = max(120, cv.measure(p.title, 15, True) + 40)
            on = i == self.tab
            cv.fill(x, HEAD + 6, w, TABS - 6, BG if on else PANEL2)
            cv.fill(x, HEAD + TABS - 3, w, 3, ACCENT if on else PANEL2)
            cv.text(p.title, x + w // 2, HEAD + 14, 15, TXT if on else DIM, bold=on, align="c")
            cv.hit(x, HEAD + 6, w, TABS - 6, lambda i=i: self._goto(i))
            x += w + 4
        cv.fill(0, HEAD + TABS, W, 1, LINE)
        page = self.pages[self.tab]
        if hasattr(page, "tick"):
            page.tick()
        page.render(cv, (0, HEAD + TABS + 1, W, H - HEAD - TABS - FOOT - 1))
        cv.fill(0, H - FOOT, W, FOOT, PANEL)
        cv.fill(0, H - FOOT, W, 1, LINE)
        busy = any(hasattr(p, "busy") and p.busy() for p in self.pages)
        cv.button("Annuler", W - 36 - 140 - 12 - 160, H - FOOT + 18, 140, 40, self._cancel)
        cv.button("Valider", W - 36 - 160, H - FOOT + 18, 160, 40, self._ok, primary=True, disabled=busy)
        if self.msg and time.monotonic() < self.msg_t:
            cv.text(self.msg, 36, H - FOOT + 14, 15, WARN, max_w=W - 480)
        else:
            cv.text("Entrée : valider · Échap : annuler · Tab : onglet suivant", 36, H - FOOT + 14, 13, FAINT)
        if self.listening:
            cv.text("Assignation en cours — les touches n'arrivent plus au PC tant que cette fenêtre attend", 36, H - FOOT + 40, 13, ACCENT)
        elif self.keys is not None:
            ok = getattr(self.keys, "_first", None)
            cv.text("Clavier du joueur (Pi) : " + ("connecté" if ok is not None and ok.is_set() else "pas de réponse"), 36, H - FOOT + 40, 13, OK if ok is not None and ok.is_set() else ERR)
        return cv.finish()

    def _goto(self, i):
        self._stop_listen()
        self.tab = i

    # -- entrées
    def mouse(self, ev, x, y, flags, _p=None):
        cv2 = self.cv2
        self.cv.mouse = (x, y)
        if ev == cv2.EVENT_LBUTTONDOWN:
            for x0, y0, x1, y1, fn in reversed(self.cv.hits):
                if x0 <= x < x1 and y0 <= y < y1:
                    fn()
                    return
        page = self.pages[self.tab]
        if hasattr(page, "mouse"):
            page.mouse(ev, x, y, flags)

    def key(self, k):
        if k < 0:
            return
        if self.listening:
            if self.keys is None and k < 256:
                if k == 27:
                    return self._stop_listen()
                name = chr(k).upper() if (chr(k).isalnum()) else "SPACE" if k == 32 else None
                if name:
                    self._assign(name)
            elif k == 27 and self.keys is None:
                self._stop_listen()
            return
        page = self.pages[self.tab]
        if hasattr(page, "key") and page.key(k):
            return
        if k in (13, 10):
            self._ok()
        elif k == 27:
            self._cancel()
        elif k == 9:
            self._goto((self.tab + 1) % len(self.pages))

    def run(self):
        cv2 = self.cv2
        self._start()
        cv2.namedWindow(self.name, cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback(self.name, self.mouse)
        pos = os.environ.get("AIMLABS_SETUP_POS")        # « x,y » : place la fenêtre à un endroit connu (tests automatisés de la souris, voir remote/mac_input.py)
        try:
            while self.result is None:
                cv2.imshow(self.name, self.render())
                if pos:
                    try:
                        cv2.moveWindow(self.name, *[int(v) for v in pos.split(",")])
                    except (ValueError, cv2.error):
                        pass
                    pos = None
                self.key(cv2.waitKeyEx(30))
        finally:
            self._stop()
            cv2.destroyWindow(self.name)
            for _ in range(4):
                cv2.waitKey(1)
        return self.result or None

    def snapshot(self, path, seconds=4.0):
        """Rend la fenêtre dans un PNG, sans l'afficher (tests, documentation) : laisse les pages vivre `seconds` s (aperçus, connexions)."""
        self._start()
        t_end = time.monotonic() + seconds
        img = None
        while time.monotonic() < t_end:
            img = self.render()
            time.sleep(0.03)
        self.cv2.imwrite(path, img)
        self._stop()
        return img
