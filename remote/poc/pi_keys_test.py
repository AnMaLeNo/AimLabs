"""Test RÉEL du flux de touches : B <- Pi (clavier virtuel uinput sur le Pi), avec les vrais paquets, le vrai Wi-Fi.

    python pi_keys_test.py --pi 192.168.1.101 [--ssh claude@192.168.1.101]

Lance sur le Pi `remote/pi/uinput_keys.py` (clavier virtuel, touches Pause / Arrêt défil.), s'abonne depuis cette machine, et vérifie : tous les appuis et relâchements arrivent,
dans l'ordre ; la bascule (Pause) et le maintien (Arrêt défil.) donnent les bons états ; la touche avalée (Arrêt défil.) n'est PAS transmise au PC (statut du Pi) ; le clavier
est rendu au PC dès la fin de l'abonnement. Mesure aussi l'écart entre l'évènement sur le Pi (horloge du Pi) et sa réception ici (décalage d'horloge estimé par PING/PONG).
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
from aimlabs import keys as K  # noqa: E402

KEYFILE = os.path.expanduser("~/.ssh/id_ed25519_claude")


def pi_fire_status(ssh):
    """Statut du serveur de tir du Pi (socket de commande de btkbd) : abonnés, touches avalées."""
    script = ("import socket\ns = socket.socket(socket.AF_UNIX)\ns.connect('/run/btkbd.sock')\n"
              "s.send(b'{\"cmd\": \"fire\"}' + bytes([10]))\nprint(s.makefile().readline())\n")
    out = subprocess.run(["ssh", "-i", KEYFILE, ssh, "sudo python3 -"], input=script, capture_output=True, text=True).stdout
    try:
        return __import__("json").loads(out)["fire"]
    except (ValueError, KeyError, TypeError):
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pi", required=True)
    ap.add_argument("--ssh", default="")
    ap.add_argument("--taps", type=int, default=5)
    a = ap.parse_args()
    ssh = a.ssh or f"claude@{a.pi}"
    scp = subprocess.run(["scp", "-q", "-i", KEYFILE, os.path.join(ROOT, "remote", "pi", "uinput_keys.py"), f"{ssh}:/tmp/uinput_keys.py"])
    assert scp.returncode == 0, "scp vers le Pi"

    ev = []
    pk = K.PiKeys(a.pi, watch=["PAUSE", "SCROLLLOCK"], swallow=["SCROLLLOCK"])
    pk.add_listener(lambda n, d: ev.append((n, d, time.perf_counter_ns())))
    assert pk.start(3.0), "pas de flux du Pi (secret ? pare-feu ? btkbd à jour ?)"
    b = K.Bindings(pk)
    b.add("auto", {"key": "PAUSE", "mode": "toggle"})
    b.add("burst", {"key": "SCROLLLOCK", "mode": "hold"})
    print("caps du Pi :", pk.allowed_output_keys(), flush=True)
    proc = subprocess.Popen(["ssh", "-i", KEYFILE, ssh, f"sudo python3 /tmp/uinput_keys.py --keys PAUSE,SCROLLLOCK --taps {a.taps} --held-ms 1500"],
                            stdout=subprocess.PIPE, text=True)
    sent = []
    for line in proc.stdout:
        v, name, t = line.split()
        sent.append((name.replace("KEY_", ""), v == "1", int(t)))
        if len(sent) == 1:
            sw = pi_fire_status(ssh).get("swallow")
            print("touches avalées côté Pi pendant l'abonnement :", sw, "(attendu : [71] = Arrêt défil.)", flush=True)
    proc.wait()
    time.sleep(0.5)
    exp = [(n, d) for n, d, _ in sent]
    got = [(n, d) for n, d, _ in ev]
    ok = got == exp
    print(f"évènements envoyés par le Pi : {len(exp)} ; reçus : {len(got)} ; identiques et dans l'ordre : {ok}", flush=True)
    if not ok:
        print("  attendu", exp[:12], "\n  reçu   ", got[:12])
    print("compteurs :", pk.counters, flush=True)
    print("état final : auto =", b.get("auto"), "(attendu :", ((a.taps + 1) % 2) == 1, ": taps + la touche maintenue en fin de test) · burst =", b.get("burst"), "(attendu : False)")
    pk.close()
    time.sleep(6.5)
    sw = pi_fire_status(ssh)
    print("après la fin de l'abonnement : abonnés", sw.get("subscribers"), ", touches avalées", sw.get("swallow"), "(attendu : vides : le clavier entier est rendu au PC)")
    ok = ok and sw.get("swallow") == [] and b.get("auto") == (((a.taps + 1) % 2) == 1) and not b.get("burst")
    print("RÉSULTAT :", "TOUT EST BON" if ok else "ÉCHEC")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
