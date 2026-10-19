class_name TriggerBench
extends Node
## Reproducible benchmark for "trigger" algorithms (they only fire, never move the mouse):
## the camera is locked on a fixed point, the range target crosses the reticle from left to
## right. At each distance the speed climbs level by level (× speed_factor); a level has
## `passes` crossings and fails when none of them scores a hit, then the target is moved
## further away and the speed starts again from speed_start. Stops when the slowest speed
## fails at some distance or distance_max is reached. Results are printed and saved as JSON.
## Works with an external algo (fire through AimBridge), a human on the mouse, or the
## built-in oracle (fires every frame — latency-free upper bound) for self-tests.

signal finished(result: Dictionary)
signal progress(text: String)

const DEFAULTS := {
	"distance_start": 5.0, "distance_step": 5.0, "distance_max": 40.0,
	"speed_start": 1.0, "speed_factor": 1.25, "speed_max": 40.0,
	"passes": 4, "settle": 3.0, "pause": 0.5, "wait_client": false, "oracle": false,
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
var _pass_index := 0
var _level: Dictionary = {}
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
	_dist_entry = { "distance": _distance, "max_speed_hit": 0.0, "levels": [] }
	_speed = float(config["speed_start"])
	_begin_level()


func _begin_level() -> void:
	_level = { "speed": _speed, "passes": [], "hit_passes": 0 }
	_pass_index = 0


func _begin_pass() -> void:
	var cam := _player.camera
	var aspect := cam.get_viewport().get_visible_rect().size.aspect()
	var half_w := _distance * tan(deg_to_rad(cam.fov) * 0.5) * aspect
	_half_travel = half_w + 1.5
	_x = -_half_travel
	_target.position = Vector3(_x, 0.0, -_distance)
	_face_player()
	if not _target.alive:
		_target.respawn()
	_pass_stats = { "hits": 0, "shots": 0, "headshots": 0, "killed": false }
	_phase = "cross"
	_emit("benchmark : %.0f m, %.2f m/s, passage %d/%d" % [_distance, _speed, _pass_index + 1, int(config["passes"])])


func _end_pass() -> void:
	_level["passes"].append(_pass_stats)
	if _pass_stats["hits"] > 0:
		_level["hit_passes"] += 1
	_pass_index += 1
	print("bench: %.0f m  %.2f m/s  passage %d/%d  tirs %d  touches %d" % [
		_distance, _speed, _pass_index, int(config["passes"]), _pass_stats["shots"], _pass_stats["hits"]])
	if _pass_index < int(config["passes"]):
		_phase = "pause"
		_timer = float(config["pause"])
		return
	_dist_entry["levels"].append(_level)
	var level_ok: bool = _level["hit_passes"] > 0
	if level_ok:
		_dist_entry["max_speed_hit"] = _speed
	var next_speed := _speed * float(config["speed_factor"])
	if level_ok and next_speed <= float(config["speed_max"]):
		_speed = next_speed
		_begin_level()
		_phase = "pause"
		_timer = float(config["pause"])
		return
	result["distances"].append(_dist_entry)
	print("bench: distance %.0f m → vitesse max touchée %.2f m/s" % [_distance, _dist_entry["max_speed_hit"]])
	var first_level_failed: bool = not level_ok and _dist_entry["levels"].size() == 1
	var next_distance := _distance + float(config["distance_step"])
	if first_level_failed or next_distance > float(config["distance_max"]):
		_finish()
		return
	_distance = next_distance
	_begin_distance()
	_phase = "pause"
	_timer = float(config["pause"])


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
			_target.position = Vector3(-_half_travel, 0.0, -_distance)
			_timer -= delta
			if _timer <= 0.0:
				_begin_pass()
		"cross":
			_x += _speed * delta
			_target.position = Vector3(_x, 0.0, -_distance)
			_face_player()
			if bool(config["oracle"]):
				_player.request_fire_from_assist()
			if _x > _half_travel:
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
