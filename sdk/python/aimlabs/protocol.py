"""Shared-memory layout of the AimBridge, mirrored from bridge/src/protocol.h (version 1)."""

import struct

MAGIC = 0x314D4941  # "AIM1"
VERSION = 1
PIXEL_FORMAT_RGBA8 = 1

MODE_VISION = 1 << 0
MODE_MOVE = 1 << 1
MODE_FIRE = 1 << 2

CMD_MOVE = 1
CMD_FIRE = 2

KEY_FORWARD = 1 << 0
KEY_BACK = 1 << 1
KEY_LEFT = 1 << 2
KEY_RIGHT = 1 << 3
KEY_JUMP = 1 << 4
KEY_SPRINT = 1 << 5
KEY_FIRE = 1 << 6
KEY_MOUSE_CAPTURED = 1 << 7
KEY_ON_FLOOR = 1 << 8

HEADER_SIZE = 256
# magic, version, header_size, pixel_format, total_size, frames_offset, frame_slots,
# frame_slot_size, max_width, max_height, state_offset, commands_offset,
# command_capacity, command_size, game_pid
HEADER_STATIC = struct.Struct("<IIIIQQIIIIQQIIQ")
assert HEADER_STATIC.size == 80
OFF_LATEST_FRAME_ID = 80   # u64
OFF_LATEST_SLOT = 88       # u32
OFF_MODE_FLAGS = 92        # u32
OFF_CLIENT_HEARTBEAT = 96  # u64
OFF_GAME_HEARTBEAT = 104   # u64
OFF_VISION_SIZE = 112      # u32 width, u32 height

FRAME_HEADER_SIZE = 64
# seq, width, height, stride, frame_id, capture_usec, yaw, pitch, fov, flags, render_frame
FRAME_HEADER = struct.Struct("<IIIIQQfffIQ")
assert FRAME_HEADER.size == 56

STATE_SIZE = 256
STATE = struct.Struct("<IIQQ3f3fffff ffff IIII II QQ fI".replace(" ", ""))
assert STATE.size == 128
STATE_FIELDS = (
    "seq", "keys", "tick", "time_usec",
    "x", "y", "z", "vx", "vy", "vz",
    "yaw", "pitch", "fov", "sens",
    "real_dx", "real_dy", "assist_dx", "assist_dy",
    "shots", "hits", "headshots", "kills",
    "cmds_applied", "cmds_dropped",
    "last_latency_usec", "avg_latency_usec",
    "fps", "frames_published",
)
assert len(STATE_FIELDS) == len(STATE.unpack(b"\0" * STATE.size))

RING_HEADER_SIZE = 64
RING_OFF_HEAD = 0  # u64, written by the client
RING_OFF_TAIL = 8  # u64, written by the game

COMMAND_SIZE = 32
COMMAND = struct.Struct("<IIffQQ")
assert COMMAND.size == COMMAND_SIZE
