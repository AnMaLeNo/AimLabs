"""Tests de l'enregistreur de comportement et du score humain, sans fenêtre ni joueur.

    python tools/behavior/test_behavior.py [--godot <exe>]

1. Lance le jeu sans affichage avec `--behavior-selftest` (entrées scénarisées passant par le même chemin qu'une vraie souris)
   et vérifie que le fichier écrit contient ce qui a été joué.
2. Vérifie la mécanique du score sur des enregistrements SYNTHÉTIQUES (un « humain » et un « automate » inventés) : cela teste
   le calcul, PAS la validité du score, qui ne peut se juger que sur de vraies parties.
"""
import argparse
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import aimbeh  # noqa: E402
import human_score as HS  # noqa: E402

FAILS = []


def check(ok, text):
    print(("  ok   " if ok else "  FAUX ") + text)
    if not ok:
        FAILS.append(text)


def synth(path, rng, n_pass, tot_ms, dur_ms, fps=240.0):
    """Passages d'une cible devant un réticule immobile : la cible va de −5° à +5° en 1 s (demi-largeur 1° : 200 ms sur la
    cible), un appui par passage, `tot_ms()` après l'entrée du réticule sur la cible, maintenu `dur_ms()`."""
    recs = [("playing", (0, 1))]
    t = 1_000_000
    dt = int(1e6 / fps)
    for _ in range(n_pass):
        enter = None
        press = release = None
        for k in range(int(1.2 * fps)):
            off = -5.0 + 10.0 * k / fps
            on = abs(off) < 1.0
            if on and enter is None:
                enter = t
                press = enter + int(max(tot_ms(), 0.0) * 1e3)
                release = press + int(max(dur_ms(), 1.0) * 1e3)
            recs.append(("frame", (t, 0.0, 0.0, 75.0, 1, 0, off, 0.0, 1.0, 2.5, 20.0, 0 if on else -1, 1 if on else 0, 0.0, 0.0)))
            for when, pressed in ((press, 1), (release, 0)):
                if when is not None and t <= when < t + dt:
                    recs.append(("button", (t + dt, pressed, 0)))        # le jeu ne voit l'évènement qu'à l'image suivante
                    if pressed:
                        recs.append(("shot", (t + dt, 1 if on else 0, 0, 1, 0)))
            t += dt
        t += int(rng.uniform(0.2, 0.6) * 1e6)
    recs.sort(key=lambda r: r[1][0])
    aimbeh.save(path, dict(version=1, mode="synth", map="range", weapon="classic"), recs)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--godot", default=os.environ.get("GODOT", r"C:\Users\antoi\Godot\Godot_v4.7.2-stable_win64_console.exe"))
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="behavior_test_")

    print("enregistreur du jeu (entrées scénarisées, sans affichage)")
    out = os.path.join(tmp, "selftest.aimbeh")
    if not os.path.exists(a.godot):
        check(False, f"Godot introuvable : {a.godot} (--godot ou variable GODOT)")
    else:
        r = subprocess.run([a.godot, "--headless", "--path", os.path.join(ROOT, "game"), "--", "--mode=silent", "--behavior-selftest",
                            "--behavior=" + out.replace("\\", "/")], capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        check(os.path.exists(out), "le jeu a écrit le fichier" + ("" if os.path.exists(out) else " : " + (r.stdout + r.stderr)[-400:]))
    if os.path.exists(out):
        log = aimbeh.load(out)
        h = log.header
        check(h["mode"] == "silent" and h["map"] == "range" and h["selftest"] is True and h["sens_deg_per_px"] > 0, f"en-tête : mode {h['mode']}, carte {h['map']}, arme {h['weapon']}")
        check(not log.truncated, "fichier lisible jusqu'au bout")
        m = log.motion
        check(len(m) == 40 and abs(float(m["dx"].sum()) - (20 * 10 - 20 * 15)) < 1e-3 and float(np.abs(m["dy"]).sum()) == 0, f"mouvements de souris : {len(m)} évènements, somme {m['dx'].sum():+.0f} px")
        b = log.button
        check(list(b["pressed"]) == [1, 0, 1, 0] and not b["key"].any(), f"bouton de tir : {[int(x) for x in b['pressed']]}")
        s = log.shot
        check(len(s) == 2 and list(s["hit"]) == [1, 0] and s["id"][0] == 0, f"tirs : {len(s)}, touché {[int(x) for x in s['hit']]} (le premier sur la cible, le second à côté)")
        f = log.frame
        check(len(f) > 80 and np.all(np.diff(f["t"].astype(np.int64)) > 0), f"{len(f)} images, temps croissants")
        check(bool(np.all(f["n_visible"] == 1)) and len(log.visible) == 1 and log.visible["visible"][0] == 1, "la cible du stand est vue visible (un seul changement de visibilité)")
        i0 = int(np.searchsorted(f["t"], m["t"][0])) - 1
        check(abs(abs(float(f["off_x"][i0])) - 200 * h["sens_deg_per_px"]) < 0.3, f"avant le balayage la cible est à {f['off_x'][i0]:+.1f}° du réticule (attendu ±{200 * h['sens_deg_per_px']:.1f}°)")
        check([int(x) for x in log.playing["captured"]] == [0, 1, 0, 1] and int(f["t"][-1]) > int(log.playing["t"][-1]), "menu, en jeu, pause (écriture), reprise : la fin est écrite à la fermeture du jeu")
        ps = HS.presses(log)
        check(len(ps) == 2 and ps[0]["on_target"] and not ps[1]["on_target"] and ps[0]["tot_ms"] > 0 and ps[0]["dur_ms"] > ps[1]["dur_ms"] > 0 and ps[0]["shot"],
              f"appuis reconstruits : sur la cible depuis {ps[0]['tot_ms']:.1f} ms puis hors cible ; durées {ps[0]['dur_ms']:.1f} et {ps[1]['dur_ms']:.1f} ms" if len(ps) == 2 else f"appuis : {ps}")
        check(abs(ps[0]["pos"]) < 1.0, f"réticule à {ps[0]['pos']:+.2f} demi-largeur du centre de la cible au premier appui")

    print("mécanique du score (enregistrements synthétiques)")
    rng = np.random.default_rng(1)
    human = lambda: (lambda: rng.normal(110, 30), lambda: rng.normal(95, 22))      # noqa: E731
    bot = lambda: (lambda: rng.normal(4, 1.5), lambda: 60.0)                         # noqa: E731
    paths = {}
    for name, gen, n in (("h1", human, 120), ("h2", human, 120), ("h3", human, 60), ("bot", bot, 60)):
        paths[name] = os.path.join(tmp, name + ".aimbeh")
        synth(paths[name], rng, n, *gen())
    hp = HS.presses(aimbeh.load(paths["h1"]))
    tot = np.array([p["tot_ms"] for p in hp if p["on_target"]])
    check(len(hp) == 120 and abs(np.median(tot) - 110) < 15 and abs(np.median([p["dur_ms"] for p in hp]) - 95) < 10,
          f"mesures retrouvées sur l'humain synthétique : temps sur cible {np.median(tot):.0f} ms (110 voulu), appui {np.median([p['dur_ms'] for p in hp]):.0f} ms (95 voulu)")
    cross = np.median([p["cross_ms"] for p in hp if p["on_target"]])
    check(abs(cross - 200) < 10, f"durée du passage sur la cible : {cross:.0f} ms (200 voulu)")
    ref = HS.fit([aimbeh.load(paths["h1"]), aimbeh.load(paths["h2"])])
    check(ref["windows"] == 16 and {"dur_med", "tot_med", "gap_med", "on_frac"} <= set(ref["features"]), f"référence : {ref['windows']} fenêtres, {len(ref['features'])} mesures")
    sh = HS.score(aimbeh.load(paths["h3"]), ref)
    sb = HS.score(aimbeh.load(paths["bot"]), ref)
    check(sh["score"] > 60 and sb["score"] < 5, f"humain synthétique hors référence : {sh['score']:.0f}/100 ; automate synthétique : {sb['score']:.1f}/100")
    worst = sorted(sb["z"].items(), key=lambda kv: -abs(kv[1]))[:3]
    check({"tot_med", "dur_med"} & {k for k, _ in worst} != set(), "mesures les plus anormales de l'automate : " + ", ".join(f"{k} (z {z:+.0f})" for k, z in worst))
    check(HS.score(aimbeh.load(out), ref)["score"] is None if os.path.exists(out) else True, "trop peu d'appuis : pas de score plutôt qu'un score inventé")

    print("\nTOUT EST BON" if not FAILS else f"\n{len(FAILS)} ÉCHEC(S) : " + " ; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
