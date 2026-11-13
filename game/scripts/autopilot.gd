extends Node
## Simulated player: moves the VIEW the way a hand on a mouse would, through the same input path as a real mouse
## (InputEventMouseMotion, whole pixels, tagged Player.SYNTHETIC_DEVICE). It never fires: the shot is the business of whatever
## is being tested (the two-machine pipeline, an algo, the bench's own trigger).
##
##   hold(point)        glide to a world point and stay on it (pre-aim), with a little tremor
##   flick(enemy, s)    ballistic move (minimum-jerk profile) onto a target in `s` seconds, with an aiming error, then track it
##   track(enemy)       follow a target with a lag
##   sweep(yaw, speed)  turn at constant speed (°/s) to a yaw
##   snap(point)        set the view at once (between trials: not an input)
##   auto = true        play by itself: wait at `rest`, react to a target that becomes visible after a human reaction time
##                      (250 ± 40 ms, never under 170), flick, track while it is visible, go back to rest
## Used by BenchCases (--cases).

const HOLD_TAU := 0.10        # s: how fast « hold » reaches its point
const TRACK_TAU := 0.06       # s: lag of « track »
const TREMOR_DEG := 0.02

var game: Node
var player: Player
var mode := "off"             # off | hold | flick | track | sweep
var auto := false
var rest := Vector3(0.0, 1.35, -10.0)

var _rng := RandomNumberGenerator.new()
var _point := Vector3.ZERO
var _enemy: Enemy
var _offset := Vector2.ZERO   # aiming error of the current flick (degrees), kept while tracking
var _from := Vector2.ZERO
var _t0 := 0.0
var _dur := 0.1
var _sweep_to := 0.0
var _sweep_speed := 0.0
var _sweep_pitch := 0.0
var _acc := Vector2.ZERO      # fraction of a pixel not sent yet
var _react_at := -1.0
var _pending: Enemy
var _auto_target: Enemy
var _lost_at := -1.0
var _phase := Vector2.ZERO


func setup(p_game: Node, p_seed: int) -> void:
	game = p_game
	player = game.player
	process_priority = -60     # before the recorder (-50) and the player (0)
	if p_seed >= 0:
		_rng.seed = p_seed + 7919
	else:
		_rng.randomize()
	_phase = Vector2(_rng.randf() * TAU, _rng.randf() * TAU)


func _now() -> float:
	return Time.get_ticks_usec() / 1e6


## (yaw, pitch) in degrees that put the reticle on a world point.
func angles_to(p: Vector3) -> Vector2:
	var dir := (p - player.camera.global_position).normalized()
	return Vector2(rad_to_deg(atan2(-dir.x, -dir.z)), rad_to_deg(asin(clampf(dir.y, -1.0, 1.0))))


func reaction_time() -> float:
	return clampf(_rng.randfn(0.25, 0.04), 0.17, 0.45)


func flick_time(angle_deg: float) -> float:
	return (0.10 + 0.0045 * absf(angle_deg)) * _rng.randf_range(0.9, 1.1)


func busy() -> bool:
	return mode == "flick" or mode == "sweep"


func off() -> void:
	mode = "off"


func snap(p: Vector3) -> void:
	player.look_at_point(p)
	_acc = Vector2.ZERO


func snap_angles(yaw: float, pitch: float) -> void:
	player.yaw_deg = wrapf(yaw, -180.0, 180.0)
	player.pitch_deg = pitch
	player._apply_view_rotation()
	_acc = Vector2.ZERO


func hold(p: Vector3) -> void:
	_point = p
	mode = "hold"


func flick(e: Enemy, seconds: float) -> void:
	_enemy = e
	_from = Vector2(player.yaw_deg, player.pitch_deg)
	_t0 = _now()
	_dur = maxf(seconds, 0.02)
	_offset = Vector2(_rng.randfn(0.0, 0.25), _rng.randfn(0.0, 0.2))
	mode = "flick"


func track(e: Enemy) -> void:
	_enemy = e
	mode = "track"


func sweep(yaw_to: float, speed_deg_s: float, pitch: float) -> void:
	_sweep_to = yaw_to
	_sweep_speed = maxf(speed_deg_s, 1.0)
	_sweep_pitch = pitch
	mode = "sweep"


func _tremor(t: float) -> Vector2:
	return Vector2(sin(t * 7.3 + _phase.x), sin(t * 5.1 + _phase.y)) * TREMOR_DEG


func _process(delta: float) -> void:
	if auto:
		_auto_step()
	if mode == "off" or not player.mouse_captured:
		return
	var t := _now()
	var cur := Vector2(player.yaw_deg, player.pitch_deg)
	var want := cur
	match mode:
		"hold":
			var goal := angles_to(_point) + _tremor(t)
			want = cur + Vector2(wrapf(goal.x - cur.x, -180.0, 180.0), goal.y - cur.y) * (1.0 - exp(-delta / HOLD_TAU))
		"flick":
			if not is_instance_valid(_enemy):
				mode = "off"
				return
			var u := clampf((t - _t0) / _dur, 0.0, 1.0)
			var s := u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)        # minimum jerk: slow start, fast middle, slow end
			var goal := angles_to(_enemy.body_position()) + _offset
			want = _from + Vector2(wrapf(goal.x - _from.x, -180.0, 180.0), goal.y - _from.y) * s
			if u >= 1.0:
				mode = "track"
		"track":
			if not is_instance_valid(_enemy):
				mode = "off"
				return
			var goal := angles_to(_enemy.body_position()) + _offset + _tremor(t)
			want = cur + Vector2(wrapf(goal.x - cur.x, -180.0, 180.0), goal.y - cur.y) * (1.0 - exp(-delta / TRACK_TAU))
		"sweep":
			var left := wrapf(_sweep_to - cur.x, -180.0, 180.0)
			var step := _sweep_speed * delta
			if absf(left) <= step:
				want.x = cur.x + left
				mode = "idle"
			else:
				want.x = cur.x + signf(left) * step
			want.y = _sweep_pitch
	var d := Vector2(wrapf(want.x - cur.x, -180.0, 180.0), want.y - cur.y)
	_acc += Vector2(-d.x, -d.y) / player.sensitivity_deg_per_px          # the player turns by −dx, −dy degrees per pixel
	var px := Vector2(roundf(_acc.x), roundf(_acc.y))                     # a mouse reports whole counts
	_acc -= px
	if px != Vector2.ZERO:
		var ev := InputEventMouseMotion.new()
		ev.device = Player.SYNTHETIC_DEVICE
		ev.relative = px
		Input.parse_input_event(ev)


## Plays by itself: see `auto` above. Uses the game's own visibility test (BehaviorLog): what a player looking at the screen sees.
func _auto_step() -> void:
	var beh: Node = game.behavior
	if beh == null:
		return
	var t := _now()
	if _auto_target != null:
		if is_instance_valid(_auto_target) and beh.is_visible(_auto_target):
			_lost_at = -1.0
			return
		if _lost_at < 0.0:
			_lost_at = t
		if t - _lost_at < 0.25:                    # lost for a moment: keep looking where it was
			return
		_auto_target = null
		_lost_at = -1.0
		hold(rest)
		return
	var seen: Enemy = null
	for e: Enemy in game.world.enemies:
		if beh.is_visible(e):
			seen = e
			break
	if seen == null:
		_react_at = -1.0
		if mode != "hold":
			hold(rest)
		return
	if _react_at < 0.0 or seen != _pending:
		_pending = seen
		_react_at = t + reaction_time()
	elif t >= _react_at:
		var a := angles_to(seen.body_position())
		flick(seen, flick_time(Vector2(wrapf(a.x - player.yaw_deg, -180.0, 180.0), a.y - player.pitch_deg).length()))
		_auto_target = seen
		_react_at = -1.0
