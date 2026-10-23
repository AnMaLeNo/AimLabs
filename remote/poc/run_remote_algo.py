"""PC B (celui qui fait tourner l'algo) : lance un algo EXISTANT de algos/ sans le modifier, en remplaçant
`AimClient` par `RemoteClient` (image et état reçus de l'agent du PC A par le réseau, commandes renvoyées).

    python run_remote_algo.py --connect <ip du PC A>:7777 algos/yolo_sniper.py [options de l'algo…]

Sur Mac / Linux : un Python avec numpy (+ ultralytics, opencv… selon l'algo) ; copier sdk/python/aimlabs/ et
l'algo suffit, aucun accès au jeu. AIMLABS_AUTOSTART=1 pour qu'il démarre armé (comme dans le benchmark).
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 3 or argv[0] != "--connect":
        print(__doc__)
        return 2
    os.environ["AIMLABS_REMOTE"] = argv[1]
    script = os.path.abspath(argv[2])
    from aimlabs import remote
    remote.install()
    sys.path.insert(0, os.path.dirname(script))
    sys.argv = [script] + argv[3:]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
