"""Tiny vision helpers shared by the example algos. Heuristics only, no learning."""

from typing import Optional, Tuple

import numpy as np


def skin_target(img: np.ndarray, min_pixels: int = 12, cell: int = 16) -> Optional[Tuple[float, float, int]]:
    """(cx, cy, n_pixels) of the skin-coloured blob nearest to the frame centre, or None.

    The Kenney characters have skin-coloured faces/hands and the arena is grey, blue
    and saturated blocks, so a skin mask is a cheap "enemy detector". Expect false
    positives on brown blocks."""
    rgb = img[..., :3].astype(np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mask = (r > 95) & (g > 40) & (b > 20) & (r > g) & (g > b) & (r - b > 25) & (r - g > 10) & (r - g < 90)
    ys, xs = np.nonzero(mask)
    if xs.size < min_pixels:
        return None
    h, w = mask.shape
    cells: dict = {}
    for x, y in zip(xs // cell, ys // cell):
        cells[(x, y)] = cells.get((x, y), 0) + 1
    cx0, cy0 = w / 2 / cell, h / 2 / cell
    (bx, by), _n = min(cells.items(), key=lambda kv: ((kv[0][0] - cx0) ** 2 + (kv[0][1] - cy0) ** 2) / max(kv[1], 1))
    sel = (xs // cell >= bx - 1) & (xs // cell <= bx + 1) & (ys // cell >= by - 1) & (ys // cell <= by + 1)
    return float(xs[sel].mean()), float(ys[sel].mean()), int(sel.sum())
