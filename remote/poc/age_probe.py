"""PC A : mesure l'ÂGE du contenu d'une capture d'écran, avec le pont interne comme vérité terrain.

Le jeu doit tourner en mode `full` (images publiées) avec une cible qui bouge (`--pattern=line --speed=8`) :
pour chaque image d'écran (screencap.py), on retrouve parmi les images récentes du pont celle qui lui ressemble
le plus (somme des écarts de pixels) ; son `capture_usec` (horloge du jeu) donne l'instant de rendu du contenu.

    age = instant où le programme reçoit l'image d'écran (horloge du jeu) − instant de rendu du contenu

Affiche moyenne, écart-type et répartition de cet âge : sa moyenne est le biais de temps d'une capture d'écran,
son écart-type la gigue qu'aucun algo ne peut compenser sans code temporel dans l'image.
"""
import collections
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "remote", "level1"))

from aimlabs import AimClient  # noqa: E402
from aimlabs import protocol as P  # noqa: E402
from screencap import ScreenCapture  # noqa: E402

SIDE = 256


def norm(img):
    g = img[..., :3].astype(np.float32).mean(axis=2)
    g = g[::2, ::2]
    return (g - g.mean()) / (g.std() + 1e-6)


def now_us() -> int:
    return time.perf_counter_ns() // 1000


def main(seconds=12.0):
    aim = AimClient(timeout=20)
    cap = ScreenCapture(crop=SIDE).start()
    W, H = cap._win
    x0, y0 = (W - SIDE) // 2, (H - SIDE) // 2
    print(f"capture {cap.backend_name}, fenêtre {W}x{H}, carré {SIDE} en ({x0},{y0})", flush=True)

    hist = collections.deque(maxlen=160)           # (capture_usec du jeu, crop RGB)
    stop = threading.Event()
    off = {"v": 0}
    samples = collections.deque(maxlen=400)

    def bridge_loop():
        last = 0
        while not stop.is_set():
            fid = aim.latest_frame_id
            if fid == last or fid == 0:
                time.sleep(0.0002)
                continue
            last = fid
            slot = aim._u32.unpack_from(aim._buf, P.OFF_LATEST_SLOT)[0]
            base = aim.frames_offset + slot * aim.frame_slot_size
            seq1, w, h, stride, f_id, cap_us, *_ = P.FRAME_HEADER.unpack_from(aim._buf, base)
            if seq1 & 1 or f_id != fid:
                continue
            raw = np.frombuffer(aim._buf, np.uint8, count=h * stride, offset=base + P.FRAME_HEADER_SIZE).reshape(h, stride // 4, 4)
            crop = norm(raw[y0:y0 + SIDE, x0:x0 + SIDE, :3])
            if aim._u32.unpack_from(aim._buf, base)[0] != seq1:
                continue
            hist.append((cap_us, crop))

    def clock_loop():
        while not stop.is_set():
            try:
                s = aim.state()
                samples.append(s["time_usec"] - now_us())
                off["v"] = max(samples)
            except RuntimeError:
                pass
            time.sleep(0.002)

    threading.Thread(target=bridge_loop, daemon=True).start()
    threading.Thread(target=clock_loop, daemon=True).start()
    time.sleep(1.0)

    pending = []     # (t_ret_game_us, image)
    ages, diffs, second = [], [], []
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        c = cap.latest(timeout=0.5)
        if c is not None:
            pending.append((c.t_us + off["v"], norm(c.image)))
        # on apparie une capture quand le pont a publié des images plus récentes qu'elle (+ latence de lecture)
        newest = hist[-1][0] if hist else 0
        while pending and pending[0][0] + 9000 < newest:
            t_ret, img = pending.pop(0)
            cands = [(cu, cr) for cu, cr in list(hist) if t_ret - 30000 <= cu <= t_ret + 1000]
            if len(cands) < 3:
                continue
            d = [float(np.abs(cr - img).sum()) for _, cr in cands]
            order = np.argsort(d)
            best = int(order[0])
            ages.append((t_ret - cands[best][0]) / 1000)
            diffs.append((d[best], float(np.median(d)), d[int(order[1])] if len(d) > 1 else 0))
    stop.set()
    cap.stop()
    a = np.array(ages)
    ok = np.array([d[1] > 0 and d[0] < 0.6 * d[1] for d in diffs])      # appariement net : un minimum franc devant la médiane (contenu en mouvement)
    print(f"{len(a)} images appariées, dont {int(ok.sum())} sans ambiguïté")
    for name, x in (("toutes", a), ("sans ambiguïté", a[ok])):
        if len(x) > 2:
            print(f"âge du contenu ({name}) : moyenne {x.mean():.2f} ms · écart-type {x.std():.2f} · p5 {np.percentile(x, 5):.2f} · p50 {np.median(x):.2f} · p95 {np.percentile(x, 95):.2f}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 12.0)
