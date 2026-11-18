"""Le jeu date-t-il un tir à son instant réel, et un tir manqué gèle-t-il une image ? Essai sur le PC seul, sans joueur.

Le jeu joue des balayages ; un client du SDK demande un tir à intervalle régulier (presque tous manquent : impact sur le décor).
Le journal de comportement donne la demande reçue, le tir daté et les images : l'outil affiche l'écart demande -> tir daté et la plus
longue image dans les 15 ms qui suivent chaque demande.

    python tools/behavior/shot_stamp_check.py SORTIE [--period 8.7] [--repeats 24]      # joue puis lit (la fenêtre du jeu s'ouvre)
    python tools/behavior/shot_stamp_check.py SORTIE --read                            # relit SORTIE.aimbeh

Pourquoi 8,7 s : avant le correctif du 2026-11-16, l'impact de balle était reconstruit dès que plus aucun impact n'existait (ils vivent
8 s) : 8 à 16 ms dans le tir. Mesuré alors : tir daté 8,8 ms après la demande, image de 10,5 à 11,3 ms ; après : 0,2 ms et 1,3 à 1,8 ms.
"""
import argparse
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, HERE)
GODOT = os.environ.get("GODOT", r"C:\Users\antoi\Godot\Godot_v4.7.2-stable_win64_console.exe")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", help="préfixe des fichiers écrits (SORTIE.aimbeh, SORTIE.game.log)")
    ap.add_argument("--period", type=float, default=8.7, help="secondes entre deux demandes de tir")
    ap.add_argument("--repeats", default="24", help="nombre d'essais du cas « sweep » joués par le jeu (2 s chacun environ)")
    ap.add_argument("--read", action="store_true")
    a = ap.parse_args()
    beh = a.out + ".aimbeh"
    if not a.read:
        if os.path.exists(beh):
            os.remove(beh)
        gl = open(a.out + ".game.log", "w", encoding="utf-8", errors="replace")
        game = subprocess.Popen([GODOT, "--path", os.path.join(ROOT, "game"), "--always-on-top", "--", "--mode=remote", "--cases=sweep", f"--case-repeats={a.repeats}", "--seed=3",
                                 f"--behavior={beh}"], stdout=gl, stderr=subprocess.STDOUT)
        try:
            time.sleep(9)
            from aimlabs import AimClient
            aim = AimClient(timeout=30)
            n = 0
            t_next = time.time() + 1.0
            while game.poll() is None:             # le jeu se ferme seul à la fin des cas (et écrit alors son journal)
                if time.time() >= t_next:
                    aim.fire(frame_id=0)
                    n += 1
                    t_next += a.period
                aim.beat()
                time.sleep(0.01)
            print("tirs demandés :", n, flush=True)
        finally:
            if game.poll() is None:
                game.kill()
            gl.close()
    from aimbeh import load
    if not os.path.exists(beh):
        print("pas de journal de comportement écrit")
        return 1
    lg = load(beh)
    b = lg.button
    req = b["t"][(b["pressed"] == 1) & (b["key"] == 2)].astype(np.float64) / 1e3
    st = lg.shot["t"].astype(np.float64) / 1e3
    hit = lg.shot["hit"].astype(bool)
    ft = lg.frame["t"].astype(np.float64) / 1e3
    dt = np.diff(ft)
    q = np.clip(np.searchsorted(req, st + 1e-4) - 1, 0, len(req) - 1)
    lag = st - req[q]
    worst = np.array([dt[(ft[1:] > r - 1.0) & (ft[1:] < r + 15.0)].max() for r in req[q]])
    print(f"images du jeu : intervalle médian {np.median(dt):.2f} ms · {len(req)} demandes · {len(st)} tirs")
    for name, m in (("touchés", hit), ("manqués", ~hit)):
        if m.any():
            print(f"tirs {name} : {int(m.sum())} | tir daté − demande reçue : médiane {np.median(lag[m]):.2f} ms, max {lag[m].max():.2f} | plus longue image dans les 15 ms : "
                  f"médiane {np.median(worst[m]):.1f} ms, max {worst[m].max():.1f} | {np.round(worst[m][:12], 1).tolist()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
