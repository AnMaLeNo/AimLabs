extends Node
## Scene root: wires the player, the world, the HUD and the AimBridge together
## and handles command-line flags used for automated checks:
##   --screenshot=<path>   render a few frames, save a PNG, quit
##   --smoke-test          aim at a target through the "assist" input path, shoot,
##                         verify the hit is registered, quit with exit code 0/1
##   --bridge=off          do not start the AimBridge (default: on)
##   --mode=<name>         game mode, see MODES (default: full)
##   --shm=<name>          shared memory segment name (default: aimlabs)

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

var bridge: AimBridge
var mode_name := "full"

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
	if _args.has("--screenshot") or _args.has("--smoke-test"):
		player.set_mouse_captured(true)
	if String(_args.get("--bridge", "on")) != "off":
		_start_bridge()


func _start_bridge() -> void:
	mode_name = String(_args.get("--mode", "full"))
	if not MODES.has(mode_name):
		push_warning("Unknown mode '%s', using 'full'" % mode_name)
		mode_name = "full"
	var m: Dictionary = MODES[mode_name]
	bridge = AimBridge.new()
	bridge.name = "AimBridge"
	bridge.shm_name = String(_args.get("--shm", "aimlabs"))
	bridge.set_mode(m["vision"], m["move"], m["fire"], m["size"])
	bridge.assist_move.connect(func(delta_px: Vector2) -> void: player.add_look_delta(delta_px, Player.LookSource.ASSIST))
	bridge.assist_fire.connect(player.request_fire_from_assist)
	bridge.client_connected_changed.connect(func(connected: bool) -> void:
		print("AimBridge: client ", "connected" if connected else "disconnected"))
	add_child(bridge)
	if not bridge.start(get_viewport()):
		push_error("AimBridge failed to start")


func _unhandled_input(event: InputEvent) -> void:
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
