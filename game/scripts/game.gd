extends Node
## Scene root: wires the player, the world, the HUD and the AimBridge together
## and handles command-line flags used for automated checks:
##   --screenshot=<path>   render a few frames, save a PNG, quit
##   --smoke-test          aim at a target through the "assist" input path, shoot,
##                         verify the hit is registered, quit with exit code 0/1
##   --bridge=off          do not start the AimBridge (default: on)
##   --mode=<name>         game mode, see MODES (default: full)
##   --shm=<name>          shared memory segment name (default: aimlabs)
##   --menu                with --screenshot: keep the Esc menu open
##   --algos=<dir>         folder scanned for algos (*.py), default: <repo>/algos
##   --algo=<file.py>      start this algo right away
##   --python=<cmd>        interpreter used to run algos (default: python3 / python)
##   --agent=<slug>        first-person arms (Kingdom Archives agent, default: jett)
##   --weapon=<slug>       held weapon (Kingdom Archives weapon, default: classic)
##   --map=<name>          showcase (default) | range (shooting range, see World)
##   --target=<slug>       range: agent used as the moving target (default: jett)
##   --pattern=<name>      range: none | line | square | circle (default: line)
##   --size=<m>            range: travel (line) / side (square) / radius (circle), default 4
##   --speed=<m/s>         range: speed along the path (default: 2)
##   --distance=<m>        range: distance spawn -> pattern centre (default: 10)

## Game modes = what the external program is allowed to see / do.
const MODES := {
	"full": { "vision": true, "move": true, "fire": true, "size": Vector2i(0, 0) },
	"lowres": { "vision": true, "move": true, "fire": true, "size": Vector2i(640, 360) },
	"tiny": { "vision": true, "move": true, "fire": true, "size": Vector2i(320, 180) },
	"aim_only": { "vision": true, "move": true, "fire": false, "size": Vector2i(0, 0) },
	"fire_only": { "vision": true, "move": false, "fire": true, "size": Vector2i(0, 0) },
	"blind": { "vision": false, "move": true, "fire": true, "size": Vector2i(0, 0) },
}

@onready var world: World = $World
@onready var player: Player = $Player
@onready var hud: HUD = $HUD
@onready var menu: AlgoMenu = $HUD/AlgoMenu

var bridge: AimBridge
var runner: AlgoRunner
var mode_name := "full"
var agent_slug := ""
var weapon_slug := ""

var _frames := 0
var _args := {}
var _stats_timer := 0.0


func _ready() -> void:
	for arg in OS.get_cmdline_user_args():
		var kv := arg.split("=", true, 1)
		_args[kv[0]] = kv[1] if kv.size() > 1 else true
	player.shot_fired.connect(hud.on_shot_fired)
	player.target_hit.connect(hud.on_target_hit)
	player.mouse_capture_changed.connect(hud.set_mouse_captured)
	if (_args.has("--screenshot") or _args.has("--smoke-test")) and not _args.has("--menu"):
		player.set_mouse_captured(true)
	if String(_args.get("--bridge", "on")) != "off":
		_start_bridge()
	runner = AlgoRunner.new()
	runner.name = "AlgoRunner"
	add_child(runner)
	runner.setup(String(_args.get("--algos", AlgoRunner.default_dir())), String(_args.get("--shm", "aimlabs")), String(_args.get("--python", "")))
	set_weapon(String(_args.get("--weapon", "classic")))
	set_agent(String(_args.get("--agent", "jett")))
	if _args.has("--pattern"):
		world.set_pattern(String(_args["--pattern"]))
	if _args.has("--size"):
		world.set_pattern_size(float(_args["--size"]))
	if _args.has("--speed"):
		world.set_pattern_speed(float(_args["--speed"]))
	if _args.has("--distance"):
		world.set_range_distance(float(_args["--distance"]))
	if _args.has("--target"):
		world.target_slug = String(_args["--target"])
	if _args.has("--map"):
		world.set_map(String(_args["--map"]))
	menu.setup(self, runner)
	menu.visible = not player.mouse_captured
	player.mouse_capture_changed.connect(func(captured: bool) -> void: menu.visible = not captured)
	if _args.has("--algo"):
		var e: Dictionary = runner.find_entry(String(_args["--algo"]))
		if e.is_empty():
			push_warning("--algo: '%s' not found in %s" % [_args["--algo"], runner.algos_dir])
		else:
			runner.start(e["path"])


func _start_bridge() -> void:
	bridge = AimBridge.new()
	bridge.name = "AimBridge"
	bridge.shm_name = String(_args.get("--shm", "aimlabs"))
	set_mode(String(_args.get("--mode", "full")))
	bridge.assist_move.connect(func(delta_px: Vector2) -> void: player.add_look_delta(delta_px, Player.LookSource.ASSIST))
	bridge.assist_fire.connect(player.request_fire_from_assist)
	bridge.client_connected_changed.connect(func(connected: bool) -> void:
		print("AimBridge: client ", "connected" if connected else "disconnected"))
	add_child(bridge)
	if not bridge.start(get_viewport()):
		push_error("AimBridge failed to start")


## Switch what the external program may see / do (see MODES); works while playing.
## Choose the played character (first-person arms). Returns false if the model is missing.
func set_agent(slug: String) -> bool:
	if not player.set_agent(slug):
		return false
	agent_slug = slug
	return true


## Choose the held weapon (its fire rate and auto / semi-auto mode come from the site data).
func set_weapon(slug: String) -> bool:
	if not player.set_weapon(slug):
		return false
	weapon_slug = slug
	return true


func set_mode(name: String) -> void:
	if not MODES.has(name):
		push_warning("Unknown mode '%s', using 'full'" % name)
		name = "full"
	mode_name = name
	var m: Dictionary = MODES[name]
	if bridge:
		bridge.set_mode(m["vision"], m["move"], m["fire"], m["size"])


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and player.mouse_captured:
		if event.keycode == KEY_R:
			player.viewmodel.play("Reload")
		elif event.keycode == KEY_F:
			player.viewmodel.play("Inspect")
	if event.is_action_pressed("respawn_targets"):
		world.respawn_all()


func _process(delta: float) -> void:
	_frames += 1
	if bridge and bridge.is_running():
		bridge.publish_state(_player_state())
		_stats_timer += delta
		if _stats_timer >= 0.25:
			_stats_timer = 0.0
			hud.bridge_text = _bridge_hud_text()
	if _args.has("--smoke-test"):
		_smoke_test_step()
	elif _args.has("--screenshot") and _frames == 20:
		_save_screenshot(String(_args["--screenshot"]))
		get_tree().quit()


func _player_state() -> Dictionary:
	var keys := 0
	if player.mouse_captured:
		keys |= int(Input.is_action_pressed("move_forward")) << 0
		keys |= int(Input.is_action_pressed("move_back")) << 1
		keys |= int(Input.is_action_pressed("move_left")) << 2
		keys |= int(Input.is_action_pressed("move_right")) << 3
		keys |= int(Input.is_action_pressed("jump")) << 4
		keys |= int(Input.is_action_pressed("sprint")) << 5
		keys |= int(Input.is_action_pressed("fire")) << 6
		keys |= 1 << 7
	keys |= int(player.is_on_floor()) << 8
	return {
		"pos": player.global_position,
		"vel": player.velocity,
		"yaw": player.yaw_deg,
		"pitch": player.pitch_deg,
		"fov": player.camera.fov,
		"sens": player.sensitivity_deg_per_px,
		"keys": keys,
		"real_look": player.look_delta_this_frame[Player.LookSource.REAL],
		"assist_look": player.look_delta_this_frame[Player.LookSource.ASSIST],
		"shots": hud.shots,
		"hits": hud.hits,
		"headshots": hud.headshots,
		"kills": hud.kills,
		"fps": Engine.get_frames_per_second(),
	}


func _bridge_hud_text() -> String:
	var s: Dictionary = bridge.get_stats()
	var line := "Bridge [%s] %s  %s" % [mode_name, s["capture_mode"], "client ON" if s["client_connected"] else "no client"]
	if s["client_connected"]:
		line += "\n%.0f frames/s  copy %.2f ms  cmd %d (%d dropped)  latency %.1f ms" % [
			s["publish_fps"], s["last_publish_ms"], s["cmds_applied"], s["cmds_dropped"], s["avg_latency_ms"]]
	if runner and runner.current_path != "":
		line += "\nAlgo: " + runner.status_text()
	return line


func _save_screenshot(path: String) -> void:
	var img := get_viewport().get_texture().get_image()
	var err := img.save_png(path)
	print("screenshot ", path, " -> ", error_string(err))


var _smoke_target: Enemy
var _smoke_hp_before := 0

func _smoke_test_step() -> void:
	match _frames:
		10:
			# Pick the nearest alive target and aim at its head through the assist path.
			var best: Enemy = null
			var best_d := INF
			for e in world.enemies:
				var d := e.global_position.distance_to(player.global_position)
				if e.alive and d < best_d:
					best = e
					best_d = d
			_smoke_target = best
			_smoke_hp_before = best.hp
			player.look_at_point(best.head_position())
			# Nudge by one pixel through the assist channel to exercise that path too.
			player.add_look_delta(Vector2(1, 0), Player.LookSource.ASSIST)
			player.add_look_delta(Vector2(-1, 0), Player.LookSource.ASSIST)
			print("smoke: aiming at ", best.name, " at ", best_d, " m, hp=", best.hp)
		12:
			player.request_fire_from_assist()
		20:
			var ok := _smoke_target.hp < _smoke_hp_before or not _smoke_target.alive
			print("smoke: hp after=", _smoke_target.hp, " alive=", _smoke_target.alive, " shots=", hud.shots, " hits=", hud.hits, " headshots=", hud.headshots)
			if _args.has("--screenshot"):
				_save_screenshot(String(_args["--screenshot"]))
			print("SMOKE ", "OK" if ok and hud.headshots == 1 else "FAILED")
			get_tree().quit(0 if ok and hud.headshots == 1 else 1)
