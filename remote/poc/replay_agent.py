"""Agent de REJEU : remplace l'agent du PC A (agent_a.py) en renvoyant des images enregistrées (`agent_a.py --record`).

Même protocole que l'agent réel (HELLO, FRAME, STATE ; commandes en retour), même cadence que l'enregistrement, mais sans jeu ni
capture d'écran : les mêmes entrées, rejouées autant de fois qu'on veut, pour comparer deux réglages de B (mode normal /
mode debug…) à entrée identique. `capture_usec` est l'instant d'émission (horloge locale) ; la ligne de stats est celle de
l'agent réel (« rtt capture→cmd reçue »).

    python replay_agent.py REC --connect <ip de B>:7777 [--seconds 60] [--speed 1.0] [--cmd-log cmds.csv]     # B : « listen:7777 »
    python replay_agent.py REC --listen 0.0.0.0:7777                                                          # B se connecte
"""
import argparse
import collections
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk", "python"))
from aimlabs import remote as R  # noqa: E402


def now_us() -> int:
    return time.perf_counter_ns() // 1000


def load(path):
    """(hello json, [(t_us, payload sans en-tête, en-tête décodé)]) d'un enregistrement d'agent_a."""
    with open(path, "rb") as f:
        assert f.readline() == b"AIMREC1\n", "ce n'est pas un enregistrement d'agent_a --record"
        hello = json.loads(f.read(struct.unpack("<I", f.read(4))[0]))
        frames = []
        while True:
            b = f.read(4)
            if len(b) < 4:
                break
            p = f.read(struct.unpack("<I", b)[0])
            fid, t, W, H, x0, y0, cw, ch, codec = R.FRAME_HDR.unpack_from(p, 0)
            frames.append((t, p[R.FRAME_HDR.size:], (W, H, x0, y0, cw, ch, codec)))
    return hello, frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rec")
    ap.add_argument("--connect", default="", help="hote:port de B (B lance « listen:port »)")
    ap.add_argument("--listen", default="0.0.0.0:7777")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--speed", type=float, default=1.0, help="facteur sur la cadence enregistrée")
    ap.add_argument("--state-hz", type=float, default=60.0)
    ap.add_argument("--stats", type=float, default=2.0)
    ap.add_argument("--cmd-log", default="", help="CSV des commandes reçues : t_s, type, frame_id, dx, dy, rtt_ms")
    ap.add_argument("--keys", default="", help="touches maintenues dans STATE (ex. ALT) pour armer B")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if sys.platform == "win32":
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x80)
    hello, frames = load(args.rec)
    span = frames[-1][0] - frames[0][0]
    print(f"rejeu : {len(frames)} images, {span / 1e6:.2f} s, {sum(len(f[1]) for f in frames) / len(frames) / 1000:.1f} Ko/img", flush=True)

    if args.connect:
        host, port = args.connect.rsplit(":", 1)
        while True:
            try:
                conn = socket.create_connection((host, int(port)), timeout=2.0)
                break
            except OSError:
                time.sleep(0.3)
    else:
        host, port = args.listen.rsplit(":", 1)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port)))
        srv.listen(1)
        conn, _ = srv.accept()
    conn.settimeout(None)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("rejeu : B connecté", flush=True)

    stop = threading.Event()
    lock = threading.Lock()
    cap_times = collections.OrderedDict()
    rtts, cmds = [], {"n": 0}
    kinds = collections.Counter()
    t_start = time.perf_counter()
    log = open(args.cmd_log, "w") if args.cmd_log else None
    if log:
        log.write("t_s,type,frame_id,dx,dy,rtt_ms\n")

    def send(typ, payload):
        with lock:
            R.send_msg(conn, typ, payload)

    def cmd_loop():
        try:
            while not stop.is_set():
                typ, p = R.recv_msg(conn)
                if typ != R.T_CMD:
                    continue
                t = now_us()
                kind, fid, dx, dy = R.CMD.unpack(p)
                rtt = (t - cap_times[fid]) / 1000 if fid in cap_times else None
                if rtt is not None:
                    rtts.append(rtt)
                cmds["n"] += 1
                kinds[kind] += 1
                if log:
                    log.write(f"{time.perf_counter() - t_start:.4f},{kind},{fid},{dx:.3f},{dy:.3f},{'' if rtt is None else f'{rtt:.3f}'}\n")
        except (ConnectionError, OSError):
            stop.set()

    def state_loop():
        keys = [k for k in args.keys.split(",") if k]
        try:
            while not stop.is_set():
                st = {"keys_down": keys, "physical_keys_down": keys, "mouse_down": [], "game_alive": True,
                      "time_usec": now_us(), "fps": 164.0, "pos": [0, 0, 0], "vel": [0, 0, 0]}
                send(R.T_STATE, json.dumps(st).encode())
                time.sleep(1.0 / args.state_hz)
        except OSError:
            stop.set()

    send(R.T_HELLO, json.dumps(hello).encode())
    threading.Thread(target=cmd_loop, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    sys.setswitchinterval(1e-4)

    fid = 0
    sent = nbytes = 0
    t_stats = time.monotonic()
    t0 = time.perf_counter()
    deadline = t0 + args.seconds
    base = None            # instant (perf_counter µs) où la lecture de la boucle courante a commencé
    try:
        while not stop.is_set() and time.perf_counter() < deadline:
            loop_t0 = frames[0][0]
            base = now_us()
            for t_rec, data, (W, H, x0, y0, cw, ch, codec) in frames:
                due = base + (t_rec - loop_t0) / args.speed
                while True:
                    w = due - now_us()
                    if w <= 0:
                        break
                    if w > 2500:
                        time.sleep((w - 2000) / 1e6)
                if stop.is_set() or time.perf_counter() >= deadline:
                    break
                fid += 1
                t = now_us()
                cap_times[fid] = t
                while len(cap_times) > 4096:
                    cap_times.popitem(last=False)
                send(R.T_FRAME, R.FRAME_HDR.pack(fid, t, W, H, x0, y0, cw, ch, codec) + data)
                sent += 1
                nbytes += len(data)
                el = time.monotonic() - t_stats
                if el >= args.stats:
                    line = f"A : {sent / el:.0f} img/s · {nbytes / max(sent, 1) / 1000:.1f} Ko/img · cmd {cmds['n']} {dict(kinds)}"
                    r = np.array(rtts) if rtts else None
                    if r is not None and len(r):
                        line += f" · rtt capture→cmd reçue : moy {r.mean():.1f} ms, p50 {np.median(r):.1f}, p99 {np.percentile(r, 99):.1f}, max {r.max():.1f}"
                    print(line, flush=True)
                    sent = nbytes = 0
                    cmds["n"] = 0
                    kinds.clear()
                    rtts.clear()
                    t_stats = time.monotonic()
    except (ConnectionError, OSError):
        pass
    except Exception:
        traceback.print_exc()
    finally:
        stop.set()
        if log:
            log.close()
        print(f"rejeu : fin après {time.perf_counter() - t0:.2f} s, {fid} images envoyées", flush=True)
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
