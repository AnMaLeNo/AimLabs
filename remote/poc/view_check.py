"""Vérifie sur des FAITS la fenêtre debug de B (`sniper_b.py --view`) dans la vraie session graphique du Mac : ouverture, rendu avec
un flux réel (rejeu), touches (1 = revoir un tir, 0 = direct, espace = pause, q = quitter) et survie de B à la fermeture de la fenêtre.
Chaque étape produit une capture d'écran du Mac (bench_results/view_check_*.png).

    python view_check.py remote/recordings/live_36ms_30pass.bin

Les touches passent par System Events (Accessibilité accordée à Terminal) : elles ne sont envoyées QUE si le processus de la fenêtre est au
premier plan (contrôlé juste avant), jamais dans une autre application.
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ab_bench as A  # noqa: E402

ROOT = A.ROOT
OUT = os.path.join(ROOT, "bench_results")


def job(script):
    r = subprocess.run(["bash", os.path.join(ROOT, "remote", "mac_job.sh")], input=script.encode(), capture_output=True, timeout=180)   # octets : pas de CRLF
    return r.stdout.decode("utf-8", "replace").strip()


def shot(name):
    path = os.path.join(OUT, f"view_check_{name}.png")
    subprocess.run(["bash", os.path.join(ROOT, "remote", "mac_screenshot.sh"), path], capture_output=True, timeout=120)
    print(f"  capture -> {path}", flush=True)
    return path


def send_key(pid, key):
    """Met la fenêtre au premier plan et envoie la touche, seulement si c'est bien elle qui y est."""
    script = f'''osascript -e 'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'
sleep 0.6
front=$(osascript -e 'tell application "System Events" to get unix id of first process whose frontmost is true')
if [ "$front" = "{pid}" ]; then osascript -e 'tell application "System Events" to keystroke "{key}"'; echo "touche {key!r} envoyée"; else echo "NON ENVOYÉE : premier plan = $front"; fi'''
    out = job(script)
    print(f"  {out}", flush=True)
    return out.startswith("touche")


def main():
    rec = sys.argv[1]
    A.ssh("pkill -f '[s]niper_b.py'; sleep 1")
    cfg = dict(A.CONFIGS["view-gui"])
    log = "~/aimlabs-remote/vc_b.log"
    b_pid = A.start_b(cfg, log)
    text = A.ssh(f"cat {log}")
    view_pid = int(re.search(r"processus (\d+)", text).group(1))
    print(f"B pid {b_pid}, fenêtre pid {view_pid} ; {re.search(r'fenêtre ouverte.*', text).group(0) if 'fenêtre ouverte' in text else 'pas de ligne « fenêtre ouverte »'}", flush=True)
    shot("0_attente")
    replay = subprocess.Popen([sys.executable, os.path.join(ROOT, "remote", "poc", "replay_agent.py"), rec, "--connect", A.DATA_HOST + ":7777", "--seconds", "45"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(14)
    print("flux en cours :", flush=True)
    shot("1_flux")
    ok = send_key(view_pid, "1")
    time.sleep(1.0)
    shot("2_touche_1_revue")
    send_key(view_pid, "0")
    send_key(view_pid, " ")
    time.sleep(1.0)
    shot("3_pause")
    send_key(view_pid, " ")
    time.sleep(2)
    print("touche q (fermer la fenêtre) :", flush=True)
    send_key(view_pid, "q")
    t_q = time.time()
    v_alive = "1"
    while v_alive.strip() != "0" and time.time() - t_q < 15:        # combien de temps la fenêtre met-elle à disparaître après q ?
        time.sleep(0.5)
        v_alive = A.ssh(f"ps -p {view_pid} -o stat= | grep -v '^Z' | wc -l").strip()      # un zombie (Z) a quitté : il attend d'être récolté
    print(f"  fenêtre disparue {time.time() - t_q:.1f} s après q" if v_alive.strip() == "0" else "  fenêtre TOUJOURS VIVANTE 15 s après q", flush=True)
    b_alive = A.ssh(f"ps -p {b_pid} -o stat= | grep -v '^Z' | wc -l").strip()
    shot("4_apres_q")
    time.sleep(3)
    stats = A.ssh(f"tail -3 {log}")
    print(f"après q : B vivant={b_alive}, fenêtre vivante={v_alive}", flush=True)
    print(stats[-600:], flush=True)
    replay.wait(timeout=60)
    A.ssh(f"for p in $(pgrep -P {b_pid}); do kill $p; done; kill {b_pid}; sleep 0.5; pkill -f '[s]niper_b.py'")


if __name__ == "__main__":
    main()
