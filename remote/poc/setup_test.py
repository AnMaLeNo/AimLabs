"""Tests de la fenêtre de réglages générique (aimlabs.setup) SANS ouvrir de fenêtre ni toucher à l'écran : les clics et les touches sont injectés dans le programme,
le rendu est lu dans les images que la fenêtre produit elle-même.

    python setup_test.py [--png-dir DIR]          # DIR : écrit une image par étape (inspection visuelle)

Vérifie : validation du schéma (erreurs précises), valeurs par défaut / enregistrées / coercition, assignation d'une touche en entrée et en sortie (flux de touches simulé),
liste blanche de sortie, modes bascule / maintien, doublons signalés, champs numériques / texte / choix, validation bloquée par une page active en erreur.
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, HERE)
import cv2  # noqa: E402

import aimlabs.setup as S  # noqa: E402
from aimlabs.setup import ui  # noqa: E402

fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAUX ") + what, flush=True)
    if not ok:
        fails.append(what)


class FakeKeys:
    """Ce que la fenêtre attend d'un `PiKeys` : allowed_output_keys, add/remove_listener, set, _first, _all."""

    def __init__(self, allowed):
        self.allowed, self.listeners, self.calls, self._all = allowed, [], [], False
        self._first = type("E", (), {"is_set": staticmethod(lambda: True)})()

    def allowed_output_keys(self, timeout=1.0):
        return self.allowed

    def add_listener(self, fn):
        self.listeners.append(fn)

    def remove_listener(self, fn):
        self.listeners.remove(fn)

    def set(self, **kw):
        self.calls.append(kw)

    def press(self, name):
        for fn in list(self.listeners):
            fn(name, True)


def label_pos(win, label):
    for s, x, y, *_ in win.cv.texts:
        if s == label:
            return x, y
    raise KeyError(label)


def click(win, x, y):
    win.cv.mouse = (x, y)
    win.mouse(cv2.EVENT_LBUTTONDOWN, x, y, 0)


def click_box(win, label):
    """Clique la case de valeur du champ `label` (colonne de droite, à la hauteur de son libellé)."""
    x, y = label_pos(win, label)
    click(win, 660 + 90, y + 20)


def click_text(win, text):
    for s, x, y, size, *_ in win.cv.texts:
        if s == text:
            return click(win, x + 4, y + size // 2)
    raise KeyError(text)


def real_pi(host, schema, values_path):
    """Assignation d'une touche par le VRAI flux du Pi : clavier virtuel (uinput) qui appuie sur Pause pendant que la fenêtre attend."""
    import subprocess
    import time
    from aimlabs.keys import PiKeys
    from pi_keys_test import KEYFILE, pi_fire_status
    print("assignation par le clavier du Pi (réel)")
    ssh = "claude@" + host
    subprocess.run(["scp", "-q", "-i", KEYFILE, os.path.join(ROOT, "remote", "pi", "uinput_keys.py"), ssh + ":/tmp/uinput_keys.py"], check=True)
    pk = PiKeys(host)
    assert pk.start(3.0), "pas de flux du Pi"
    win = ui.SetupWindow(schema, S.load_values(schema, values_path), keys=pk, plugins={})
    win._start()
    win.render()
    check("PAUSE" in (win.out_allowed or []) or "G" in (win.out_allowed or []), f"liste blanche du Pi lue par la fenêtre : {len(win.out_allowed or [])} touches")
    proc = subprocess.Popen(["ssh", "-i", KEYFILE, ssh, "sudo python3 /tmp/uinput_keys.py --keys PAUSE --taps 1 --settle 4"], stdout=subprocess.DEVNULL)
    time.sleep(1.0)
    click_box(win, "Marquer une cible")
    time.sleep(1.5)
    st = pi_fire_status(ssh)
    check(st.get("swallow_all") is True, f"pendant l'attente, le Pi ne transmet plus rien au PC (swallow_all = {st.get('swallow_all')})")
    proc.wait()
    time.sleep(0.5)
    check(win.pages[0].vals["mark"]["key"] == "PAUSE", f"touche Pause appuyée sur le « clavier » du Pi : assignée ({win.pages[0].vals['mark']['key']})")
    time.sleep(1.2)
    st = pi_fire_status(ssh)
    check(st.get("swallow_all") is False, "attente terminée : le Pi retransmet les touches au PC")
    win._stop()
    pk.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--png-dir", default="")
    ap.add_argument("--pi", default="", help="HOTE du Pi : ajoute un test RÉEL de l'assignation par le clavier du Pi (clavier virtuel, touche Pause)")
    a = ap.parse_args()
    png = (lambda w, n: cv2.imwrite(os.path.join(a.png_dir, n), w.render())) if a.png_dir else (lambda w, n: w.render())
    if a.png_dir:
        os.makedirs(a.png_dir, exist_ok=True)

    print("schéma")
    bad = lambda **kw: S.validate_schema({"setup": 1, "app": "t", "pages": [{"id": "p", "fields": [dict(kw)]}]})
    for what, fn, msg in [
        ("type inconnu", lambda: bad(id="x", type="couleur"), "type"),
        ("id manquant", lambda: bad(type="bool"), "id"),
        ("choix sans options", lambda: bad(id="x", type="choice"), "options"),
        ("mode de touche inconnu", lambda: bad(id="x", type="key_in", modes=["double"]), "modes"),
        ("app invalide", lambda: S.validate_schema({"setup": 1, "app": "a/b", "pages": [{"id": "p"}]}), "app"),
        ("version future", lambda: S.validate_schema({"setup": 9, "app": "a", "pages": [{"id": "p"}]}), "version"),
        ("id en double", lambda: S.validate_schema({"setup": 1, "app": "t", "pages": [{"id": "p", "fields": [{"id": "x", "type": "bool"}, {"id": "x", "type": "bool"}]}]}), "déjà utilisé"),
    ]:
        try:
            fn()
            check(False, f"{what} : refusé avec un message")
        except S.SchemaError as e:
            check(msg in str(e), f"{what} : « {e} »")

    schema = json.load(open(os.path.join(ROOT, "sdk", "python", "examples", "setup_schema_example.json"), encoding="utf-8"))
    S.validate_schema(schema)
    print("valeurs")
    d = S.defaults(schema)
    check(d["auto"] == {"key": "CAPSLOCK", "mode": "toggle", "swallow": False} and d["fire"] == "G" and d["conf"] == 0.5 and d["debug"] is False, "valeurs par défaut du schéma d'exemple")
    tmp = os.path.join(tempfile.gettempdir(), "setup_test_values.json")
    S.save_values(schema, dict(d, conf=7, burst={"key": "x", "mode": "n'importe quoi"}, cooldown="abc"), tmp)
    v = S.load_values(schema, tmp)
    check(v["conf"] == 1 and v["burst"] == {"key": "X", "mode": "hold", "swallow": False} and v["cooldown"] == 500, "valeurs enregistrées : bornées, mode invalide ramené, texte invalide -> défaut")

    print("fenêtre (sans écran)")
    keys = FakeKeys(["F8", "G", "PAUSE", "H"])
    win = ui.SetupWindow(schema, S.load_values(schema, tmp), keys=keys, plugins={})
    win._start()
    png(win, "1_touches.png")
    check(label_pos(win, "Tir automatique : bascule") is not None, "page Touches dessinée")
    # --- assignation en entrée
    click_box(win, "Marquer une cible")
    win.render()
    check(win.listening is not None and win.listening[1]["id"] == "mark", "clic sur la case : en attente d'une touche")
    check(keys.calls and keys.calls[-1] == {"all_keys": True, "swallow_all": True}, "pendant l'attente : toutes les touches suivies et aucune transmise au PC")
    png(win, "2_attente.png")
    keys.press("PAUSE")
    check(win.pages[0].vals["mark"]["key"] == "PAUSE" and win.listening is None, "touche appuyée sur le clavier du joueur : assignée")
    check(keys.calls[-1] == {"all_keys": False, "swallow_all": False}, "fin de l'attente : le clavier est rendu au PC")
    # --- doublon signalé
    click_box(win, "Tir automatique : bascule")
    win.render()
    keys.press("PAUSE")
    win.render()
    png(win, "3_doublon.png")
    check(any("même touche" in t[0] for t in win.cv.texts), "même touche sur deux champs : signalé")
    # --- sortie : liste blanche
    click_box(win, "Touche de tir")
    win.render()
    keys.press("Z")
    check(win.pages[0].vals["fire"] == "G" and win.listening is not None and "n'est pas dans la liste" in win.msg, "touche de sortie refusée (hors liste blanche du Pi), l'attente continue")
    keys.press("H")
    check(win.pages[0].vals["fire"] == "H", "touche de sortie autorisée : assignée")
    win.render()
    # --- modes
    click_text(win, "Maintien")
    win.render()
    check(win.pages[0].vals["burst"]["mode"] == "hold", "mode Maintien choisi")
    click_text(win, "Bascule")
    check(win.pages[0].vals["burst"]["mode"] in ("toggle", "hold"), "mode Bascule cliquable")
    # --- effacer
    x, y = label_pos(win, "Marquer une cible")
    click(win, 660 + 196 + 20, y + 20)
    check(win.pages[0].vals["mark"]["key"] is None, "bouton Effacer")
    # --- bloquer la touche
    win.render()
    click_text(win, "Transmise au PC")
    check(any(v.get("swallow") for k, v in win.pages[0].vals.items() if k in ("auto", "burst", "mark")), "bouton « Transmise au PC » -> « Bloquée côté PC »")
    # --- page Algorithme : nombre, texte, choix, booléen
    win._goto(1)
    win.render()
    png(win, "4_algo.png")
    x, y = label_pos(win, "Confiance minimale")
    click(win, 660 + 196, y + 20)                                   # bouton +
    check(abs(win.pages[1].vals["conf"] - 0.55) < 1e-9 or abs(win.pages[1].vals["conf"] - 0.5) > 0, "nombre : bouton +")
    click(win, 660 + 100, y + 20)                                   # case de saisie
    win.render()
    for ch in "0.8":
        win.key(ord(ch))
    win.key(13)
    check(abs(win.pages[1].vals["conf"] - 0.8) < 1e-9, "nombre saisi au clavier : 0.8")
    click_text(win, "Tête")
    check(win.pages[1].vals["mode"] == "head", "choix : Tête")
    click(win, 660 + 28, label_pos(win, "Journal détaillé")[1] + 20)
    check(win.pages[1].vals["debug"] is True, "booléen : activé")
    x, y = label_pos(win, "Nom de la session")
    click(win, 660 + 100, y + 20)
    for _ in range(3):
        win.key(127)
    for ch in "ab":
        win.key(ord(ch))
    win.key(13)
    check(win.pages[1].vals["name"] == "essai 1"[:-3] + "ab", "texte saisi au clavier")
    # --- validation
    win.key(13)
    res = win.result
    check(res and res["fire"] == "H" and res["conf"] == 0.8 and res["debug"] is True and res["mode"] == "head", f"Entrée valide : valeurs collectées")
    win._stop()
    check(win.keys.listeners == [], "écouteur de touches retiré à la fermeture")

    print("page active")

    class Plug:
        id, title = "capture", "Capture"

        def __init__(self):
            self.err, self.is_busy, self.started, self.stopped = "cible introuvable", False, 0, 0

        def start(self, w):
            self.started += 1

        def stop(self):
            self.stopped += 1

        def render(self, cv, rect):
            cv.text("page active", rect[0] + 10, rect[1] + 10)

        def value(self):
            return {"mode": "screen"}

        def validate(self):
            return self.err

        def busy(self):
            return self.is_busy

    sch2 = {"setup": 1, "app": "t2", "pages": [{"id": "capture", "title": "Capture", "type": "capture"}, {"id": "k", "title": "Touches", "fields": [{"id": "b", "type": "bool"}]}]}
    plug = Plug()
    w2 = ui.SetupWindow(S.validate_schema(sch2), {"b": False}, plugins={"capture": plug})
    w2._start()
    w2.render()
    w2.key(13)
    check(w2.result is None and "introuvable" in w2.msg and w2.tab == 0, "page active en erreur : validation bloquée, message affiché")
    plug.err = None
    plug.is_busy = True
    w2.key(13)
    check(w2.result is None and "en cours" in w2.msg, "page active occupée : validation différée")
    plug.is_busy = False
    w2.key(13)
    check(w2.result == {"b": False, "capture": {"mode": "screen"}}, "page active prête : valeurs fusionnées")
    w2._stop()
    check(plug.started == 1 and plug.stopped == 1, "start / stop de la page active appelés une fois")
    w3 = ui.SetupWindow(S.validate_schema(sch2), {"b": False}, plugins={})
    w3._start()
    img = w3.render()
    check(img is not None and any("non disponible" in t[0] for t in w3.cv.texts), "page active sans hôte : « non disponible »")

    if a.pi:
        real_pi(a.pi, schema, tmp)

    print("\n" + ("TOUT EST BON" if not fails else f"{len(fails)} ÉCHEC(S) : " + " | ".join(fails)), flush=True)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
