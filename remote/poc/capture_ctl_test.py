"""Tests du cadrage de la capture (sans jeu, sans Mac) :

    python capture_ctl_test.py            # unitaires (géométrie, configuration, choix de fenêtre) + bout en bout : agent_a --no-bridge <-> RemoteClient local

Bout en bout : lance `agent_a.py --no-bridge --listen`, s'y connecte comme le ferait B, puis vérifie que (1) par défaut la capture est le centre de l'écran,
(2) `set` recible la capture à chaud (la géométrie annoncée dans les images change), (3) le mode fenêtre trouve une fenêtre par un motif de titre,
(4) l'aperçu est un JPEG de la référence, (5) une configuration absurde est bornée. Des images doivent arriver : il faut que l'écran change (une horloge suffit).
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
from capture_cfg import DEFAULT_CFG, region_in, sanitize  # noqa: E402

fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAUX ") + what, flush=True)
    if not ok:
        fails.append(what)


def unit():
    print("unitaires")
    ref = (0, 0, 2560, 1440)
    check(region_in(ref, DEFAULT_CFG) == (1088, 528, 384, 384), "centre de l'écran 2560x1440, carré 384 -> (1088, 528)")
    check(region_in(ref, dict(DEFAULT_CFG, dx=-5000)) [0] == 0, "décalage trop grand : ramené dans la référence")
    check(region_in((100, 50, 200, 120), dict(DEFAULT_CFG, crop=500)) == (100 + 40, 50, 120, 120), "carré plus grand que la référence : réduit au petit côté, centré")
    check(region_in((1920, 0, 1280, 720), DEFAULT_CFG)[:2] == (1920 + 448, 168), "référence décalée (2e écran)")
    s = sanitize({"mode": "n'importe quoi", "crop": 10**9, "dx": "x", "title": "a" * 999, "resize": 5, "inconnu": 1})
    check(s["mode"] == "screen" and s["crop"] == 2048 and s["dx"] == 0 and len(s["title"]) == 200 and s["resize"] == 32 and "inconnu" not in s, "configuration absurde bornée")
    check(sanitize({"resize": 0})["resize"] == 0, "resize 0 = pas de réduction")
    if sys.platform == "win32":
        import capture_ctl as CC
        import screencap as SC
        wins = [{"hwnd": 1, "title": "AimLabs (DEBUG)", "process": "Godot.exe", "rect": (10, 10, 800, 600)},
                {"hwnd": 2, "title": "AimLabs - Visual Studio Code", "process": "Code.exe", "rect": (0, 0, 1900, 1000)}]
        r = CC.resolve(dict(DEFAULT_CFG, mode="window", title="AimLabs (DEBUG)"), windows=wins)
        check(r["found"] and r["window"]["hwnd"] == 1 and r["region"] == (10 + 208, 10 + 108, 384, 384), "fenêtre : titre identique, carré centré dans la fenêtre")
        r = CC.resolve(dict(DEFAULT_CFG, mode="window", title="AimLabs"), windows=wins)
        check(r["window"]["hwnd"] == 2, "motif « AimLabs » seul : commence-par, la plus grande (ici Code : d'où --window-process)")
        r = CC.resolve(dict(DEFAULT_CFG, mode="window", title="AimLabs", process="godot"), windows=wins)
        check(r["window"]["hwnd"] == 1, "motif + processus « godot » : la fenêtre du jeu")
        r = CC.resolve(dict(DEFAULT_CFG, mode="window", title="zzz"), windows=wins)
        check(not r["found"] and r["error"], "fenêtre introuvable : erreur claire")
        check(SC.monitors()[0]["primary"], "écran principal listé en premier")


def e2e(port=7791):
    print("bout en bout (agent_a local)")
    import cv2
    from aimlabs.remote import RemoteClient
    log = open(os.path.join(__import__("tempfile").gettempdir(), "capture_ctl_test.agent.log"), "w", encoding="utf-8")
    agent = subprocess.Popen([sys.executable, os.path.join(HERE, "agent_a.py"), "--no-bridge", "--listen", f"127.0.0.1:{port}", "--jpeg", "60"], stdout=log, stderr=subprocess.STDOUT)
    clock = None
    try:
        c = RemoteClient(f"127.0.0.1:{port}", timeout=30)
        check(c.has_ctrl, "l'agent annonce le canal de réglage")
        cap0 = c.hello["capture"]
        check(cap0["config"]["mode"] == "screen" and cap0["found"], "défaut : mode écran, sans fenêtre")

        def geometry(timeout=4.0):
            t_end = time.monotonic() + timeout
            fr = None
            while time.monotonic() < t_end:
                f = c.crop(timeout=0.5)
                fr = f or fr
                if f:
                    return fr
            return fr

        # l'écran doit changer pour que dxcam livre des images : petite fenêtre d'horloge
        clock = subprocess.Popen([sys.executable, "-c", "import tkinter as tk,time\nr=tk.Tk();r.title('Horloge test');r.geometry('520x300+200+200')\nl=tk.Label(r,font=('Arial',40));l.pack()\n"
                                  "def f():\n l.config(text=str(time.time()));r.after(5,f)\nf();r.mainloop()"])
        time.sleep(1.5)
        fr = geometry()
        check(fr is not None, "des images arrivent")
        if fr:
            _fid, _cap, _rx, crop, (x0, y0), (W, H) = fr
            check((W, H) == tuple(cap0["ref"][2:]) and crop.shape[:2] == (256, 256), f"écran entier {W}x{H} annoncé, carré reçu {crop.shape[1]}x{crop.shape[0]} (réduit à 256)")
            check((x0, y0) == ((W - 384) // 2, (H - 384) // 2), f"carré centré sur l'écran : origine ({x0}, {y0})")
        r = c.ctrl("set", config={"crop": 200, "dx": 100, "dy": -50, "resize": 128})
        check(r is not None and r[0]["config"]["crop"] == 200, "set : accusé avec la configuration appliquée")
        time.sleep(0.5)
        fr = geometry()
        if fr:
            _fid, _cap, _rx, crop, (x0, y0), (W, H) = fr
            check(crop.shape[:2] == (128, 128) and (x0, y0) == (W // 2 + 100 - 100, H // 2 - 50 - 100), f"recadrage à chaud : carré 200 px décalé (100, -50), origine ({x0}, {y0}), image 128x128")
        r = c.ctrl("get_info")
        wins = r[0].get("windows", []) if r else []
        check(any("Horloge test" in w["title"] for w in wins), f"liste des fenêtres du PC A ({len(wins)}) contient la fenêtre d'horloge")
        r = c.ctrl("set", config={"mode": "window", "title": "Horloge test", "dx": 0, "dy": 0, "crop": 300, "resize": 256})
        check(r is not None and r[0]["found"] and r[0]["window"]["title"] == "Horloge test", "mode fenêtre : fenêtre trouvée par son titre")
        time.sleep(0.5)
        fr = geometry()
        if fr:
            _fid, _cap, _rx, crop, (x0, y0), (W, H) = fr
            ref = r[0]["ref"]
            check((W, H) == tuple(ref[2:]), f"la référence annoncée est la fenêtre : {W}x{H}")
        import ctypes
        reg0 = c.ctrl("get_info")[0]["region"]
        ctypes.windll.user32.SetWindowPos(r[0]["window"]["hwnd"], 0, 600, 400, 0, 0, 0x0001 | 0x0004)     # SWP_NOSIZE | SWP_NOZORDER : l'utilisateur déplace la fenêtre
        time.sleep(1.0)
        reg1 = c.ctrl("get_info")[0]["region"]
        check(reg1 != reg0, f"fenêtre déplacée : la capture la suit {tuple(reg0)} -> {tuple(reg1)}")
        r = c.ctrl("get_preview", target={"mode": "screen"}, max_w=640)
        check(r is not None and r[1] and r[0]["ref"][2] > 640, "aperçu JPEG de l'écran (cible demandée sans l'appliquer)")
        if r and r[1]:
            import numpy as np
            im = cv2.imdecode(np.frombuffer(r[1], np.uint8), cv2.IMREAD_COLOR)
            check(im is not None and im.shape[1] == 640, f"aperçu décodable, {im.shape[1]}x{im.shape[0]}, {len(r[1]) // 1000} Ko")
        r = c.ctrl("set", config={"mode": "window", "title": "fenetre qui n'existe pas"})
        check(r is not None and r[0]["found"] is False and r[0]["error"], "fenêtre introuvable : signalé à B, l'agent continue")
        check(c.crop(timeout=3.0) is not None, "l'agent continue d'envoyer des images")
        r = c.ctrl("set", config={"mode": "screen", "crop": 10**8, "dx": 10**8})
        check(r is not None and r[0]["config"]["crop"] == 2048, "valeurs absurdes bornées par l'agent")
        r = c.ctrl("n_importe_quoi")
        check(r is not None and "error" in r[0], "opération inconnue : erreur renvoyée, pas de plantage")
        c.close()
    finally:
        if clock:
            clock.kill()
        agent.kill()
        log.close()


if __name__ == "__main__":
    unit()
    if "--no-e2e" not in sys.argv:
        e2e()
    print("\n" + ("TOUT EST BON" if not fails else f"{len(fails)} ÉCHEC(S) : " + " | ".join(fails)))
    sys.exit(1 if fails else 0)
