class_name TargetMover
extends Node
## Moves its parent (an Enemy) on the shooting range along a deterministic pattern,
## centred on the parent's spawn position, always facing the spawn point of the player.
##   line   : back and forth along X, total travel = size
##   square : perimeter of a square of side = size (centre = spawn position)
##   circle : circumference of radius = size
## speed is in metres per second along the path. Setting pattern = "none" parks the target.

const PATTERNS: Array[String] = ["none", "line", "square", "circle"]

var pattern := "line"
var size := 4.0       # metres: travel (line), side (square), radius (circle)
var speed := 2.0      # m/s
var face_point := Vector3.ZERO  # the target keeps facing this point (player spawn)

var _centre := Vector3.ZERO
var _distance := 0.0  # metres travelled along the path


func _ready() -> void:
	_centre = get_parent().position


func configure(p_pattern: String, p_size: float, p_speed: float) -> void:
	pattern = p_pattern if PATTERNS.has(p_pattern) else "line"
	size = maxf(p_size, 0.0)
	speed = maxf(p_speed, 0.0)
	_distance = 0.0


func recentre(centre: Vector3) -> void:
	_centre = centre
	_distance = 0.0


func _physics_process(delta: float) -> void:
	var parent := get_parent() as Node3D
	if parent == null:
		return
	_distance += speed * delta
	parent.position = _centre + offset_at(_distance)
	var to_player := face_point - parent.position
	to_player.y = 0.0
	if to_player.length_squared() > 1e-6:
		# The models face +Z at rest, so look towards the player with -Z flipped.
		parent.rotation.y = atan2(to_player.x, to_player.z)


## Position offset from the centre after travelling `d` metres along the pattern.
func offset_at(d: float) -> Vector3:
	match pattern:
		"line":
			if size <= 0.0:
				return Vector3.ZERO
			var period := 2.0 * size
			var t := fposmod(d, period)
			var x := t if t <= size else period - t
			return Vector3(x - size * 0.5, 0.0, 0.0)
		"square":
			if size <= 0.0:
				return Vector3.ZERO
			var t := fposmod(d, 4.0 * size)
			var h := size * 0.5
			var side := int(t / size)
			var u := t - side * size
			match side:
				0: return Vector3(-h + u, 0.0, -h)   # back edge, left -> right
				1: return Vector3(h, 0.0, -h + u)    # right edge, back -> front
				2: return Vector3(h - u, 0.0, h)     # front edge, right -> left
				_: return Vector3(-h, 0.0, h - u)    # left edge, front -> back
		"circle":
			if size <= 0.0:
				return Vector3.ZERO
			var a := d / size
			return Vector3(sin(a) * size, 0.0, cos(a) * size - size)
	return Vector3.ZERO
