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
import case_report as CR  # noqa: E402

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

    print("arène : apparitions derrière des murs, joueur scénarisé dans le jeu (2 × 20 s)")
    arena = {}
    for react in (0, 250):
        arena[react] = os.path.join(tmp, f"arena{react}.aimbeh")
        if os.path.exists(a.godot):
            subprocess.run([a.godot, "--headless", "--path", os.path.join(ROOT, "game"), "--", "--mode=silent", "--seed=3",
                            f"--behavior-selftest=arena:{react}:20", "--behavior=" + arena[react].replace("\\", "/")],
                           capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    if all(os.path.exists(p) for p in arena.values()):
        fast, slow = aimbeh.load(arena[0]), aimbeh.load(arena[250])
        check(fast.header["version"] == 2 and fast.header["map"] == "arena" and fast.header["scenario"]["walls"] is True, "en-tête : version 2, carte arena, murs")
        ef, es = HS.reactions(fast), HS.reactions(slow)
        f = fast.frame
        check(len(ef) >= 5 and len(es) >= 5 and len(np.unique(f["seen"])) > 5 and f["seen"].min() == 0, f"{len(ef)} et {len(es)} apparitions en 20 s ; la part visible de la cible varie ({len(np.unique(f['seen']))} valeurs)")
        hidden = f["n_visible"] == 0
        check(0.2 < hidden.mean() < 0.95 and bool(np.all(f["on_id"][hidden] < 0)), f"cible cachée {100 * hidden.mean():.0f} % du temps, jamais sous le réticule quand elle est cachée")
        rf, rs = HS.reaction_score(ef), HS.reaction_score(es)
        check(rf["n"] >= 5 and rf["median_ms"] < 100 and rf["score"] < 30, f"joueur qui tire dès qu'il voit le torse : délai médian {rf['median_ms']:.0f} ms, {100 * rf['impossible']:.0f} % sous 100 ms, score de réaction {rf['score']:.0f}/100")
        check(rs["n"] >= 4 and rs["min_ms"] >= 250 and (rs["score"] is None or rs["score"] == 100), f"joueur qui attend 250 ms : délai minimal {rs['min_ms']:.0f} ms, médian {rs['median_ms']:.0f} ms, score de réaction {rs['score']}")
        check(all(e["react_ms"] is None or e["react_ms"] <= e["seen_ms"] + 1 for e in ef + es), "un délai de réaction ne dépasse jamais la durée de l'apparition")
    else:
        check(False, "les deux parties d'arène n'ont pas été écrites")

    print("banc scénarisé : joueur simulé + déclencheur du banc, sans puis avec la règle « visible depuis 150 ms » (2 × ~25 s)")
    runs = {}
    for rule in (0, 150):
        runs[rule] = os.path.join(tmp, f"cases{rule}.aimbeh")
        if os.path.exists(a.godot):
            subprocess.run([a.godot, "--headless", "--path", os.path.join(ROOT, "game"), "--", "--mode=silent", "--seed=1", "--case-repeats=3",
                            "--cases=flick_side,peek_preaim,two_near,sweep", f"--cases-shooter=trigger:0:{rule}", "--behavior=" + runs[rule].replace("\\", "/")],
                           capture_output=True, text=True, timeout=240, encoding="utf-8", errors="replace")
    if all(os.path.exists(p) for p in runs.values()):
        t0, t150 = CR.trials(aimbeh.load(runs[0])), CR.trials(aimbeh.load(runs[150]))
        s0, s150 = CR.summarize(t0), CR.summarize(t150)
        check(len(t0) == 12 and len(t150) == 12 and all(t["vu"] is not None and t["dessus"] is not None and t["hit"] for t in t0 + t150),
              "12 essais marqués par partie ; à chaque essai la cible devient visible, le réticule l'atteint, elle est touchée")
        check(set(s0) == {"flick_side", "peek_preaim", "two_near", "sweep"}, "les quatre cas demandés sont dans l'enregistrement")
        check(s0["peek_preaim"]["lt100"] + s0["peek_preaim"]["lt150"] == 3 and s0["two_near"]["react_med"] < 150,
              f"réticule posé d'avance, sans règle : tirs {s0['peek_preaim']['react_med']:.0f} ms (médiane) après l'apparition — impossible pour un humain")
        check(all(t["react"] >= 150 for t in t150) and 150 <= s150["peek_preaim"]["react_med"] < 175 and s150["peek_preaim"]["tot_med"] > 10,
              f"avec la règle : aucun tir avant 150 ms (médiane {s150['peek_preaim']['react_med']:.0f} ms), au prix de {s150['peek_preaim']['tot_med']:.0f} ms d'attente réticule sur la cible")
        check(300 < s0["flick_side"]["react_med"] < 700 and abs(s0["flick_side"]["react_med"] - s150["flick_side"]["react_med"]) < 40 and s150["flick_side"]["tot_med"] < 10,
              f"coup de souris du joueur simulé : tir {s0['flick_side']['react_med']:.0f} ms après l'apparition (réaction + geste) ; la règle n'y change rien")
        check(s150["sweep"]["react_med"] > 800 and s150["sweep"]["tot_med"] < 10, "cible visible depuis longtemps, balayée : tir à l'arrivée du réticule, règle ou pas")
        b = aimbeh.load(runs[0]).button
        check(len(b) > 0 and bool(np.all(b["key"] == 2)), "les tirs demandés par un programme sont enregistrés comme tels (appui de type 2)")
        mo = aimbeh.load(runs[0]).motion
        check(len(mo) > 200 and bool(np.all(mo["dx"] == np.round(mo["dx"]))), f"le joueur simulé passe par les mouvements de souris ({len(mo)} évènements, pixels entiers)")
    else:
        check(False, "les deux parties scénarisées n'ont pas été écrites")

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
    check(HS.score(aimbeh.load(paths["h3"]), ref)["reaction"]["score"] is None, "carte sans apparitions : pas de score de réaction, le score global est celui de la référence")

    print("\nTOUT EST BON" if not FAILS else f"\n{len(FAILS)} ÉCHEC(S) : " + " ; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
