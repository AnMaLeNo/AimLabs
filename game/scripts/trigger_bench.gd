class_name TriggerBench
extends Node
## Reproducible benchmark for "trigger" algorithms (they only fire, never move the mouse):
## the camera is locked on a fixed point and the range target sweeps back and forth across the
## reticle on a short line (± `travel_widths` target widths). At each distance the limit speed
## is found by bisection: start at speed_start, double while a sweep scores a hit, then halve
## the interval between the last hit speed and the first miss until it is `precision` wide;
## `confirm` extra sweeps at the result guard against a lucky hit. Then the target is moved
## `distance_step` further and the search restarts; the run stops when no speed hits at some
## distance or distance_max is reached. Results are printed and saved as JSON.
## Works with an external algo (fire through AimBridge), a human on the mouse, or the
## built-in oracle (fires every frame — latency-free upper bound) for self-tests.

signal finished(result: Dictionary)
signal progress(text: String)

const DEFAULTS := {
	"distance_start": 5.0, "distance_step": 5.0, "distance_max": 40.0,
	"speed_start": 2.0, "speed_min": 0.25, "speed_max": 80.0, "precision": 0.1, "confirm": 2,
	"width": 1.2, "travel_widths": 2.0, "settle": 3.0, "pause": 0.3, "wait_client": false, "oracle": false,
}

var game: Node
var running := false
var config := {}
var result := {}
var out_path := ""

var _world: World
var _player: Player
var _target: Enemy
var _distance := 0.0
var _speed := 0.0
var _half_travel := 0.0
var _dir := 1.0            # +1 left -> right, -1 right -> left (sweeps alternate)
var _lo := 0.0             # highest speed that scored a hit at this distance (0 = none yet)
var _hi := 0.0             # lowest speed that missed (0 = none yet)
var _confirm_left := 0
var _confirm_hits := 0
var _dist_entry: Dictionary = {}
var _phase := ""           # "settle" | "pause" | "cross" | "done"
var _timer := 0.0
var _x := 0.0
var _pass_stats := {}
var _saved_respawn_delay := 3.0
var _last_text := ""


func setup(p_game: Node) -> void:
	game = p_game
	_world = game.world
	_player = game.player
	_player.target_hit.connect(_on_target_hit)
	_player.shot_fired.connect(_on_shot_fired)


## Start with `cfg` (keys of DEFAULTS; unknown keys ignored). Requires the range map.
func start(cfg: Dictionary = {}) -> bool:
	if running:
		return false
	if _world.map != "range" or _world.target == null:
		push_warning("TriggerBench: switch to the range map first")
		return false
	config = DEFAULTS.duplicate()
	for k in cfg:
		if config.has(k):
			config[k] = cfg[k]
	_target = _world.target
	_saved_respawn_delay = _target.respawn_delay
	_target.respawn_delay = 0.05
	if _world.mover:
		_world.mover.set_physics_process(false)
	result = {
		"algo": game.runner.current_name() if game.runner and game.runner.is_running() else ("oracle" if config["oracle"] else "human"),
		"weapon": game.weapon_slug, "agent": game.agent_slug, "target": _world.target_slug,
		"config": config.duplicate(), "started": Time.get_datetime_string_from_system(),
		"fps": 0.0, "distances": [],
	}
	_distance = float(config["distance_start"])
	_speed = float(config["speed_start"])
	_begin_distance()
	_phase = "settle"
	_timer = float(config["settle"])
	running = true
	_emit("benchmark : préparation (%.0f s)…" % _timer)
	return true


func stop() -> void:
	if not running:
		return
	running = false
	_phase = "done"
	_target.respawn_delay = _saved_respawn_delay
	if _world.mover:
		_world.mover.set_physics_process(true)
		_world.mover.recentre(Vector3(0.0, 0.0, -_world.range_distance))
	_target.position = Vector3(0.0, 0.0, -_world.range_distance)


func status_text() -> String:
	return _last_text


func _emit(text: String) -> void:
	_last_text = text
	progress.emit(text)


func _begin_distance() -> void:
	_dist_entry = { "distance": _distance, "max_speed_hit": 0.0, "sweeps": [], "confirm_hits": 0 }
	_speed = float(config["speed_start"])
	_lo = 0.0
	_hi = 0.0
	_confirm_left = 0
	_confirm_hits = 0
	_half_travel = float(config["width"]) * float(config["travel_widths"])
	_dir = 1.0


func _begin_pass() -> void:
	_x = -_half_travel * _dir
	_target.position = Vector3(_x, 0.0, -_distance)
	_face_player()
	if not _target.alive:
		_target.respawn()
	_pass_stats = { "speed": _speed, "hits": 0, "shots": 0, "headshots": 0, "killed": false,
		"confirm": _confirm_left > 0 }
	_phase = "cross"
	var what := "confirmation %d/%d" % [int(config["confirm"]) - _confirm_left + 1, int(config["confirm"])] if _confirm_left > 0 else (
		"touché jusqu'à %.2f, raté dès %.2f" % [_lo, _hi] if _hi > 0.0 else "recherche de la limite")
	_emit("benchmark : %.0f m, %.2f m/s — %s" % [_distance, _speed, what])


func _end_pass() -> void:
	_dist_entry["sweeps"].append(_pass_stats)
	var hit: bool = _pass_stats["hits"] > 0
	print("bench: %.0f m  %.2f m/s  %s  tirs %d  touches %d%s" % [
		_distance, _speed, "TOUCHÉ" if hit else "raté", _pass_stats["shots"], _pass_stats["hits"],
		"  (confirmation)" if _confirm_left > 0 else ""])
	_dir = -_dir
	if _confirm_left > 0:
		_confirm_left -= 1
		_confirm_hits += int(hit)
		if _confirm_left == 0:
			_dist_entry["confirm_hits"] = _confirm_hits
			if _confirm_hits * 2 >= int(config["confirm"]) or _lo <= float(config["speed_min"]):
				_end_distance()
				return
			# lucky hit: the limit is lower, keep bisecting below
			_hi = _lo
			_lo = 0.0
			for sw in _dist_entry["sweeps"]:
				if sw["hits"] > 0 and float(sw["speed"]) < _hi and float(sw["speed"]) > _lo:
					_lo = float(sw["speed"])
			_speed = (_lo + _hi) * 0.5 if _lo > 0.0 else _hi * 0.5
			_confirm_hits = 0
		_next_sweep()
		return
	if hit:
		_lo = _speed
	else:
		_hi = _speed
	if _hi == 0.0:
		# still climbing
		if _speed * 2.0 > float(config["speed_max"]):
			_start_confirm()
			return
		_speed *= 2.0
	elif _lo == 0.0:
		# nothing hit yet: come down
		if _speed * 0.5 < float(config["speed_min"]):
			_end_distance()  # even the slowest speed misses: range limit
			return
		_speed *= 0.5
	elif (_hi - _lo) <= float(config["precision"]) * _hi:
		_start_confirm()
		return
	else:
		_speed = (_lo + _hi) * 0.5
	_next_sweep()


func _start_confirm() -> void:
	_speed = _lo
	_confirm_left = int(config["confirm"])
	_confirm_hits = 0
	if _confirm_left == 0:
		_end_distance()
	else:
		_next_sweep()


func _next_sweep() -> void:
	_phase = "pause"
	_timer = float(config["pause"])


func _end_distance() -> void:
	_dist_entry["max_speed_hit"] = _lo
	result["distances"].append(_dist_entry)
	print("bench: distance %.0f m → vitesse limite %.2f m/s (%d traversées)" % [_distance, _lo, _dist_entry["sweeps"].size()])
	var next_distance := _distance + float(config["distance_step"])
	if _lo == 0.0 or next_distance > float(config["distance_max"]):
		_finish()
		return
	_distance = next_distance
	_begin_distance()
	_next_sweep()


func _finish() -> void:
	result["fps"] = Engine.get_frames_per_second()
	result["finished"] = Time.get_datetime_string_from_system()
	var summary := PackedStringArray()
	for d in result["distances"]:
		summary.append("%.0f m : %.2f m/s" % [d["distance"], d["max_speed_hit"]])
	result["summary"] = ", ".join(summary)
	stop()
	if out_path != "":
		save_json(out_path)
	_emit("benchmark terminé — " + result["summary"])
	print("BENCH DONE ", result["summary"])
	finished.emit(result)


func save_json(path: String) -> bool:
	var dir := path.get_base_dir()
	if dir != "":
		DirAccess.make_dir_recursive_absolute(dir)
	var fa := FileAccess.open(path, FileAccess.WRITE)
	if fa == null:
		push_warning("TriggerBench: cannot write %s" % path)
		return false
	fa.store_string(JSON.stringify(result, "  "))
	print("bench: résultats → ", path)
	return true


func _face_player() -> void:
	var to_player := -_target.position
	to_player.y = 0.0
	if to_player.length_squared() > 1e-6:
		_target.rotation.y = atan2(to_player.x, to_player.z)


func _lock_camera() -> void:
	# Reticle on the chest height of the target at the current distance, dead ahead.
	var chest_y := _target.body_position().y - _target.global_position.y
	_player.look_at_point(_player.camera.global_position + Vector3(0.0, chest_y - _player.camera.global_position.y, -_distance))


func _physics_process(delta: float) -> void:
	if not running:
		return
	_lock_camera()
	match _phase:
		"settle":
			if bool(config["wait_client"]) and not (game.bridge and game.bridge.get_stats()["client_connected"]):
				_emit("benchmark : en attente de l'algo (client AimBridge)…")
				return
			_timer -= delta
			if _timer <= 0.0:
				_begin_pass()
		"pause":
			_target.position = Vector3(-_half_travel * _dir, 0.0, -_distance)
			_face_player()
			_timer -= delta
			if _timer <= 0.0:
				_begin_pass()
		"cross":
			_x += _speed * delta * _dir
			_target.position = Vector3(_x, 0.0, -_distance)
			_face_player()
			if bool(config["oracle"]):
				_player.request_fire_from_assist()
			if absf(_x) > _half_travel:
				_end_pass()


func _on_target_hit(enemy: Node, zone: String, _damage: int, killed: bool) -> void:
	if running and _phase == "cross" and enemy == _target:
		_pass_stats["hits"] += 1
		if zone == "head":
			_pass_stats["headshots"] += 1
		if killed:
			_pass_stats["killed"] = true


func _on_shot_fired(_origin: Vector3, _hit: Vector3, _hit_something: bool) -> void:
	if running and _phase == "cross":
		_pass_stats["shots"] += 1
