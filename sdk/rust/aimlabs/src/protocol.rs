//! Mirror of `bridge/src/protocol.h` (version 2). All structs are `#[repr(C)]`, little-endian,
//! natively aligned — keep in sync with the C++ header and `sdk/python/aimlabs/protocol.py`.

pub const MAGIC: u32 = 0x314D_4941; // "AIM1"
pub const VERSION: u32 = 2;
pub const PIXEL_FORMAT_RGBA8: u32 = 1;

pub const MODE_VISION: u32 = 1 << 0;
pub const MODE_MOVE: u32 = 1 << 1;
pub const MODE_FIRE: u32 = 1 << 2;
pub const MODE_INFO_STATE: u32 = 1 << 3; // State block carries game info (else only the keys)
pub const MODE_INFO_META: u32 = 1 << 4;  // frame headers carry capture time / view angles / render frame

pub const CMD_MOVE: u32 = 1;
pub const CMD_FIRE: u32 = 2;
pub const CMD_MARK: u32 = 3;

pub const KEY_FORWARD: u32 = 1 << 0;
pub const KEY_BACK: u32 = 1 << 1;
pub const KEY_LEFT: u32 = 1 << 2;
pub const KEY_RIGHT: u32 = 1 << 3;
pub const KEY_JUMP: u32 = 1 << 4;
pub const KEY_SPRINT: u32 = 1 << 5;
pub const KEY_FIRE: u32 = 1 << 6;
pub const KEY_MOUSE_CAPTURED: u32 = 1 << 7;
pub const KEY_ON_FLOOR: u32 = 1 << 8;

pub const KEY_SLOTS: usize = 512;
pub const GODOT_KEY_SPECIAL: u32 = 1 << 22;
/// Key slots of the special keys used by the algos (slot = 256 + (keycode & !GODOT_KEY_SPECIAL)).
pub const SLOT_ESCAPE: usize = 257;
pub const SLOT_SHIFT: usize = 277;
pub const SLOT_CTRL: usize = 278;
pub const SLOT_ALT: usize = 280;
pub const SLOT_CAPSLOCK: usize = 281;

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Header {
    pub magic: u32,
    pub version: u32,
    pub header_size: u32,
    pub pixel_format: u32,
    pub total_size: u64,
    pub frames_offset: u64,
    pub frame_slots: u32,
    pub frame_slot_size: u32,
    pub max_width: u32,
    pub max_height: u32,
    pub state_offset: u64,
    pub commands_offset: u64,
    pub command_capacity: u32,
    pub command_size: u32,
    pub game_pid: u64,
    pub latest_frame_id: u64, // atomic, 0 = nothing published yet
    pub latest_slot: u32,     // atomic
    pub mode_flags: u32,      // atomic, MODE_*
    pub client_heartbeat: u64, // atomic, incremented by the client
    pub game_heartbeat_usec: u64, // atomic, game clock, written every frame
    pub vision_width: u32,
    pub vision_height: u32,
    pub reserved: [u8; 256 - 120],
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct FrameHeader {
    pub seq: u32, // seqlock, odd = being written
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub frame_id: u64,
    pub capture_usec: u64,
    pub yaw_deg: f32,
    pub pitch_deg: f32,
    pub fov_deg: f32,
    pub flags: u32,
    pub render_frame: u64,
    pub reserved: u64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct State {
    pub seq: u32,
    pub keys: u32,
    pub tick: u64,
    pub time_usec: u64,
    pub pos: [f32; 3],
    pub vel: [f32; 3],
    pub yaw_deg: f32,
    pub pitch_deg: f32,
    pub fov_deg: f32,
    pub sens_deg_per_px: f32,
    pub real_look_dx: f32,
    pub real_look_dy: f32,
    pub assist_look_dx: f32,
    pub assist_look_dy: f32,
    pub shots: u32,
    pub hits: u32,
    pub headshots: u32,
    pub kills: u32,
    pub cmds_applied: u32,
    pub cmds_dropped: u32,
    pub last_latency_usec: u64,
    pub avg_latency_usec: u64,
    pub fps: f32,
    pub frames_published: u32,
    pub key_bits: [u64; KEY_SLOTS / 64],
    pub physical_key_bits: [u64; KEY_SLOTS / 64],
    pub mouse_buttons: u32,
    pub reserved: [u8; 512 - 260],
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct CommandRing {
    pub head: u64, // written by the client
    pub tail: u64, // written by the game
    pub reserved: [u8; 64 - 16],
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct Command {
    pub kind: u32,
    pub flags: u32,
    pub dx: f32,
    pub dy: f32,
    pub frame_id: u64,
    pub client_usec: u64,
}

const _: () = assert!(std::mem::size_of::<Header>() == 256);
const _: () = assert!(std::mem::size_of::<FrameHeader>() == 64);
const _: () = assert!(std::mem::size_of::<State>() == 512);
const _: () = assert!(std::mem::size_of::<CommandRing>() == 64);
const _: () = assert!(std::mem::size_of::<Command>() == 32);
