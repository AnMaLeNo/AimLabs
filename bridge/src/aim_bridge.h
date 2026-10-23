// AimBridge: publishes the rendered frame and the player state to a shared
// memory segment and turns the commands written there by an external program
// (the player's aim-assist pipeline) into `assist_move` / `assist_fire` signals.
#pragma once

#include "protocol.h"
#include "shared_memory.h"

#include <godot_cpp/classes/node.hpp>
#include <godot_cpp/classes/viewport.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>

#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

namespace godot {

class AimBridge : public Node {
	GDCLASS(AimBridge, Node)

public:
	AimBridge();
	~AimBridge() override;

	bool start(Viewport *p_viewport);
	void stop();
	bool is_running() const { return _running; }

	void set_shm_name(const String &p_name) { _shm_name = p_name; }
	String get_shm_name() const { return _shm_name; }

	// vision_size = Vector2i(0, 0) publishes the native viewport resolution.
	void set_mode(bool p_vision, bool p_allow_move, bool p_allow_fire, const Vector2i &p_vision_size);
	// Information profile: p_state = the State block carries game info (else only the keys, and the game heartbeat
	// is an opaque counter instead of the game clock); p_meta = frame headers carry capture time / view angles.
	void set_info(bool p_state, bool p_meta);
	Dictionary get_mode() const;

	void publish_state(const Dictionary &p_state);
	bool is_client_connected() const { return _client_connected; }
	Dictionary get_stats() const;

	void _process(double p_delta) override;
	void _exit_tree() override;

protected:
	static void _bind_methods();

private:
	struct PendingCapture {
		uint64_t frame_id;
		uint64_t capture_usec;
		uint64_t render_frame;
		uint32_t width;
		uint32_t height;
		float yaw;
		float pitch;
		float fov;
	};
	struct Job {
		PendingCapture meta;
		PackedByteArray data;
	};

	void _on_frame_post_draw();
	void _request_readback_rd();
	void _on_readback(const PackedByteArray &p_data);
	void _capture_sync();
	void _enqueue(const PendingCapture &p_meta, const PackedByteArray &p_data);

	void _worker_main();
	void _publish_frame(const Job &p_job);
	void _drain_commands();
	void _write_mode_flags();

	aimbridge::Header *_header() const { return reinterpret_cast<aimbridge::Header *>(_shm.data()); }
	aimbridge::State *_state() const { return reinterpret_cast<aimbridge::State *>(_shm.data() + _header()->state_offset); }
	aimbridge::CommandRing *_ring() const { return reinterpret_cast<aimbridge::CommandRing *>(_shm.data() + _header()->commands_offset); }
	aimbridge::Command *_commands() const { return reinterpret_cast<aimbridge::Command *>(_shm.data() + _header()->commands_offset + sizeof(aimbridge::CommandRing)); }
	aimbridge::FrameHeader *_slot(uint32_t p_index) const {
		return reinterpret_cast<aimbridge::FrameHeader *>(_shm.data() + _header()->frames_offset + (size_t)p_index * _header()->frame_slot_size);
	}

	String _shm_name = "aimlabs";
	aimbridge::SharedMemory _shm;
	Viewport *_viewport = nullptr;
	bool _running = false;
	bool _use_rd = false;
	bool _rd_format_warned = false;

	bool _vision = true;
	bool _allow_move = true;
	bool _allow_fire = true;
	Vector2i _vision_size;

	// Last published view, attached to captured frames.
	float _yaw = 0.0f;
	float _pitch = 0.0f;
	float _fov = 75.0f;
	float _sens = 0.07f;

	std::atomic<uint64_t> _next_frame_id{ 1 };
	std::mutex _pending_mutex;
	std::deque<PendingCapture> _pending; // RD async requests in flight, FIFO

	std::thread _worker;
	std::mutex _job_mutex;
	std::condition_variable _job_cv;
	bool _job_ready = false;
	bool _worker_quit = false;
	Job _job;
	std::vector<uint8_t> _scratch;

	// frame_id -> capture_usec for the last published frames (latency stats).
	static constexpr uint32_t HISTORY = 256;
	std::mutex _history_mutex;
	uint64_t _history_id[HISTORY] = {};
	uint64_t _history_usec[HISTORY] = {};

	std::atomic<uint32_t> _frames_published{ 0 };
	std::atomic<uint32_t> _frames_dropped{ 0 };
	std::atomic<double> _last_publish_ms{ 0.0 };
	std::atomic<double> _readback_ms_avg{ 0.0 }; // capture → data back on the CPU (async GPU readback)
	std::atomic<double> _publish_total_ms_avg{ 0.0 }; // capture → frame visible in shared memory
	uint32_t _cmds_applied = 0;
	uint32_t _cmds_dropped = 0;
	uint64_t _last_latency_usec = 0;
	double _avg_latency_usec = 0.0;
	uint64_t _last_heartbeat = 0;
	uint64_t _last_heartbeat_change_usec = 0;
	bool _client_connected = false;
	std::atomic<bool> _info_state{ true };
	std::atomic<bool> _info_meta{ true };
	uint64_t _tick = 0;
	double _fps_accum = 0.0;
	uint32_t _fps_frames = 0;
	uint32_t _fps_published_at = 0;
	float _publish_fps = 0.0f;
};

} // namespace godot
