"""AimClient: read frames / player state from a running AimLabs game and send it
aim-assist commands, through the AimBridge shared memory."""

from __future__ import annotations

import struct
import sys
import time
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Optional

import numpy as np

from . import protocol as P


@dataclass
class Frame:
    id: int
    width: int
    height: int
    capture_usec: int  # game clock (Time.get_ticks_usec) when the GPU readback was requested
    yaw: float
    pitch: float
    fov: float
    render_frame: int
    image: np.ndarray  # (height, width, 4) uint8 RGBA, own copy


class AimClient:
    """Attach with ``AimClient()`` while the game runs (default segment name ``aimlabs``)."""

    def __init__(self, name: str = "aimlabs", timeout: float = 10.0, poll_interval: float = 0.0005):
        self.name = name
        self.poll_interval = poll_interval
        self._shm: Optional[shared_memory.SharedMemory] = None
        self._heartbeat = 0
        self.last_frame_id = 0
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._attach()
                break
            except (FileNotFoundError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"no AimBridge segment '{name}' (is the game running with the bridge enabled?)")
                time.sleep(0.1)

    def _attach(self) -> None:
        if sys.version_info >= (3, 13):
            shm = shared_memory.SharedMemory(self.name, track=False)
        else:
            shm = shared_memory.SharedMemory(self.name)
            if sys.platform != "win32":
                # Without this the resource tracker unlinks the game's segment when we exit.
                from multiprocessing import resource_tracker
                try:
                    resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
                except Exception:
                    pass
        buf = shm.buf
        if len(buf) < P.HEADER_SIZE:
            shm.close()
            raise ConnectionError("segment too small")
        (magic, version, header_size, pixel_format, total_size, frames_offset, frame_slots,
         frame_slot_size, max_width, max_height, state_offset, commands_offset,
         command_capacity, command_size, game_pid) = P.HEADER_STATIC.unpack_from(buf, 0)
        if magic != P.MAGIC:
            shm.close()
            raise ConnectionError("segment not initialised yet")
        if version != P.VERSION:
            shm.close()
            raise RuntimeError(f"AimBridge protocol version {version}, this SDK speaks {P.VERSION}")
        if pixel_format != P.PIXEL_FORMAT_RGBA8:
            shm.close()
            raise RuntimeError(f"unsupported pixel format {pixel_format}")
        self._shm = shm
        self._buf = buf
        self.frames_offset = frames_offset
        self.frame_slots = frame_slots
        self.frame_slot_size = frame_slot_size
        self.max_width = max_width
        self.max_height = max_height
        self.state_offset = state_offset
        self.commands_offset = commands_offset
        self.command_capacity = command_capacity
        self.game_pid = game_pid
        self._u64 = struct.Struct("<Q")
        self._u32 = struct.Struct("<I")
        if not self.game_alive(0.5):
            self.close()
            raise ConnectionError("segment belongs to a game that is no longer running")

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._shm is not None:
            self._buf = None
            self._shm.close()
            self._shm = None

    def __enter__(self) -> "AimClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _beat(self) -> None:
        self._heartbeat += 1
        self._u64.pack_into(self._buf, P.OFF_CLIENT_HEARTBEAT, self._heartbeat)

    def game_alive(self, timeout: float = 0.5) -> bool:
        """True if the game updates its heartbeat within `timeout` seconds (a stale segment
        left by a crashed game keeps its last values forever)."""
        hb = self._u64.unpack_from(self._buf, P.OFF_GAME_HEARTBEAT)[0]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.01)
            if self._u64.unpack_from(self._buf, P.OFF_GAME_HEARTBEAT)[0] != hb:
                return True
        return False

    # ------------------------------------------------------------ mode

    @property
    def mode(self) -> dict:
        flags = self._u32.unpack_from(self._buf, P.OFF_MODE_FLAGS)[0]
        vw, vh = struct.unpack_from("<II", self._buf, P.OFF_VISION_SIZE)
        return {
            "vision": bool(flags & P.MODE_VISION),
            "move": bool(flags & P.MODE_MOVE),
            "fire": bool(flags & P.MODE_FIRE),
            "vision_size": (vw, vh),  # (0, 0) = native viewport resolution
        }

    # ------------------------------------------------------------ frames

    @property
    def latest_frame_id(self) -> int:
        return self._u64.unpack_from(self._buf, P.OFF_LATEST_FRAME_ID)[0]

    def latest_frame(self) -> Optional[Frame]:
        """Copy of the most recent published frame, or None if nothing was published yet."""
        for _ in range(64):
            frame_id = self.latest_frame_id
            if frame_id == 0:
                return None
            slot = self._u32.unpack_from(self._buf, P.OFF_LATEST_SLOT)[0]
            base = self.frames_offset + slot * self.frame_slot_size
            seq1, w, h, stride, fid, cap, yaw, pitch, fov, _flags, rframe = P.FRAME_HEADER.unpack_from(self._buf, base)
            if seq1 & 1 or fid != frame_id:
                continue  # being written or already recycled; retry
            n = h * stride
            pixels = np.frombuffer(self._buf, dtype=np.uint8, count=n, offset=base + P.FRAME_HEADER_SIZE).copy()
            seq2 = self._u32.unpack_from(self._buf, base)[0]
            if seq2 != seq1:
                continue
            self._beat()
            return Frame(fid, w, h, cap, yaw, pitch, fov, rframe, pixels.reshape(h, stride // 4, 4)[:, :w, :])
        return None

    def frame(self, timeout: float = 1.0) -> Optional[Frame]:
        """Block until a frame newer than the last one returned is available (None on timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            if self.latest_frame_id > self.last_frame_id:
                f = self.latest_frame()
                if f is not None and f.id > self.last_frame_id:
                    self.last_frame_id = f.id
                    return f
            if time.monotonic() >= deadline:
                return None
            if self.poll_interval > 0:
                time.sleep(self.poll_interval)

    # ------------------------------------------------------------ state

    def state(self) -> dict:
        for _ in range(64):
            values = P.STATE.unpack_from(self._buf, self.state_offset)
            if values[0] & 1:
                continue
            seq2 = self._u32.unpack_from(self._buf, self.state_offset)[0]
            if seq2 != values[0]:
                continue
            d = dict(zip(P.STATE_FIELDS, values))
            d["pos"] = (d.pop("x"), d.pop("y"), d.pop("z"))
            d["vel"] = (d.pop("vx"), d.pop("vy"), d.pop("vz"))
            keys = d["keys"]
            d["keys"] = {
                "forward": bool(keys & P.KEY_FORWARD), "back": bool(keys & P.KEY_BACK),
                "left": bool(keys & P.KEY_LEFT), "right": bool(keys & P.KEY_RIGHT),
                "jump": bool(keys & P.KEY_JUMP), "sprint": bool(keys & P.KEY_SPRINT),
                "fire": bool(keys & P.KEY_FIRE), "mouse_captured": bool(keys & P.KEY_MOUSE_CAPTURED),
                "on_floor": bool(keys & P.KEY_ON_FLOOR),
            }
            return d
        raise RuntimeError("state block kept changing")

    # ------------------------------------------------------------ commands

    def _push(self, ctype: int, dx: float, dy: float, frame_id: int) -> bool:
        ring = self.commands_offset
        head = self._u64.unpack_from(self._buf, ring + P.RING_OFF_HEAD)[0]
        tail = self._u64.unpack_from(self._buf, ring + P.RING_OFF_TAIL)[0]
        if head - tail >= self.command_capacity:
            return False  # the game is not draining fast enough
        off = ring + P.RING_HEADER_SIZE + (head % self.command_capacity) * P.COMMAND_SIZE
        P.COMMAND.pack_into(self._buf, off, ctype, 0, float(dx), float(dy), int(frame_id), time.perf_counter_ns() // 1000)
        self._u64.pack_into(self._buf, ring + P.RING_OFF_HEAD, head + 1)
        self._beat()
        return True

    def move(self, dx: float, dy: float, frame_id: Optional[int] = None) -> bool:
        """Rotate the view by (dx, dy) pixels of mouse movement (x right, y down).
        Pass the id of the frame the decision was based on so the game can measure latency."""
        return self._push(P.CMD_MOVE, dx, dy, self.last_frame_id if frame_id is None else frame_id)

    def fire(self, frame_id: Optional[int] = None) -> bool:
        """Request one shot (subject to the weapon's fire rate and the game mode)."""
        return self._push(P.CMD_FIRE, 0.0, 0.0, self.last_frame_id if frame_id is None else frame_id)
