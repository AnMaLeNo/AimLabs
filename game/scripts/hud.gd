class_name HUD
extends CanvasLayer
## Crosshair, hit marker, performance counters and session stats.

@onready var crosshair: Control = $Crosshair
@onready var stats_label: Label = $Stats
@onready var hint_label: Label = $Hint
@onready var overlay: ColorRect = $PauseOverlay
@onready var overlay_label: Label = $PauseOverlay/Label

var shots := 0
var hits := 0
var headshots := 0
var kills := 0
var _hitmarker_time := 0.0
var _hitmarker_color := Color.WHITE
var _frame_times: Array[float] = []


func _ready() -> void:
	crosshair.draw.connect(_draw_crosshair)
	set_mouse_captured(false)


func _process(delta: float) -> void:
	_frame_times.append(delta)
	if _frame_times.size() > 60:
		_frame_times.pop_front()
	var avg := 0.0
	for t in _frame_times:
		avg += t
	avg /= maxf(_frame_times.size(), 1.0)
	if _hitmarker_time > 0.0:
		_hitmarker_time -= delta
	crosshair.queue_redraw()
	var accuracy := 100.0 * hits / shots if shots > 0 else 0.0
	stats_label.text = "%d FPS  (%.1f ms)\nShots %d  Hits %d  (%.0f%%)\nHeadshots %d  Kills %d" % [
		Engine.get_frames_per_second(), avg * 1000.0, shots, hits, accuracy, headshots, kills]


func set_mouse_captured(captured: bool) -> void:
	overlay.visible = not captured
	hint_label.visible = captured
	if not captured:
		overlay_label.text = "AimLabs\n\nClick to play\n\nWASD move · Shift sprint · Space jump\nLeft click shoot · T reset targets · Esc release mouse"


func on_shot_fired(_origin: Vector3, _hit_point: Vector3, _hit_something: bool) -> void:
	shots += 1


func on_target_hit(_enemy: Node, zone: String, _damage: int, killed: bool) -> void:
	hits += 1
	if zone == "head":
		headshots += 1
	if killed:
		kills += 1
	_hitmarker_color = Color(1.0, 0.25, 0.2) if killed else (Color(1.0, 0.85, 0.3) if zone == "head" else Color.WHITE)
	_hitmarker_time = 0.12


func _draw_crosshair() -> void:
	var c := crosshair.size * 0.5
	var gap := 5.0
	var length := 9.0
	var thickness := 2.0
	var col := Color(0.2, 1.0, 0.4)
	var outline := Color(0, 0, 0, 0.8)
	for dir in [Vector2.RIGHT, Vector2.LEFT, Vector2.UP, Vector2.DOWN]:
		var a: Vector2 = c + dir * gap
		var b: Vector2 = c + dir * (gap + length)
		crosshair.draw_line(a, b, outline, thickness + 2.0)
		crosshair.draw_line(a, b, col, thickness)
	crosshair.draw_circle(c, 1.5, col)
	if _hitmarker_time > 0.0:
		var alpha := clampf(_hitmarker_time / 0.12, 0.0, 1.0)
		var hc := Color(_hitmarker_color, alpha)
		var inner := 7.0
		var outer := 15.0
		for d in [Vector2(1, 1), Vector2(-1, 1), Vector2(1, -1), Vector2(-1, -1)]:
			crosshair.draw_line(c + d * inner, c + d * outer, outline * alpha, 4.0)
			crosshair.draw_line(c + d * inner, c + d * outer, hc, 2.0)
