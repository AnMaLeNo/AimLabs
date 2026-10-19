"""Toy aim-assist to prove the loop frame -> decision -> command end to end.

Heuristic only (no learning): the Kenney characters have skin-coloured faces, the
arena is grey/blue/saturated blocks, so we mask "skin-like" pixels, take the blob
closest to the crosshair, nudge the view towards it and fire once centred.
Expect false positives on brown blocks; this is a demo, not a good bot.

    python skin_bot.py            # attach to a running game
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from aimlabs import AimClient  # noqa: E402

GAIN = 0.6          # pixels of mouse per pixel of error (sensitivity-dependent)
FIRE_RADIUS = 6.0   # px
MIN_PIXELS = 12


def find_target(img: np.ndarray):
    """Return (cx, cy, count) of the skin blob nearest to the frame centre, or None."""
    rgb = img[..., :3].astype(np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mask = (r > 95) & (g > 40) & (b > 20) & (r > g) & (g > b) & (r - b > 25) & (r - g > 10) & (r - g < 90)
    ys, xs = np.nonzero(mask)
    if xs.size < MIN_PIXELS:
        return None
    h, w = mask.shape
    # Cluster coarsely: 16 px cells, keep the cell with most skin pixels near the centre.
    cell = 16
    cells = {}
    for x, y in zip(xs // cell, ys // cell):
        cells[(x, y)] = cells.get((x, y), 0) + 1
    cx0, cy0 = w / 2 / cell, h / 2 / cell
    best = min(cells.items(), key=lambda kv: ((kv[0][0] - cx0) ** 2 + (kv[0][1] - cy0) ** 2) / max(kv[1], 1))
    (bx, by), n = best
    sel = (xs // cell >= bx - 1) & (xs // cell <= bx + 1) & (ys // cell >= by - 1) & (ys // cell <= by + 1)
    return xs[sel].mean(), ys[sel].mean(), int(sel.sum())


def main() -> None:
    with AimClient() as c:
        print("connected, mode", c.mode)
        last_fire = 0.0
        while True:
            f = c.frame(timeout=2.0)
            if f is None:
                if not c.game_alive():
                    print("game gone")
                    return
                continue
            t = find_target(f.image)
            if t is None:
                continue
            cx, cy, n = t
            ex, ey = cx - f.width / 2, cy - f.height / 2
            c.move(ex * GAIN, ey * GAIN, frame_id=f.id)
            if abs(ex) < FIRE_RADIUS and abs(ey) < FIRE_RADIUS and time.monotonic() - last_fire > 0.12:
                c.fire(frame_id=f.id)
                last_fire = time.monotonic()
                s = c.state()
                print(f"frame #{f.id}: blob {n}px at ({cx:.0f},{cy:.0f}) -> fire; shots={s['shots']} hits={s['hits']} kills={s['kills']} latency={s['avg_latency_usec'] / 1000:.1f} ms")


if __name__ == "__main__":
    main()
