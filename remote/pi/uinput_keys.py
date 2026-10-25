#!/usr/bin/env python3
"""Clavier VIRTUEL (uinput) pour tester le flux de touches du Pi sans toucher au clavier : à lancer SUR le Pi (sudo).

    sudo python3 uinput_keys.py --keys PAUSE,SCROLLLOCK --taps 3 --hold-ms 60 --gap-ms 150 [--held-ms 1500]

Crée un clavier « aimlabs-test-kbd » (btkbd le détecte dans les 2 s et le relaie comme un vrai clavier : les touches arrivent donc aussi au PC Windows, d'où des touches
inoffensives par défaut : Pause, Arrêt défil.), attend qu'il soit pris en charge, appuie `--taps` fois chaque touche, puis en maintient une `--held-ms`. Affiche l'horloge
monotone du Pi (ns) de chaque évènement, à comparer aux évènements reçus par B (voir remote/poc/pi_keys_test.py).
"""
import argparse
import time

from evdev import UInput, ecodes as E


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keys", default="PAUSE,SCROLLLOCK")
    ap.add_argument("--taps", type=int, default=3)
    ap.add_argument("--hold-ms", type=float, default=60)
    ap.add_argument("--gap-ms", type=float, default=150)
    ap.add_argument("--held-ms", type=float, default=0, help="en fin de test, maintient la première touche ce temps (test du maintien)")
    ap.add_argument("--settle", type=float, default=4.0, help="attente de la prise en charge par btkbd (analyse toutes les 2 s)")
    a = ap.parse_args()
    keys = [getattr(E, "KEY_" + k.strip().upper()) for k in a.keys.split(",")]
    caps = {E.EV_KEY: sorted(set(keys + [E.KEY_A, E.KEY_Z, E.KEY_ENTER, E.KEY_SPACE, E.KEY_LEFTCTRL]))}
    with UInput(caps, name="aimlabs-test-kbd") as ui:
        time.sleep(a.settle)
        for _ in range(a.taps):
            for k in keys:
                for v in (1, 0):
                    ui.write(E.EV_KEY, k, v)
                    ui.syn()
                    print(v, E.KEY[k], time.monotonic_ns(), flush=True)
                    time.sleep((a.hold_ms if v else a.gap_ms) / 1000)
        if a.held_ms:
            ui.write(E.EV_KEY, keys[0], 1)
            ui.syn()
            print(1, E.KEY[keys[0]], time.monotonic_ns(), flush=True)
            time.sleep(a.held_ms / 1000)
            ui.write(E.EV_KEY, keys[0], 0)
            ui.syn()
            print(0, E.KEY[keys[0]], time.monotonic_ns(), flush=True)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
