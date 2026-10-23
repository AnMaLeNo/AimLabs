"""Mode « deux machines » : protocole TCP de référence + `RemoteClient`, un remplaçant d'`AimClient` pour
l'ordinateur B (celui qui fait tourner l'algo). Les algos existants tournent sans modification :

    python remote/poc/run_remote_algo.py --connect <ip du PC A>:7777 algos/yolo_sniper.py

Ce module est une implémentation de RÉFÉRENCE — les joueurs peuvent utiliser un autre transport, un autre
codage, un autre protocole : le jeu ne voit que le script du PC A qui parle au SDK (voir remote/README.md).

Protocole (octets little-endian, un seul flux TCP, TCP_NODELAY) : chaque message = u8 type, u32 longueur, charge.
  A → B  HELLO  json {"version", "width", "height", "mode"}
  A → B  FRAME  <QQHHHHHHB> frame_id, capture_usec, W, H, x0, y0, cw, ch, codec + pixels (0 = RGB brut, 1 = JPEG)
                 (la région (x0, y0, cw, ch) de la fenêtre W×H ; le reste de l'image n'est pas transmis)
  A → B  STATE  json : touches / clavier / souris du jeu + mesures de l'agent (`time_usec`, `fps`)
  B → A  CMD    <BQff> type (1 move, 2 fire, 3 mark, 4 fire_at, 5 cancel), frame_id, dx, dy
                 fire_at : l'agent tire à (capture de frame_id + dx ms) sur SON horloge : l'instant est décidé par B,
                 exécuté avec la précision de A, indépendamment du retard et de la gigue du réseau
`capture_usec` et `time_usec` (STATE) sont à l'horloge locale de l'agent du PC A : le jeu n'en fournit aucune en profil
« réel » (il ne sort que les touches) ; `fps` est le rythme de capture mesuré par l'agent.
"""
import json
import os
import socket
import struct
import threading
import time
from typing import Optional

import numpy as np

from .client import Frame, Keys

VERSION = 1
T_HELLO, T_FRAME, T_STATE, T_CMD = 1, 2, 3, 4
CMD_MOVE, CMD_FIRE, CMD_MARK = 1, 2, 3
CMD_FIRE_AT, CMD_CANCEL = 4, 5   # tir programmé (dx = délai en ms après la capture de frame_id) / annulation
CODEC_RAW, CODEC_JPEG = 0, 1
HDR = struct.Struct("<BI")
# Transport UDP optionnel (images / état / commandes) : un message = N datagrammes de <= UDP_CHUNK octets,
# en-tête <BIBB> type, seq, index, nombre. Une image dont un datagramme manque est simplement jetée (la
# suivante arrive 6 ms plus tard) ; chaque commande est émise deux fois et dédoublonnée par `seq`.
UDP_HDR = struct.Struct("<BIBB")
UDP_CHUNK = 1200
U_HELLO, U_FRAME, U_STATE, U_CMD = 4, T_FRAME, T_STATE, 5
FRAME_HDR = struct.Struct("<QQHHHHHHB")
CMD = struct.Struct("<BQff")


def send_msg(sock: socket.socket, typ: int, payload: bytes) -> None:
    sock.sendall(HDR.pack(typ, len(payload)) + payload)


def recv_exact(sock: socket.socket, n: int) -> bytearray:
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionError("connexion fermée")
        got += k
    return buf


def recv_msg(sock: socket.socket):
    typ, n = HDR.unpack(recv_exact(sock, HDR.size))
    return typ, recv_exact(sock, n)


class RemoteClient:
    """Même interface qu'`AimClient` (frame, state, keys, mode, move, fire, mark, game_alive), mais l'image et
    l'état arrivent par le réseau. `frame().image` est un canevas W×H RGBA dont seule la région reçue est
    remplie ; il n'est valable que jusqu'au prochain `frame()`."""

    def __init__(self, name: Optional[str] = None, timeout: float = 10.0, poll_interval: float = 0.0005):
        addr = name or os.environ.get("AIMLABS_REMOTE")
        if not addr or ":" not in addr:
            raise RuntimeError("adresse de l'agent du PC A : RemoteClient('hote:port' | 'listen:port') ou variable AIMLABS_REMOTE")
        deadline = time.monotonic() + timeout
        if addr.startswith("listen:"):
            # B écoute, l'agent du PC A se connecte (traverse le pare-feu / NAT du PC A sans règle entrante)
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind = addr[len("listen:"):]
            host, port = ("0.0.0.0", bind) if ":" not in bind else bind.rsplit(":", 1)
            srv.bind((host, int(port)))
            srv.listen(1)
            self._usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # lié avant l'accept : l'agent peut émettre tout de suite
            self._usock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
            self._usock.bind((host, int(port)))
            srv.settimeout(max(deadline - time.monotonic(), 0.1))
            try:
                self._sock, _ = srv.accept()
            except socket.timeout:
                raise TimeoutError(f"aucun agent ne s'est connecté sur {addr}")
            finally:
                srv.close()
        else:
            host, port = addr.rsplit(":", 1)
            while True:
                try:
                    self._sock = socket.create_connection((host, int(port)), timeout=2.0)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"agent du PC A injoignable sur {addr}")
                    time.sleep(0.2)
        self._sock.settimeout(None)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send_lock = threading.Lock()
        self._peer = None               # adresse UDP de l'agent une fois la poignée de main UDP faite
        self._cmd_seq = 0
        self._reasm = {}                # seq -> [n, {idx: bytes}] pour les messages UDP
        self.rx_time = 0.0              # horloge locale (monotonic) à la réception de la dernière image
        self._cond = threading.Condition()
        self._closed = False
        self._pending = None            # (id, capture_usec, W, H, x0, y0, rgb crop)
        self._pending_x = None          # (crop BGR d'origine ou None, décodage JPEG en ms, octets reçus) de _pending
        self.last_x = (None, 0.0, 0)    # idem pour la dernière image rendue par crop() : sert au mode debug de B
        self.last_frame_id = 0
        self._canvas = None
        self._state = None
        self._state_rx = 0.0            # horloge locale à la réception du dernier STATE
        self._prev_keys = frozenset()
        typ, payload = recv_msg(self._sock)
        if typ != T_HELLO:
            raise RuntimeError("l'agent ne parle pas ce protocole")
        hello = json.loads(payload)
        if hello.get("version") != VERSION:
            raise RuntimeError(f"protocole {hello.get('version')} ≠ {VERSION}")
        self.hello = hello
        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx.start()
        if getattr(self, "_usock", None) is not None:
            threading.Thread(target=self._udp_loop, daemon=True).start()
        with self._cond:
            if not self._cond.wait_for(lambda: self._state is not None, timeout):
                raise TimeoutError("pas d'état reçu de l'agent")

    # ---------------------------------------------------------------- réception
    def _on_message(self, typ: int, p) -> None:
        if typ == T_FRAME:
            fid, cap, w, h, x0, y0, cw, ch, codec = FRAME_HDR.unpack_from(p, 0)
            data = memoryview(p)[FRAME_HDR.size:]
            bgr, dec_ms = None, 0.0
            if codec == CODEC_JPEG:
                import cv2
                t_dec = time.perf_counter()
                bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                dec_ms = (time.perf_counter() - t_dec) * 1e3
                crop = bgr[:, :, ::-1]
            else:
                crop = np.frombuffer(data, np.uint8).reshape(ch, cw, 3)
            with self._cond:
                if self._pending is None or fid > self._pending[0]:
                    self._pending = (fid, cap, w, h, x0, y0, crop)
                    self._pending_x = (bgr, dec_ms, len(data))
                    self.rx_time = time.monotonic()
                    self._cond.notify_all()
        elif typ == T_STATE:
            st = json.loads(bytes(p))
            with self._cond:
                self._state, self._state_rx = st, time.monotonic()
                self._cond.notify_all()

    def _rx_loop(self) -> None:
        try:
            while not self._closed:
                typ, p = recv_msg(self._sock)
                self._on_message(typ, p)
        except (ConnectionError, OSError):
            pass
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _udp_loop(self) -> None:
        try:
            while not self._closed:
                d, addr = self._usock.recvfrom(2048)
                typ, seq, idx, n = UDP_HDR.unpack_from(d, 0)
                if typ == U_HELLO:
                    self._peer = addr
                    self._usock.sendto(UDP_HDR.pack(U_HELLO, 0, 0, 1), addr)
                    continue
                body = d[UDP_HDR.size:]
                if n == 1:
                    self._on_message(typ, body)
                    continue
                ent = self._reasm.setdefault((typ, seq), [n, {}])
                ent[1][idx] = body
                if len(ent[1]) == n:
                    del self._reasm[(typ, seq)]
                    self._on_message(typ, b"".join(ent[1][i] for i in range(n)))
                if len(self._reasm) > 32:   # messages incomplets : on garde les plus récents
                    for k in sorted(self._reasm)[:len(self._reasm) - 16]:
                        del self._reasm[k]
        except OSError:
            pass

    # ---------------------------------------------------------------- AimClient
    def close(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def __enter__(self) -> "RemoteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def game_alive(self, timeout: float = 0.5) -> bool:
        with self._cond:
            if self._closed:
                return False
            self._cond.wait_for(lambda: time.monotonic() - self._state_rx < 1.0 or self._closed, timeout)
            return not self._closed and bool(self._state.get("game_alive", True)) and time.monotonic() - self._state_rx < 1.0

    @property
    def mode(self) -> dict:
        m = dict(self.hello.get("mode", {}))
        m["vision"] = True  # l'image arrive, par un autre chemin que le pont
        m["vision_size"] = (self.hello["width"], self.hello["height"])
        return m

    @property
    def latest_frame_id(self) -> int:
        return self._pending[0] if self._pending else 0

    def frame(self, timeout: float = 1.0) -> Optional[Frame]:
        with self._cond:
            ok = self._cond.wait_for(lambda: (self._pending is not None and self._pending[0] > self.last_frame_id) or self._closed, timeout)
            if not ok or self._pending is None or self._pending[0] <= self.last_frame_id:
                return None
            fid, cap, w, h, x0, y0, crop = self._pending
        self.last_frame_id = fid
        if self._canvas is None or self._canvas.shape[:2] != (h, w):
            self._canvas = np.zeros((h, w, 4), np.uint8)
            self._canvas[..., 3] = 255
        self._canvas[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1], :3] = crop
        return Frame(fid, w, h, cap, 0.0, 0.0, 0.0, fid, self._canvas, time.perf_counter_ns() // 1000)

    latest_frame = frame

    def crop(self, timeout: float = 1.0):
        """(frame_id, capture_usec (horloge du jeu), rx_time local, crop RGB (h, w, 3), (x0, y0), (W, H)) de la
        dernière image plus récente que la précédente, sans passer par le canevas ; None au timeout."""
        with self._cond:
            ok = self._cond.wait_for(lambda: (self._pending is not None and self._pending[0] > self.last_frame_id) or self._closed, timeout)
            if not ok or self._pending is None or self._pending[0] <= self.last_frame_id:
                return None
            fid, cap, w, h, x0, y0, crop = self._pending
            rx = self.rx_time
            self.last_x = self._pending_x
        self.last_frame_id = fid
        return fid, cap, rx, crop, (x0, y0), (w, h)

    def state(self) -> dict:
        with self._cond:
            st = dict(self._state)
            age_us = int((time.monotonic() - self._state_rx) * 1e6)
        st["time_usec"] = st["time_usec"] + age_us  # extrapolé à l'horloge du jeu « maintenant »
        st["pos"], st["vel"] = tuple(st.get("pos", (0, 0, 0))), tuple(st.get("vel", (0, 0, 0)))
        for k in ("keys_down", "physical_keys_down", "mouse_down"):
            st[k] = frozenset(st.get(k, ()))
        return st

    def keys(self) -> Keys:
        s = self.state()
        down = s["keys_down"] | s["mouse_down"]
        k = Keys(s["keys_down"], s["physical_keys_down"], s["mouse_down"], down - self._prev_keys, self._prev_keys - down)
        self._prev_keys = down
        return k

    def _push(self, kind: int, dx: float, dy: float, frame_id: Optional[int]) -> bool:
        payload = CMD.pack(kind, frame_id or 0, dx, dy)
        peer = self._peer
        try:
            if peer is not None:
                self._cmd_seq += 1
                d = UDP_HDR.pack(U_CMD, self._cmd_seq, 0, 1) + payload
                self._usock.sendto(d, peer)
                self._usock.sendto(d, peer)   # doublon : dédoublonné par seq côté A
                return True
            with self._send_lock:
                send_msg(self._sock, T_CMD, payload)
            return True
        except OSError:
            return False

    def move(self, dx: float, dy: float, frame_id: Optional[int] = None) -> bool:
        return self._push(CMD_MOVE, dx, dy, frame_id)

    def mark(self, frame_id: Optional[int] = None, x: float = 0.0, y: float = 0.0) -> bool:
        return self._push(CMD_MARK, x, y, frame_id)

    def fire(self, frame_id: Optional[int] = None) -> bool:
        return self._push(CMD_FIRE, 0.0, 0.0, frame_id)

    def fire_at(self, frame_id: int, delay_s: float) -> bool:
        """Tir programmé : `delay_s` secondes après la capture de l'image `frame_id` (remplace le tir déjà programmé)."""
        return self._push(CMD_FIRE_AT, delay_s * 1e3, 0.0, frame_id)

    def cancel_fire(self) -> bool:
        return self._push(CMD_CANCEL, 0.0, 0.0, 0)


def install() -> None:
    """Fait pointer `aimlabs.AimClient` sur `RemoteClient` : les algos (`from aimlabs import AimClient`) qui
    sont importés ensuite parlent au PC A sans être modifiés."""
    import aimlabs
    aimlabs.AimClient = RemoteClient
