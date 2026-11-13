extends Node
## Scripted bench of « what happens in a normal game », on the arena map, with a simulated player (Autopilot) moving the view.
## Each case is repeated; every trial is marked in the behaviour record (K records), so the game's own measurements (when the
## target became visible, when the reticle reached it, when the shot left, what it hit) can be read case by case afterwards
## (tools/behavior/case_report.py). Nothing fires by itself: the shots come from what is being tested.
##
##   --cases=<names|all>        comma-separated list (default all but mix)
##   --case-repeats=<n>         trials per case (default 10)
##   --cases-shooter=trigger[:<delay ms>[:<min visible ms>]]
##                              the bench's own trigger instead of an outside program: fires <delay> ms after the reticle is on
##                              a target, and only if the target has been visible for <min visible> ms. A reference: what a
##                              perfect trigger does, with and without the « visible for 150 ms » rule.
##   --seed=<n>                 same trials every run
##
## Cases (the player's reticle starts on the middle of the walls unless said otherwise):
##   flick_side    a target appears at once in the open, 14 to 30° to the side (outside the central square of the capture);
##                 the player reacts like a human (250 ± 40 ms) and flicks onto it
##   peek_preaim   the reticle waits where a target hidden behind a wall end will stop; the target steps out at 4 to 9 m/s
##   cross_preaim  the reticle waits 0.5 m inside a gap; a target runs across the gap at 4 to 8 m/s
##   two_far       peek_preaim while ANOTHER target has been standing in the open for a while, far to the other side
##   two_near      peek_preaim while another target has been standing 1 m from where the first one will stop
##   sweep         a target stands in the open for a while; the player sweeps the reticle across it at 60, 150, 300, 600 °/s
##   peek_far, flick_far   the same as peek_preaim / flick_side with the walls at 30 m instead of 12
##   mix           60 s of the random arena (ScenarioDirector) with the player playing by itself

const CASES: Array[String] = ["flick_side", "peek_preaim", "cross_preaim", "two_far", "two_near", "sweep", "peek_far", "flick_far", "mix"]
const NEAR := 12.0
const FAR := 30.0
const AutopilotScript := preload("res://scripts/autopilot.gd")

var game: Node
var world: World
var player: Player
var pilot: Node
var cases: Array[String] = []
var repeats := 10
var trials_done := 0

var _rng := RandomNumberGenerator.new()
var _seed := -1
var _shooter := false
var _shoot_delay_ms := 0.0
var _shoot_min_visible_ms := 0.0
var _last_shot := 0


func setup(p_game: Node, spec: String, p_repeats: int, shooter_spec: String, p_seed: int) -> void:
	game = p_game
	world = game.world
	player = game.player
	repeats = maxi(p_repeats, 1)
	_seed = p_seed
	if p_seed >= 0:
		_rng.seed = p_seed
	else:
		_rng.randomize()
	for name in spec.split(",", false):
		var n := String(name).strip_edges()
		if n == "all":
			cases.assign(CASES.filter(func(c: String) -> bool: return c != "mix"))
			break
		if CASES.has(n):
			cases.append(n)
		else:
			push_warning("--cases: unknown case '%s' (cases: %s)" % [n, ", ".join(CASES)])
	if shooter_spec.begins_with("trigger"):
		var parts := shooter_spec.split(":")
		_shooter = true
		_shoot_delay_ms = float(parts[1]) if parts.size() > 1 else 0.0
		_shoot_min_visible_ms = float(parts[2]) if parts.size() > 2 else 0.0
	process_priority = -40          # after the recorder (-50): what is under the reticle is this frame's
	pilot = AutopilotScript.new()
	pilot.name = "Autopilot"
	game.add_child(pilot)
	pilot.setup(game, p_seed)
	player.synthetic_only = true    # the real mouse of the PC is not part of the run
	_run.call_deferred()


func _wait(s: float) -> void:
	await get_tree().create_timer(s, false).timeout


func _build(distance: float, n_targets: int, directed: bool) -> void:
	game.behavior.on_target = null
	world.range_distance = distance
	world.arena_targets = n_targets
	world.arena_directed = directed
	world.map = "arena"
	world._build_map()
	await get_tree().physics_frame
	await get_tree().physics_frame
	for e: Enemy in world.targets:
		if not directed:
			e.max_hp = 1000000           # a bench target never dies: every trial sees the same thing
			e.hp = e.max_hp
	pilot.rest = Vector3(0.0, 1.35, -distance)


func _run() -> void:
	await _build(NEAR, 2, false)
	player.set_mouse_captured(true)
	if game.bridge and not _shooter:
		print("BenchCases: waiting for the outside program (bridge client)…")
		while not bool(game.bridge.get_stats().get("client_connected", false)):
			await _wait(0.2)
		await _wait(3.0)
	print("BenchCases: %s, %d trials each%s" % [", ".join(cases), repeats, (" ; own trigger %.0f ms, visible ≥ %.0f ms" % [_shoot_delay_ms, _shoot_min_visible_ms]) if _shooter else ""])
	for name in cases:
		var id := CASES.find(name) + 1
		var far := name == "peek_far" or name == "flick_far"       # the walls at 30 m (« two_far » is about the OTHER target)
		var base: String = {"peek_far": "peek_preaim", "flick_far": "flick_side"}.get(name, name)
		if name == "mix":
			await _build(NEAR, 1, true)
			await _case_mix(id)
			await _build(NEAR, 2, false)
			continue
		if absf(world.range_distance - (FAR if far else NEAR)) > 0.01:
			await _build(FAR if far else NEAR, 2, false)
		for r in repeats:
			for e: Enemy in world.targets:
				e.set_shown(false)
			match base:
				"flick_side":
					await _case_flick_side(id, r)
				"peek_preaim":
					await _case_peek(id, r, "")
				"cross_preaim":
					await _case_cross(id, r)
				"two_far":
					await _case_peek(id, r, "far")
				"two_near":
					await _case_peek(id, r, "near")
				"sweep":
					await _case_sweep(id, r)
			trials_done += 1
	pilot.off()
	game.behavior.flush()
	print("BenchCases: done, %d trials" % trials_done)
	get_tree().quit()


func _physics_process(_delta: float) -> void:
	for e: Enemy in world.targets:                       # the targets always face the player, like on the range
		if world.director == null or e != world.target:
			e.rotation.y = atan2(-e.position.x, -e.position.z)


func _process(_delta: float) -> void:
	if not _shooter:
		return
	var beh: Node = game.behavior
	var e: Enemy = beh.on_target
	if e == null or not is_instance_valid(e):
		return
	var now := Time.get_ticks_usec()
	var since := int(beh.visible_since(e))
	if since == 0 or now - _last_shot < 400000:
		return
	if (now - int(beh.on_since_usec)) / 1000.0 >= _shoot_delay_ms and (now - since) / 1000.0 >= _shoot_min_visible_ms:
		_last_shot = now
		player.request_fire_from_assist()
		beh.record_assist_fire()


# ---------------------------------------------------------------------------------------------- geometry
func _lane_z() -> float:
	return -(world.range_distance + World.ARENA_BEHIND)


func _open_z() -> float:
	return -(world.range_distance - 1.5)


## Where the shadow of a wall end falls on the line the first target moves on (seen from the spawn).
func _shadow(x_end: float) -> float:
	return x_end * -_lane_z() / world.range_distance


func _chest(e: Enemy, x: float, z: float) -> Vector3:
	return Vector3(x, e.body_position().y - e.global_position.y, z)


func _place(e: Enemy, x: float, z: float, shown: bool) -> void:
	e.position = Vector3(x, 0.0, z)
	e.rotation.y = atan2(-x, -z)
	e.set_shown(shown)


func _move(e: Enemy, to_x: float, speed: float) -> void:
	var tw := create_tween()
	tw.tween_property(e, "position:x", to_x, absf(to_x - e.position.x) / maxf(speed, 0.1))
	await tw.finished


func _mark(id: int, trial: int, phase: int, subject: int, param: float) -> void:
	game.behavior.mark(id, trial, phase, subject, param)


# ---------------------------------------------------------------------------------------------- cases
func _case_flick_side(id: int, trial: int) -> void:
	var s: Enemy = world.targets[0]
	var ang := _rng.randf_range(14.0, 30.0) * (1.0 if _rng.randf() < 0.5 else -1.0)
	var z := _open_z()
	_place(s, tan(deg_to_rad(ang)) * -z, z, false)
	pilot.snap(pilot.rest)
	pilot.hold(pilot.rest)
	_mark(id, trial, 0, 0, ang)
	await _wait(_rng.randf_range(0.8, 1.6))
	_mark(id, trial, 1, 0, ang)
	s.set_shown(true)
	var react: float = pilot.reaction_time()
	await _wait(react)
	pilot.flick(s, pilot.flick_time(ang))
	await _wait(1.3 - react)
	_mark(id, trial, 2, 0, ang)
	s.set_shown(false)
	pilot.hold(pilot.rest)
	await _wait(0.4)


## The reticle waits where the target will stop. `other`: "" | "far" | "near" = a second target standing in the open since the
## beginning of the trial, far on the other side / 1 m beyond the place where the first one stops.
func _case_peek(id: int, trial: int, other: String) -> void:
	var s: Enemy = world.targets[0]
	var o: Enemy = world.targets[1]
	var e: Array = world.arena_wall_ends[_rng.randi() % world.arena_wall_ends.size()]
	var side: float = e[1]
	var xs := _shadow(e[0])
	var z := _lane_z()
	var hidden := xs - side * 1.0
	var final := xs + side * 0.5
	_place(s, hidden, z, true)
	if other == "far":
		var oz := _open_z()
		_place(o, tan(deg_to_rad(35.0)) * -oz * (-1.0 if final > 0.0 else 1.0), oz, true)
	elif other == "near":
		_place(o, xs + side * 1.5, z - 0.7, true)
	var aim := _chest(s, final, z)
	pilot.snap(aim)
	pilot.hold(aim)
	var speed := _rng.randf_range(4.0, 9.0)
	_mark(id, trial, 0, 0, speed)
	await _wait(_rng.randf_range(0.9, 1.6))
	_mark(id, trial, 1, 0, speed)
	await _move(s, final, speed)
	await _wait(1.0)
	_mark(id, trial, 2, 0, speed)
	await _move(s, hidden, 6.0)
	o.set_shown(false)
	await _wait(0.2)


func _case_cross(id: int, trial: int) -> void:
	var s: Enemy = world.targets[0]
	var ends: Array = world.arena_wall_ends.duplicate()
	ends.sort_custom(func(a: Array, b: Array) -> bool: return a[0] < b[0])
	var g := 1 + 2 * (_rng.randi() % ((ends.size() - 2) / 2))          # ends 1-2, 3-4…: a wall end and the next wall's start
	var left := _shadow(ends[g][0])
	var right := _shadow(ends[g + 1][0])
	var dirn := 1.0 if _rng.randf() < 0.5 else -1.0
	var z := _lane_z()
	var from_x := left - 1.0 if dirn > 0.0 else right + 1.0
	var to_x := right + 1.0 if dirn > 0.0 else left - 1.0
	_place(s, from_x, z, true)
	var aim := _chest(s, left + 0.5 if dirn > 0.0 else right - 0.5, z)
	pilot.snap(aim)
	pilot.hold(aim)
	var speed := _rng.randf_range(4.0, 8.0)
	_mark(id, trial, 0, 0, speed)
	await _wait(_rng.randf_range(0.9, 1.6))
	_mark(id, trial, 1, 0, speed)
	await _move(s, to_x, speed)
	await _wait(0.3)
	_mark(id, trial, 2, 0, speed)
	await _wait(0.2)


func _case_sweep(id: int, trial: int) -> void:
	var s: Enemy = world.targets[0]
	var z := _open_z()
	_place(s, _rng.randf_range(-2.0, 2.0), z, true)
	await get_tree().process_frame
	var a: Vector2 = pilot.angles_to(s.body_position())
	var dirn := 1.0 if _rng.randf() < 0.5 else -1.0
	var speed: float = [60.0, 150.0, 300.0, 600.0][trial % 4]
	pilot.off()
	pilot.snap_angles(a.x - dirn * 25.0, a.y)
	_mark(id, trial, 0, 0, speed)
	await _wait(_rng.randf_range(1.0, 1.5))
	_mark(id, trial, 1, 0, speed)
	pilot.sweep(a.x + dirn * 25.0, speed, a.y)
	while pilot.busy():
		await get_tree().process_frame
	await _wait(0.3)
	_mark(id, trial, 2, 0, speed)
	s.set_shown(false)
	await _wait(0.2)


func _case_mix(id: int) -> void:
	pilot.snap(pilot.rest)
	pilot.hold(pilot.rest)
	pilot.auto = true
	_mark(id, 0, 0, 0, 60.0)
	_mark(id, 0, 1, 0, 60.0)
	await _wait(60.0)
	_mark(id, 0, 2, 0, 60.0)
	pilot.auto = false
	trials_done += 1
