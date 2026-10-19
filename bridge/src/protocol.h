// AimBridge shared-memory protocol, version 1.
//
// One shared-memory segment (POSIX shm on Linux, named file mapping on Windows)
// is created by the game and attached by the player's program. Layout:
//
//   [Header 256 B][State 256 B][Command ring: 64 B header + N x 32 B]
//   [Frame slot 0][Frame slot 1]...   (each: 64 B header + max_width*max_height*4 B)
//
// Frames and the state block are protected by seqlocks (odd `seq` = being
// written); the command ring is a single-producer (client) / single-consumer
// (game) ring buffer. All multi-byte fields are little-endian, natively aligned.
// Keep sdk/python/aimlabs/protocol.py in sync with this file.
#pragma once

#include <cstdint>

namespace aimbridge {

constexpr uint32_t MAGIC = 0x314D4941; // "AIM1"
constexpr uint32_t VERSION = 1;
constexpr uint32_t PIXEL_FORMAT_RGBA8 = 1;

constexpr uint32_t MODE_VISION = 1u << 0; // frames are published
constexpr uint32_t MODE_MOVE = 1u << 1;   // MOVE commands are applied
constexpr uint32_t MODE_FIRE = 1u << 2;   // FIRE commands are applied

constexpr uint32_t CMD_MOVE = 1;
constexpr uint32_t CMD_FIRE = 2;

constexpr uint32_t KEY_FORWARD = 1u << 0;
constexpr uint32_t KEY_BACK = 1u << 1;
constexpr uint32_t KEY_LEFT = 1u << 2;
constexpr uint32_t KEY_RIGHT = 1u << 3;
constexpr uint32_t KEY_JUMP = 1u << 4;
constexpr uint32_t KEY_SPRINT = 1u << 5;
constexpr uint32_t KEY_FIRE = 1u << 6;
constexpr uint32_t KEY_MOUSE_CAPTURED = 1u << 7;
constexpr uint32_t KEY_ON_FLOOR = 1u << 8;

struct Header {
	uint32_t magic;
	uint32_t version;
	uint32_t header_size;
	uint32_t pixel_format;
	uint64_t total_size;
	uint64_t frames_offset;
	uint32_t frame_slots;
	uint32_t frame_slot_size;
	uint32_t max_width;
	uint32_t max_height;
	uint64_t state_offset;
	uint64_t commands_offset;
	uint32_t command_capacity;
	uint32_t command_size;
	uint64_t game_pid;
	uint64_t latest_frame_id; // atomic, 0 = nothing published yet
	uint32_t latest_slot; // atomic
	uint32_t mode_flags; // atomic, MODE_*
	uint64_t client_heartbeat; // atomic, incremented by the client
	uint64_t game_heartbeat_usec; // atomic, game clock, written every frame
	uint32_t vision_width; // 0 = native viewport size
	uint32_t vision_height;
	uint8_t reserved[256 - 120];
};
static_assert(sizeof(Header) == 256, "Header must be 256 bytes");

struct FrameHeader {
	uint32_t seq; // atomic seqlock
	uint32_t width;
	uint32_t height;
	uint32_t stride; // bytes per row (= width * 4)
	uint64_t frame_id;
	uint64_t capture_usec; // game clock (Time.get_ticks_usec) when the readback was requested
	float yaw_deg;
	float pitch_deg;
	float fov_deg;
	uint32_t flags;
	uint64_t render_frame; // Engine.get_frames_drawn()
	uint64_t reserved;
};
static_assert(sizeof(FrameHeader) == 64, "FrameHeader must be 64 bytes");

struct State {
	uint32_t seq; // atomic seqlock
	uint32_t keys; // KEY_*
	uint64_t tick;
	uint64_t time_usec;
	float pos[3];
	float vel[3];
	float yaw_deg;
	float pitch_deg;
	float fov_deg;
	float sens_deg_per_px;
	float real_look_dx; // physical mouse pixels this frame
	float real_look_dy;
	float assist_look_dx; // bridge pixels applied this frame
	float assist_look_dy;
	uint32_t shots;
	uint32_t hits;
	uint32_t headshots;
	uint32_t kills;
	uint32_t cmds_applied;
	uint32_t cmds_dropped;
	uint64_t last_latency_usec; // frame capture -> command applied
	uint64_t avg_latency_usec;
	float fps;
	uint32_t frames_published;
	uint8_t reserved[256 - 128];
};
static_assert(sizeof(State) == 256, "State must be 256 bytes");

struct CommandRing {
	uint64_t head; // atomic, written by the client
	uint64_t tail; // atomic, written by the game
	uint8_t reserved[64 - 16];
};
static_assert(sizeof(CommandRing) == 64, "CommandRing must be 64 bytes");

struct Command {
	uint32_t type; // CMD_*
	uint32_t flags;
	float dx; // MOVE: pixels, x = right, y = down (same convention as the mouse)
	float dy;
	uint64_t frame_id; // frame the decision was based on (0 = unknown), used for latency stats
	uint64_t client_usec; // client clock, informative only
};
static_assert(sizeof(Command) == 32, "Command must be 32 bytes");

constexpr uint32_t DEFAULT_FRAME_SLOTS = 3;
constexpr uint32_t DEFAULT_COMMAND_CAPACITY = 1024;

} // namespace aimbridge
