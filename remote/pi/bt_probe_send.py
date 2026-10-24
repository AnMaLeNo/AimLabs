"""Côté Pi de la mesure de latence « Pi -> Bluetooth HID -> Windows » : envoie des frappes de la touche Pause (usage HID 0x48) à
intervalle fixe par le socket local du relais (/run/btkbd.sock, commande `raw`) et note l'instant d'envoi (horloge murale précise).
À rapprocher des horloges de la sonde Windows (remote/poc/kbd_probe_win.py) avec remote/poc/clock_sync.py.

    sudo python3 bt_probe_send.py --n 300 --interval-ms 100 --hold-ms 20 --out /tmp/pi_probe.jsonl

Chaque ligne : {"i": .., "send_ns": <wall_ns avant l'envoi>, "ack_ns": <wall_ns après la réponse du relais>, "kind": "down"/"up"}
"""
import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "poc"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clocks import wall_ns  # noqa: E402

PAUSE = 0x48


def wait_until(t_mono_ns):
    """Sommeil jusqu'à ~1,5 ms avant l'échéance, puis attente active (précision de l'ordre de la microseconde)."""
    while True:
        d = t_mono_ns - time.perf_counter_ns()
        if d <= 0:
            return
        if d > 1_500_000:
            time.sleep((d - 1_200_000) / 1e9)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--interval-ms", type=float, default=100.0)
    ap.add_argument("--hold-ms", type=float, default=20.0)
    ap.add_argument("--out", default="/tmp/pi_probe.jsonl")
    ap.add_argument("--usage", type=lambda v: int(v, 0), default=PAUSE, help="touche HID (0x48 Pause ; 0x47 ScrollLock : Windows renvoie alors un rapport de LED = écho pour mesurer l'aller-retour)")
    ap.add_argument("--jitter-ms", type=float, default=0.0, help="décalage aléatoire de chaque intervalle (évite de se caler sur un rythme du lien)")
    a = ap.parse_args()
    s = socket.socket(socket.AF_UNIX)
    s.connect("/run/btkbd.sock")
    f = s.makefile("rw")

    def send(hexs):
        t0 = wall_ns()
        f.write(json.dumps({"cmd": "raw", "hex": hexs}) + "\n")
        f.flush()
        r = json.loads(f.readline())
        return t0, wall_ns(), r

    import random
    out = open(a.out, "w")
    t_next = time.perf_counter_ns() + 500_000_000
    for i in range(a.n):
        wait_until(t_next)
        t0, t1, r = send(f"00{a.usage:02x}")
        out.write(json.dumps({"i": i, "send_ns": t0, "ack_ns": t1, "kind": "down", "ok": r.get("ok")}) + "\n")
        time.sleep(a.hold_ms / 1000)
        t0, t1, r = send("00")
        out.write(json.dumps({"i": i, "send_ns": t0, "ack_ns": t1, "kind": "up", "ok": r.get("ok")}) + "\n")
        t_next += int((a.interval_ms + random.uniform(0, a.jitter_ms)) * 1e6)
    out.close()
    print(f"{a.n} frappes -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
