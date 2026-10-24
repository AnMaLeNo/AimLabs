"""PC A (celui qui fait tourner le jeu) — agent de référence du mode « deux machines ».

  1. capture l'écran avec remote/level1/screencap.py (rien n'est lu dans le jeu),
  2. envoie le carré central au PC B (algo) — UDP ou TCP,
  3. reçoit les commandes de B et les rejoue dans le jeu via le SDK (mémoire partagée du pont).

Le jeu doit tourner en `--mode=remote` (aucune image publiée, commandes acceptées). Les joueurs écrivent leur
propre version de ce script ; celle-ci sert de preuve de concept et de mesure de référence.

    python agent_a.py --connect <ip de B>:7777 [--transport udp] [--crop 256] [--jpeg 60]    # B : « listen:7777 »
    python agent_a.py --listen 0.0.0.0:7777                                                   # B se connecte

Mesures toutes les 2 s : images/s, débit, temps d'encodage, et la boucle complète « capture → B → commande reçue »
(`rtt`, horloge locale de A : capture de l'image N → arrivée de la commande décidée sur N).
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
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "remote", "level1"))

from aimlabs import AimClient  # noqa: E402
from aimlabs import protocol as P  # noqa: E402
from aimlabs import remote as R  # noqa: E402
from screencap import ScreenCapture  # noqa: E402


def now_us() -> int:
    return time.perf_counter_ns() // 1000


def state_json(aim: AimClient, game_alive: bool, fps: float) -> bytes:
    """L'état que B reçoit : les TOUCHES du jeu (seule information que le profil « réel » laisse sortir) et les
    mesures de l'agent lui-même. `time_usec` et `fps` sont à l'horloge / au rythme de CET agent (le jeu n'en donne
    aucun) : `time_usec − capture_usec` d'une image reste donc la latence depuis sa capture."""
    if aim is None:   # --no-bridge (jeu en mode silent) : le jeu ne donne même pas les touches
        s = {"keys_down": [], "physical_keys_down": [], "mouse_down": []}
    else:
        s = aim.state()
    for k in ("keys_down", "physical_keys_down", "mouse_down"):
        s[k] = sorted(s[k])
    s["game_alive"] = game_alive
    s["time_usec"] = now_us()
    s["fps"] = fps
    return json.dumps(s).encode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--connect", default="", help="hote:port de B (B lance « listen:port ») ; défaut : l'agent écoute")
    ap.add_argument("--listen", default="0.0.0.0:7777")
    ap.add_argument("--transport", choices=["tcp", "udp"], default="tcp", help="udp : images/état/commandes en datagrammes (nécessite --connect)")
    ap.add_argument("--crop", type=int, default=240, help="côté du carré central envoyé (0 = fenêtre entière)")
    ap.add_argument("--resize", type=int, default=0, help="réduit le carré capturé à ce côté (INTER_AREA) avant l'envoi : champ plus large, même débit et même inférence")
    ap.add_argument("--interp", choices=["linear", "area", "nearest"], default="linear", help="interpolation de --resize (area : 0,44 ms, linear : 0,10 ms)")
    ap.add_argument("--jpeg", type=int, default=0, help="qualité JPEG 1-100 (0 = pixels bruts)")
    ap.add_argument("--window", default="AimLabs (DEBUG)")
    ap.add_argument("--backend", choices=["auto", "dxcam", "mss"], default="auto")
    ap.add_argument("--shm", default=os.environ.get("AIMLABS_SHM", "aimlabs"))
    ap.add_argument("--apply-lead-ms", type=float, default=1.0, help="tirs programmés : avance de l'envoi dans le jeu (le jeu applique les commandes au début de sa frame suivante)")
    ap.add_argument("--state-hz", type=float, default=60.0)
    ap.add_argument("--stats", type=float, default=2.0)
    ap.add_argument("--no-bridge", action="store_true", help="n'attache aucun pont au jeu (jeu en mode silent) : capture d'écran et envoi des images seulement")
    ap.add_argument("--record", default="", help="enregistre les images envoyées (HELLO + FRAME) dans ce fichier : à rejouer avec replay_agent.py")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):   # sortie redirigée sous Windows = cp1252 : « → » y plante le print des stats
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.transport == "udp" and not args.connect:
        ap.error("--transport udp nécessite --connect")

    if sys.platform == "win32":
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)                                  # résolution des attentes : 1 ms
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x80)  # HIGH_PRIORITY_CLASS
    if args.no_bridge:
        aim = None       # jeu en mode silent : aucun pont, l'agent ne fait que capturer l'écran et envoyer les images
        print("agent A : sans pont (mode silent) : capture d'écran seulement", flush=True)
    else:
        aim = AimClient(args.shm, timeout=30)
        print(f"agent A : pont attaché, mode {aim.mode}", flush=True)
    cap = ScreenCapture(title=args.window, crop=args.crop or None, backend=args.backend, color="BGR").start()
    W, H = cap._win
    print(f"agent A : capture {cap.backend_name}, fenêtre {W}x{H}, région {cap._region}", flush=True)

    if args.connect:
        host, port = args.connect.rsplit(":", 1)
        print(f"agent A : connexion à B ({args.connect})…", flush=True)
        while True:
            try:
                conn = socket.create_connection((host, int(port)), timeout=2.0)
                break
            except OSError:
                time.sleep(0.3)
        conn.settimeout(None)
        peer_ip = host
    else:
        host, port = args.listen.rsplit(":", 1)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port)))
        srv.listen(1)
        print(f"agent A : en attente de B sur {args.listen}…", flush=True)
        conn, addr = srv.accept()
        peer_ip = addr[0]
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"agent A : B connecté ({peer_ip})", flush=True)

    send_lock = threading.Lock()
    stop = threading.Event()
    rate = {"fps": 0.0}                    # images/s capturées, mesurées ici
    cap_times = collections.OrderedDict()   # frame_id -> instant local de capture (pour le rtt)
    rtts = []
    cmds = {"n": 0}
    sent = {"frames": 0, "bytes": 0, "enc": 0.0}
    seen_cmd = collections.deque(maxlen=1024)
    seen_set = set()

    use_udp = args.transport == "udp"
    usock = None
    udp_ready = threading.Event()
    if use_udp:
        usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        usock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        usock.settimeout(0.5)

    def send(typ: int, payload: bytes) -> None:
        if use_udp and udp_ready.is_set() and typ in (R.T_FRAME, R.T_STATE):
            seq = int.from_bytes(payload[:8], "little") if typ == R.T_FRAME else int(time.monotonic() * 1000)
            n = (len(payload) + R.UDP_CHUNK - 1) // R.UDP_CHUNK
            for i in range(n):
                usock.sendto(R.UDP_HDR.pack(typ, seq & 0xFFFFFFFF, i, n) + payload[i * R.UDP_CHUNK:(i + 1) * R.UDP_CHUNK], (peer_ip, int(port)))
            return
        with send_lock:
            R.send_msg(conn, typ, payload)

    # tir programmé : un seul tir en attente, remplacé par chaque nouvelle commande ; jamais deux tirs à moins de 0,5 s
    sched = {"t": None, "last": -10**12}
    sched_cv = threading.Condition()
    sched_n = {"fired": 0, "late": 0}

    def sched_loop() -> None:
        lead_us = int(args.apply_lead_ms * 1000)
        while not stop.is_set():
            with sched_cv:
                while sched["t"] is None and not stop.is_set():
                    sched_cv.wait(0.2)
                if stop.is_set():
                    return
                t_fire = sched["t"]
            wait = t_fire - lead_us - now_us()
            if wait > 2500:                       # dort jusqu'à ~2 ms avant, puis attend activement
                with sched_cv:
                    sched_cv.wait((wait - 2000) / 1e6)
                continue                          # une commande a pu remplacer l'instant : on recalcule
            while now_us() < t_fire - lead_us:
                if sched["t"] != t_fire:
                    break
            if sched["t"] != t_fire:
                continue
            with sched_cv:
                sched["t"] = None
            now = now_us()
            if now - sched["last"] < 500_000:
                continue
            if now - (t_fire - lead_us) > 3000:
                sched_n["late"] += 1
            aim.fire(frame_id=0)
            sched["last"] = now
            sched_n["fired"] += 1

    def handle_cmd(kind: int, fid: int, dx: float, dy: float) -> None:
        if aim is None:        # sans pont (mode silent) il n'y a personne à qui transmettre : les ordres de tir passent par le Pi
            cmds["n"] += 1
            return
        t = now_us()
        if kind == R.CMD_FIRE_AT:
            if fid in cap_times:
                tf = cap_times[fid] + int(dx * 1000)
                if tf > t - 3000 and tf - t < 400_000:    # ni périmé (>3 ms), ni lointain (>400 ms)
                    with sched_cv:
                        sched["t"] = tf
                        sched_cv.notify_all()
                rtts.append((t - cap_times[fid]) / 1000)
            cmds["n"] += 1
            return
        if kind == R.CMD_CANCEL:
            with sched_cv:
                sched["t"] = None
                sched_cv.notify_all()
            return
        if fid in cap_times:
            rtts.append((t - cap_times[fid]) / 1000)
        cmds["n"] += 1
        # l'id de frame de B n'existe pas dans l'historique du jeu (pas d'image publiée) : 0
        if kind == R.CMD_MOVE:
            aim.move(dx, dy, frame_id=0)
        elif kind == R.CMD_FIRE:
            aim.fire(frame_id=0)
            with sched_cv:                       # un tir immédiat annule le tir programmé et relance le délai de sécurité
                sched["t"], sched["last"] = None, t
        elif kind == R.CMD_MARK:
            aim.mark(frame_id=0, x=dx, y=dy)

    hello = json.dumps({"version": R.VERSION, "width": W, "height": H, "mode": aim.mode if aim else {"vision": False, "move": False, "fire": False}, "udp": use_udp}).encode()
    send(R.T_HELLO, hello)
    rec = None
    if args.record:
        rec = open(args.record, "wb", buffering=1 << 22)
        rec.write(b"AIMREC1\n" + struct.pack("<I", len(hello)) + hello)

    def udp_loop() -> None:
        # poignée de main : on envoie U_HELLO jusqu'à ce que B réponde, puis on écoute les commandes
        hello = R.UDP_HDR.pack(R.U_HELLO, 0, 0, 1)
        try:
            while not stop.is_set():
                if not udp_ready.is_set():
                    usock.sendto(hello, (peer_ip, int(port)))
                try:
                    d, _ = usock.recvfrom(2048)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    time.sleep(0.02)
                    continue
                typ, seq, _i, _n = R.UDP_HDR.unpack_from(d, 0)
                if typ == R.U_HELLO:
                    udp_ready.set()
                elif typ == R.U_CMD:
                    if seq in seen_set:
                        continue
                    seen_cmd.append(seq)
                    seen_set.add(seq)
                    if len(seen_set) > 1024:
                        seen_set.intersection_update(seen_cmd)
                    kind, fid, dx, dy = R.CMD.unpack_from(d, R.UDP_HDR.size)
                    handle_cmd(kind, fid, dx, dy)
        except OSError:
            stop.set()

    def game_hb() -> int:
        return aim._u64.unpack_from(aim._buf, P.OFF_GAME_HEARTBEAT)[0] if aim else 0

    def state_loop() -> None:
        last_hb, last_change = game_hb(), time.monotonic()
        period = 1.0 / args.state_hz
        try:
            while not stop.is_set():
                hb = game_hb()
                if hb != last_hb:
                    last_hb, last_change = hb, time.monotonic()
                alive = True if aim is None else time.monotonic() - last_change < 1.0   # sans pont, on ne sait rien du jeu : B ne s'arrête jamais de lui-même
                if aim is not None:
                    aim.beat()
                try:
                    send(R.T_STATE, state_json(aim, alive, rate["fps"]))
                except RuntimeError:
                    pass
                time.sleep(period)
        except OSError:
            stop.set()

    def cmd_loop() -> None:   # commandes reçues par TCP (poignée de main UDP pas encore faite, ou transport tcp)
        try:
            while not stop.is_set():
                typ, p = R.recv_msg(conn)
                if typ == R.T_CMD:
                    handle_cmd(*R.CMD.unpack(p))
        except (ConnectionError, OSError):
            stop.set()

    sys.setswitchinterval(1e-4)   # le tir programmé attend activement : les autres threads doivent reprendre la main vite
    threading.Thread(target=sched_loop, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    threading.Thread(target=cmd_loop, daemon=True).start()
    if use_udp:
        threading.Thread(target=udp_loop, daemon=True).start()
        print("agent A : poignée de main UDP…", flush=True)
        if not udp_ready.wait(10):
            print("agent A : pas de réponse UDP de B (pare-feu ?), repli sur TCP", flush=True)
            use_udp = False
        else:
            print("agent A : UDP établi", flush=True)

    codec = R.CODEC_JPEG if args.jpeg else R.CODEC_RAW
    import cv2
    enc_params = [cv2.IMWRITE_JPEG_QUALITY, args.jpeg]
    INTERP = {"linear": cv2.INTER_LINEAR, "area": cv2.INTER_AREA, "nearest": cv2.INTER_NEAREST}[args.interp]
    t_stats = time.monotonic()
    try:
        while not stop.is_set():
            c = cap.latest(timeout=1.0)
            if c is None:
                continue
            ox, oy = c.origin
            img = c.image
            te = time.perf_counter()
            if args.resize and img.shape[0] != args.resize:
                img = cv2.resize(img, (args.resize, args.resize), interpolation=INTERP)
            h, w = img.shape[:2]
            if codec == R.CODEC_JPEG:
                ok, buf = cv2.imencode(".jpg", img, enc_params)
                data = buf.tobytes()
            else:
                data = img[:, :, ::-1].tobytes()          # l'image est BGR ; le protocole brut est RGB
            sent["enc"] += time.perf_counter() - te
            cap_times[c.index] = c.t_us
            while len(cap_times) > 4096:
                cap_times.popitem(last=False)
            hdr = R.FRAME_HDR.pack(c.index, c.t_us, W, H, ox, oy, w, h, codec)
            send(R.T_FRAME, hdr + data)
            if rec is not None:
                rec.write(struct.pack("<I", len(hdr) + len(data)) + hdr + data)
            sent["frames"] += 1
            sent["bytes"] += len(data)
            el = time.monotonic() - t_stats
            if el >= args.stats:
                rate["fps"] = sent["frames"] / el
                r = np.array(rtts) if rtts else None
                line = (f"A : {sent['frames'] / el:.0f} img/s · {sent['bytes'] / max(sent['frames'], 1) / 1000:.1f} Ko/img · "
                        f"encodage {sent['enc'] / max(sent['frames'], 1) * 1e3:.2f} ms · cmd {cmds['n']} · tirs prog. {sched_n['fired']} (tardifs {sched_n['late']})")
                if r is not None and len(r):
                    line += f" · rtt capture→cmd reçue : moy {r.mean():.1f} ms, p50 {np.median(r):.1f}, p99 {np.percentile(r, 99):.1f}"
                print(line, flush=True)
                sent["frames"] = sent["bytes"] = 0
                sent["enc"] = 0.0
                cmds["n"] = 0
                t_stats = time.monotonic()
                rtts.clear()
    except (ConnectionError, OSError):
        pass
    except Exception:
        traceback.print_exc()   # os._exit ci-dessous perdrait la trace : un agent qui meurt en silence ressemble à « jeu fermé » côté B
    finally:
        stop.set()
        cap.stop()
        if rec is not None:
            rec.close()
        print("agent A : fin", flush=True)
        os._exit(0)  # évite le bruit COM de dxcam à l'arrêt


if __name__ == "__main__":
    sys.exit(main())
