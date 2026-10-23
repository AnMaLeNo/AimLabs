"""Mode debug de sniper_b : une fenêtre qui montre ce que l'IA reçoit, détecte et décide — sans gêner la détection.

Principe (chaque point existe pour que le debug ne coûte rien au chemin chaud de B) :

  * la fenêtre vit dans un PROCESSUS SÉPARÉ, en priorité basse (macOS : QoS « utility » = cœurs d'efficacité) : ni GIL partagé,
    ni cœur de performance volé au thread qui décide ;
  * B ne fait que COPIER des octets en mémoire partagée (≈ 15 µs par instantané : image BGR d'origine 3 µs, sorties du
    réseau 8 µs, en-tête 0,5 µs), et seulement APRÈS avoir envoyé sa commande ;
  * un enregistrement de télémétrie de 64 octets par image (1 µs) ; un instantané complet seulement quand la fenêtre en
    réclame un (≤ `--view-fps` par seconde, protocole demande/accusé : B n'écrit jamais pendant que la fenêtre lit) ;
  * un instantané « événement » (tir) hors protocole : il garde l'image qui a décidé le tir, protégée par un compteur de
    version (la fenêtre relit si c'est incohérent) ;
  * si la fenêtre se ferme ou plante, B continue sans rien changer.

Disposition de la mémoire partagée : en-tête (compteurs, méta JSON) | anneau de télémétrie | instantané régulier | instantané
événement. Le rendu (`render`) est une fonction pure de l'état de la fenêtre : on peut l'essayer sans Mac (`--demo`).

    python sniper_view.py --demo [--out DIR] [--seconds 6]    # producteur synthétique + fenêtre sans écran -> PNG dans DIR
"""
import argparse
import json
import math
import os
import struct
import sys
import time
import unicodedata

import numpy as np

NaN = float("nan")
MAXB = 8                     # boîtes détaillées (avec leurs coefficients de masque) par instantané
MAXT = 24                    # points de suivi
NCOEF = 32
RING = 4096                  # enregistrements de télémétrie (≈ 30 s à 140 images/s)
MAGIC = 0x56494D41           # "AMIV"
VERSION = 1

# raisons (clés de `why` dans sniper_b) -> bit du masque de télémétrie ; les deux derniers bits n'existent que pour la vue
REASONS = ["aucune boîte", "hors bande", "confiance", "suivi court", "masque vide", "masque hors bande",
           "vitesse incohérente", "tau hors horizon", "trop tard pour programmer", "verrouillé", "refroidissement"]
REASON_BIT = {r: 1 << i for i, r in enumerate(REASONS)}
R_LOCKED, R_COOL = REASON_BIT["verrouillé"], REASON_BIT["refroidissement"]


def reason_diff(why: dict, prev: dict):
    """Raisons de ne pas tirer apparues DANS cette image : B compte déjà ses raisons dans `why` ; on compare avant/après
    (rien à changer dans la logique de décision de B)."""
    m = 0
    for r, bit in REASON_BIT.items():
        if why.get(r, 0) != prev.get(r, 0):
            m |= bit
    return m, dict(why)

# états de l'image dans la télémétrie
S_IDLE, S_NOBOX, S_TRACK = 0, 1, 2
# événements (bits de `flags`)
F_FIRE, F_SCHED, F_FIRED = 1, 2, 4

# ------------------------------------------------------------------------------------------------ télémétrie
REC = struct.Struct("<IIdfffffIffHBBIff")      # 64 octets
REC_DTYPE = np.dtype([("fid", "<u4"), ("flags", "<u4"), ("rx", "<f8"), ("gap_ms", "<f4"), ("inf_ms", "<f4"),
                      ("post_ms", "<f4"), ("proc_ms", "<f4"), ("dec_ms", "<f4"), ("nbytes", "<u4"), ("conf", "<f4"),
                      ("cx", "<f4"), ("nbox", "<u2"), ("state", "u1"), ("pad", "u1"), ("rmask", "<u4"), ("tau", "<f4"),
                      ("vx", "<f4")])
assert REC.size == REC_DTYPE.itemsize == 64

# ------------------------------------------------------------------------------------------------ instantané
SNAP_FIELDS = [("seq0", "I"), ("fid", "I"), ("kind", "I"), ("flags", "I"), ("rx", "d"), ("t_pub", "d"),
               ("w", "I"), ("h", "I"), ("x0", "I"), ("y0", "I"), ("cw", "I"), ("ch", "I"), ("codec", "I"), ("nbytes", "I"),
               ("bgr", "I"), ("state", "I"), ("locked", "I"), ("cool_left", "f"), ("sched_in", "f"),
               ("nbox", "I"), ("sel", "i"), ("under", "I"), ("ext_ok", "I"), ("ext0", "f"), ("ext1", "f"),
               ("vel_ok", "I"), ("vx", "f"), ("vy", "f"), ("vres", "f"), ("tau", "f"), ("elapsed", "f"),
               ("inf_ms", "f"), ("post_ms", "f"), ("proc_ms", "f"), ("dec_ms", "f"), ("gap_ms", "f"),
               ("rmask", "I"), ("fire_dx", "f"), ("fire_dy", "f"), ("ntrack", "I")]
SNAP = struct.Struct("<" + "".join(f for _, f in SNAP_FIELDS))
SNAP_NAMES = [n for n, _ in SNAP_FIELDS]
BOXES = struct.Struct("<" + "f" * (MAXB * 6))
TRACK = struct.Struct("<" + "d" * (MAXT * 5))

H_MAGIC, H_VERSION, H_SIDE, H_N, H_MH, H_MW, H_NCOEF, H_NROWS = range(8)
H_VIEWER, H_ACK, H_PUB, H_EVPUB, H_PRODUCER, H_METALEN = 8, 9, 10, 11, 12, 13
HDR_SIZE = 8192
META_OFF = 1024
HEAD_OFF = 128               # u64 : nombre d'enregistrements de télémétrie écrits


def _a64(x: int) -> int:
    return (x + 63) & ~63


class Layout:
    """Décalages de la mémoire partagée, calculés des mêmes quatre nombres des deux côtés."""

    def __init__(self, side, n, mh, mw):
        self.side, self.n, self.mh, self.mw = side, n, mh, mw
        self.ring = HDR_SIZE
        o = 512                                              # en-tête de l'instantané
        self.o_boxes = o; o += _a64(BOXES.size)
        self.o_coefs = o; o += _a64(MAXB * NCOEF * 4)
        self.o_track = o; o += _a64(TRACK.size)
        self.o_head = o; o += _a64(5 * n * 4)
        self.o_img = o; o += _a64(side * side * 3)
        self.o_protos = o; o += _a64(NCOEF * mh * mw * 4)
        self.slot_size = o + 64                              # +64 : compteur de fin (seq1) en dernier
        self.slot0 = self.ring + RING * REC.size
        self.slot1 = self.slot0 + self.slot_size
        self.total = self.slot1 + self.slot_size


class Slot:
    """Vues numpy d'un instantané dans la mémoire partagée (même code d'écriture et de lecture)."""

    def __init__(self, buf, base, L: Layout):
        self.buf, self.base, self.L = buf, base, L
        self.boxes_off, self.track_off = base + L.o_boxes, base + L.o_track
        self.seq1_off = base + L.slot_size - 4
        self.coefs = np.ndarray((MAXB, NCOEF), np.float32, buffer=buf, offset=base + L.o_coefs)
        self.head = np.ndarray((5, L.n), np.float32, buffer=buf, offset=base + L.o_head)
        self.img = np.ndarray((L.side, L.side, 3), np.uint8, buffer=buf, offset=base + L.o_img)
        self.protos = np.ndarray((NCOEF, L.mh, L.mw), np.float32, buffer=buf, offset=base + L.o_protos)

    def release(self):
        self.coefs = self.head = self.img = self.protos = None


def set_low_priority() -> None:
    """La fenêtre ne doit jamais disputer un cœur de performance à B."""
    try:
        if sys.platform == "darwin":
            import ctypes
            ctypes.CDLL("/usr/lib/libSystem.B.dylib").pthread_set_qos_class_self_np(0x11, 0)   # QOS_CLASS_UTILITY
        elif sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


# ================================================================================================ producteur (dans B)
class ViewTx:
    """Côté B. Tout est conçu pour ne rien coûter : voir l'en-tête du module. `frame()` est appelé une fois par image,
    APRÈS l'envoi de la commande."""

    def __init__(self, side, n_anchors, protos_shape, meta=None, fps=30.0, scale=3, snap_dir="", snap_every=0.0,
                 headless=False, linger=5.0, log="", spawn=True):
        from multiprocessing import shared_memory
        self.L = Layout(side, n_anchors, protos_shape[1], protos_shape[2])
        self.shm = shared_memory.SharedMemory(create=True, size=self.L.total)
        self.buf = self.shm.buf
        self.buf[:HDR_SIZE] = bytes(HDR_SIZE)
        self.h = self.buf[:HDR_SIZE].cast("I")
        self.h[H_MAGIC], self.h[H_VERSION], self.h[H_SIDE], self.h[H_N] = MAGIC, VERSION, side, n_anchors
        self.h[H_MH], self.h[H_MW], self.h[H_NCOEF], self.h[H_NROWS] = protos_shape[1], protos_shape[2], NCOEF, 116
        mj = json.dumps(meta or {}).encode()[:HDR_SIZE - META_OFF]
        self.buf[META_OFF:META_OFF + len(mj)] = mj
        self.h[H_METALEN] = len(mj)
        self.h[H_PRODUCER] = 1
        self.slots = (Slot(self.buf, self.L.slot0, self.L), Slot(self.buf, self.L.slot1, self.L))
        self.head_n = 0
        self.pub = 0
        self.ev_pub = 0
        self.t_ev = -1e9
        self.proc = None
        self.opts = dict(fps=fps, scale=scale, snap_dir=snap_dir, snap_every=snap_every, headless=headless, linger=linger, log=log,
                         parent=os.getpid())
        if spawn:
            import multiprocessing as mp
            self.proc = mp.get_context("spawn").Process(target=viewer_main, args=(self.shm.name, self.opts), daemon=False)
            self.proc.start()

    # ---- état de la fenêtre
    def alive(self) -> bool:
        return self.h[H_VIEWER] == 1

    def want(self) -> bool:
        """La fenêtre a consommé le dernier instantané et en réclame un nouveau."""
        return self.h[H_VIEWER] == 1 and self.h[H_ACK] == self.pub

    # ---- une image
    def frame(self, *, fid, rx, flags, gap_ms, inf_ms, post_ms, proc_ms, dec_ms, nbytes, state, rmask,
              boxes=(), sel=-1, under=False, ext=None, vel=None, tau=NaN, elapsed=NaN, locked=False, cool_left=0.0,
              sched_in=-1.0, fire_dx=0.0, fire_dy=0.0, track=None, img=None, img_bgr=True, out0=None, protos=None,
              geom=(0, 0, 0, 0, 0, 0, 0)):
        h = self.h
        if h[H_VIEWER] != 1:
            return
        if (self.head_n & 1023) == 1023 and self.proc is not None and not self.proc.is_alive():
            h[H_VIEWER] = 0                                   # fenêtre tuée sans prévenir : on cesse de publier
            return
        conf = boxes[sel][4] if 0 <= sel < len(boxes) else NaN
        cx = (boxes[sel][0] + boxes[sel][2]) / 2 if 0 <= sel < len(boxes) else NaN
        REC.pack_into(self.buf, self.L.ring + (self.head_n % RING) * 64, fid, flags, rx, gap_ms, inf_ms, post_ms, proc_ms,
                      dec_ms, nbytes, conf, cx, len(boxes), state, 0, rmask, tau, vel[0] if vel else 0.0)
        self.head_n += 1
        struct.pack_into("<Q", self.buf, HEAD_OFF, self.head_n)
        if img is None or out0 is None:
            return
        ev = bool(flags & (F_FIRE | F_SCHED))
        if ev and not ((flags & F_FIRE) or rx - self.t_ev > 0.08):
            ev = False                                        # une rafale de tirs programmés : un instantané tous les 80 ms
        if ev:
            self.t_ev = rx
            self._snap(self.slots[1], 1, fid, flags, rx, locals())
            self.ev_pub += 1
            h[H_EVPUB] = self.ev_pub
        elif h[H_ACK] == self.pub:
            self.pub += 1
            self._snap(self.slots[0], 0, fid, flags, rx, locals())
            h[H_PUB] = self.pub

    def _snap(self, s: Slot, kind, fid, flags, rx, a):
        """Écrit un instantané ; `a` = variables locales de frame(). Compteur de début, données, compteur de fin."""
        buf, L = self.buf, self.L
        seq = (self.pub if kind == 0 else self.ev_pub) + 1
        boxes, sel, ext, vel, track = a["boxes"], a["sel"], a["ext"], a["vel"], a["track"]
        nb = min(len(boxes), MAXB)
        w, hh, x0, y0, cw, ch, codec = a["geom"]
        tr = (track or [])[-MAXT:]
        vals = dict(seq0=seq, fid=fid, kind=kind, flags=flags, rx=rx, t_pub=time.monotonic(), w=w, h=hh, x0=x0, y0=y0, cw=cw, ch=ch,
                    codec=codec, nbytes=a["nbytes"], bgr=1 if a["img_bgr"] else 0, state=a["state"], locked=1 if a["locked"] else 0,
                    cool_left=a["cool_left"], sched_in=a["sched_in"], nbox=nb, sel=sel if sel < nb else -1,
                    under=1 if a["under"] else 0, ext_ok=1 if ext else 0, ext0=ext[0] if ext else 0.0, ext1=ext[1] if ext else 0.0,
                    vel_ok=1 if vel else 0, vx=vel[0] if vel else 0.0, vy=vel[1] if vel else 0.0, vres=vel[2] if vel else 0.0,
                    tau=a["tau"], elapsed=a["elapsed"], inf_ms=a["inf_ms"], post_ms=a["post_ms"], proc_ms=a["proc_ms"],
                    dec_ms=a["dec_ms"], gap_ms=a["gap_ms"], rmask=a["rmask"], fire_dx=a["fire_dx"], fire_dy=a["fire_dy"], ntrack=len(tr))
        SNAP.pack_into(buf, s.base, *[vals[n] for n in SNAP_NAMES])
        flat = []
        for b in boxes[:nb]:
            flat += (b[0], b[1], b[2], b[3], b[4], float(b[5]))
        flat += [0.0] * (MAXB * 6 - len(flat))
        BOXES.pack_into(buf, s.boxes_off, *flat)
        if nb:
            s.coefs[:nb] = a["out0"][84:84 + NCOEF, [b[5] for b in boxes[:nb]]].T
        tflat = []
        for p in tr:
            tflat += p
        tflat += [0.0] * (MAXT * 5 - len(tflat))
        TRACK.pack_into(buf, s.track_off, *tflat)
        np.copyto(s.head, a["out0"][:5])
        np.copyto(s.img, a["img"])
        np.copyto(s.protos, a["protos"])
        struct.pack_into("<I", buf, s.seq1_off, seq)

    def close(self):
        try:
            self.h[H_PRODUCER] = 0
        except Exception:
            pass
        if self.proc is not None:
            self.proc.join(self.opts["linger"] + 5.0)
            if self.proc.is_alive():
                self.proc.terminate()
        try:
            for s in self.slots:
                s.release()
            self.h.release()
            self.buf.release()
            self.shm.close()
            self.shm.unlink()
        except Exception:
            pass


# ================================================================================================ rendu
BG, PANEL, GRID = (24, 22, 20), (38, 35, 32), (64, 60, 56)
WHITE, GRAY, DIM = (235, 235, 235), (165, 165, 165), (110, 110, 110)
GREEN, ORANGE, RED, CYAN, YELLOW, BLUE, MAGENTA = (90, 220, 110), (40, 165, 255), (70, 70, 245), (240, 215, 70), (60, 230, 240), (255, 150, 70), (230, 90, 230)


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def put(cv2, img, s, x, y, color=WHITE, scale=0.45, thick=1):
    cv2.putText(img, ascii_fold(s), (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else 0.0


def graph(cv2, img, rect, title, series, ymax, hlines=(), marks=(), ymin=0.0, note=""):
    """Courbes dans un cadre. series = [(valeurs, couleur)] ; marks = [(indice, couleur)] ; hlines = [(y, couleur, étiquette)]."""
    x, y, w, h = rect
    cv2.rectangle(img, (x, y), (x + w, y + h), PANEL, -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), GRID, 1)
    put(cv2, img, title, x + 6, y + 14, GRAY, 0.42)
    if note:
        put(cv2, img, note, x + w - 6 - 7 * len(note), y + 14, GRAY, 0.4)
    top, bot = y + 22, y + h - 4
    sc = (bot - top) / max(ymax - ymin, 1e-6)
    for yv, col, lab in hlines:
        if ymin <= yv <= ymax:
            yy = int(bot - (yv - ymin) * sc)
            cv2.line(img, (x + 1, yy), (x + w - 1, yy), col, 1, cv2.LINE_AA)
            if lab:
                put(cv2, img, lab, x + 4, yy - 2, col, 0.35)
    n = max((len(v) for v, _ in series), default=0)
    if n < 2:
        return
    for vals, col in series:
        v = np.asarray(vals, np.float32)
        xs = x + 2 + (np.arange(len(v)) + (n - len(v))) * ((w - 4) / (n - 1))
        ys = bot - (np.clip(v, ymin, ymax) - ymin) * sc
        ok = np.isfinite(v)
        # une courbe par tronçon de valeurs définies (pas de cible = trou, pas une chute à zéro)
        edges = np.flatnonzero(np.diff(np.concatenate([[0], ok.astype(np.int8), [0]])))
        for a, b in zip(edges[::2], edges[1::2]):
            pts = np.stack([xs[a:b], ys[a:b]], 1).astype(np.int32).reshape(-1, 1, 2)
            if b - a == 1:
                cv2.circle(img, (int(xs[a]), int(ys[a])), 1, col, -1)
            else:
                cv2.polylines(img, [pts], False, col, 1, cv2.LINE_AA)
    for i, col in marks:
        xx = int(x + 2 + (i + 0) * ((w - 4) / (n - 1)))
        cv2.line(img, (xx, top), (xx, bot), col, 1)


def box_px(b, S):
    return int(round(b[0] * S)), int(round(b[1] * S)), int(round(b[2] * S)), int(round(b[3] * S))


def dashed_rect(cv2, img, p1, p2, color, dash=6):
    (x1, y1), (x2, y2) = p1, p2
    for xa, ya, xb, yb in ((x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)):
        L = max(abs(xb - xa), abs(yb - ya))
        for t in range(0, L, dash * 2):
            e = min(t + dash, L)
            cv2.line(img, (xa + (xb - xa) * t // max(L, 1), ya + (yb - ya) * t // max(L, 1)),
                     (xa + (xb - xa) * e // max(L, 1), ya + (yb - ya) * e // max(L, 1)), color, 1)


def draw_scene(cv2, snap, conf_thr, opts, S, cand=True):
    """L'image reçue (côté réseau × S) avec boîtes, masques, réticule, zone de tir, vitesse, position prédite, suivi."""
    side = snap["side"]
    img = snap["img"]
    if snap["bgr"] == 0:
        img = img[:, :, ::-1]
    inter = cv2.INTER_NEAREST if opts.get("nearest", True) else cv2.INTER_LINEAR
    big = cv2.resize(np.ascontiguousarray(img), (side * S, side * S), interpolation=inter)
    mh, mw = snap["protos"].shape[1:]
    cxr = cyr = side / 2.0
    sel = snap["sel"]
    boxes = snap["boxes"]
    # candidats faibles (sous le seuil de décision de B) : ce que le réseau a presque vu
    if cand and opts.get("cand", True):
        for b in low_candidates(snap, 0.10, kept=boxes):
            cv2.rectangle(big, box_px(b, S)[:2], box_px(b, S)[2:], DIM, 1)
            put(cv2, big, f"{b[4]:.2f}", b[0] * S + 2, b[1] * S + 11, DIM, 0.35)
    # masques
    if opts.get("mask", True):
        for i, b in enumerate(boxes):
            col = GREEN if i == sel else BLUE
            draw_mask(cv2, big, snap, i, b, col, S, mh, mw)
    # zone de tir et ligne de décision autour de la cible choisie
    if 0 <= sel < len(boxes):
        b = boxes[sel]
        bw, bh = b[2] - b[0], b[3] - b[1]
        bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        p = snap["params"]
        hw, hh_ = bw / 2 * p.get("band", 0.7), bh / 2 * p.get("vband", 0.9)
        dashed_rect(cv2, big, (int((bcx - hw) * S), int((bcy - hh_) * S)), (int((bcx + hw) * S), int((bcy + hh_) * S)), MAGENTA)
        # ligne du masque lue par B au niveau du réticule (cellule de la grille des protos) et son étendue en x
        py = min(int(cyr * mh / side), mh - 1)
        ya, yb = int(py * side / mh * S), int((py + 1) * side / mh * S)
        strip = big[ya:yb]                                    # seulement la bande : copier toute l'image pour 12 px coûtait ~1 ms
        tint = np.empty_like(strip); tint[:] = CYAN
        strip[:] = cv2.addWeighted(strip, 0.82, tint, 0.18, 0)
        if snap["ext_ok"]:
            cv2.line(big, (int(snap["ext0"] * S), int(cyr * S)), (int(snap["ext1"] * S), int(cyr * S)), CYAN, 3, cv2.LINE_AA)
        # position prédite au moment du tir
        if snap["vel_ok"] and not math.isnan(snap["tau"]) and 0 < snap["tau"] < 1:
            dxp, dyp = snap["vx"] * snap["tau"], snap["vy"] * snap["tau"]
            g1 = (int((b[0] + dxp) * S), int((b[1] + dyp) * S)); g2 = (int((b[2] + dxp) * S), int((b[3] + dyp) * S))
            dashed_rect(cv2, big, g1, g2, ORANGE)
            cv2.arrowedLine(big, (int(bcx * S), int(bcy * S)), (int((bcx + dxp) * S), int((bcy + dyp) * S)), ORANGE, 2, cv2.LINE_AA, tipLength=0.15)
        elif snap["vel_ok"]:
            cv2.arrowedLine(big, (int(bcx * S), int(bcy * S)), (int((bcx + snap["vx"] * 0.03) * S), int((bcy + snap["vy"] * 0.03) * S)),
                            ORANGE, 2, cv2.LINE_AA, tipLength=0.2)
    # suivi : centres successifs de la boîte
    tr = snap["track"]
    for j, t in enumerate(tr):
        a = 0.35 + 0.65 * (j + 1) / max(len(tr), 1)
        cv2.circle(big, (int((t[1] + t[2]) / 2 * S), int((t[3] + t[4]) / 2 * S)), 2, tuple(int(c * a) for c in YELLOW), -1, cv2.LINE_AA)
    # boîtes
    for i, b in enumerate(boxes):
        ok = b[4] >= conf_thr
        col = (GREEN if ok else ORANGE) if i == sel else (BLUE if ok else DIM)
        x1, y1, x2, y2 = box_px(b, S)
        cv2.rectangle(big, (x1, y1), (x2, y2), col, 2 if i == sel else 1)
        put(cv2, big, f"{b[4]:.2f}" + (" CIBLE" if i == sel else ""), x1 + 2, max(y1 - 4, 11), col, 0.42, 1)
    # réticule
    c = int(cxr * S)
    for (xa, ya, xb, yb) in ((c - 14, c, c - 4, c), (c + 4, c, c + 14, c), (c, c - 14, c, c - 4), (c, c + 4, c, c + 14)):
        cv2.line(big, (xa, ya), (xb, yb), WHITE, 1, cv2.LINE_AA)
    cv2.rectangle(big, (c - S // 2, c - S // 2), (c + S // 2, c + S // 2), WHITE, 1)
    return big


def low_candidates(snap, thr, kept):
    """Boîtes sous le seuil de B (≥ thr), après NMS, hors celles que B a gardées."""
    sc = snap["head"][4]
    idx = np.nonzero(sc >= thr)[0]
    if idx.size == 0:
        return []
    h = snap["head"]
    cand = [(float(h[0, i] - h[2, i] / 2), float(h[1, i] - h[3, i] / 2), float(h[0, i] + h[2, i] / 2), float(h[1, i] + h[3, i] / 2), float(sc[i]), int(i)) for i in idx[:200]]
    kept_idx = {int(k[5]) for k in kept}
    out = []
    for b in sorted(cand, key=lambda d: -d[4]):
        if all(iou(b, k) <= 0.45 for k in out) and all(iou(b, k) <= 0.45 for k in kept):
            out.append(b)
    return [b for b in out[:6] if b[5] not in kept_idx]


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    uni = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / uni if uni > 0 else 0.0


def draw_mask(cv2, big, snap, i, b, color, S, mh, mw):
    side = snap["side"]
    gx, gy = mw / side, mh / side
    x1, y1 = max(b[0], 0.0), max(b[1], 0.0)
    x2, y2 = min(b[2], side), min(b[3], side)
    rx1, ry1 = max(int(math.floor(x1 * gx)) - 1, 0), max(int(math.floor(y1 * gy)) - 1, 0)
    rx2, ry2 = min(int(math.ceil(x2 * gx)) + 1, mw), min(int(math.ceil(y2 * gy)) + 1, mh)
    if rx2 <= rx1 or ry2 <= ry1:
        return
    logit = np.tensordot(snap["coefs"][i], snap["protos"][:, ry1:ry2, rx1:rx2], axes=(0, 0))
    X1, Y1, X2, Y2 = int(rx1 / gx * S), int(ry1 / gy * S), int(rx2 / gx * S), int(ry2 / gy * S)
    up = cv2.resize(logit, (X2 - X1, Y2 - Y1), interpolation=cv2.INTER_LINEAR)
    m = up > 0
    bx1, by1, bx2, by2 = int(x1 * S) - X1, int(y1 * S) - Y1, int(x2 * S) - X1, int(y2 * S) - Y1    # le masque est rogné à la boîte
    keep = np.zeros_like(m)
    keep[max(by1, 0):max(by2, 0), max(bx1, 0):max(bx2, 0)] = True
    m &= keep
    roi = big[Y1:Y2, X1:X2]
    tint = np.empty_like(roi); tint[:] = color
    np.copyto(roi, cv2.addWeighted(roi, 0.6, tint, 0.4, 0), where=m[..., None])
    cts, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(roi, cts, -1, color, 1, cv2.LINE_AA)


def stats5(h):
    """Fenêtre glissante de 5 s : cadence, images perdues et (p50, p99, max) de chaque mesure — un appel numpy par colonne."""
    recent = h[h["rx"] > (h["rx"][-1] - 5.0)] if len(h) else h
    out = {"recent": recent, "fps": 0.0, "drops": 0}
    if len(recent) > 1:
        span = recent["rx"][-1] - recent["rx"][0]
        out["fps"] = (len(recent) - 1) / span if span > 0 else 0.0
        out["drops"] = int(np.maximum(np.diff(recent["fid"].astype(np.int64)) - 1, 0).sum())
    for k in ("inf_ms", "post_ms", "proc_ms", "gap_ms", "nbytes", "dec_ms"):
        out[k] = tuple(float(v) for v in np.percentile(recent[k], [50, 99, 100])) if len(recent) else (0.0, 0.0, 0.0)
    return out


def render_panel(st, pan, wr):
    """Partie lente de la colonne de droite (statistiques, graphes, raisons, vignettes) dessinée dans `pan` ; mise en cache."""
    import cv2
    side, snap, h = st["side"], st["snap"], st["hist"]
    conf_thr = st["meta"].get("conf", 0.5)
    H = pan.shape[0]
    s5 = stats5(h)
    recent = s5["recent"]
    y = 0
    lines = [
        (f"images/s {s5['fps']:5.0f}   perdues (5 s) {s5['drops']}   fenetre {st['fps_view']:.0f} fps  rendu {st['render_ms']:.1f} ms  instantanes {st['snap_hz']:.0f}/s", WHITE),
        (f"inference p50 {s5['inf_ms'][0]:.2f}  p99 {s5['inf_ms'][1]:.2f}  max {s5['inf_ms'][2]:.2f} ms   post p50 {s5['post_ms'][0] * 1e3:.0f} us", CYAN),
        (f"reception->commande p50 {s5['proc_ms'][0]:.2f}  p99 {s5['proc_ms'][1]:.2f}  max {s5['proc_ms'][2]:.2f} ms", YELLOW),
        (f"ecart entre images p50 {s5['gap_ms'][0]:.1f}  p99 {s5['gap_ms'][1]:.1f}  max {s5['gap_ms'][2]:.0f} ms   "
         f"{s5['nbytes'][0] / 1000:.1f} Ko/img   decode JPEG {s5['dec_ms'][0]:.2f} ms", GRAY),
    ]
    if snap:
        v = f"v {snap['vx']:+.0f},{snap['vy']:+.0f} px/s (res {snap['vres']:.1f})" if snap["vel_ok"] else "v ?"
        tau = f"tau {snap['tau'] * 1e3:+.1f} ms" if not math.isnan(snap["tau"]) else "tau -"
        el = f"ecoule {snap['elapsed'] * 1e3:.1f} ms" if not math.isnan(snap["elapsed"]) else ""
        lines.append((f"cible: {v}  {tau}  {el}  suivi {snap['ntrack']} pts  sous reticule {'OUI' if snap['under'] else 'non'}", GREEN if snap["under"] else GRAY))
    for t, c in lines:
        put(cv2, pan, t, 2, y + 12, c, 0.4)
        y += 17
    y += 4
    n = min(len(h), 600)
    hv = h[-n:] if n else h
    infm = max(2.0, s5["inf_ms"][1] * 1.6)
    drop_marks = []
    if n > 1:
        fd = np.diff(hv["fid"].astype(np.int64))
        drop_marks = [(i + 1, RED) for i in np.nonzero(fd > 1)[0]]
    graph(cv2, pan, (0, y, wr, 110), "latence de B (ms) : inference (cyan), reception->commande (jaune)",
          [(hv["inf_ms"], CYAN), (hv["proc_ms"], YELLOW)], infm, hlines=[(1.0, GRID, "1 ms"), (infm / 2, GRID, "")], note=f"{n} images")
    y += 116
    gapm = max(12.0, min(s5["gap_ms"][1] * 1.3, 60.0))
    graph(cv2, pan, (0, y, wr, 80), "ecart entre images recues (ms) ; rouge = image(s) perdue(s)",
          [(hv["gap_ms"], GRAY)], gapm, hlines=[(6.1, GRID, "164 Hz")], marks=drop_marks)
    y += 86
    m = h[-200:] if len(h) else h
    fl = m["flags"]
    fire_marks = [(int(i), RED) for i in np.nonzero(fl & (F_FIRE | F_FIRED))[0]]
    sched_marks = [(int(i), ORANGE) for i in np.nonzero(fl & F_SCHED)[0]]
    graph(cv2, pan, (0, y, wr, 100), "x du centre de la cible (px reseau) vs reticule ; orange = tir programme, rouge = tir",
          [(m["cx"], GREEN)], float(side), hlines=[(side / 2, WHITE, "reticule")], marks=sched_marks + fire_marks)
    y += 106
    graph(cv2, pan, (0, y, wr, 64), "confiance de la cible (seuil en blanc)", [(m["conf"], MAGENTA)], 1.0,
          hlines=[(conf_thr, WHITE, f"{conf_thr:.2f}")])
    y += 70
    cv2.rectangle(pan, (0, y), (wr, y + 92), PANEL, -1)
    put(cv2, pan, "pourquoi B ne tire pas (5 s, images)", 6, y + 14, GRAY, 0.42)
    if len(recent):
        rm = recent["rmask"]
        cnt = sorted([(int(((rm >> i) & 1).sum()), r) for i, r in enumerate(REASONS)], reverse=True)
        cnt = [c for c in cnt if c[0] > 0][:5]
    else:
        cnt = []
    mx = max([c[0] for c in cnt], default=1)
    for i, (c_, r) in enumerate(cnt):
        yy = y + 22 + i * 13
        put(cv2, pan, r, 6, yy + 8, WHITE, 0.36)
        cv2.rectangle(pan, (190, yy), (190 + int((wr - 240) * c_ / mx), yy + 9), ORANGE, -1)
        put(cv2, pan, str(c_), 194 + int((wr - 240) * c_ / mx), yy + 8, WHITE, 0.36)
    y += 98
    cv2.rectangle(pan, (0, y), (wr, H), PANEL, -1)
    put(cv2, pan, "derniers tirs (image de la decision)", 6, y + 14, GRAY, 0.42)
    th = min(H - (y + 20), 150)
    for i, e in enumerate(reversed(st["events"][-4:])):
        tx = 6 + i * (th + 6)
        if tx + th > wr or th < 40:
            break
        pan[y + 20:y + 20 + th, tx:tx + th] = cv2.resize(e["thumb"], (th, th), interpolation=cv2.INTER_AREA)
        put(cv2, pan, f"#{e['fid']} {e['label']}", tx + 2, y + 32, YELLOW, 0.36)
        put(cv2, pan, f"conf {e['conf']:.2f} {e['tau']}", tx + 2, y + 20 + th - 4, WHITE, 0.36)


def render(st, W, H, S, opts):
    """Image complète de la fenêtre (BGR). La scène et le bandeau d'état sont redessinés à chaque image ; le panneau de droite
    (statistiques, graphes) est mis en cache et recalculé à 8 Hz : c'est lui qui coûtait le plus."""
    import cv2
    side = st["side"]
    canvas = st["bg"].copy()
    img_px = side * S
    snap = st["snap"]
    conf_thr = st["meta"].get("conf", 0.5)
    x_r = img_px + 24                                      # colonne de droite
    wr = W - x_r - 10
    now = time.monotonic()
    put(cv2, canvas, "AimLabs - sniper_b - vue debug", 10, 16, WHITE, 0.5, 1)
    if snap is None:
        put(cv2, canvas, "en attente de la premiere image...", 20, 60, GRAY, 0.6)
    else:
        canvas[24:24 + img_px, 10:10 + img_px] = draw_scene(cv2, snap, conf_thr, opts, S)
        cv2.rectangle(canvas, (10, 24), (10 + img_px - 1, 24 + img_px - 1), GRID, 1)
        band = canvas[25:45, 11:10 + img_px - 1]
        band[:] = (band * 0.3).astype(np.uint8)               # lisible sur le ciel clair
        age = (now - snap["rx"]) * 1e3
        put(cv2, canvas, f"#{snap['fid']}  {side}x{side} <- carre {snap['cw']}x{snap['ch']} @({snap['x0']},{snap['y0']}) de {snap['w']}x{snap['h']}  "
                         f"{'JPEG' if snap['codec'] == 1 else 'brut'} {snap['nbytes'] / 1000:.1f} Ko  decode {snap['dec_ms']:.2f} ms  vue il y a {age:.0f} ms",
            14, 38, WHITE, 0.4)
    y = 24
    if snap:
        if snap["state"] == S_IDLE:
            badge, col = "DESARME (CAPSLOCK ou ALT)", GRAY
        elif snap["locked"]:
            badge, col = "VERROUILLE (tir parti)", ORANGE
        elif snap["cool_left"] > 0:
            badge, col = f"REFROIDISSEMENT {snap['cool_left']:.1f} s", ORANGE
        elif snap["sched_in"] >= 0:
            badge, col = f"TIR PROGRAMME dans {snap['sched_in'] * 1e3:.0f} ms", RED
        elif snap["state"] == S_TRACK:
            badge, col = "CIBLE VUE - ARME", GREEN
        else:
            badge, col = "ARME - aucune cible", CYAN
    else:
        badge, col = "-", GRAY
    cv2.rectangle(canvas, (x_r, y), (x_r + wr, y + 26), PANEL, -1)
    cv2.rectangle(canvas, (x_r, y), (x_r + 6, y + 26), col, -1)
    put(cv2, canvas, badge, x_r + 14, y + 18, col, 0.6, 2)
    y0 = y + 34
    c = st["cache"]
    key = (len(st["events"]), st["events"][-1]["fid"] if st["events"] else 0)
    if c.get("img") is None or now - c["t"] >= 0.125 or c["key"] != key:
        pan = canvas[y0:H - 6, x_r:x_r + wr].copy()
        render_panel(st, pan, wr)
        c["img"], c["t"], c["key"] = pan, now, key
    canvas[y0:y0 + c["img"].shape[0], x_r:x_r + wr] = c["img"]
    put(cv2, canvas, "q quitter  espace pause  s capture  m masques  c candidats  i lissage  1-4 revoir un tir  0 direct", 10, H - 8, DIM, 0.38)
    if st.get("review"):
        put(cv2, canvas, f"REVUE : image #{st['review']} qui a decide le tir (0 = retour au direct)", 20, 64, YELLOW, 0.6, 2)
    if st["paused"]:
        put(cv2, canvas, "PAUSE", 20, 60, YELLOW, 0.9, 2)
    if st["gone"]:
        put(cv2, canvas, "B arrete", 20, 90, RED, 0.9, 2)
    elif snap is not None and now - snap["rx"] > 1.5 and not st["paused"] and not st.get("review"):
        put(cv2, canvas, "plus d'images recues", 20, 90, RED, 0.7, 2)
    return canvas


# ================================================================================================ fenêtre (processus séparé)
class Viewer:
    def __init__(self, name, opts):
        from multiprocessing import shared_memory
        self.shm = shared_memory.SharedMemory(name=name)
        self.buf = self.shm.buf
        self.h = self.buf[:HDR_SIZE].cast("I")
        assert self.h[H_MAGIC] == MAGIC and self.h[H_VERSION] == VERSION, "mémoire partagée incompatible"
        side, n, mh, mw = self.h[H_SIDE], self.h[H_N], self.h[H_MH], self.h[H_MW]
        self.L = Layout(side, n, mh, mw)
        self.slots = (Slot(self.buf, self.L.slot0, self.L), Slot(self.buf, self.L.slot1, self.L))
        self.ring = np.ndarray((RING,), REC_DTYPE, buffer=self.buf, offset=self.L.ring)
        self.meta = json.loads(bytes(self.buf[META_OFF:META_OFF + self.h[H_METALEN]]) or b"{}")
        self.opts = opts
        self.S = int(opts.get("scale", 3))
        self.side = side
        self.W = side * self.S + 24 + 650
        self.H = side * self.S + 46
        self.bg = np.empty((self.H, self.W, 3), np.uint8)    # fond précalculé : np.full((H, W, 3), BG) coûtait 4 ms par rendu
        self.bg[...] = np.array(BG, np.uint8)
        self.cache = {}
        self.read_n = 0
        self.hist = np.zeros(0, REC_DTYPE)
        self.snap = None
        self.last_pub = 0
        self.ack_pending = None
        self.t_next_ack = 0.0
        self.last_ev = 0
        self.events = []
        self.t_last_sched_ev = -1e9
        self.snap_hz = 0.0
        self.nearest = True
        self.show = dict(mask=True, cand=True, nearest=True)
        self.paused = False
        self.review = 0                                       # 0 = direct ; n = n-ième dernier tir, en grand
        self.n_snap = 0
        self.fps_view = 0.0
        self.render_ms = 0.0
        self.log = open(opts["log"], "w") if opts.get("log") else None
        if self.log:
            self.log.write(",".join(REC_DTYPE.names) + "\n")
        if opts.get("snap_dir"):
            os.makedirs(opts["snap_dir"], exist_ok=True)

    # ---- lecture
    def poll_ring(self):
        head = struct.unpack_from("<Q", self.buf, HEAD_OFF)[0]
        if head == self.read_n:
            return 0
        first = max(self.read_n, head - RING + 1)             # +1 : l'enregistrement le plus ancien peut être en cours d'écrasement
        idx = np.arange(first, head) % RING
        new = self.ring[idx].copy()
        self.read_n = head
        self.hist = np.concatenate([self.hist, new])[-3000:]
        if self.log is not None:
            for r in new:
                self.log.write(",".join(f"{v:.6g}" if isinstance(v, (float, np.floating)) else str(int(v)) for v in r) + "\n")
        return len(new)

    def read_slot(self, s: Slot):
        """Copie cohérente d'un instantané (None si le producteur écrivait pendant la lecture)."""
        b = self.buf
        seq0 = struct.unpack_from("<I", b, s.base)[0]
        vals = dict(zip(SNAP_NAMES, SNAP.unpack_from(b, s.base)))
        nb = vals["nbox"]
        flat = BOXES.unpack_from(b, s.boxes_off)
        boxes = [tuple(flat[i * 6:i * 6 + 6]) for i in range(nb)]
        boxes = [(x1, y1, x2, y2, c, int(ix)) for x1, y1, x2, y2, c, ix in boxes]
        tf = TRACK.unpack_from(b, s.track_off)
        track = [tuple(tf[i * 5:i * 5 + 5]) for i in range(vals["ntrack"])]
        d = dict(vals, boxes=boxes, track=track, coefs=s.coefs[:max(nb, 1)].copy(), head=s.head.copy(), img=s.img.copy(),
                 protos=s.protos.copy(), side=self.side, params=self.meta)
        seq1 = struct.unpack_from("<I", b, s.seq1_off)[0]
        seq0_end = struct.unpack_from("<I", b, s.base)[0]    # verrou de séquence complet : début, fin, et début encore
        if not (seq0 == seq1 == seq0_end == vals["seq0"]):
            return None
        return d

    def take_snapshot(self):
        """Lit l'instantané publié, s'il y en a un nouveau. L'accusé de réception part plus tard (voir run)."""
        pub = self.h[H_PUB]
        if pub == self.last_pub:
            return
        d = self.read_slot(self.slots[0])
        if d is not None and not self.paused:
            self.snap = d
        self.n_snap += 1
        self.last_pub = pub
        self.ack_pending = pub

    def poll_events(self, now):
        ev = self.h[H_EVPUB]
        if ev == self.last_ev:
            return
        d = None
        for _ in range(3):
            d = self.read_slot(self.slots[1])
            if d is not None:
                break
        self.last_ev = ev
        if d is None:
            return
        import cv2
        thumb = cv2.resize(draw_scene(cv2, d, self.meta.get("conf", 0.5), self.show, 2, cand=False), (300, 300), interpolation=cv2.INTER_AREA)
        label = "TIR" if d["flags"] & F_FIRE else "PROG"
        tau = f"tau {d['tau'] * 1e3:+.0f}ms" if not math.isnan(d["tau"]) else f"av {d['fire_dx']:+.1f}px"
        conf = d["boxes"][d["sel"]][4] if 0 <= d["sel"] < len(d["boxes"]) else 0.0
        e = dict(fid=d["fid"], thumb=thumb, label=label, tau=tau, conf=conf, t=now, snap=d)
        if label == "PROG" and self.events and self.events[-1]["label"] == "PROG" and now - self.events[-1]["t"] < 0.6:
            self.events[-1] = e                               # même passage : on garde la dernière décision
        else:
            self.events.append(e)
        self.events = self.events[-8:]
        if self.opts.get("snap_dir"):                         # PNG de l'image qui a décidé (tests à distance)
            st = self.state(now, False)
            st["snap"], st["review"] = d, d["fid"]
            self.save_png("tir_" + label, render(st, self.W, self.H, self.S, self.show))

    def save_png(self, tag, canvas):
        d = self.opts.get("snap_dir")
        if d and canvas is not None:
            import cv2
            cv2.imwrite(os.path.join(d, f"{tag}_{int(time.time() * 1000)}.png"), canvas)

    # ---- boucle
    def state(self, now, gone):
        snap, review = self.snap, 0
        if self.review and len(self.events) >= self.review:
            snap = self.events[-self.review]["snap"]
            review = snap["fid"]
        return dict(side=self.side, snap=snap, review=review, hist=self.hist, meta=self.meta, events=self.events, paused=self.paused,
                    gone=gone, fps_view=self.fps_view, render_ms=self.render_ms, snap_hz=self.snap_hz, bg=self.bg, cache=self.cache)

    def run(self):
        """Deux échéances par période P = 1/fps : l'ACCUSÉ part `lead` avant le rendu suivant (le producteur écrit alors un instantané
        frais, prêt à temps), puis le RENDU lit cet instantané. Entre deux, la boucle dort dans waitKey : ~60 réveils par seconde
        au lieu de 500 (un sondage à 2 ms faisait consommer 30 % d'un cœur à la fenêtre)."""
        import cv2
        headless = self.opts.get("headless")
        title = "AimLabs - vue debug de sniper_b"
        if not headless:
            cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
        self.h[H_VIEWER] = 1
        P = 1.0 / self.opts["fps"]
        lead = min(0.012, P / 2)
        now = time.monotonic()
        next_render, next_ack = now, 0.0
        t_fps, n_fps, n_snap0, t_snap_png, t_gone, shown = now, 0, 0, 0.0, None, 0
        ppid = self.opts.get("parent")
        orphan = False
        dirty, t_last_render = True, 0.0
        try:
            while True:
                now = time.monotonic()
                if self.poll_ring():
                    dirty = True
                n_ev = len(self.events)
                self.poll_events(now)
                dirty = dirty or len(self.events) != n_ev or self.last_ev != self.h[H_EVPUB]
                orphan = ppid is not None and os.getppid() != ppid          # B tué : plus personne ne fermera la mémoire partagée
                gone = self.h[H_PRODUCER] == 0 or orphan
                if gone and t_gone is None:
                    t_gone = now
                if orphan or (gone and now - t_gone > self.opts["linger"]):
                    break
                if now >= next_render and (dirty or now - t_last_render > 0.5):   # rien de neuf : on ne redessine pas (la fenêtre restait à 14 % de CPU au repos)
                    dirty = False
                    t_last_render = now
                    self.take_snapshot()
                    t0 = time.perf_counter()
                    canvas = render(self.state(now, gone), self.W, self.H, self.S, self.show)
                    self.last_canvas = canvas
                    self.render_ms = 0.9 * self.render_ms + 0.1 * (time.perf_counter() - t0) * 1e3
                    n_fps += 1
                    if now - t_fps >= 1.0:
                        self.fps_view, self.snap_hz = n_fps / (now - t_fps), (self.n_snap - n_snap0) / (now - t_fps)
                        n_fps, n_snap0, t_fps = 0, self.n_snap, now
                    every = self.opts.get("snap_every") or 0
                    if every and now - t_snap_png >= every and self.snap is not None:
                        self.save_png("view", canvas)
                        t_snap_png = now
                    if not headless:
                        cv2.imshow(title, canvas)
                        shown += 1
                        if shown == 5:
                            print(f"vue : fenêtre ouverte (visible={cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE)}), {self.W}x{self.H}", flush=True)
                if now >= next_render:
                    next_render = max(next_render + P, time.monotonic() + P / 2)   # échéance suivante, rendu fait ou non ; en retard : on ne rattrape pas
                    next_ack = next_render - lead
                if self.ack_pending is not None and time.monotonic() >= next_ack:
                    self.h[H_ACK] = self.ack_pending
                    self.ack_pending = None
                wake = next_render if self.ack_pending is None else min(next_render, next_ack)
                wait_ms = max(1, int((wake - time.monotonic()) * 1000))
                if headless:
                    time.sleep(wait_ms / 1000)
                    k = 255
                else:
                    k = cv2.waitKey(wait_ms) & 0xFF
                if k in (ord("q"), 27):
                    break
                dirty = dirty or k != 255 and k != 0xFF
                if k == ord(" "):
                    self.paused = not self.paused
                elif k == ord("s"):
                    self.save_png("capture", self.last_canvas)
                elif k == ord("m"):
                    self.show["mask"] = not self.show["mask"]
                elif k == ord("c"):
                    self.show["cand"] = not self.show["cand"]
                elif k == ord("i"):
                    self.show["nearest"] = not self.show["nearest"]
                elif ord("0") <= k <= ord("4"):
                    self.review = k - ord("0")
                if not headless and shown > 30 and cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    print("vue : fenêtre fermée", flush=True)
                    break
        finally:
            self.h[H_VIEWER] = 0
            if self.log:
                self.log.close()
            if not headless:
                cv2.destroyAllWindows()
            for sl in self.slots:
                sl.release()
            self.ring = None
            self.h.release()
            self.buf.release()
            self.shm.close()
            if orphan:                                          # le producteur n'est plus là pour supprimer le segment
                try:
                    self.shm.unlink()
                except Exception:
                    pass


def viewer_main(name, opts):
    set_low_priority()
    Viewer(name, opts).run()


# ================================================================================================ démonstration sans Mac
def demo(out, seconds, side=256, scale=3):
    """Producteur synthétique (cible qui traverse, tirs programmés) + fenêtre sans écran : écrit des PNG dans `out`."""
    os.makedirs(out, exist_ok=True)
    n, mh, mw = 1344, 64, 64
    vw = ViewTx(side, n, (NCOEF, mh, mw), meta=dict(conf=0.5, band=0.7, vband=0.9, track=2, cooldown=1.0, side=side, model="demo"),
                fps=30, scale=scale, snap_dir=out, snap_every=1.0, headless=True, linger=0.5)
    t_end = time.monotonic() + seconds
    rng = np.random.default_rng(1)
    bg = rng.integers(40, 90, (side, side, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:mh, 0:mw]
    fid, rx0, last = 0, time.monotonic(), time.monotonic()
    while time.monotonic() < t_end and vw.proc.is_alive():
        t = time.monotonic()
        while time.monotonic() - t < 1 / 150:
            pass
        now = time.monotonic()
        fid += 1
        ph = ((now - rx0) % 2.5) / 2.5
        cx, cy, bw, bh = 20 + ph * (side - 40), 130 + 8 * math.sin(now * 3), 46, 110
        img = bg.copy()
        img[int(cy - bh / 2):int(cy + bh / 2), int(cx - bw / 2):int(cx + bw / 2)] = (60, 120, 210)
        protos = np.zeros((NCOEF, mh, mw), np.float32)
        protos[0] = 8.0 * (1 - (((xx + 0.5) * side / mw - cx) / (bw / 2)) ** 2 - (((yy + 0.5) * side / mh - cy) / (bh / 2)) ** 2)
        out0 = np.zeros((116, n), np.float32)
        out0[:4, 5] = (cx, cy, bw, bh); out0[4, 5] = 0.55 + 0.3 * math.sin(now * 2); out0[84, 5] = 1.0
        out0[:4, 9] = (cx + 60, cy + 10, 30, 70); out0[4, 9] = 0.3
        conf = float(out0[4, 5])
        boxes = [(cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2, conf, 5)]
        flags = F_SCHED if 0.55 < ph < 0.56 else (F_FIRE if 0.62 < ph < 0.621 else 0)
        track = [(now - 0.01 * k, cx - 4 * k - bw / 2, cx - 4 * k + bw / 2, cy - bh / 2, cy + bh / 2) for k in range(10, -1, -1)]
        vw.frame(fid=fid, rx=now, flags=flags, gap_ms=(now - last) * 1e3, inf_ms=0.6 + 0.1 * rng.random(), post_ms=0.03, proc_ms=0.75 + 0.1 * rng.random(),
                 dec_ms=0.12, nbytes=5800, state=S_TRACK, rmask=REASON_BIT["hors bande"] if ph < 0.5 else REASON_BIT["confiance"], boxes=boxes, sel=0,
                 under=abs(cx - side / 2) < bw / 2, ext=(cx - bw / 3, cx + bw / 3), vel=(side / 2.5, 0.0, 0.4), tau=(side / 2 - cx) / (side / 2.5),
                 elapsed=0.004, cool_left=0.0, sched_in=0.012 if 0.55 < ph < 0.62 else -1.0, track=track, img=img, img_bgr=True, out0=out0,
                 protos=protos, geom=(1280, 720, 448, 168, 256, 256, 1))
        last = now
    vw.close()
    print("demo ->", out, sorted(os.listdir(out))[:6])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default="view_demo")
    ap.add_argument("--seconds", type=float, default=6.0)
    a = ap.parse_args()
    if not a.demo:
        ap.error("rien à faire sans --demo : la fenêtre est lancée par sniper_b --view")
    demo(a.out, a.seconds)
