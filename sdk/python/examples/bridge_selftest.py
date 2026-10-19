"""End-to-end check of the AimBridge: attach to a running game (or launch one with
--launch), read frames, send a move command and a fire command, verify the game saw
them, and report the capture→command latency the game measured.

    python bridge_selftest.py --launch --godot ~/godot/godot --save /tmp/frame.png
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from aimlabs import AimClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true", help="start the game from ../../game")
    ap.add_argument("--godot", default=os.environ.get("GODOT", "godot"))
    ap.add_argument("--mode", default="full")
    ap.add_argument("--shm", default="aimlabs")
    ap.add_argument("--save", help="save the first frame as PNG (needs Pillow)")
    ap.add_argument("--godot-args", default="", help="extra args, e.g. '--rendering-method gl_compatibility'")
    args = ap.parse_args()

    proc = None
    if args.launch:
        game = os.path.join(os.path.dirname(__file__), "..", "..", "..", "game")
        cmd = [args.godot, "--path", game] + args.godot_args.split() + ["--", f"--mode={args.mode}", f"--shm={args.shm}"]
        proc = subprocess.Popen(cmd)
    try:
        return run(args)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_until(pred, timeout: float = 3.0):
    """Poll the state until pred(state) holds (the game applies commands one frame later)."""
    deadline = time.monotonic() + timeout
    while True:
        st = pred()
        if st is not None:
            return st
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def run(args) -> int:
    ok = True
    t0 = time.monotonic()
    with AimClient(args.shm, timeout=60.0) as c:
        print(f"attached after {time.monotonic() - t0:.1f}s: pid={c.game_pid} max={c.max_width}x{c.max_height} mode={c.mode}")

        f = c.frame(timeout=15.0)
        if f is None:
            print("FAIL: no frame published")
            return 1
        print(f"frame #{f.id}: {f.width}x{f.height} yaw={f.yaw:.1f} pitch={f.pitch:.1f} mean_rgb={f.image[..., :3].mean():.1f}")
        if f.image[..., :3].mean() < 2.0:
            print("FAIL: frame is black")
            ok = False
        if args.save:
            from PIL import Image
            Image.fromarray(f.image, "RGBA").save(args.save)
            print("saved", args.save)

        # Frame rate over one second.
        n = 0
        t1 = time.monotonic()
        last = f
        while time.monotonic() - t1 < 1.0:
            g = c.frame(timeout=1.0)
            if g:
                n += 1
                last = g
        print(f"{n} frames in 1 s; latest #{last.id}")
        if n == 0:
            print("FAIL: frames stopped")
            ok = False

        # Move command: yaw must change by dx * sensitivity (x right => yaw decreases).
        s0 = c.state()
        c.move(100.0, 0.0, frame_id=last.id)
        s1 = wait_until(lambda: (lambda st: st if st["cmds_applied"] + st["cmds_dropped"] > s0["cmds_applied"] + s0["cmds_dropped"] else None)(c.state())) or c.state()
        time.sleep(0.05)
        s1 = c.state()
        dyaw = (s1["yaw"] - s0["yaw"] + 180.0) % 360.0 - 180.0
        expected = -100.0 * s0["sens"]
        print(f"move(100, 0): yaw {s0['yaw']:.2f} -> {s1['yaw']:.2f} (d={dyaw:.2f}, expected {expected:.2f}), cmds applied={s1['cmds_applied']} dropped={s1['cmds_dropped']}")
        if c.mode["move"]:
            if abs(dyaw - expected) > 0.05:
                print("FAIL: move not applied")
                ok = False
        elif s1["cmds_dropped"] != s0["cmds_dropped"] + 1:
            print("FAIL: move should have been dropped in this mode")
            ok = False
        print(f"latency (capture -> applied, game clock): last={s1['last_latency_usec'] / 1000:.1f} ms")

        # Fire command.
        c.fire(frame_id=c.last_frame_id)
        s2 = wait_until(lambda: (lambda st: st if st["shots"] > s1["shots"] or st["cmds_dropped"] > s1["cmds_dropped"] else None)(c.state())) or c.state()
        print(f"fire(): shots {s1['shots']} -> {s2['shots']}, hits {s2['hits']}")
        if c.mode["fire"]:
            if s2["shots"] != s1["shots"] + 1:
                print("FAIL: fire not applied")
                ok = False
        elif s2["cmds_dropped"] != s1["cmds_dropped"] + 1:
            print("FAIL: fire should have been dropped in this mode")
            ok = False

        print("keys:", s2["keys"], "pos:", tuple(round(v, 2) for v in s2["pos"]), "fps:", round(s2["fps"]))
        k = c.keys()
        if not (isinstance(k.down, frozenset) and isinstance(k.mouse_down, frozenset)):
            print("FAIL: keys() unreadable")
            ok = False
        print("keyboard down:", sorted(k.down), "physical:", sorted(k.physical_down), "mouse:", sorted(k.mouse_down))
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
