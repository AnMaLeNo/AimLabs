extends Node
## Scene root: wires the player, the world, the HUD and the AimBridge together
## and handles command-line flags used for automated checks:
##   --screenshot=<path>   render a few frames, save a PNG, quit
##   --smoke-test          aim at a target through the "assist" input path, shoot,
##                         verify the hit is registered, quit with exit code 0/1
##   --bridge=off          do not start the AimBridge (default: on)
##   --mode=<name>         game mode, see MODES (default: full). silent = no bridge, nothing at all is published: the game
##                         only reacts to real input events (the fire key below, the mouse)
##   --fire-key=<name>     keyboard fire key besides the left mouse button (e.g. G, F5, KP_0; default G; also settable in the
##                         Esc menu, saved in user://settings.cfg)
##   --info=<profile>      information profile: real (the game gives out nothing but the keys) | training[,-key,+key,-all]
##                         (everything, each source switchable: state, meta, hud, timecode, truth); default real for
##                         --mode=remote and --mode=silent, training otherwise
##   --info-port=<n> --info-hz=<n>   ground-truth UDP stream (information option "truth"), default 7790 / 240
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
##   --algo-args="…"       extra command-line arguments for --algo (e.g. "--mode seg")
##   --show-hitboxes       draw collision shapes (check hitboxes against the visual model)
##   --bench-trigger       range: run the trigger benchmark
##   --bench-sniper        same, « one bullet » rules: a sweep succeeds only if hit with ≤ 1 shot (weapon operator by default) (TriggerBench) then quit; with
##                         --algo the algo is started first and the bench waits for it.
##   --bench-<key>=<v>     TriggerBench.DEFAULTS keys: distance-start/-step/-max, speed-start/
##                         -min/-max, precision, confirm, width, travel-widths, heights
##                         (feet,knees,pelvis,belly,chest,head), criterion (mark|hit|auto),
##                         mark-tolerance, settle, pause; --bench-oracle (fires + marks every tick),
##                         --bench-out=<json> (default bench_results/trigger_<algo>_<date>.json),
##                         --no-quit (stay in game when done)

## Game modes = what the external program is allowed to see / do.
const MODES := {
	"full": { "vision": true, "move": true, "fire": true, "size": Vector2i(0, 0) },
	"lowres": { "vision": true, "move": true, "fire": true, "size": Vector2i(640, 360) },
	"tiny": { "vision": true, "move": true, "fire": true, "size": Vector2i(320, 180) },
	"aim_only": { "vision": true, "move": true, "fire": false, "size": Vector2i(0, 0) },
	"fire_only": { "vision": true, "move": false, "fire": true, "size": Vector2i(0, 0) },
	"blind": { "vision": false, "move": true, "fire": true, "size": Vector2i(0, 0) },
	# Two-machine mode: the game publishes no image at all and only accepts commands; the player
	# gets the pixels by his own means (screen capture…) and runs the algo elsewhere (see https://github.com/AnMaLeNo/aimlabs-pipeline).
	"remote": { "vision": false, "move": true, "fire": true, "size": Vector2i(0, 0) },
	# Nothing at all: no bridge, no image, no state, no command. The game only reacts to REAL input events (the configurable fire
	# key, the mouse), so everything else — grabbing the picture, deciding, sending the key press — is the player's problem.
	"silent": { "vision": false, "move": false, "fire": false, "size": Vector2i(0, 0) },
}

## Fire key (besides the left mouse button): a physical keycode, configurable from the Esc menu or with --fire-key=<name>.
## Saved in user://settings.cfg. Physical = position on the keyboard, whatever the layout.
const SETTINGS_PATH := "user://settings.cfg"
const DEFAULT_FIRE_KEY := KEY_G
var fire_key: int = DEFAULT_FIRE_KEY
signal fire_key_changed

@onready var world: World = $World
@onready var player: Player = $Player
@onready var hud: HUD = $HUD
@onready var menu: AlgoMenu = $HUD/AlgoMenu

## Information profiles: what the game gives out besides the commands it accepts and the image of the chosen mode.
##   "real"     : nothing at all but the keys (the contest condition: the player's program has to cope on its own);
##   "training" : everything, each source switchable (for building and debugging the program).
## Sources: state = the bridge's State block (position, shots, fps, clock…) · meta = frame headers (capture time, view
## angles) · hud = every HUD text · timecode = the game clock encoded in the image · truth = ground-truth UDP stream.
const INFO_KEYS := ["state", "meta", "hud", "timecode", "truth"]
const INFO_LABELS := {
	"state": "État (pont)", "meta": "Méta des images", "hud": "Textes du HUD", "timecode": "Code temporel dans l'image",
	"truth": "Vérité terrain (UDP)",
}
signal info_changed

const TimeCodeScript := preload("res://scripts/timecode.gd")
const TruthStreamScript := preload("res://scripts/truth_stream.gd")

var bridge: AimBridge
var runner: AlgoRunner
var bench: TriggerBench
var mode_name := "full"
var info_profile := "training"
var info_toggles := { "state": true, "meta": true, "hud": true, "timecode": true, "truth": true }
var timecode: Control
var truth: Node
var _state_blocked := false
var agent_slug := ""
var weapon_slug := ""

var _frames := 0
var _args := {}
var _stats_timer := 0.0


func _ready() -> void:
	for arg in OS.get_cmdline_user_args():
		var kv := arg.split("=", true, 1)
		_args[kv[0]] = kv[1] if kv.size() > 1 else true
	if _args.has("--show-hitboxes"):
		get_tree().debug_collisions_hint = true  # draw the capsules (benchmark sanity check)
	player.shot_fired.connect(hud.on_shot_fired)
	player.target_hit.connect(hud.on_target_hit)
	player.mouse_capture_changed.connect(hud.set_mouse_captured)
	if (_args.has("--screenshot") or _args.has("--smoke-test")) and not _args.has("--menu"):
		player.set_mouse_captured(true)
	_load_fire_key()
	if String(_args.get("--mode", "")) == "silent":
		_args["--bridge"] = "off"   # silent: the game gives out nothing at all, so there is nothing to bridge
		set_mode("silent")          # no bridge to configure: the mode is just a name (information profile "real")
	if String(_args.get("--bridge", "on")) != "off":
		_start_bridge()
	runner = AlgoRunner.new()
	runner.name = "AlgoRunner"
	add_child(runner)
	runner.setup(String(_args.get("--algos", AlgoRunner.default_dir())), String(_args.get("--shm", "aimlabs")), String(_args.get("--python", "")))
	set_weapon(String(_args.get("--weapon", "operator" if _args.has("--bench-sniper") else "classic")))
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
	bench = TriggerBench.new()
	bench.name = "TriggerBench"
	add_child(bench)
	bench.setup(self)
	bench.progress.connect(func(t: String) -> void: hud.bench_text = t)
	_setup_info()
	menu.setup(self, runner)
	menu.visible = not player.mouse_captured
	player.mouse_capture_changed.connect(func(captured: bool) -> void: menu.visible = not captured)
	if _args.has("--bench-sniper"):
		_args["--bench-trigger"] = true
		_args["--bench-sniper"] = true
	if _args.has("--bench-trigger"):
		runner.echo_console = true
		OS.set_environment("AIMLABS_AUTOSTART", "1")  # algos start armed (no CapsLock needed)
	if _args.has("--algo"):
		var e: Dictionary = runner.find_entry(String(_args["--algo"]))
		if e.is_empty():
			push_warning("--algo: '%s' not found in %s" % [_args["--algo"], runner.algos_dir])
		else:
			var extra := PackedStringArray()
			if _args.has("--algo-args"):
				extra = String(_args["--algo-args"]).strip_edges().split(" ", false)
			runner.start(e["path"], extra)
	if _args.has("--bench-trigger"):
		_start_bench_from_args()


func _start_bench_from_args() -> void:
	world.set_map("range")
	player.set_mouse_captured(true)
	var cfg := {}
	for key in TriggerBench.DEFAULTS:
		var flag := "--bench-" + String(key).replace("_", "-")
		if _args.has(flag):
			var v: Variant = _args[flag]
			cfg[key] = v if typeof(v) == TYPE_BOOL or typeof(TriggerBench.DEFAULTS[key]) == TYPE_STRING else float(v)
	cfg["wait_client"] = _args.has("--algo") or _args.has("--bench-wait-client")  # remote mode: the relay is not an --algo
	if _args.has("--bench-sniper"):
		cfg["sniper"] = true
	var algo_name := String(_args.get("--algo", "oracle" if cfg.get("oracle", false) else "human")).get_basename()
	bench.out_path = String(_args.get("--bench-out", AlgoRunner._repo_root().path_join(
		"bench_results/trigger_%s_%s.json" % [algo_name, Time.get_datetime_string_from_system().replace(":", "-")])))
	bench.finished.connect(func(_r: Dictionary) -> void:
		if _args.has("--no-quit"):
			bench.show_sheet()
		else:
			get_tree().quit())
	if not bench.start(cfg):
		push_error("bench: could not start")
		get_tree().quit(1)


func _start_bridge() -> void:
	bridge = AimBridge.new()
	bridge.name = "AimBridge"
	# Drain the algo's commands before the player's _process of the same frame: otherwise the FIRE flag
	# set by the bridge is only consumed one frame later (~1.9 ms at 500 fps).
	bridge.process_priority = -100
	bridge.shm_name = String(_args.get("--shm", "aimlabs"))
	set_mode(String(_args.get("--mode", "full")))
	bridge.assist_move.connect(func(delta_px: Vector2) -> void: player.add_look_delta(delta_px, Player.LookSource.ASSIST))
	bridge.assist_fire.connect(player.request_fire_from_assist)
	bridge.client_connected_changed.connect(func(connected: bool) -> void:
		print("AimBridge: client ", "connected" if connected else "disconnected"))
	add_child(bridge)
	if not bridge.start(get_viewport()):
		push_error("AimBridge failed to start")


## Fire key: --fire-key=<name> (e.g. G, F5, KP_0) wins over the saved setting, which wins over the default.
func _load_fire_key() -> void:
	var cfg := ConfigFile.new()
	if cfg.load(SETTINGS_PATH) == OK:
		fire_key = int(cfg.get_value("input", "fire_key", DEFAULT_FIRE_KEY))
	if _args.has("--fire-key"):
		var k := OS.find_keycode_from_string(String(_args["--fire-key"]))
		if k == KEY_NONE:
			push_warning("--fire-key: unknown key '%s', keeping %s" % [_args["--fire-key"], fire_key_name()])
		else:
			fire_key = k
	_apply_fire_key()


## Make [param physical_keycode] the keyboard fire key (the left mouse button keeps firing). Saved for the next run.
func set_fire_key(physical_keycode: int, save := true) -> void:
	fire_key = physical_keycode
	_apply_fire_key()
	if save:
		var cfg := ConfigFile.new()
		cfg.load(SETTINGS_PATH)
		cfg.set_value("input", "fire_key", fire_key)
		cfg.save(SETTINGS_PATH)
	fire_key_changed.emit()


func fire_key_name() -> String:
	if DisplayServer.get_name() == "headless":   # that server cannot translate a physical key (nothing to display anyway)
		return OS.get_keycode_string(fire_key as Key)
	return OS.get_keycode_string(DisplayServer.keyboard_get_keycode_from_physical(fire_key as Key))


## The "fire" action = left mouse button + the fire key (the player fires only while the mouse is captured, i.e. while playing).
func _apply_fire_key() -> void:
	for ev in InputMap.action_get_events("fire"):
		if ev is InputEventKey:
			InputMap.action_erase_event("fire", ev)
	var e := InputEventKey.new()
	e.physical_keycode = fire_key as Key
	InputMap.action_add_event("fire", e)


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


## --info=real | training[,-key][,+key][,-all][,+all]   (default: real for --mode=remote, training otherwise)
func _setup_info() -> void:
	timecode = TimeCodeScript.new()
	timecode.name = "TimeCode"
	hud.add_child(timecode)
	truth = TruthStreamScript.new()
	truth.name = "TruthStream"
	add_child(truth)
	truth.setup(self, int(_args.get("--info-port", 7790)), float(_args.get("--info-hz", 240.0)))
	var spec := String(_args.get("--info", "real" if mode_name in ["remote", "silent"] else "training"))
	var parts := spec.split(",", false)
	var profile := String(parts[0]) if parts.size() > 0 else "training"
	for p in parts.slice(1):
		var key := String(p).substr(1)
		var on := String(p).begins_with("+")
		if key == "all":
			for k in INFO_KEYS:
				info_toggles[k] = on
		elif INFO_KEYS.has(key):
			info_toggles[key] = on
		else:
			push_warning("--info: unknown option '%s' (options: %s)" % [p, ", ".join(INFO_KEYS)])
	set_info_profile(profile)


func info_on(key: String) -> bool:
	return info_profile == "training" and bool(info_toggles.get(key, false))


func set_info_profile(profile: String) -> void:
	info_profile = "real" if profile == "real" else "training"
	_apply_info()


func set_info_toggle(key: String, on: bool) -> void:
	info_toggles[key] = on
	_apply_info()


func _apply_info() -> void:
	var st := info_on("state")
	var me := info_on("meta")
	_state_blocked = false
	if bridge:
		if bridge.has_method("set_info"):
			bridge.set_info(st, me)
		elif not (st and me):
			# fail closed: an old bridge binary cannot mask the State block, so it gets nothing at all
			push_error("AimBridge binary too old for the 'real' information profile: rebuild it (bridge/). State block disabled.")
			_state_blocked = true
	hud.set_text_enabled(info_on("hud"))
	if timecode:
		timecode.visible = info_on("timecode")
	if truth:
		truth.enabled = info_on("truth")
	info_changed.emit()


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and player.mouse_captured:
		if event.keycode == KEY_R:
			player.viewmodel.play("Reload")
		elif event.keycode == KEY_F:
			player.viewmodel.play("Inspect")
	if event.is_action_pressed("respawn_targets"):
		world.respawn_all()


var _trace_timer := 0.0


func _process(delta: float) -> void:
	_frames += 1
	if bridge and bridge.is_running() and not _state_blocked:
		bridge.publish_state(_player_state())
		_stats_timer += delta
		if _stats_timer >= 0.25:
			_stats_timer = 0.0
			hud.bridge_text = _bridge_hud_text()
			if _args.has("--bridge-trace"):
				_trace_timer += 0.25
				if _trace_timer >= 2.0:
					_trace_timer = 0.0
					var t: Dictionary = bridge.get_stats()
					print("TRACE fps %.0f  readback %.2f ms  published %.2f ms  copy %.2f ms  capture->cmd %.2f ms  cmd->fire %.2f ms" % [
						Engine.get_frames_per_second(), t.get("readback_ms", 0.0), t.get("publish_total_ms", 0.0), t["last_publish_ms"], t["avg_latency_ms"], player.assist_fire_lag_ms])
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
		line += "\nreadback %.1f ms  published %.1f ms after capture  fire lag %.1f ms" % [s.get("readback_ms", 0.0), s.get("publish_total_ms", 0.0), player.assist_fire_lag_ms]
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
