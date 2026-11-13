extends Node
## Behaviour recorder: everything the GAME itself can observe about how the player plays, kept for the game's own judgement
## (the « human score », see tools/behavior/). Nothing is given to the player: the records stay in memory and are written to
## disk only when the game is paused (Esc menu) or closed, so a program running next to the game cannot read them live.
##
##   --behavior=on|off|<file>   default: on with --mode=silent, off otherwise. on = <repo>/behavior_logs/<mode>_<date>.aimbeh
##   --behavior-selftest        scripted inputs (range map), then quit: used by tools/behavior/test_behavior.py
##   --behavior-selftest=arena:<reaction ms>:<seconds>   arena map, a scripted player that fires <reaction ms> after it can see the chest
##
## What is recorded (clock = Time.get_ticks_usec(), read when the game RECEIVES the event: its resolution is one frame):
##   M  real mouse motion (pixels, as the game received it: one event per frame, the OS reports of that frame added up)
##   B  fire button / fire key pressed or released
##   F  one per rendered frame: view angles, number of visible targets, the target nearest to the reticle (angular offset of
##      its centre, angular half-size, distance), what is under the reticle (target, zone), look input of an assist program,
##      how much of that nearest target can be seen (0–255)
##   V  a target becomes visible / stops being visible. Visible = alive, shown, on screen and, when the map has walls, at
##      least one of its test points in line of sight (Enemy.visibility_points: the hitbox capsules, out to their surface)
##   S  a shot left the weapon (hit or not, target, zone, kill)
##   P  playing (mouse captured) / paused
##   K  mark of a scripted bench case (BenchCases): case, trial, phase (0 set up, 1 stimulus, 2 end), subject target, a parameter
## A shot asked by an assist program through the bridge (--mode=remote…) is recorded as a B press + release with device 2.
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
const REC_MARK := 7
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
var _visible_since := {}          # target instance id -> clock (µs) when it last became visible
var on_target: Enemy = null       # what is under the reticle right now (read by the bench cases / the simulated player)
var on_since_usec := 0
var _pending_hit := {}            # filled by target_hit, consumed by shot_fired (emitted right after, same call)
var _records := 0
var _self_frame := 0
var _arena_react_ms := -1.0       # selftest on the arena: reaction time of the scripted player (< 0: range selftest)
var _arena_seconds := 20.0
var _arena_seen_usec := 0
var _arena_fired := false
var _arena_release_frame := -1
var _arena_t0 := 0


func setup(p_game: Node, p_path: String, p_selftest: bool, p_selftest_spec := "") -> void:
	game = p_game
	path = p_path
	selftest = p_selftest
	_buf.big_endian = false
	process_priority = -50        # after the bridge (-100: assist look input of this frame is in), before the player (0: it resets it)
	var player: Player = game.player
	player.shot_fired.connect(_on_shot)
	player.target_hit.connect(_on_hit)
	player.mouse_capture_changed.connect(_on_capture)
	if game.bridge:
		game.bridge.assist_fire.connect(record_assist_fire)
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	print("BehaviorLog: recording to ", path)
	_put_playing(player.mouse_captured)
	if selftest:
		var parts := p_selftest_spec.split(":")
		if parts[0] == "arena":
			_arena_react_ms = float(parts[1]) if parts.size() > 1 else 0.0
			_arena_seconds = float(parts[2]) if parts.size() > 2 else 20.0
			_arena_t0 = Time.get_ticks_usec()
			get_window().size = Vector2i(1280, 720)      # without a display the window is 64×64: give the scripted player a real field of view
			game.world.set_map("arena")
		else:
			game.world.set_map("range")
			game.world.set_pattern("none")
		player.set_mouse_captured(true)


func _header() -> Dictionary:
	var player: Player = game.player
	var world: World = game.world
	var vp := get_viewport().get_visible_rect().size
	return {
		"version": 2, "started_unix": Time.get_unix_time_from_system(), "t0_usec": Time.get_ticks_usec(),
		"mode": game.mode_name, "info": game.info_profile, "map": world.map, "weapon": game.weapon_slug, "agent": game.agent_slug,
		"weapon_automatic": player.viewmodel.automatic, "fire_interval_s": player.viewmodel.fire_interval() if player.viewmodel.weapon_slug != "" else 60.0 / player.rounds_per_minute,
		"sens_deg_per_px": player.sensitivity_deg_per_px, "viewport": [vp.x, vp.y], "fire_key": game.fire_key_name(),
		"range": { "pattern": world.pattern, "size": world.pattern_size, "speed": world.pattern_speed, "distance": world.range_distance, "target": world.target_slug },
		"scenario": { "kinds": world.scenario, "seed": world.scenario_seed, "walls": world.has_occluders },
		"max_fps": Engine.max_fps, "vsync": DisplayServer.window_get_vsync_mode(), "accumulated_input": Input.use_accumulated_input,
		"selftest": selftest,
	}


func _input(event: InputEvent) -> void:
	var player: Player = game.player
	if not player.mouse_captured:
		return
	if player.synthetic_only and event.device != Player.SYNTHETIC_DEVICE:
		return                        # simulated player: the real mouse is not part of the game
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
		if _arena_react_ms >= 0.0:
			_selftest_arena()
		else:
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
	var near_seen := 0                            # 0–255: share of the nearest target's test points in line of sight
	var walls: bool = game.world.has_occluders
	var enemies: Array = game.world.enemies
	for i in enemies.size():
		var e: Enemy = enemies[i]
		var feet: Vector3 = e.global_position
		var top: Vector3 = e.head_position() + Vector3(0.0, 0.12, 0.0)
		var centre := (feet + top) * 0.5
		var vis := e.alive and e.is_visible_in_tree()
		var seen := 255
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
		if vis and walls:                            # line of sight: how many test points have nothing of the WORLD in front of them
			var pts := e.visibility_points(right)
			var clear := 0
			for p: Vector3 in pts:
				if space.intersect_ray(PhysicsRayQueryParameters3D.create(from, p, WORLD_MASK, [player.get_rid()])).is_empty():
					clear += 1
			vis = clear > 0
			seen = clampi(ceili(255.0 * clear / maxi(pts.size(), 1)), 0, 255)
		var key := e.get_instance_id()
		if bool(_visible.get(key, false)) != vis:
			_visible[key] = vis
			if vis:
				_visible_since[key] = now
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
			near_seen = seen
			var dist := from.distance_to(centre)
			near = [ox, oy, rad_to_deg(atan(HALF_WIDTH_M / maxf(dist, 0.01))), rad_to_deg(atan(feet.distance_to(top) * 0.5 / maxf(dist, 0.01))), dist]
	var on_id := -1
	var zone := 0
	var query := PhysicsRayQueryParameters3D.create(from, from - cam.global_transform.basis.z * 500.0, RAY_MASK, [player.get_rid()])
	var hit := space.intersect_ray(query)
	var under: Enemy = null
	if hit and hit.collider is Node and hit.collider.is_in_group("hitbox"):
		under = hit.collider.get_meta("enemy")
		on_id = enemies.find(under)
		zone = int(ZONES.get(String(hit.collider.get_meta("zone")), 1))
	if under != on_target:
		on_target = under
		on_since_usec = now
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
	_buf.put_u8(near_seen)
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


## Is this target visible right now (same test as the V records)? Since when (µs, 0 if it is not)?
func is_visible(e: Enemy) -> bool:
	return bool(_visible.get(e.get_instance_id(), false))


func visible_since(e: Enemy) -> int:
	return int(_visible_since.get(e.get_instance_id(), 0)) if is_visible(e) else 0


## A shot requested by an assist program (bridge) or by the bench's own trigger: a press + release with device 2.
func record_assist_fire() -> void:
	for pressed in [1, 0]:
		_buf.put_u8(REC_BUTTON)
		_buf.put_u64(Time.get_ticks_usec())
		_buf.put_u8(pressed)
		_buf.put_u8(2)
		_records += 1


func mark(case_id: int, trial: int, phase: int, subject: int, param: float) -> void:
	_buf.put_u8(REC_MARK)
	_buf.put_u64(Time.get_ticks_usec())
	_buf.put_u8(case_id)
	_buf.put_u16(trial)
	_buf.put_u8(phase)
	_buf.put_16(subject)
	_buf.put_float(param)
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


## Scripted player on the arena: as soon as the target's chest has been in line of sight for <reaction> ms, aim at it and click
## once (3 frames); again at the next appearance. Quits after <seconds>.
func _selftest_arena() -> void:
	_self_frame += 1
	var player: Player = game.player
	var target: Enemy = game.world.target
	var now := Time.get_ticks_usec()
	if _arena_release_frame == _self_frame:
		_send_button(false)
		player.look_at_point(Vector3(0.0, 1.5, -game.world.range_distance))      # back to the middle of the walls, like a player waiting
	var clear := false
	if target != null and target.alive and target.is_visible_in_tree():
		var q := PhysicsRayQueryParameters3D.create(player.camera.global_position, target.body_position(), WORLD_MASK, [player.get_rid()])
		clear = player.get_world_3d().direct_space_state.intersect_ray(q).is_empty()
		var cam := player.camera                 # …and it has to be on the screen: the scripted player sees no more than a real one
		clear = clear and not cam.is_position_behind(target.body_position()) and get_viewport().get_visible_rect().has_point(cam.unproject_position(target.body_position()))
	if not clear:
		_arena_seen_usec = 0
		_arena_fired = false
	else:
		if _arena_seen_usec == 0:
			_arena_seen_usec = now
		if not _arena_fired and float(now - _arena_seen_usec) / 1000.0 >= _arena_react_ms:
			_arena_fired = true
			player.look_at_point(target.body_position())
			_send_button(true)
			_arena_release_frame = _self_frame + 3
	if float(now - _arena_t0) / 1e6 >= _arena_seconds:
		print("BehaviorLog selftest (arena): %d acts" % game.world.director.acts_done)
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
