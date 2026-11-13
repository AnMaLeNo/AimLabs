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
## Extra line(s) about the AimBridge, set by the scene root.
var bridge_text := ""
## Progress of the trigger benchmark (TriggerBench), empty when idle.
var bench_text := ""
## Information profile: false hides every text the HUD would give out (stats, bridge, benchmark, hints).
var text_enabled := true


func _ready() -> void:
	crosshair.draw.connect(_draw_crosshair)
	overlay_label.vertical_alignment = VERTICAL_ALIGNMENT_BOTTOM
	overlay_label.offset_bottom = -16.0
	overlay_label.add_theme_font_size_override("font_size", 14)
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
	stats_label.visible = text_enabled
	var accuracy := 100.0 * hits / shots if shots > 0 else 0.0
	stats_label.text = "%d FPS  (%.1f ms)\nShots %d  Hits %d  (%.0f%%)\nHeadshots %d  Kills %d" % [
		Engine.get_frames_per_second(), avg * 1000.0, shots, hits, accuracy, headshots, kills]
	if bridge_text != "":
		stats_label.text += "\n" + bridge_text
	if bench_text != "":
		stats_label.text += "\n" + bench_text


func set_text_enabled(enabled: bool) -> void:
	text_enabled = enabled
	stats_label.visible = enabled
	set_mouse_captured(not overlay.visible)


func set_mouse_captured(captured: bool) -> void:
	overlay.visible = not captured
	hint_label.visible = captured and text_enabled
	if not captured:
		overlay_label.text = "" if not text_enabled else "WASD move · Shift sprint · Space jump · Left click shoot · R reload · F inspect · T reset targets · Esc menu (character, weapon, algos)"


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


## Reticle style: "small" (default: four thin 4 px arms, no outline, no dot: the very centre stays clear), "dot" (a 1.5 px dot
## only), "classic" (the former reticle: four 9 px arms + dot with a dark outline, ~28 px across), "none".
## A big reticle hides a far target — a person at 30 m is ~28 px tall in a 1280×720 window — from whatever looks at the screen,
## the player's detector included. Measured with the scripted bench through the two-machine pipeline, targets at 30 m:
## classic 7/8 and 6/8 hit, shot 203 ms after the reticle is on the target; small 8/8 and 8/8, 113 ms.
const CROSSHAIRS: Array[String] = ["small", "dot", "classic", "none"]
var crosshair_style := "small"


func _draw_crosshair() -> void:
	var c := crosshair.size * 0.5
	var col := Color(0.2, 1.0, 0.4)
	var outline := Color(0, 0, 0, 0.8)
	match crosshair_style:
		"classic":
			var gap := 5.0
			var length := 9.0
			var thickness := 2.0
			for dir in [Vector2.RIGHT, Vector2.LEFT, Vector2.UP, Vector2.DOWN]:
				var a: Vector2 = c + dir * gap
				var b: Vector2 = c + dir * (gap + length)
				crosshair.draw_line(a, b, outline, thickness + 2.0)
				crosshair.draw_line(a, b, col, thickness)
			crosshair.draw_circle(c, 1.5, col)
		"small":
			for dir in [Vector2.RIGHT, Vector2.LEFT, Vector2.UP, Vector2.DOWN]:
				crosshair.draw_line(c + dir * 4.0, c + dir * 8.0, Color(col, 0.85), 1.0)
		"dot":
			crosshair.draw_circle(c, 1.5, col)
	if _hitmarker_time > 0.0:
		var alpha := clampf(_hitmarker_time / 0.12, 0.0, 1.0)
		var hc := Color(_hitmarker_color, alpha)
		var inner := 7.0
		var outer := 15.0
		for d in [Vector2(1, 1), Vector2(-1, 1), Vector2(1, -1), Vector2(-1, -1)]:
			crosshair.draw_line(c + d * inner, c + d * outer, outline * alpha, 4.0)
			crosshair.draw_line(c + d * inner, c + d * outer, hc, 2.0)
