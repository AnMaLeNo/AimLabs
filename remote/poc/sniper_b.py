"""Machine B (Mac) : « sniper » une balle, Core ML (Neural Engine) — port de algos/yolo_sniper.py / sniper-rs.

Reçoit le carré central du PC A (RemoteClient), YOLO11n-seg 256×256 sur le Neural Engine (~0,4 ms), décode
boîtes + masque, suit la cible (vitesse mesurée sur les bords libres), tire une balle seulement quand le pixel
du réticule — avancé de vitesse × latence — est sûrement dans le masque.

    python sniper_b.py --connect listen:7777 --model algos/yolo11n-seg_256.mlpackage [--extra-ms 4] [--units ALL]

AIMLABS_AUTOSTART=1 : armé d'office (sinon CAPSLOCK on/off, ALT maintenu).

--view : mode debug — une fenêtre (processus séparé, priorité basse) montre l'image reçue, les détections, les masques, la zone
de tir, la vitesse, les latences et les raisons de ne pas tirer (voir sniper_view.py ; ne coûte rien au chemin chaud : B copie
des octets en mémoire partagée APRÈS avoir envoyé sa commande).
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk", "python"))
from aimlabs.remote import RemoteClient  # noqa: E402


# bits de télémétrie partagés avec sniper_view.py (contrôlés à l'ouverture de la vue)
F_FIRE, F_SCHED, F_FIRED = 1, 2, 4
R_LOCKED, R_COOL = 1 << 9, 1 << 10


def qos_interactive() -> None:
    """macOS : priorité maximale du thread (les threads créés ensuite l'héritent). Sans effet ailleurs."""
    if sys.platform == "darwin":
        import ctypes
        ctypes.CDLL("/usr/lib/libSystem.B.dylib").pthread_set_qos_class_self_np(0x21, 0)  # QOS_CLASS_USER_INTERACTIVE


def _spin(model_path, mode):
    """Processus « chauffeur » : un Mac qui n'est sollicité que toutes les 6 ms baisse ses horloges et l'appel Core ML
    passe de 0,45 à 1,5 ms (3,8 ms dans la chaîne réelle). Un processus qui occupe un cœur — ou boucle sur le
    Neural Engine (mode « ane ») — garde la machine à pleine vitesse ; il doit être SÉPARÉ (un thread partage le GIL)."""
    qos_interactive()
    parent = os.getppid()        # si B est tué (pkill, SIGKILL), ce processus devenait orphelin et brûlait un cœur indéfiniment
    if mode == "ane":
        import coremltools as ct
        from PIL import Image
        m = ct.models.MLModel(model_path, compute_units=ct.ComputeUnit.CPU_AND_NE)
        img = Image.fromarray(np.zeros((256, 256, 3), np.uint8))
        while os.getppid() == parent:
            m.predict({"image": img})
        return
    while os.getppid() == parent:
        for _ in range(200000):
            pass


class Detector:
    def __init__(self, path: str, units: str):
        import coremltools as ct
        from PIL import Image
        self.Image = Image
        self.m = ct.models.MLModel(path, compute_units=getattr(ct.ComputeUnit, units))
        spec = self.m.get_spec()
        self.inp = spec.description.input[0].name
        outs = [(o.name, list(o.type.multiArrayType.shape)) for o in spec.description.output]
        self.o0 = next(n for n, s in outs if len(s) == 3)      # (1, 116, N)
        self.o1 = next(n for n, s in outs if len(s) == 4)      # (1, 32, mh, mw)
        self.side = spec.description.input[0].type.imageType.width
        shapes = dict(outs)
        self.n = shapes[self.o0][2]                           # ancres (1344 à 256²)
        self.pshape = tuple(shapes[self.o1][1:])              # (32, mh, mw)
        self.last_ms = 0.0

    def __call__(self, img: np.ndarray, bgr: bool = False):
        """`img` : carré RGB, ou BGR (`bgr=True`) tel que décodé par OpenCV. Image.fromarray de la vue RGB renversée de
        cv2.imdecode(...)[:, :, ::-1] coûtait 496 µs par image (mesuré sur le M5) ; PIL convertit le BGR d'origine en 34 µs."""
        t = time.perf_counter()
        pil = self.Image.frombuffer("RGB", (img.shape[1], img.shape[0]), img, "raw", "BGR", 0, 1) if bgr else self.Image.fromarray(img)
        o = self.m.predict({self.inp: pil})
        out0 = np.asarray(o[self.o0])[0]
        protos = np.asarray(o[self.o1])[0]
        self.last_ms = (time.perf_counter() - t) * 1e3
        return out0, protos


def fit_crop(crop: np.ndarray, side_net: int) -> np.ndarray:
    """Ramène le carré reçu à la taille du réseau (plus proche voisin) ; inutile quand l'agent l'a déjà réduit à 256."""
    if crop.shape[0] != side_net:
        from PIL import Image
        crop = np.asarray(Image.fromarray(crop).resize((side_net, side_net), Image.NEAREST))
    return crop


def nms(boxes, iou_thr=0.45):
    keep = []
    for b in sorted(boxes, key=lambda d: -d[4]):
        ok = True
        for k in keep:
            ix = max(0.0, min(b[2], k[2]) - max(b[0], k[0]))
            iy = max(0.0, min(b[3], k[3]) - max(b[1], k[1]))
            inter = ix * iy
            uni = (b[2] - b[0]) * (b[3] - b[1]) + (k[2] - k[0]) * (k[3] - k[1]) - inter
            if uni > 0 and inter / uni > iou_thr:
                ok = False
                break
        if ok:
            keep.append(b)
    return keep


def decode(out0: np.ndarray, conf: float):
    """Boîtes « person » (x1, y1, x2, y2, conf, index d'ancre) ≥ conf, après NMS."""
    sc = out0[4]
    idx = np.nonzero(sc >= conf)[0]
    # float Python (pas np.float32) : un instant visé = horloge monotone (≈ 10⁶ s) + tau perdrait toute précision en float32
    boxes = [(float(out0[0, i] - out0[2, i] / 2), float(out0[1, i] - out0[3, i] / 2), float(out0[0, i] + out0[2, i] / 2),
              float(out0[1, i] + out0[3, i] / 2), float(sc[i]), int(i)) for i in idx]
    return nms(boxes)


def mask_extent(out0, protos, d, y, side, x_pad=2.0):
    """Étendue en x (px du réseau) où le masque de la détection `d` est ≥ 0,5 sur la ligne `y`."""
    mh, mw = protos.shape[1], protos.shape[2]
    py = min(int(y * mh / side), mh - 1)
    logit = out0[84:116, d[5]] @ protos[:, py, :]          # (mw,)
    xs = np.nonzero(logit > 0)[0] * (side / mw)
    xs = xs[(xs >= d[0] - x_pad) & (xs <= d[2] + x_pad)]
    if xs.size == 0:
        return None
    return float(xs[0]), float(xs[-1] + side / mw)


class Track:
    def __init__(self, side):
        self.hist, self.side = [], side

    def reset(self):
        self.hist = []

    def push(self, t, b):
        self.hist.append((t, b[0], b[2], b[1], b[3]))
        self.hist = [p for p in self.hist if t - p[0] <= 0.12]

    @staticmethod
    def fit(pts):
        if len(pts) < 2:
            return None
        t = np.array([p[0] for p in pts]); v = np.array([p[1] for p in pts])
        t = t - t[-1]
        den = len(pts) * (t * t).sum() - t.sum() ** 2
        if abs(den) < 1e-9:
            return None
        a = (len(pts) * (t * v).sum() - t.sum() * v.sum()) / den
        b = (v.sum() - a * t.sum()) / len(pts)
        res = float(np.sqrt(np.mean((a * t + b - v) ** 2)))
        return float(a), res

    def velocity(self):
        m = 2.0
        L = [(p[0], p[1]) for p in self.hist if p[1] > m]
        Rr = [(p[0], p[2]) for p in self.hist if p[2] < self.side - m]
        T = [(p[0], p[3]) for p in self.hist if p[3] > m]
        B = [(p[0], p[4]) for p in self.hist if p[4] < self.side - m]
        fx = [f for f in (self.fit(L), self.fit(Rr)) if f]
        fy = [f for f in (self.fit(T), self.fit(B)) if f]
        if not fx:
            return None
        vx, rx = min(fx, key=lambda f: f[1])
        vy, ry = min(fy, key=lambda f: f[1]) if fy else (0.0, 0.0)
        return vx, vy, max(rx, ry)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--connect", default=os.environ.get("AIMLABS_REMOTE", "listen:7777"))
    ap.add_argument("--model", default="algos/yolo11n-seg_256.mlpackage")
    ap.add_argument("--units", default="ALL", choices=["ALL", "CPU_AND_NE", "CPU_AND_GPU", "CPU_ONLY"])
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--band", type=float, default=0.7)
    ap.add_argument("--vband", type=float, default=0.9)
    ap.add_argument("--track", type=int, default=2)
    ap.add_argument("--lead-scale", type=float, default=1.0)
    ap.add_argument("--cooldown", type=float, default=1.0)
    ap.add_argument("--extra-ms", type=float, default=6.5, help="tir immédiat de repli : latence non mesurée par B (retour B→A + application dans le jeu) ajoutée au temps écoulé")
    ap.add_argument("--warm", choices=["cpu", "ane", "none"], default="cpu", help="processus qui garde le Mac à pleine vitesse")
    ap.add_argument("--no-schedule", action="store_true", help="tir immédiat dès que c'est sûr (sans tir programmé)")
    ap.add_argument("--lag-ms", type=float, default=3.5, help="délai A→B (encodage + réseau) à ajouter au temps écoulé depuis la réception de l'image")
    ap.add_argument("--return-ms", type=float, default=2.5, help="tir programmé : temps réservé au retour B→A + application avant l'instant visé")
    ap.add_argument("--bias-ms", type=float, default=-1.0, help="tir programmé : décalage ajouté à l'instant visé (calibration)")
    ap.add_argument("--vmin", type=float, default=60.0, help="vitesse (px réseau/s) en dessous de laquelle on tire immédiatement")
    ap.add_argument("--horizon", type=float, default=0.4, help="on ne programme pas un tir plus lointain (s)")
    ap.add_argument("--stats", type=float, default=5.0)
    ap.add_argument("--debug", action="store_true", help="journal texte image par image (ralentit : préférer --view)")
    ap.add_argument("--view", action="store_true", help="mode debug : fenêtre « ce que voit l'IA » (processus séparé, sans coût pour B)")
    ap.add_argument("--view-fps", type=float, default=30.0, help="cadence de la fenêtre (instantanés complets par seconde)")
    ap.add_argument("--view-scale", type=int, default=3, help="agrandissement de l'image (3 : 768 px)")
    ap.add_argument("--view-headless", action="store_true", help="pas de fenêtre : seulement --view-snap / --view-log (tests à distance)")
    ap.add_argument("--view-snap", default="", help="dossier où la vue écrit des PNG (un par tir, + un toutes les --view-snap-every s)")
    ap.add_argument("--view-snap-every", type=float, default=0.0)
    ap.add_argument("--view-log", default="", help="CSV de la télémétrie par image (inférence, latence, boîte, décision…)")
    ap.add_argument("--view-linger", type=float, default=5.0, help="secondes pendant lesquelles la fenêtre reste ouverte après l'arrêt de B")
    a = ap.parse_args(argv)

    qos_interactive()
    sys.setswitchinterval(1e-4)   # bascule de GIL entre threads de réception et de décision : 0,1 ms au lieu de 5 ms
    warm = None
    if a.warm != "none":
        import multiprocessing as mp
        warm = mp.get_context("spawn").Process(target=_spin, args=(a.model, a.warm), daemon=True)
        warm.start()
    det = Detector(a.model, a.units)
    side_net = det.side
    vw = None
    if a.view:
        sys.path.insert(0, HERE)
        import atexit
        import sniper_view as SV
        assert (SV.F_FIRE, SV.F_SCHED, SV.F_FIRED, SV.R_LOCKED, SV.R_COOL) == (F_FIRE, F_SCHED, F_FIRED, R_LOCKED, R_COOL)
        meta = dict(conf=a.conf, band=a.band, vband=a.vband, track=a.track, lead_scale=a.lead_scale, cooldown=a.cooldown, extra_ms=a.extra_ms,
                    lag_ms=a.lag_ms, return_ms=a.return_ms, bias_ms=a.bias_ms, vmin=a.vmin, horizon=a.horizon, side=side_net,
                    units=a.units, model=os.path.basename(a.model))
        vw = SV.ViewTx(side_net, det.n, det.pshape, meta, fps=a.view_fps, scale=a.view_scale, snap_dir=a.view_snap,
                       snap_every=a.view_snap_every, headless=a.view_headless, linger=a.view_linger, log=a.view_log)
        atexit.register(vw.close)
        print(f"sniper_b : vue debug ouverte (processus {vw.proc.pid}, {a.view_fps:.0f} fps, mémoire partagée {vw.shm.name})", flush=True)
    enabled = os.environ.get("AIMLABS_AUTOSTART") == "1"
    print(f"sniper_b : Core ML {a.units}, réseau {side_net}px, en attente de l'agent du PC A ({a.connect})…", flush=True)
    track, locked, last_fire = None, False, -1e9
    n = n_fire = n_sched = 0
    why = {}
    sched_t = None
    rx_last = 0.0
    t_inf = t_post = t_proc = t_view = 0.0
    why_prev, rmask = {}, 0
    lats = []                                        # délais réception→commande de la fenêtre de stats (p99, max)
    t_stats = time.monotonic()
    with RemoteClient(a.connect, timeout=60) as c:
        print("sniper_b : connecté, mode", c.mode, flush=True)
        while True:
            fr = c.crop(timeout=2.0)
            if fr is None:
                if not c.game_alive():
                    print("jeu fermé", flush=True)
                    return 0
                continue
            prev_rx = rx_last
            fid, cap_us, rx, crop, (x0, y0), (W, H) = fr
            rx_last = rx
            k = c.keys()
            if "CAPSLOCK" in k.just_pressed:
                enabled = not enabled
                print("sniper", "ON" if enabled else "OFF", flush=True)
            active = enabled or "ALT" in k
            now = time.monotonic()
            if now - t_stats > a.stats:
                if active and n:
                    print(f"[stats] {n / (now - t_stats):.0f} images/s · inférence {t_inf / n * 1e3:.2f} ms · post {t_post / n * 1e3:.2f} ms · "
                          f"réception→commande {t_proc / n * 1e3:.2f} ms (p99 {sorted(lats)[int(len(lats) * 0.99)] * 1e3:.2f}, max {max(lats) * 1e3:.2f}) · "
                          f"tirs {n_fire} (programmés {n_sched})"
                          + (f" · vue {t_view / n * 1e6:.0f} µs" if vw is not None else ""), flush=True)
                if a.debug and why:
                    print("[raisons]", dict(sorted(why.items())), flush=True)
                why, why_prev, lats = {}, {}, []
                n = n_fire = n_sched = 0
                t_inf = t_post = t_proc = t_view = 0.0
                t_stats = now
            side = crop.shape[0]
            src_bgr = c.last_x[0] if side == side_net else None   # BGR d'origine (JPEG déjà à la taille du réseau)
            if not active:
                if track:
                    track.reset()
                if vw is not None:
                    tv = time.perf_counter()
                    gap_ms, dec_ms, nbytes = (rx - prev_rx) * 1e3, c.last_x[1], c.last_x[2]
                    if vw.want():                         # vue : détections même désarmé, à la cadence de la fenêtre seulement
                        crop_i = crop if src_bgr is not None else fit_crop(crop, side_net)
                        o0, pr = det(src_bgr, True) if src_bgr is not None else det(crop_i)
                        vw.frame(fid=fid, rx=rx, flags=0, gap_ms=gap_ms, inf_ms=det.last_ms, post_ms=0.0, proc_ms=(time.monotonic() - rx) * 1e3,
                                 dec_ms=dec_ms, nbytes=nbytes, state=SV.S_IDLE, rmask=0, boxes=decode(o0, min(a.conf, 0.25)), sel=-1,
                                 img=src_bgr if src_bgr is not None else crop_i, img_bgr=src_bgr is not None, out0=o0, protos=pr,
                                 geom=(W, H, x0, y0, side, side, 1 if src_bgr is not None else 0))
                    else:
                        vw.frame(fid=fid, rx=rx, flags=0, gap_ms=gap_ms, inf_ms=0.0, post_ms=0.0, proc_ms=0.0, dec_ms=dec_ms, nbytes=nbytes,
                                 state=SV.S_IDLE, rmask=0)
                    t_view += time.perf_counter() - tv
                continue
            if src_bgr is None:
                crop = fit_crop(crop, side_net)
            scale = side / side_net                       # px réseau -> px de l'image reçue
            out0, protos = det(src_bgr, True) if src_bgr is not None else det(crop)
            t1 = time.perf_counter()
            boxes = decode(out0, min(a.conf, 0.25))
            n += 1
            t_inf += det.last_ms / 1e3
            cx = cy = side_net / 2.0                      # le réticule est au centre du carré
            ev, rmask, tau_v, elapsed_v, vel_v, fdx, fdy = 0, 0, float("nan"), float("nan"), None, 0.0, 0.0   # télémétrie de la vue
            if not boxes:
                why["aucune boîte"] = why.get("aucune boîte", 0) + 1
                if track:
                    track.reset()
                locked = False
                tp = time.perf_counter() - t1
                tm = time.monotonic() - rx
                t_post += tp
                t_proc += tm
                lats.append(tm)
                if vw is not None:
                    tv = time.perf_counter()
                    rm, why_prev = SV.reason_diff(why, why_prev)
                    vw.frame(fid=fid, rx=rx, flags=0, gap_ms=(rx - prev_rx) * 1e3, inf_ms=det.last_ms, post_ms=tp * 1e3, proc_ms=tm * 1e3,
                             dec_ms=c.last_x[1], nbytes=c.last_x[2], state=SV.S_NOBOX, rmask=rm, boxes=boxes, sel=-1,
                             img=src_bgr if src_bgr is not None else crop, img_bgr=src_bgr is not None, out0=out0, protos=protos,
                             geom=(W, H, x0, y0, side, side, 1 if src_bgr is not None else 0))
                    t_view += time.perf_counter() - tv
                continue
            b = min(boxes, key=lambda q: abs((q[0] + q[2]) / 2 - cx) + abs((q[1] + q[3]) / 2 - cy))
            if track is None:
                track = Track(side_net)
            track.push(cap_us / 1e6, b)
            ext_now = mask_extent(out0, protos, b, cy, side_net)
            under_now = b[0] <= cx <= b[2] and b[1] <= cy <= b[3] and ext_now is not None and ext_now[0] <= cx <= ext_now[1]
            if under_now:
                c.mark(frame_id=fid)
            else:
                locked = False
            if sched_t is not None and now >= sched_t + 0.02:       # le tir programmé est parti (horloge locale de B)
                if a.debug:
                    print(f"[parti] image {fid} : now − sched_t = {(now - sched_t) * 1e3:.1f} ms ; image reçue il y a {(now - rx) * 1e3:.1f} ms ; écart depuis l'image précédente {(rx - prev_rx) * 1e3:.1f} ms", flush=True)
                sched_t, locked, last_fire = None, True, now
                n_fire += 1
                n_sched += 1
                ev |= F_FIRED
            if a.debug:
                print(f"[g] image {fid} boîte x {(b[0] + b[2]) / 2:.0f} conf {b[4]:.2f} verrou {locked} refroid. {now - last_fire < a.cooldown} prog {sched_t is not None} under {under_now}", flush=True)
            if locked or now - last_fire < a.cooldown:
                rmask |= R_LOCKED if locked else R_COOL
            else:
                elapsed = elapsed_v = (time.monotonic() - rx) + a.lag_ms / 1e3     # depuis la capture, sur l'horloge locale de B
                vel = vel_v = track.velocity()
                coherent = vel is not None and a.lead_scale > 0 and vel[2] < 0.3 * (b[2] - b[0]) + 2.0

                def sure_at(dx, dy):
                    """Le pixel du réticule, au moment du tir, est-il sûrement dans la bande centrale du masque ?"""
                    py1, py2 = b[1] + dy, b[3] + dy
                    px1, px2 = b[0] + dx, b[2] + dx
                    in_band = abs(cx - (px1 + px2) / 2) <= (px2 - px1) / 2 * a.band and abs(cy - (py1 + py2) / 2) <= (py2 - py1) / 2 * a.vband
                    if not in_band:
                        why["hors bande"] = why.get("hors bande", 0) + 1
                        return False
                    if b[4] < a.conf:
                        why["confiance"] = why.get("confiance", 0) + 1
                        return False
                    if len(track.hist) < a.track:
                        why["suivi court"] = why.get("suivi court", 0) + 1
                        return False
                    ext = mask_extent(out0, protos, b, cy - dy, side_net)
                    if ext is None:
                        why["masque vide"] = why.get("masque vide", 0) + 1
                        return False
                    if abs((cx - dx) - (ext[0] + ext[1]) / 2) > max((ext[1] - ext[0]) / 2 * a.band, 1.0):
                        why["masque hors bande"] = why.get("masque hors bande", 0) + 1
                        return False
                    return True

                done = False
                if not coherent:
                    why["vitesse incohérente"] = why.get("vitesse incohérente", 0) + 1
                if not a.no_schedule and coherent and abs(vel[0]) > a.vmin:
                    vx, vy, _res = vel
                    tau = tau_v = float(cx - (b[0] + b[2]) / 2) / vx      # s après la capture : le centre de la cible atteint le réticule
                    if a.debug:
                        print(f"[f] image {fid} tau {tau * 1e3:+.1f} ms v {vx:+.0f} vy {vy:+.0f} res {vel[2]:.1f} écoulé {elapsed * 1e3:.1f} conf {b[4]:.2f} suivi {len(track.hist)} prog {'oui' if sched_t is not None else 'non'}", flush=True)
                    if not (0 < tau < a.horizon):
                        why["tau hors horizon"] = why.get("tau hors horizon", 0) + 1
                    elif tau < elapsed + a.return_ms / 1e3:
                        why["trop tard pour programmer"] = why.get("trop tard pour programmer", 0) + 1
                    if 0 < tau < a.horizon and tau >= elapsed + a.return_ms / 1e3:
                        done = True                                # assez tôt pour être programmé
                        if sure_at(vx * tau, vy * tau) and c.fire_at(fid, tau + a.bias_ms / 1e3):
                            sched_t = rx - a.lag_ms / 1e3 + tau              # instant visé sur l'horloge de B
                            ev |= F_SCHED
                            if a.debug:
                                print(f"[prog] image {fid} conf {b[4]:.2f} tir dans {tau * 1e3:.1f} ms (v {vx:+.0f} px/s, écoulé {elapsed * 1e3:.1f} ms)", flush=True)
                if not done:                                       # repli : tir immédiat avec avance sur la latence
                    latency = elapsed + a.extra_ms / 1e3
                    dx = dy = 0.0
                    if coherent:
                        dx, dy = vel[0] * latency * a.lead_scale, vel[1] * latency * a.lead_scale
                    if sure_at(dx, dy) and c.fire(frame_id=fid):
                        n_fire += 1
                        last_fire, locked = now, True
                        sched_t = None
                        ev |= F_FIRE
                        fdx, fdy = dx, dy
                        c.cancel_fire()
                        if a.debug:
                            print(f"[tir] image {fid} {'' if under_now else 'anticipé '}conf {b[4]:.2f} avance ({dx:+.1f},{dy:+.1f}) px latence {latency * 1e3:.1f} ms", flush=True)
            tp = time.perf_counter() - t1
            tm = time.monotonic() - rx
            t_post += tp
            t_proc += tm
            lats.append(tm)
            if vw is not None:                            # vue : APRÈS les commandes, jamais avant
                tv = time.perf_counter()
                rm, why_prev = SV.reason_diff(why, why_prev)
                vw.frame(fid=fid, rx=rx, flags=ev, gap_ms=(rx - prev_rx) * 1e3, inf_ms=det.last_ms, post_ms=tp * 1e3, proc_ms=tm * 1e3,
                         dec_ms=c.last_x[1], nbytes=c.last_x[2], state=SV.S_TRACK, rmask=rm | rmask, boxes=boxes, sel=boxes.index(b),
                         under=under_now, ext=ext_now, vel=vel_v, tau=tau_v, elapsed=elapsed_v, locked=locked,
                         cool_left=max(0.0, a.cooldown - (now - last_fire)),
                         sched_in=max(0.0, sched_t - time.monotonic()) if sched_t is not None else -1.0, fire_dx=fdx, fire_dy=fdy,
                         track=track.hist, img=src_bgr if src_bgr is not None else crop, img_bgr=src_bgr is not None, out0=out0, protos=protos,
                         geom=(W, H, x0, y0, side, side, 1 if src_bgr is not None else 0))
                t_view += time.perf_counter() - tv


if __name__ == "__main__":
    sys.exit(main())
