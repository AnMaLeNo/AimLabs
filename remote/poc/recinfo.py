"""Résumé d'un enregistrement d'agent_a (`agent_a.py --record`) : nombre d'images, durée, taille, régularité de la cadence.

    python recinfo.py remote/recordings/live_36ms_30pass.bin
"""
import json
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sdk", "python"))
from aimlabs import remote as R  # noqa: E402


def main() -> None:
    with open(sys.argv[1], "rb") as f:
        assert f.readline() == b"AIMREC1\n", "ce n'est pas un enregistrement d'agent_a --record"
        print(json.loads(f.read(struct.unpack("<I", f.read(4))[0])))
        ts, sizes = [], []
        while True:
            b = f.read(4)
            if len(b) < 4:
                break
            n = struct.unpack("<I", b)[0]
            p = f.read(n)
            fid, t, W, H, x0, y0, cw, ch, codec = R.FRAME_HDR.unpack_from(p, 0)
            ts.append(t)
            sizes.append(n - R.FRAME_HDR.size)
    gaps = np.diff(ts) / 1000
    print(f"{len(ts)} images en {(ts[-1] - ts[0]) / 1e6:.1f} s ({len(ts) / ((ts[-1] - ts[0]) / 1e6):.0f}/s), {np.mean(sizes) / 1000:.1f} Ko/img, "
          f"région {(x0, y0, cw, ch)} dans {W}x{H}, codec {codec}")
    print(f"écart entre images (ms) : moy {gaps.mean():.2f}, p50 {np.median(gaps):.2f}, p99 {np.percentile(gaps, 99):.2f}, max {gaps.max():.1f}; "
          f"écarts > 20 ms : {int((gaps > 20).sum())}")


if __name__ == "__main__":
    main()
