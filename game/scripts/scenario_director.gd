
extends Node
## Arena: drives its parent (an Enemy) behind a row of walls so that it shows up at times and places nobody can predict.
## Every « act » starts hidden, waits a random time, shows the target, hides it again:
##   peek  : steps out from behind a wall end, stays a moment, steps back
##   cross : runs across a gap, from behind one wall to behind the next
##   popup : appears at once in the open, then vanishes
## Which wall end / gap, the waiting time, the speed and the time in the open are all drawn at random (--seed to repeat a run).
## A killed target disappears and the next act starts once it has respawned.

const KINDS: Array[String] = ["peek", "cross", "popup"]
const HIDE := 1.0                  # metres inside the wall's shadow where the target waits (its body is ±0.45 m wide)
const WAIT := Vector2(1.0, 3.5)    # seconds hidden before showing up
const PEEK_OUT := Vector2(0.4, 0.9)     # metres past the wall's shadow
const PEEK_HOLD := Vector2(0.3, 1.2)    # seconds in the open
const PEEK_SPEED := Vector2(2.0, 7.0)   # m/s
const CROSS_SPEED := Vector2(2.0, 8.0)
const POPUP_HOLD := Vector2(0.4, 1.2)

var kinds: Array[String] = KINDS.duplicate()
var face_point := Vector3.ZERO
var act := ""                      # current act (for logs / tests)
var acts_done := 0

var _rng := RandomNumberGenerator.new()
var _ends: Array = []              # [x of the shadow's border on the target line, side (+1 = open space on +x)]
var _gaps: Array = []              # [x_left, x_right] of every open stretch between two walls, on the target line
var _z := 0.0
var _steps: Array = []             # queue: ["wait", s] | ["move", x, speed] | ["show", bool] | ["jump", x]
var _timer := 0.0


func configure(scenario: String, p_seed: int, wall_ends: Array, shadow_scale: float, z: float) -> void:
	kinds.clear()
	for k in scenario.split(",", false):
		var name := String(k).strip_edges()
		if name == "mix":
			kinds = KINDS.duplicate()
			break
		if KINDS.has(name):
			kinds.append(name)
		else:
			push_warning("Unknown scenario '%s' (scenarios: %s, mix)" % [name, ", ".join(KINDS)])
	if kinds.is_empty():
		kinds = KINDS.duplicate()
	if p_seed >= 0:
		_rng.seed = p_seed
	else:
		_rng.randomize()
	_z = z
	_ends.clear()
	_gaps.clear()
	for e: Array in wall_ends:
		_ends.append([float(e[0]) * shadow_scale, float(e[1])])
	_ends.sort_custom(func(a: Array, b: Array) -> bool: return a[0] < b[0])
	for i in range(1, _ends.size() - 1, 2):       # ends come in pairs: wall start, wall end ; a gap = wall end -> next wall start
		_gaps.append([_ends[i][0], _ends[i + 1][0]])
	_steps.clear()
	act = ""


func _ready() -> void:
	var enemy := get_parent() as Enemy
	enemy.died.connect(_on_died)
	enemy.respawned.connect(_on_respawned)


func _on_died() -> void:
	_steps = [["wait", 0.7], ["show", false], ["hold"]]     # the body lies there a moment, then is gone until the respawn
	_timer = 0.0


func _on_respawned() -> void:
	_steps.clear()


func _r(v: Vector2) -> float:
	return _rng.randf_range(v.x, v.y)


func _next_act() -> void:
	act = kinds[_rng.randi() % kinds.size()]
	acts_done += 1
	match act:
		"peek":
			var e: Array = _ends[_rng.randi() % _ends.size()]
			var hidden: float = e[0] - e[1] * HIDE
			var speed := _r(PEEK_SPEED)
			_steps = [["show", false], ["jump", hidden], ["show", true], ["wait", _r(WAIT)],
				["move", e[0] + e[1] * _r(PEEK_OUT), speed], ["wait", _r(PEEK_HOLD)], ["move", hidden, speed]]
		"cross":
			var g: Array = _gaps[_rng.randi() % _gaps.size()]
			var a: float = g[0] - HIDE
			var b: float = g[1] + HIDE
			if _rng.randf() < 0.5:
				var t := a
				a = b
				b = t
			_steps = [["show", false], ["jump", a], ["show", true], ["wait", _r(WAIT)], ["move", b, _r(CROSS_SPEED)]]
		_:
			var g: Array = _gaps[_rng.randi() % _gaps.size()]
			_steps = [["show", false], ["jump", _rng.randf_range(g[0] + 0.5, g[1] - 0.5)], ["wait", _r(WAIT)],
				["show", true], ["wait", _r(POPUP_HOLD)], ["show", false]]


func _physics_process(delta: float) -> void:
	var enemy := get_parent() as Enemy
	if enemy == null or _ends.is_empty():
		return
	if _steps.is_empty():
		if not enemy.alive:
			return
		_next_act()
	while not _steps.is_empty():          # « show » and « jump » take no time: several can happen in the same tick
		var s: Array = _steps[0]
		var kind := String(s[0])
		if kind == "show":
			enemy.set_shown(bool(s[1]))
			_steps.pop_front()
			continue
		if kind == "jump":
			enemy.position = Vector3(float(s[1]), 0.0, _z)
			_steps.pop_front()
			continue
		if kind == "wait":
			_timer += delta
			if _timer >= float(s[1]):
				_timer = 0.0
				_steps.pop_front()
		elif kind == "move":
			var x := move_toward(enemy.position.x, float(s[1]), float(s[2]) * delta)
			enemy.position = Vector3(x, 0.0, _z)
			if is_equal_approx(x, float(s[1])):
				_steps.pop_front()
		break                             # « wait », « move », « hold » (dead: until the respawn) last at least one tick
	var to_player := face_point - enemy.position
	to_player.y = 0.0
	if to_player.length_squared() > 1e-6:
		enemy.rotation.y = atan2(to_player.x, to_player.z)
