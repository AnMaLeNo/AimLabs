extends Control
## Training aid (information option "timecode"): a strip of 26 cells in the HUD that encodes the game clock, so a
## screen-capture program can tell exactly when a captured image was rendered. NOT available in the "real"
## information profile (the game then gives no information about the image).
##
## Cell 0 is white and cell 1 black (threshold calibration), then the 24 low bits of Time.get_ticks_usec() (the clock
## of the bridge's `time_usec`), most significant bit first, read when the strip is drawn. 4 px cells, centred
## horizontally, its top edge 110 px above the middle of the window (inside any capture of the central square).

const CELL := 4.0
const BITS := 24


func _init() -> void:
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	size = Vector2(CELL * (BITS + 2), CELL)


func _process(_delta: float) -> void:
	var vp := get_viewport().get_visible_rect().size
	position = (vp * 0.5 + Vector2(-size.x * 0.5, -110.0)).floor()
	queue_redraw()


func _draw() -> void:
	var t := Time.get_ticks_usec() & 0xFFFFFF
	draw_rect(Rect2(0, 0, CELL, CELL), Color.WHITE)
	draw_rect(Rect2(CELL, 0, CELL, CELL), Color.BLACK)
	for i in BITS:
		var bit := (t >> (BITS - 1 - i)) & 1
		draw_rect(Rect2(CELL * (i + 2), 0, CELL, CELL), Color.WHITE if bit == 1 else Color.BLACK)
