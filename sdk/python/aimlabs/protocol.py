"""Shared-memory layout of the AimBridge, mirrored from bridge/src/protocol.h (version 2)."""

import struct

MAGIC = 0x314D4941  # "AIM1"
VERSION = 2
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

STATE_SIZE = 512
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

# Raw keyboard / mouse, appended to the state block (see protocol.h):
#   key_bits[8] (layout keycodes), physical_key_bits[8] (physical keycodes), mouse_buttons
KEY_SLOTS = 512
STATE_KEYS_OFFSET = 128
STATE_KEYS = struct.Struct("<8Q8QI")
assert STATE_KEYS.size == 132

# slot -> Godot key name (slot < 256: Latin-1 keycode, slot >= 256: 256 + special index)
KEY_NAMES = {i: chr(i) for i in range(32, 127)}
KEY_NAMES.update({32: "SPACE", 165: "YEN", 167: "SECTION"})
_SPECIALS = (
    "ESCAPE TAB BACKTAB BACKSPACE ENTER KP_ENTER INSERT DELETE PAUSE PRINT SYSREQ CLEAR HOME END "
    "LEFT UP RIGHT DOWN PAGEUP PAGEDOWN SHIFT CTRL META ALT CAPSLOCK NUMLOCK SCROLLLOCK "
    + " ".join(f"F{i}" for i in range(1, 36))
)
KEY_NAMES.update({257 + i: n for i, n in enumerate(_SPECIALS.split())})
KEY_NAMES.update({322: "MENU", 323: "HYPER", 325: "HELP", 328: "BACK", 329: "FORWARD", 330: "STOP",
                  331: "REFRESH", 332: "VOLUMEDOWN", 333: "VOLUMEMUTE", 334: "VOLUMEUP",
                  340: "MEDIAPLAY", 341: "MEDIASTOP", 342: "MEDIAPREVIOUS", 343: "MEDIANEXT",
                  344: "MEDIARECORD", 345: "HOMEPAGE", 346: "FAVORITES", 347: "SEARCH", 348: "STANDBY",
                  349: "OPENURL", 350: "LAUNCHMAIL", 351: "LAUNCHMEDIA"})
KEY_NAMES.update({352 + i: f"LAUNCH{i:X}" for i in range(16)})
KEY_NAMES.update({368: "GLOBE", 369: "KEYBOARD", 370: "JIS_EISU", 371: "JIS_KANA",
                  385: "KP_MULTIPLY", 386: "KP_DIVIDE", 387: "KP_SUBTRACT", 388: "KP_PERIOD", 389: "KP_ADD"})
KEY_NAMES.update({390 + i: f"KP_{i}" for i in range(10)})
assert KEY_NAMES[281] == "CAPSLOCK" and KEY_NAMES[284] == "F1" and KEY_NAMES[318] == "F35"
KEY_SLOTS_BY_NAME = {n: s for s, n in KEY_NAMES.items()}
KEY_SLOTS_BY_NAME[" "] = 32

MOUSE_BUTTON_NAMES = {1: "LEFT", 2: "RIGHT", 3: "MIDDLE", 4: "WHEEL_UP", 5: "WHEEL_DOWN",
                      6: "WHEEL_LEFT", 7: "WHEEL_RIGHT", 8: "XBUTTON1", 9: "XBUTTON2"}

RING_HEADER_SIZE = 64
RING_OFF_HEAD = 0  # u64, written by the client
RING_OFF_TAIL = 8  # u64, written by the game

COMMAND_SIZE = 32
COMMAND = struct.Struct("<IIffQQ")
assert COMMAND.size == COMMAND_SIZE
