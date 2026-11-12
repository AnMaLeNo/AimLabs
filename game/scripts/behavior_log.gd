extends Node
## Behaviour recorder: everything the GAME itself can observe about how the player plays, kept for the game's own judgement
## (the « human score », see tools/behavior/). Nothing is given to the player: the records stay in memory and are written to
## disk only when the game is paused (Esc menu) or closed, so a program running next to the game cannot read them live.
##
##   --behavior=on|off|<file>   default: on with --mode=silent, off otherwise. on = <repo>/behavior_logs/<mode>_<date>.aimbeh
##   --behavior-selftest        scripted inputs (range map), then quit: used by tools/behavior/test_behavior.py
##
## What is recorded (clock = Time.get_ticks_usec(), read when the game RECEIVES the event: its resolution is one frame):
##   M  real mouse motion (pixels, as the game received it: one event per frame, the OS reports of that frame added up)
##   B  fire button / fire key pressed or released
##   F  one per rendered frame: view angles, number of visible targets, the target nearest to the reticle (angular offset of
##      its centre, angular half-size, distance), what is under the reticle (target, zone), look input of an assist program
##   V  a target becomes visible / stops being visible (on screen, alive, line of sight not blocked by the world)
##   S  a shot left the weapon (hit or not, target, zone, kill)
##   P  playing (mouse captured) / paused
##
## File: "AIMBEH1\n", u32 header size, header (JSON, UTF-8), then the records, little-endian, each starting with a type byte
## and a u64 time. Reader: tools/behavior/aimbeh.py.

const MAGIC := "AIMBEH1\n"
const REC_MOTION := 1
const REC_BUTTON := 2
const REC_FRAME := 3
const REC_VISIBLE := 4
const REC_SHOT := 5
const REC_PLAYING := 6
const ZONES := { "": 0, "body": 1, "head": 2, "arm": 3 }
const RAY_MASK := (1 << 2) | (1 << 3) | (1 << 0)
const WORLD_MASK := 1 << 0
const HALF_WIDTH_M := 0.35       # same box as TruthStream: feet ± 0.35 m, head + 0.12 m

var game: Node
var path := ""
var selftest := false

var _buf := StreamPeerBuffer.new()
var _header_written := false
var _visible := {}                # target instance id -> bool
var _pending_hit := {}            # filled by target_hit, consumed by shot_fired (emitted right after, same call)
var _records := 0
var _self_frame := 0


func setup(p_game: Node, p_path: String, p_selftest: bool) -> void:
	game = p_game
	path = p_path
	selftest = p_selftest
	_buf.big_endian = false
	process_priority = -50        # after the bridge (-100: assist look input of this frame is in), before the player (0: it resets it)
	var player: Player = game.player
	player.shot_fired.connect(_on_shot)
	player.target_hit.connect(_on_hit)
	player.mouse_capture_changed.connect(_on_capture)
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	print("BehaviorLog: recording to ", path)
	_put_playing(player.mouse_captured)
	if selftest:
		game.world.set_map("range")
		game.world.set_pattern("none")
		player.set_mouse_captured(true)


func _header() -> Dictionary:
	var player: Player = game.player
	var world: World = game.world
	var vp := get_viewport().get_visible_rect().size
	return {
		"version": 1, "started_unix": Time.get_unix_time_from_system(), "t0_usec": Time.get_ticks_usec(),
		"mode": game.mode_name, "info": game.info_profile, "map": world.map, "weapon": game.weapon_slug, "agent": game.agent_slug,
		"weapon_automatic": player.viewmodel.automatic, "fire_interval_s": player.viewmodel.fire_interval() if player.viewmodel.weapon_slug != "" else 60.0 / player.rounds_per_minute,
		"sens_deg_per_px": player.sensitivity_deg_per_px, "viewport": [vp.x, vp.y], "fire_key": game.fire_key_name(),
		"range": { "pattern": world.pattern, "size": world.pattern_size, "speed": world.pattern_speed, "distance": world.range_distance, "target": world.target_slug },
		"max_fps": Engine.max_fps, "vsync": DisplayServer.window_get_vsync_mode(), "accumulated_input": Input.use_accumulated_input,
		"selftest": selftest,
	}


func _input(event: InputEvent) -> void:
	var player: Player = game.player
	if not player.mouse_captured:
		return
	if event is InputEventMouseMotion:
		_buf.put_u8(REC_MOTION)
		_buf.put_u64(Time.get_ticks_usec())
		_buf.put_float(event.relative.x)
		_buf.put_float(event.relative.y)
		_records += 1
	elif event.is_action("fire") and not event.is_echo():
		_buf.put_u8(REC_BUTTON)
		_buf.put_u64(Time.get_ticks_usec())
		_buf.put_u8(1 if event.is_pressed() else 0)
		_buf.put_u8(1 if event is InputEventKey else 0)
		_records += 1


func _process(_delta: float) -> void:
	if selftest:
		_selftest_step()
	var player: Player = game.player
	if not player.mouse_captured:
		return
	var cam: Camera3D = player.camera
	var now := Time.get_ticks_usec()
	var inv := cam.global_transform.affine_inverse()
	var screen := Rect2(Vector2.ZERO, get_viewport().get_visible_rect().size)
	var space := player.get_world_3d().direct_space_state
	var from := cam.global_position
	var right: Vector3 = cam.global_transform.basis.x
	var n_visible := 0
	var near_id := -1
	var near_d := INF
	var near := [0.0, 0.0, 0.0, 0.0, 0.0]         # off_x, off_y, half_w, half_h (degrees), distance (m)
	var enemies: Array = game.world.enemies
	for i in enemies.size():
		var e: Enemy = enemies[i]
		var feet: Vector3 = e.global_position
		var top: Vector3 = e.head_position() + Vector3(0.0, 0.12, 0.0)
		var centre := (feet + top) * 0.5
		var vis := e.alive
		if vis:
			var mn := Vector2(INF, INF)
			var mx := Vector2(-INF, -INF)
			for p: Vector3 in [feet + right * HALF_WIDTH_M, feet - right * HALF_WIDTH_M, top + right * 0.18, top - right * 0.18]:
				if cam.is_position_behind(p):
					vis = false
					break
				var s := cam.unproject_position(p)
				mn = mn.min(s)
				mx = mx.max(s)
			vis = vis and screen.intersects(Rect2(mn, mx - mn))
		if vis:                                      # line of sight: nothing of the WORLD between the camera and the chest
			var q := PhysicsRayQueryParameters3D.create(from, e.body_position(), WORLD_MASK, [player.get_rid()])
			vis = space.intersect_ray(q).is_empty()
		var key := e.get_instance_id()
		if bool(_visible.get(key, false)) != vis:
			_visible[key] = vis
			_buf.put_u8(REC_VISIBLE)
			_buf.put_u64(now)
			_buf.put_16(i)
			_buf.put_u8(1 if vis else 0)
			_records += 1
		if not vis:
			continue
		n_visible += 1
		var l := inv * centre
		var ox := rad_to_deg(atan2(l.x, -l.z))
		var oy := rad_to_deg(atan2(l.y, -l.z))
		var d := ox * ox + oy * oy
		if d < near_d:
			near_d = d
			near_id = i
			var dist := from.distance_to(centre)
			near = [ox, oy, rad_to_deg(atan(HALF_WIDTH_M / maxf(dist, 0.01))), rad_to_deg(atan(feet.distance_to(top) * 0.5 / maxf(dist, 0.01))), dist]
	var on_id := -1
	var zone := 0
	var query := PhysicsRayQueryParameters3D.create(from, from - cam.global_transform.basis.z * 500.0, RAY_MASK, [player.get_rid()])
	var hit := space.intersect_ray(query)
	if hit and hit.collider is Node and hit.collider.is_in_group("hitbox"):
		on_id = enemies.find(hit.collider.get_meta("enemy"))
		zone = int(ZONES.get(String(hit.collider.get_meta("zone")), 1))
	var assist: Vector2 = player.look_delta_this_frame[Player.LookSource.ASSIST]
	_buf.put_u8(REC_FRAME)
	_buf.put_u64(now)
	_buf.put_float(player.yaw_deg)
	_buf.put_float(player.pitch_deg)
	_buf.put_float(cam.fov)
	_buf.put_u16(n_visible)
	_buf.put_16(near_id)
	for v: float in near:
		_buf.put_float(v)
	_buf.put_16(on_id)
	_buf.put_u8(zone)
	_buf.put_float(assist.x)
	_buf.put_float(assist.y)
	_records += 1


func _on_hit(enemy: Node, zone: String, _damage: int, killed: bool) -> void:
	_pending_hit = { "id": game.world.enemies.find(enemy), "zone": int(ZONES.get(zone, 1)), "killed": killed }


func _on_shot(_origin: Vector3, _hit_point: Vector3, _hit_something: bool) -> void:
	_buf.put_u8(REC_SHOT)
	_buf.put_u64(Time.get_ticks_usec())
	_buf.put_u8(0 if _pending_hit.is_empty() else 1)
	_buf.put_16(int(_pending_hit.get("id", -1)))
	_buf.put_u8(int(_pending_hit.get("zone", 0)))
	_buf.put_u8(1 if bool(_pending_hit.get("killed", false)) else 0)
	_pending_hit = {}
	_records += 1


func _on_capture(captured: bool) -> void:
	_put_playing(captured)
	if not captured:
		flush()                       # paused: the player is not playing, what is written is the past


func _put_playing(captured: bool) -> void:
	_buf.put_u8(REC_PLAYING)
	_buf.put_u64(Time.get_ticks_usec())
	_buf.put_u8(1 if captured else 0)
	_records += 1


## Append what is in memory to the file (header first, once).
func flush() -> void:
	if path == "":
		return
	var f: FileAccess
	if not _header_written:
		f = FileAccess.open(path, FileAccess.WRITE)
		if f == null:
			push_error("BehaviorLog: cannot write %s (%s)" % [path, error_string(FileAccess.get_open_error())])
			return
		var head := JSON.stringify(_header()).to_utf8_buffer()
		f.store_buffer(MAGIC.to_ascii_buffer())
		f.store_32(head.size())
		f.store_buffer(head)
		_header_written = true
	else:
		f = FileAccess.open(path, FileAccess.READ_WRITE)
		if f == null:
			push_error("BehaviorLog: cannot append to %s" % path)
			return
		f.seek_end()
	f.store_buffer(_buf.data_array)
	f.close()
	_buf.clear()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		_final_flush()


func _exit_tree() -> void:            # quit(): children leave the tree before the scene root, so `game` is still there
	_final_flush()


func _final_flush() -> void:
	if game != null and _buf.get_size() > 0:
		flush()
		print("BehaviorLog: %d records written to %s" % [_records, path])


## Scripted session for the automated check: aim next to the target, sweep onto it with « mouse » events, click (80 ms), sweep
## away, click off target (4 frames), quit. Everything goes through the same input path as a real mouse.
func _selftest_step() -> void:
	_self_frame += 1
	var player: Player = game.player
	var target: Enemy = game.world.target
	match _self_frame:
		5:
			player.look_at_point(target.body_position())
			player.add_look_delta(Vector2(-200, 0), Player.LookSource.REAL)      # 14° to the left of the target, not recorded as input
	if _self_frame >= 10 and _self_frame < 30:
		_send_motion(Vector2(10, 0))                                             # 20 events × 10 px = back on the target
	elif _self_frame == 40:
		_send_button(true)
	elif _self_frame == 60:
		_send_button(false)
	elif _self_frame >= 70 and _self_frame < 90:
		_send_motion(Vector2(-15, 0))
	elif _self_frame == 100:
		_send_button(true)
	elif _self_frame == 104:
		_send_button(false)
	elif _self_frame == 120:
		player.set_mouse_captured(false)                                         # pause: flush
	elif _self_frame == 122:
		player.set_mouse_captured(true)                                          # what follows is only written by the flush at exit
	elif _self_frame == 125:
		get_tree().quit()


func _send_motion(rel: Vector2) -> void:
	var ev := InputEventMouseMotion.new()
	ev.relative = rel
	Input.parse_input_event(ev)


func _send_button(pressed: bool) -> void:
	var ev := InputEventMouseButton.new()
	ev.button_index = MOUSE_BUTTON_LEFT
	ev.pressed = pressed
	Input.parse_input_event(ev)
