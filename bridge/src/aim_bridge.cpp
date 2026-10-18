#include "aim_bridge.h"

#include <godot_cpp/classes/display_server.hpp>
#include <godot_cpp/classes/engine.hpp>
#include <godot_cpp/classes/image.hpp>
#include <godot_cpp/classes/os.hpp>
#include <godot_cpp/classes/rd_texture_format.hpp>
#include <godot_cpp/classes/rendering_device.hpp>
#include <godot_cpp/classes/rendering_server.hpp>
#include <godot_cpp/classes/time.hpp>
#include <godot_cpp/classes/viewport_texture.hpp>
#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/variant/callable_method_pointer.hpp>
#include <godot_cpp/variant/utility_functions.hpp>

#include <algorithm>
#include <chrono>
#include <cstring>

using namespace godot;
using namespace aimbridge;

namespace {

template <typename T>
inline void atomic_store(T *p_ptr, T p_value) {
	reinterpret_cast<std::atomic<T> *>(p_ptr)->store(p_value, std::memory_order_release);
}

template <typename T>
inline T atomic_load(const T *p_ptr) {
	return reinterpret_cast<const std::atomic<T> *>(p_ptr)->load(std::memory_order_acquire);
}

static_assert(std::atomic<uint64_t>::is_always_lock_free, "64-bit atomics must be lock-free for cross-process use");

inline uint64_t now_usec() {
	return Time::get_singleton()->get_ticks_usec();
}

inline size_t align_up(size_t p_value, size_t p_align) {
	return (p_value + p_align - 1) / p_align * p_align;
}

constexpr uint64_t CLIENT_TIMEOUT_USEC = 2000000;

// Area-average downscale of a tightly packed RGBA8 image.
void downscale_rgba8(const uint8_t *p_src, uint32_t p_sw, uint32_t p_sh, uint8_t *p_dst, uint32_t p_dw, uint32_t p_dh) {
	for (uint32_t oy = 0; oy < p_dh; oy++) {
		uint32_t sy0 = (uint32_t)((uint64_t)oy * p_sh / p_dh);
		uint32_t sy1 = std::max(sy0 + 1, (uint32_t)((uint64_t)(oy + 1) * p_sh / p_dh));
		uint8_t *dst_row = p_dst + (size_t)oy * p_dw * 4;
		for (uint32_t ox = 0; ox < p_dw; ox++) {
			uint32_t sx0 = (uint32_t)((uint64_t)ox * p_sw / p_dw);
			uint32_t sx1 = std::max(sx0 + 1, (uint32_t)((uint64_t)(ox + 1) * p_sw / p_dw));
			uint32_t acc[4] = { 0, 0, 0, 0 };
			for (uint32_t sy = sy0; sy < sy1; sy++) {
				const uint8_t *src = p_src + ((size_t)sy * p_sw + sx0) * 4;
				for (uint32_t sx = sx0; sx < sx1; sx++, src += 4) {
					acc[0] += src[0];
					acc[1] += src[1];
					acc[2] += src[2];
					acc[3] += src[3];
				}
			}
			uint32_t n = (sy1 - sy0) * (sx1 - sx0);
			dst_row[ox * 4 + 0] = (uint8_t)(acc[0] / n);
			dst_row[ox * 4 + 1] = (uint8_t)(acc[1] / n);
			dst_row[ox * 4 + 2] = (uint8_t)(acc[2] / n);
			dst_row[ox * 4 + 3] = (uint8_t)(acc[3] / n);
		}
	}
}

} // namespace

AimBridge::AimBridge() {
}

AimBridge::~AimBridge() {
	stop();
}

void AimBridge::_bind_methods() {
	ClassDB::bind_method(D_METHOD("start", "viewport"), &AimBridge::start);
	ClassDB::bind_method(D_METHOD("stop"), &AimBridge::stop);
	ClassDB::bind_method(D_METHOD("is_running"), &AimBridge::is_running);
	ClassDB::bind_method(D_METHOD("set_shm_name", "name"), &AimBridge::set_shm_name);
	ClassDB::bind_method(D_METHOD("get_shm_name"), &AimBridge::get_shm_name);
	ClassDB::bind_method(D_METHOD("set_mode", "vision", "allow_move", "allow_fire", "vision_size"), &AimBridge::set_mode, DEFVAL(Vector2i()));
	ClassDB::bind_method(D_METHOD("get_mode"), &AimBridge::get_mode);
	ClassDB::bind_method(D_METHOD("publish_state", "state"), &AimBridge::publish_state);
	ClassDB::bind_method(D_METHOD("is_client_connected"), &AimBridge::is_client_connected);
	ClassDB::bind_method(D_METHOD("get_stats"), &AimBridge::get_stats);

	ADD_PROPERTY(PropertyInfo(Variant::STRING, "shm_name"), "set_shm_name", "get_shm_name");

	ADD_SIGNAL(MethodInfo("assist_move", PropertyInfo(Variant::VECTOR2, "delta_px")));
	ADD_SIGNAL(MethodInfo("assist_fire"));
	ADD_SIGNAL(MethodInfo("client_connected_changed", PropertyInfo(Variant::BOOL, "connected")));
}

bool AimBridge::start(Viewport *p_viewport) {
	stop();
	ERR_FAIL_NULL_V(p_viewport, false);
	_viewport = p_viewport;

	Vector2i vp_size = _viewport->get_visible_rect().size;
	Vector2i screen = DisplayServer::get_singleton()->screen_get_size();
	uint32_t max_w = (uint32_t)std::max({ vp_size.x, screen.x, 1280 });
	uint32_t max_h = (uint32_t)std::max({ vp_size.y, screen.y, 720 });

	size_t state_offset = sizeof(Header);
	size_t commands_offset = state_offset + sizeof(State);
	size_t commands_size = sizeof(CommandRing) + (size_t)DEFAULT_COMMAND_CAPACITY * sizeof(Command);
	size_t frames_offset = align_up(commands_offset + commands_size, 4096);
	size_t slot_size = align_up(sizeof(FrameHeader) + (size_t)max_w * max_h * 4, 64);
	size_t total = frames_offset + (size_t)DEFAULT_FRAME_SLOTS * slot_size;

	std::string error;
	if (!_shm.create(_shm_name.utf8().get_data(), total, error)) {
		UtilityFunctions::push_error("AimBridge: cannot create shared memory '", _shm_name, "': ", error.c_str());
		return false;
	}

	Header *h = _header();
	h->version = VERSION;
	h->header_size = sizeof(Header);
	h->pixel_format = PIXEL_FORMAT_RGBA8;
	h->total_size = total;
	h->frames_offset = frames_offset;
	h->frame_slots = DEFAULT_FRAME_SLOTS;
	h->frame_slot_size = (uint32_t)slot_size;
	h->max_width = max_w;
	h->max_height = max_h;
	h->state_offset = state_offset;
	h->commands_offset = commands_offset;
	h->command_capacity = DEFAULT_COMMAND_CAPACITY;
	h->command_size = sizeof(Command);
	h->game_pid = (uint64_t)OS::get_singleton()->get_process_id();
	h->game_heartbeat_usec = now_usec();
	_write_mode_flags();
	atomic_store<uint32_t>(&h->magic, MAGIC); // last: clients wait for the magic

	RenderingServer *rs = RenderingServer::get_singleton();
	_use_rd = rs->get_rendering_device() != nullptr;
	_rd_format_warned = false;
	rs->connect("frame_post_draw", callable_mp(this, &AimBridge::_on_frame_post_draw));

	_worker_quit = false;
	_job_ready = false;
	_worker = std::thread(&AimBridge::_worker_main, this);

	_running = true;
	_last_heartbeat = 0;
	_last_heartbeat_change_usec = 0;
	_client_connected = false;
	UtilityFunctions::print("AimBridge: shared memory '", _shm_name, "' (", (int64_t)(total / (1024 * 1024)), " MiB, ", (int64_t)max_w, "x", (int64_t)max_h, " max), capture=", _use_rd ? "rd_async" : "sync");
	return true;
}

void AimBridge::stop() {
	if (!_running) {
		return;
	}
	_running = false;
	RenderingServer *rs = RenderingServer::get_singleton();
	Callable cb = callable_mp(this, &AimBridge::_on_frame_post_draw);
	if (rs->is_connected("frame_post_draw", cb)) {
		rs->disconnect("frame_post_draw", cb);
	}
	{
		std::lock_guard<std::mutex> lock(_job_mutex);
		_worker_quit = true;
	}
	_job_cv.notify_all();
	if (_worker.joinable()) {
		_worker.join();
	}
	{
		std::lock_guard<std::mutex> lock(_pending_mutex);
		_pending.clear();
	}
	if (_client_connected) {
		_client_connected = false;
		emit_signal("client_connected_changed", false);
	}
	_shm.close();
	_viewport = nullptr;
}

void AimBridge::_exit_tree() {
	stop();
}

void AimBridge::set_mode(bool p_vision, bool p_allow_move, bool p_allow_fire, const Vector2i &p_vision_size) {
	_vision = p_vision;
	_allow_move = p_allow_move;
	_allow_fire = p_allow_fire;
	_vision_size = Vector2i(std::max(p_vision_size.x, 0), std::max(p_vision_size.y, 0));
	if (_shm.is_open()) {
		_write_mode_flags();
	}
}

void AimBridge::_write_mode_flags() {
	Header *h = _header();
	h->vision_width = (uint32_t)_vision_size.x;
	h->vision_height = (uint32_t)_vision_size.y;
	uint32_t flags = (_vision ? MODE_VISION : 0) | (_allow_move ? MODE_MOVE : 0) | (_allow_fire ? MODE_FIRE : 0);
	atomic_store<uint32_t>(&h->mode_flags, flags);
}

Dictionary AimBridge::get_mode() const {
	Dictionary d;
	d["vision"] = _vision;
	d["allow_move"] = _allow_move;
	d["allow_fire"] = _allow_fire;
	d["vision_size"] = _vision_size;
	return d;
}

// ---------------------------------------------------------------- capture

void AimBridge::_on_frame_post_draw() {
	if (!_running || !_vision || !_viewport) {
		return;
	}
	if (_use_rd) {
		RenderingServer::get_singleton()->call_on_render_thread(callable_mp(this, &AimBridge::_request_readback_rd));
	} else {
		_capture_sync();
	}
}

void AimBridge::_request_readback_rd() {
	if (!_running || !_viewport) {
		return;
	}
	RenderingServer *rs = RenderingServer::get_singleton();
	RenderingDevice *rd = rs->get_rendering_device();
	RID tex = rs->viewport_get_texture(_viewport->get_viewport_rid());
	RID rd_tex = rs->texture_get_rd_texture(tex, false);
	if (!rd_tex.is_valid()) {
		return;
	}
	Ref<RDTextureFormat> fmt = rd->texture_get_format(rd_tex);
	if (fmt.is_null()) {
		return;
	}
	RenderingDevice::DataFormat f = fmt->get_format();
	if (f != RenderingDevice::DATA_FORMAT_R8G8B8A8_UNORM && f != RenderingDevice::DATA_FORMAT_R8G8B8A8_SRGB && f != RenderingDevice::DATA_FORMAT_R8G8B8A8_UINT) {
		if (!_rd_format_warned) {
			_rd_format_warned = true;
			UtilityFunctions::push_warning("AimBridge: viewport texture format ", (int64_t)f, " is not RGBA8 (HDR 2D enabled?), falling back to synchronous capture");
		}
		_use_rd = false;
		return;
	}

	PendingCapture meta;
	meta.frame_id = _next_frame_id.fetch_add(1);
	meta.capture_usec = now_usec();
	meta.render_frame = (uint64_t)Engine::get_singleton()->get_frames_drawn();
	meta.width = (uint32_t)fmt->get_width();
	meta.height = (uint32_t)fmt->get_height();
	meta.yaw = _yaw;
	meta.pitch = _pitch;
	meta.fov = _fov;
	{
		std::lock_guard<std::mutex> lock(_pending_mutex);
		if (_pending.size() > 8) {
			_pending.pop_front(); // a callback never came back; keep the FIFO bounded
		}
		_pending.push_back(meta);
	}
	Error err = rd->texture_get_data_async(rd_tex, 0, callable_mp(this, &AimBridge::_on_readback));
	if (err != OK) {
		std::lock_guard<std::mutex> lock(_pending_mutex);
		if (!_pending.empty()) {
			_pending.pop_back();
		}
	}
}

void AimBridge::_on_readback(const PackedByteArray &p_data) {
	if (!_running) {
		return;
	}
	PendingCapture meta;
	{
		std::lock_guard<std::mutex> lock(_pending_mutex);
		if (_pending.empty()) {
			return;
		}
		meta = _pending.front();
		_pending.pop_front();
	}
	if ((size_t)p_data.size() != (size_t)meta.width * meta.height * 4) {
		_frames_dropped.fetch_add(1);
		return;
	}
	_enqueue(meta, p_data);
}

void AimBridge::_capture_sync() {
	Ref<ViewportTexture> vt = _viewport->get_texture();
	if (vt.is_null()) {
		return;
	}
	PendingCapture meta;
	meta.frame_id = _next_frame_id.fetch_add(1);
	meta.capture_usec = now_usec();
	meta.render_frame = (uint64_t)Engine::get_singleton()->get_frames_drawn();
	meta.yaw = _yaw;
	meta.pitch = _pitch;
	meta.fov = _fov;
	Ref<Image> img = vt->get_image();
	if (img.is_null() || img->is_empty()) {
		return;
	}
	if (img->get_format() != Image::FORMAT_RGBA8) {
		img->convert(Image::FORMAT_RGBA8);
	}
	meta.width = (uint32_t)img->get_width();
	meta.height = (uint32_t)img->get_height();
	_enqueue(meta, img->get_data());
}

void AimBridge::_enqueue(const PendingCapture &p_meta, const PackedByteArray &p_data) {
	{
		std::lock_guard<std::mutex> lock(_job_mutex);
		if (_job_ready) {
			_frames_dropped.fetch_add(1); // worker still busy: latest frame wins
		}
		_job.meta = p_meta;
		_job.data = p_data;
		_job_ready = true;
	}
	_job_cv.notify_one();
}

// ---------------------------------------------------------------- worker

void AimBridge::_worker_main() {
	while (true) {
		Job job;
		{
			std::unique_lock<std::mutex> lock(_job_mutex);
			_job_cv.wait(lock, [this] { return _job_ready || _worker_quit; });
			if (_worker_quit) {
				return;
			}
			job.meta = _job.meta;
			job.data = _job.data;
			_job.data = PackedByteArray();
			_job_ready = false;
		}
		_publish_frame(job);
	}
}

void AimBridge::_publish_frame(const Job &p_job) {
	auto t0 = std::chrono::steady_clock::now();
	Header *h = _header();
	const PendingCapture &m = p_job.meta;

	uint32_t out_w = m.width;
	uint32_t out_h = m.height;
	if (h->vision_width > 0 && h->vision_height > 0) {
		out_w = h->vision_width;
		out_h = h->vision_height;
	}
	if (out_w > h->max_width || out_h > h->max_height) {
		double s = std::min((double)h->max_width / out_w, (double)h->max_height / out_h);
		out_w = std::max(1u, (uint32_t)(out_w * s));
		out_h = std::max(1u, (uint32_t)(out_h * s));
	}

	uint32_t slot_index = (uint32_t)(m.frame_id % h->frame_slots);
	FrameHeader *slot = _slot(slot_index);
	uint8_t *pixels = reinterpret_cast<uint8_t *>(slot) + sizeof(FrameHeader);

	atomic_store<uint32_t>(&slot->seq, slot->seq + 1); // odd: writing
	slot->width = out_w;
	slot->height = out_h;
	slot->stride = out_w * 4;
	slot->frame_id = m.frame_id;
	slot->capture_usec = m.capture_usec;
	slot->yaw_deg = m.yaw;
	slot->pitch_deg = m.pitch;
	slot->fov_deg = m.fov;
	slot->flags = 0;
	slot->render_frame = m.render_frame;
	const uint8_t *src = p_job.data.ptr();
	if (out_w == m.width && out_h == m.height) {
		std::memcpy(pixels, src, (size_t)out_w * out_h * 4);
	} else {
		downscale_rgba8(src, m.width, m.height, pixels, out_w, out_h);
	}
	atomic_store<uint32_t>(&slot->seq, slot->seq + 1); // even: complete

	atomic_store<uint32_t>(&h->latest_slot, slot_index);
	atomic_store<uint64_t>(&h->latest_frame_id, m.frame_id);

	{
		std::lock_guard<std::mutex> lock(_history_mutex);
		uint32_t i = (uint32_t)(m.frame_id % HISTORY);
		_history_id[i] = m.frame_id;
		_history_usec[i] = m.capture_usec;
	}
	_frames_published.fetch_add(1);
	_last_publish_ms.store(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count());
}

// ---------------------------------------------------------------- main thread

void AimBridge::_process(double p_delta) {
	if (!_running) {
		return;
	}
	_tick++;
	uint64_t now = now_usec();
	Header *h = _header();
	atomic_store<uint64_t>(&h->game_heartbeat_usec, now);

	uint64_t hb = atomic_load<uint64_t>(&h->client_heartbeat);
	if (hb != _last_heartbeat) {
		_last_heartbeat = hb;
		_last_heartbeat_change_usec = now;
	}
	bool connected = hb != 0 && (now - _last_heartbeat_change_usec) < CLIENT_TIMEOUT_USEC;
	if (connected != _client_connected) {
		_client_connected = connected;
		emit_signal("client_connected_changed", connected);
	}

	_drain_commands();

	_fps_accum += p_delta;
	_fps_frames++;
	if (_fps_accum >= 0.5) {
		uint32_t published = _frames_published.load();
		_publish_fps = (float)((published - _fps_published_at) / _fps_accum);
		_fps_published_at = published;
		_fps_accum = 0.0;
		_fps_frames = 0;
	}
}

void AimBridge::_drain_commands() {
	Header *h = _header();
	CommandRing *ring = _ring();
	Command *cmds = _commands();
	uint64_t head = atomic_load<uint64_t>(&ring->head);
	uint64_t tail = ring->tail;
	if (head < tail || head - tail > h->command_capacity) {
		tail = head; // client restarted or overran the ring: resynchronise
	}
	uint64_t now = now_usec();
	uint32_t processed = 0;
	while (tail < head && processed < h->command_capacity) {
		Command c = cmds[tail % h->command_capacity];
		tail++;
		processed++;
		bool applied = false;
		if (c.type == CMD_MOVE && _allow_move) {
			emit_signal("assist_move", Vector2(c.dx, c.dy));
			applied = true;
		} else if (c.type == CMD_FIRE && _allow_fire) {
			emit_signal("assist_fire");
			applied = true;
		}
		if (!applied) {
			_cmds_dropped++;
			continue;
		}
		_cmds_applied++;
		if (c.frame_id != 0) {
			std::lock_guard<std::mutex> lock(_history_mutex);
			uint32_t i = (uint32_t)(c.frame_id % HISTORY);
			if (_history_id[i] == c.frame_id && now >= _history_usec[i]) {
				_last_latency_usec = now - _history_usec[i];
				_avg_latency_usec = _avg_latency_usec <= 0.0 ? (double)_last_latency_usec : _avg_latency_usec * 0.9 + (double)_last_latency_usec * 0.1;
			}
		}
	}
	atomic_store<uint64_t>(&ring->tail, tail);
}

void AimBridge::publish_state(const Dictionary &p_state) {
	if (!_running) {
		return;
	}
	Vector3 pos = p_state.get("pos", Vector3());
	Vector3 vel = p_state.get("vel", Vector3());
	Vector2 real_look = p_state.get("real_look", Vector2());
	Vector2 assist_look = p_state.get("assist_look", Vector2());
	_yaw = (float)(double)p_state.get("yaw", 0.0);
	_pitch = (float)(double)p_state.get("pitch", 0.0);
	_fov = (float)(double)p_state.get("fov", 75.0);
	_sens = (float)(double)p_state.get("sens", 0.07);

	State *s = _state();
	atomic_store<uint32_t>(&s->seq, s->seq + 1);
	s->keys = (uint32_t)(int64_t)p_state.get("keys", 0);
	s->tick = _tick;
	s->time_usec = now_usec();
	s->pos[0] = pos.x;
	s->pos[1] = pos.y;
	s->pos[2] = pos.z;
	s->vel[0] = vel.x;
	s->vel[1] = vel.y;
	s->vel[2] = vel.z;
	s->yaw_deg = _yaw;
	s->pitch_deg = _pitch;
	s->fov_deg = _fov;
	s->sens_deg_per_px = _sens;
	s->real_look_dx = real_look.x;
	s->real_look_dy = real_look.y;
	s->assist_look_dx = assist_look.x;
	s->assist_look_dy = assist_look.y;
	s->shots = (uint32_t)(int64_t)p_state.get("shots", 0);
	s->hits = (uint32_t)(int64_t)p_state.get("hits", 0);
	s->headshots = (uint32_t)(int64_t)p_state.get("headshots", 0);
	s->kills = (uint32_t)(int64_t)p_state.get("kills", 0);
	s->cmds_applied = _cmds_applied;
	s->cmds_dropped = _cmds_dropped;
	s->last_latency_usec = _last_latency_usec;
	s->avg_latency_usec = (uint64_t)_avg_latency_usec;
	s->fps = (float)(double)p_state.get("fps", 0.0);
	s->frames_published = _frames_published.load();
	atomic_store<uint32_t>(&s->seq, s->seq + 1);
}

Dictionary AimBridge::get_stats() const {
	Dictionary d;
	d["running"] = _running;
	d["capture_mode"] = !_running ? "off" : (_vision ? (_use_rd ? "rd_async" : "sync") : "disabled");
	d["frames_published"] = (int64_t)_frames_published.load();
	d["frames_dropped"] = (int64_t)_frames_dropped.load();
	d["publish_fps"] = _publish_fps;
	d["last_publish_ms"] = _last_publish_ms.load();
	d["last_latency_ms"] = _last_latency_usec / 1000.0;
	d["avg_latency_ms"] = _avg_latency_usec / 1000.0;
	d["cmds_applied"] = (int64_t)_cmds_applied;
	d["cmds_dropped"] = (int64_t)_cmds_dropped;
	d["client_connected"] = _client_connected;
	d["shm_name"] = _shm_name;
	d["shm_size"] = (int64_t)_shm.size();
	if (_running) {
		d["max_size"] = Vector2i((int)_header()->max_width, (int)_header()->max_height);
	}
	return d;
}
