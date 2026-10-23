"""What does the game give out? Attaches to a running game and lists every piece of information it can read.

    python info_check.py [--expect real|training]

`real` : the State block carries only the keys, the heartbeat is an opaque counter, frames (if any) carry no
capture time / view angles → exit code 1 if anything else is non-zero.
`training` : everything is available (no check on the details).
"""
import argparse
import sys
import time

from aimlabs import AimClient
from aimlabs import protocol as P

LEAKS = ["tick", "time_usec", "yaw", "pitch", "fov", "sens", "real_dx", "real_dy", "assist_dx", "assist_dy", "shots", "hits",
         "headshots", "kills", "cmds_applied", "cmds_dropped", "last_latency_usec", "avg_latency_usec", "fps", "frames_published"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", choices=["real", "training"])
    args = ap.parse_args()
    with AimClient(timeout=15) as c:
        mode = c.mode
        print("mode        :", mode)
        time.sleep(0.5)
        s = c.state()
        leaks = {k: s[k] for k in LEAKS if s.get(k) not in (0, 0.0, None)}
        leaks.update({k: s[k] for k in ("pos", "vel") if any(s[k])})
        keys = s["keys"]
        info_keys = {k: v for k, v in keys.items() if k in ("mouse_captured", "on_floor") and v}
        leaks.update(info_keys)
        hb0 = c._u64.unpack_from(c._buf, P.OFF_GAME_HEARTBEAT)[0]
        time.sleep(0.05)
        hb1 = c._u64.unpack_from(c._buf, P.OFF_GAME_HEARTBEAT)[0]
        print("keys        :", {k: v for k, v in keys.items() if v} or "(aucune touche)", "| clavier", sorted(s["keys_down"]), "souris", sorted(s["mouse_down"]))
        print("état (hors touches) non nul :", leaks or "rien")
        is_clock = (hb1 - hb0) > 20000        # 50 ms plus tard : une horloge en µs avance de ~50000, un compteur de quelques frames
        print(f"heartbeat   : {hb0} → {hb1}  ({'horloge du jeu (µs)' if is_clock else 'compteur opaque'})")
        meta = None
        if mode["vision"]:
            f = c.frame(timeout=3.0)
            if f is not None:
                meta = {"capture_usec": f.capture_usec, "yaw": f.yaw, "pitch": f.pitch, "fov": f.fov, "render_frame": f.render_frame}
                print("image       :", f"{f.width}x{f.height}", "méta", {k: v for k, v in meta.items() if v} or "vide")
        else:
            print("image       : aucune (mode sans vision)")
        if args.expect == "real":
            bad = bool(leaks) or is_clock or bool(meta and any(meta.values())) or mode["info_state"] or mode["info_meta"]
            print("VERDICT     :", "FUITE — le profil réel laisse passer de l'information" if bad else "OK — rien d'autre que les touches")
            return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
