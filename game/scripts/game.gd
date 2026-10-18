extends Node
## Scene root: wires the player, the world and the HUD together and handles
## command-line flags used for automated checks:
##   --screenshot=<path>   render a few frames, save a PNG, quit
##   --smoke-test          aim at a target through the "assist" input path, shoot,
##                         verify the hit is registered, quit with exit code 0/1

@onready var world: World = $World
@onready var player: Player = $Player
@onready var hud: HUD = $HUD

var _frames := 0
var _args := {}


func _ready() -> void:
	for arg in OS.get_cmdline_user_args():
		var kv := arg.split("=", true, 1)
		_args[kv[0]] = kv[1] if kv.size() > 1 else true
	player.shot_fired.connect(hud.on_shot_fired)
	player.target_hit.connect(hud.on_target_hit)
	player.mouse_capture_changed.connect(hud.set_mouse_captured)
	if _args.has("--screenshot") or _args.has("--smoke-test"):
		player.set_mouse_captured(true)


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("respawn_targets"):
		world.respawn_all()


func _process(_delta: float) -> void:
	_frames += 1
	if _args.has("--smoke-test"):
		_smoke_test_step()
	elif _args.has("--screenshot") and _frames == 20:
		_save_screenshot(String(_args["--screenshot"]))
		get_tree().quit()


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
