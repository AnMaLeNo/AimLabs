"""Test de la SOURIS dans la vraie fenêtre de réglages, sur le Mac (la seule chose que setup_test.py ne couvre pas : la boucle d'évènements Cocoa).

    python setup_click_check.py        # ~25 s ; le pointeur du Mac bouge un instant et la fenêtre passe au premier plan : prévenir l'utilisateur

Déroulement : sniper_b --setup (fenêtre placée en (60, 80) par AIMLABS_SETUP_POS) + agent_a ; puis, dans la session graphique du Mac (remote/mac_job.sh) : glisser dans l'aperçu
de la page Capture (déplace le carré), clic sur l'onglet « Touches », clic sur la case « Tir automatique : bascule », une touche Pause « appuyée » par le clavier virtuel du Pi
l'assigne, Entrée valide. Vérifie ~/.aimlabs/sniper_b.json : décalage non nul (glisser), touche PAUSE (clic + flux du Pi). Les clics passent par remote/mac_input.py (CoreGraphics).
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import ab_bench as A  # noqa: E402

PI = "192.168.1.101"
W, H, HEAD, TABS, FOOT = 1280, 800, 64, 44, 76
log = "/tmp/aim_click_b.log"


def main():
    A.ssh("pkill -f '[s]niper_b.py'; rm -f ~/.aimlabs/sniper_b.json")
    cmd = (f"cd ~/aimlabs-remote/repo && exec env AIMLABS_SETUP_POS=60,80 {A.VENV} remote/poc/sniper_b.py --connect listen:7777 --model {A.MODEL} --stats 5 "
           f"--fire-via pi:{PI} --setup > {log} 2>&1 < /dev/null")
    subprocess.run(["bash", os.path.join(ROOT, "remote", "mac_job.sh"), f"rm -f {log}; nohup bash -c '{cmd}' > /dev/null 2>&1 < /dev/null &"], capture_output=True, text=True, timeout=120)
    for _ in range(60):
        if "en attente de l'agent" in A.ssh(f"cat {log} 2>/dev/null"):
            break
        time.sleep(0.5)
    agent = subprocess.Popen([sys.executable, os.path.join(HERE, "agent_a.py"), "--no-bridge", "--connect", A.DATA_HOST + ":7777", "--jpeg", "60"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(6)
    subprocess.run(["scp", "-q", "-i", A.KEY, os.path.join(ROOT, "remote", "pi", "uinput_keys.py"), f"claude@{PI}:/tmp/uinput_keys.py"])
    mi = "python3 ~/aimlabs-remote/repo/remote/mac_input.py"
    # Position du contenu mesurée sur une capture réelle (macOS 26.5) : AIMLABS_SETUP_POS=60,80 place la fenêtre à x = 60 et à y = 80 SOUS la barre de menus (33 pt) ;
    # la barre de titre fait ~32 pt : le contenu commence en (60, 145). Pas de System Events ici (ses requêtes de fenêtres échouent par moments : -1719 / -25211).
    CX, CY = 60, 145
    job = f"""
{mi} click {CX + 300} {CY - 16}                     # barre de titre : active la fenêtre
sleep 1
{mi} drag {CX + 440} {CY + 380} {CX + 600} {CY + 450}
sleep 1
{mi} click {CX + 208} {CY + HEAD + 25}              # onglet « Touches »
sleep 1
{mi} click {CX + 755} {CY + 201}                    # case « Tir automatique : bascule »
sleep 11                                            # le clavier virtuel du Pi appuie sur Pause pendant ce temps (11 s après le départ : le « runner » met quelques secondes à démarrer le job)
{mi} click {CX + W - 36 - 80} {CY + H - FOOT + 38}  # bouton « Valider »
sleep 2
"""
    pi = subprocess.Popen(["ssh", "-i", A.KEY, f"claude@{PI}", "sudo python3 /tmp/uinput_keys.py --keys PAUSE --taps 1 --settle 11"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(["bash", os.path.join(ROOT, "remote", "mac_job.sh")], input=job.encode("utf-8"), capture_output=True, timeout=120)      # octets : le mode texte de Windows ajouterait des CR
    print(r.stdout.decode("utf-8", "replace").strip(), r.stderr.decode("utf-8", "replace").strip()[-400:])
    pi.wait(timeout=30)
    agent.kill()
    out = A.ssh(f"cat {log}; cat ~/.aimlabs/sniper_b.json 2>/dev/null")
    A.ssh("pkill -f '[s]niper_b.py'")
    try:
        vals = json.loads(out[out.rindex(chr(10) + "{") + 1:])
    except ValueError:
        print(out)
        sys.exit("pas de fichier de réglages : la fenêtre n'a pas été validée")
    A.ssh("rm -f ~/.aimlabs/sniper_b.json")
    cap = vals["capture"]
    ok1 = (cap["dx"], cap["dy"]) != (0, 0)
    ok2 = vals["auto_toggle"]["key"] == "PAUSE"
    verdict1 = "ok" if ok1 else "FAUX : rien n'a bougé"
    print(f"glisser dans l'aperçu : décalage ({cap['dx']}, {cap['dy']}) -> {verdict1}")
    print(f"clic sur la case + touche Pause du Pi : {vals['auto_toggle']} -> {'ok' if ok2 else 'FAUX'}")
    print("RÉSULTAT :", "TOUT EST BON" if ok1 and ok2 else "ÉCHEC")
    sys.exit(0 if ok1 and ok2 else 1)


if __name__ == "__main__":
    main()
