"""Training aid: decode the game clock the `timecode` information option draws in the image (see game/scripts/timecode.gd).

    from timecode_decode import decode
    t_game_us_low24 = decode(image_rgb, window_w, window_h)      # None if the strip is absent / unreadable

and, run as a script with the game in `--info=training` and its window visible, compares it with the game's own clock to
print how old a screen capture is when the program gets it:   python timecode_decode.py [seconds]

The strip: 26 cells of 4 px, centred horizontally, top edge 110 px above the window centre; cell 0 white and cell 1
black (threshold), then the 24 low bits of Time.get_ticks_usec() (the bridge's `time_usec` clock), MSB first.
"""
import os
import sys
import time

import numpy as np

CELL, BITS = 4, 24


def decode(img: np.ndarray, window_w: int, window_h: int, origin=(0, 0)):
    """`img` = (h, w, 3|4) RGB pixels of a capture whose top-left corner is at `origin` in the window. Returns the 24-bit
    value (µs, wraps every 16.7 s) or None."""
    x0 = window_w // 2 - CELL * (BITS + 2) // 2 - origin[0]
    y0 = window_h // 2 - 110 - origin[1]
    if x0 < 0 or y0 < 0 or y0 + CELL > img.shape[0] or x0 + CELL * (BITS + 2) > img.shape[1]:
        return None
    cells = [int(img[y0 + CELL // 2, x0 + CELL * i + CELL // 2, 1]) for i in range(BITS + 2)]
    hi, lo = cells[0], cells[1]
    if hi - lo < 100:
        return None
    thr = (hi + lo) / 2
    v = 0
    for c in cells[2:]:
        v = (v << 1) | (1 if c > thr else 0)
    return v


def main(seconds: float = 6.0) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..")), sys.path.insert(0, os.path.join(here, "..", "..", "..", "remote", "level1"))
    from aimlabs import AimClient
    from screencap import ScreenCapture

    aim = AimClient(timeout=15)
    cap = ScreenCapture(crop=0).start()
    W, H = cap._win
    offs, ages = [], []
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        c = cap.latest(0.5)
        if c is None:
            continue
        t_local = time.perf_counter_ns() // 1000
        offs.append(aim.state()["time_usec"] - time.perf_counter_ns() // 1000)
        v = decode(c.image, W, H)
        if v is None:
            continue
        d = ((t_local + max(offs[-200:])) - v) & 0xFFFFFF
        ages.append((d - 0x1000000 if d > 0x800000 else d) / 1000)
    a = np.array(ages)
    print(f"{len(a)} lectures ; âge du contenu à la réception : moyenne {a.mean():.2f} ms, écart-type {a.std():.2f}, "
          f"p5 {np.percentile(a, 5):.2f}, p95 {np.percentile(a, 95):.2f}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 6.0)
