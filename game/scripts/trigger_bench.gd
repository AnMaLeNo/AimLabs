class_name TriggerBench
extends Node
## Reproducible benchmark for "trigger" algorithms (they only fire / mark, never move the mouse).
## The camera is locked on a fixed point of the target (one or more aim heights) and the range
## target sweeps back and forth across the reticle on a short line (± `travel_widths` widths).
## At each distance the limit speed is found by bisection: start at speed_start, double while a
## sweep "scores", then halve the interval between the last scoring speed and the first failing
## one until it is `precision` wide; `confirm` extra sweeps guard against a lucky score. Then the
## target moves `distance_step` further; the run stops when nothing scores or at distance_max.
##
## Scoring ("laser", weapon independent): the algo sends CMD_MARK (« the target is under the
## reticle on frame N ») through AimBridge; every physics tick the bench casts the reticle ray
## itself and knows the truth (zone + point on the body). A mark is correct when the target was
## under the reticle at the capture time of its frame. A sweep scores when ≥ 1 mark is correct
## (criterion "mark"); without marks (human / oracle) it scores on a weapon hit ("hit").
## Output: JSON with every tick / mark / segment on the body, an SVG « fiche » (body-width bars:
## green = detected, red = under the reticle but missed, orange = false positive) saved next to
## the JSON and shown in game.

signal finished(result: Dictionary)
signal progress(text: String)

const DEFAULTS := {
	"distance_start": 5.0, "distance_step": 5.0, "distance_max": 40.0,
	"speed_start": 2.0, "speed_min": 0.25, "speed_max": 80.0, "precision": 0.1, "confirm": 2,
	"width": 1.2, "travel_widths": 2.0, "heights": "chest", "criterion": "auto",
	"mark_tolerance": 0.03, "settle": 3.0, "pause": 0.3, "wait_client": false, "oracle": false,
}
## Aim heights as fraction of the head height (chest/head use the hitboxes directly).
const HEIGHT_FRACTIONS := { "feet": 0.08, "knees": 0.3, "pelvis": 0.52, "belly": 0.62, "chest": -1.0, "head": -2.0 }
const RAY_MASK := (1 << 2) | (1 << 3) | (1 << 0)

var game: Node
var running := false
var config := {}
var result := {}
var out_path := ""
var svg_text := ""

var _world: World
var _player: Player
var _target: Enemy
var _heights: PackedStringArray = []
var _height_index := 0
var _height_entry: Dictionary = {}
var _distance := 0.0
var _speed := 0.0
var _half_travel := 0.0
var _dir := 1.0
var _lo := 0.0
var _hi := 0.0
var _confirm_left := 0
var _confirm_hits := 0
var _dist_entry: Dictionary = {}
var _phase := ""           # "settle" | "pause" | "cross" | "done"
var _timer := 0.0
var _x := 0.0
var _pass_stats := {}
var _ticks: Array = []     # per sweep: [usec, under(bool), zone, local_x, local_y, path_x]
var _marks: Array = []     # per sweep: [recv_usec, capture_usec, frame_id]
var _criterion := "hit"
var _saved_respawn_delay := 3.0
var _last_text := ""
var _sheet_layer: CanvasLayer


func setup(p_game: Node) -> void:
	game = p_game
	_world = game.world
	_player = game.player
	_player.target_hit.connect(_on_target_hit)
	_player.shot_fired.connect(_on_shot_fired)
	if game.bridge:
		game.bridge.assist_mark.connect(_on_mark)


## Start with `cfg` (keys of DEFAULTS; unknown keys ignored). Requires the range map.
func start(cfg: Dictionary = {}) -> bool:
	if running:
		return false
	if _world.map != "range" or _world.target == null:
		push_warning("TriggerBench: switch to the range map first")
		return false
	hide_sheet()
	config = DEFAULTS.duplicate()
	for k in cfg:
		if config.has(k):
			config[k] = cfg[k]
	_target = _world.target
	_saved_respawn_delay = _target.respawn_delay
	_target.respawn_delay = 0.05
	if _world.mover:
		_world.mover.set_physics_process(false)
	_heights = PackedStringArray()
	for h in String(config["heights"]).split(","):
		if HEIGHT_FRACTIONS.has(h.strip_edges()):
			_heights.append(h.strip_edges())
	if _heights.is_empty():
		_heights.append("chest")
	var client: bool = game.bridge != null and game.bridge.get_stats()["client_connected"]
	_criterion = String(config["criterion"])
	if _criterion == "auto":
		_criterion = "mark" if (client or bool(config["oracle"]) or bool(config["wait_client"])) else "hit"
	result = {
		"algo": game.runner.current_name() if game.runner and game.runner.is_running() else ("oracle" if config["oracle"] else "human"),
		"weapon": game.weapon_slug, "agent": game.agent_slug, "target": _world.target_slug,
		"criterion": _criterion, "config": config.duplicate(),
		"started": Time.get_datetime_string_from_system(), "fps": 0.0, "heights": [],
	}
	_height_index = 0
	_begin_height()
	_phase = "settle"
	_timer = float(config["settle"])
	running = true
	_emit("benchmark : préparation (%.0f s)…" % _timer)
	return true


func stop() -> void:
	if not running:
		return
	running = false
	_phase = "done"
	_target.respawn_delay = _saved_respawn_delay
	if _world.mover:
		_world.mover.set_physics_process(true)
		_world.mover.recentre(Vector3(0.0, 0.0, -_world.range_distance))
	_target.position = Vector3(0.0, 0.0, -_world.range_distance)
	_target.rotation.y = 0.0


func status_text() -> String:
	return _last_text


func _emit(text: String) -> void:
	_last_text = text
	progress.emit(text)


func _aim_y() -> float:
	var h := _heights[_height_index]
	var frac: float = HEIGHT_FRACTIONS[h]
	if frac == -1.0:
		return _target.body_position().y - _target.global_position.y
	if frac == -2.0:
		return _target.head_position().y - _target.global_position.y
	var head_h := _target.head_position().y - _target.global_position.y
	return head_h * frac


func _begin_height() -> void:
	_height_entry = { "height": _heights[_height_index], "aim_y": 0.0, "distances": [] }
	_distance = float(config["distance_start"])
	_begin_distance()


func _begin_distance() -> void:
	_dist_entry = { "distance": _distance, "max_speed_hit": 0.0, "sweeps": [], "confirm_hits": 0 }
	_speed = float(config["speed_start"])
	_lo = 0.0
	_hi = 0.0
	_confirm_left = 0
	_confirm_hits = 0
	_half_travel = float(config["width"]) * float(config["travel_widths"])
	_dir = 1.0


func _begin_pass() -> void:
	_x = -_half_travel * _dir
	_target.position = Vector3(_x, 0.0, -_distance)
	_face_player()
	if not _target.alive:
		_target.respawn()
	_height_entry["aim_y"] = _aim_y()
	_pass_stats = { "speed": _speed, "dir": _dir, "hits": 0, "shots": 0, "headshots": 0, "killed": false,
		"confirm": _confirm_left > 0 }
	_ticks = []
	_marks = []
	_phase = "cross"
	var what := "confirmation %d/%d" % [int(config["confirm"]) - _confirm_left + 1, int(config["confirm"])] if _confirm_left > 0 else (
		"ok jusqu'à %.2f, raté dès %.2f" % [_lo, _hi] if _hi > 0.0 else "recherche de la limite")
	_emit("benchmark [%s, %s] : %.0f m, %.2f m/s — %s" % [_heights[_height_index], _criterion, _distance, _speed, what])


## Ground truth + algo marks → segments on the body, correctness, coverage, latency.
func _analyse_sweep() -> void:
	var tol := int(float(config["mark_tolerance"]) * 1e6)
	var under_ticks := 0
	var detected := {}  # tick index → true
	var correct := 0
	var false_pos := 0
	var first_under := -1
	var first_correct := -1
	for i in _ticks.size():
		if _ticks[i][1]:
			under_ticks += 1
			if first_under < 0:
				first_under = int(_ticks[i][0])
	var mark_rows := []
	for m in _marks:
		var cap := int(m[1])
		var idx := _nearest_tick(cap if cap >= 0 else int(m[0]))
		var ok: bool = idx >= 0 and bool(_ticks[idx][1]) and absi(int(_ticks[idx][0]) - (cap if cap >= 0 else int(m[0]))) <= tol
		if ok:
			correct += 1
			detected[idx] = true
			if first_correct < 0 or int(m[0]) < first_correct:
				first_correct = int(m[0])
		else:
			false_pos += 1
		mark_rows.append({ "frame_id": m[2], "capture_usec": cap, "recv_usec": m[0], "correct": ok,
			"local_x": _ticks[idx][3] if idx >= 0 else 0.0, "local_y": _ticks[idx][4] if idx >= 0 else 0.0,
			"path_x": _ticks[idx][5] if idx >= 0 else 0.0 })
	# segments along the body (local x, metres, + = target's left… as seen by the player: right)
	var det_segs := []
	var miss_segs := []
	var cur := []
	var cur_kind := ""
	for i in _ticks.size():
		var kind := ""
		if _ticks[i][1]:
			kind = "det" if detected.has(i) else "miss"
		if kind != cur_kind:
			if cur_kind == "det":
				det_segs.append(cur)
			elif cur_kind == "miss":
				miss_segs.append(cur)
			cur = [_ticks[i][3], _ticks[i][3]] if kind != "" else []
			cur_kind = kind
		elif kind != "":
			cur[1] = _ticks[i][3]
	if cur_kind == "det":
		det_segs.append(cur)
	elif cur_kind == "miss":
		miss_segs.append(cur)
	_pass_stats["under_ticks"] = under_ticks
	_pass_stats["ticks"] = _ticks.size()
	_pass_stats["marks"] = _marks.size()
	_pass_stats["marks_correct"] = correct
	_pass_stats["false_positives"] = false_pos
	_pass_stats["coverage"] = float(detected.size()) / under_ticks if under_ticks > 0 else 0.0
	_pass_stats["first_mark_latency_ms"] = (first_correct - first_under) / 1000.0 if first_correct >= 0 and first_under >= 0 else null
	_pass_stats["detected_segments"] = det_segs
	_pass_stats["missed_segments"] = miss_segs
	_pass_stats["mark_rows"] = mark_rows
	_pass_stats["false_positive_path_x"] = []
	for r in mark_rows:
		if not r["correct"]:
			_pass_stats["false_positive_path_x"].append(r["path_x"])


func _nearest_tick(usec: int) -> int:
	if _ticks.is_empty():
		return -1
	var lo := 0
	var hi := _ticks.size() - 1
	while lo < hi:
		var mid := (lo + hi) / 2
		if int(_ticks[mid][0]) < usec:
			lo = mid + 1
		else:
			hi = mid
	if lo > 0 and absi(int(_ticks[lo - 1][0]) - usec) < absi(int(_ticks[lo][0]) - usec):
		return lo - 1
	return lo


func _sweep_scored() -> bool:
	if _criterion == "mark":
		return int(_pass_stats["marks_correct"]) > 0
	return int(_pass_stats["hits"]) > 0


func _end_pass() -> void:
	_analyse_sweep()
	_dist_entry["sweeps"].append(_pass_stats)
	var hit := _sweep_scored()
	print("bench: [%s] %.0f m  %.2f m/s  %s  marques %d (%d justes, %d fausses)  couverture %.0f %%  tirs %d  touches %d%s" % [
		_heights[_height_index], _distance, _speed, "OK" if hit else "raté", _pass_stats["marks"], _pass_stats["marks_correct"],
		_pass_stats["false_positives"], 100.0 * float(_pass_stats["coverage"]), _pass_stats["shots"], _pass_stats["hits"],
		"  (confirmation)" if _confirm_left > 0 else ""])
	_dir = -_dir
	if _confirm_left > 0:
		_confirm_left -= 1
		_confirm_hits += int(hit)
		if _confirm_left == 0:
			_dist_entry["confirm_hits"] = _confirm_hits
			if _confirm_hits * 2 >= int(config["confirm"]) or _lo <= float(config["speed_min"]):
				_end_distance()
				return
			_hi = _lo
			_lo = 0.0
			for sw in _dist_entry["sweeps"]:
				var ok_sw: bool = (int(sw["marks_correct"]) > 0) if _criterion == "mark" else (int(sw["hits"]) > 0)
				if ok_sw and float(sw["speed"]) < _hi and float(sw["speed"]) > _lo:
					_lo = float(sw["speed"])
			_speed = (_lo + _hi) * 0.5 if _lo > 0.0 else _hi * 0.5
			_confirm_hits = 0
		_next_sweep()
		return
	if hit:
		_lo = _speed
	else:
		_hi = _speed
	if _hi == 0.0:
		if _speed * 2.0 > float(config["speed_max"]):
			_start_confirm()
			return
		_speed *= 2.0
	elif _lo == 0.0:
		if _speed * 0.5 < float(config["speed_min"]):
			_end_distance()
			return
		_speed *= 0.5
	elif (_hi - _lo) <= float(config["precision"]) * _hi:
		_start_confirm()
		return
	else:
		_speed = (_lo + _hi) * 0.5
	_next_sweep()


func _start_confirm() -> void:
	_speed = _lo
	_confirm_left = int(config["confirm"])
	_confirm_hits = 0
	if _confirm_left == 0:
		_end_distance()
	else:
		_next_sweep()


func _next_sweep() -> void:
	_phase = "pause"
	_timer = float(config["pause"])


func _end_distance() -> void:
	_dist_entry["max_speed_hit"] = _lo
	_height_entry["distances"].append(_dist_entry)
	print("bench: [%s] distance %.0f m → vitesse limite %.2f m/s (%d traversées)" % [_heights[_height_index], _distance, _lo, _dist_entry["sweeps"].size()])
	var next_distance := _distance + float(config["distance_step"])
	if _lo > 0.0 and next_distance <= float(config["distance_max"]):
		_distance = next_distance
		_begin_distance()
		_next_sweep()
		return
	result["heights"].append(_height_entry)
	_height_index += 1
	if _height_index < _heights.size():
		_begin_height()
		_next_sweep()
		return
	_finish()


func _finish() -> void:
	result["fps"] = Engine.get_frames_per_second()
	result["finished"] = Time.get_datetime_string_from_system()
	var summary := PackedStringArray()
	for h in result["heights"]:
		var parts := PackedStringArray()
		for d in h["distances"]:
			parts.append("%.0f m : %.2f m/s" % [d["distance"], d["max_speed_hit"]])
		summary.append(("%s → " % h["height"] if result["heights"].size() > 1 else "") + ", ".join(parts))
	result["summary"] = " | ".join(summary)
	stop()
	svg_text = build_svg()
	if out_path != "":
		save_json(out_path)
		var svg_path := out_path.get_basename() + ".svg"
		var fa := FileAccess.open(svg_path, FileAccess.WRITE)
		if fa:
			fa.store_string(svg_text)
			result["svg"] = svg_path
			print("bench: fiche → ", svg_path)
	_emit("benchmark terminé — " + result["summary"])
	print("BENCH DONE ", result["summary"])
	finished.emit(result)


func save_json(path: String) -> bool:
	var dir := path.get_base_dir()
	if dir != "":
		DirAccess.make_dir_recursive_absolute(dir)
	var fa := FileAccess.open(path, FileAccess.WRITE)
	if fa == null:
		push_warning("TriggerBench: cannot write %s" % path)
		return false
	fa.store_string(JSON.stringify(result, "  "))
	print("bench: résultats → ", path)
	return true


## « Fiche » : one bar per sweep across the body width (local x, metres), green = detected,
## red = under the reticle but no correct mark, orange ticks = false positives (path position).
func build_svg() -> String:
	var w := float(config["width"])
	var half := _half_travel if _half_travel > 0.0 else w * 2.0
	var bar_w := 520.0
	var x0 := 330.0
	var row_h := 14.0
	var rows := []
	for h in result["heights"]:
		rows.append(["title", "Hauteur de visée : %s (y = %.2f m)" % [h["height"], h["aim_y"]]])
		for d in h["distances"]:
			rows.append(["title", "   %.0f m — vitesse limite %.2f m/s" % [d["distance"], d["max_speed_hit"]]])
			var sweeps: Array = d["sweeps"].duplicate()
			sweeps.sort_custom(func(a, b): return float(a["speed"]) < float(b["speed"]))
			for sw in sweeps:
				rows.append(["sweep", sw])
	var height := 70.0 + rows.size() * row_h + 30.0
	var out := PackedStringArray()
	out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" font-family="sans-serif" font-size="11">' % [int(x0 + bar_w + 40.0), int(height)])
	out.append('<rect width="100%" height="100%" fill="#14161c"/>')
	out.append('<text x="14" y="22" fill="#eee" font-size="16">AimLabs — benchmark tir auto : %s · arme %s · cible %s · critère %s</text>' % [
		_esc(String(result["algo"])), _esc(String(result["weapon"])), _esc(String(result["target"])), String(result["criterion"])])
	out.append('<text x="14" y="40" fill="#aaa">%s · %s</text>' % [_esc(String(result.get("started", ""))), _esc(String(result.get("summary", "")))])
	out.append('<text x="14" y="58" fill="#aaa">barre = largeur du trajet (± %.1f m autour du réticule) · vert = détecté · rouge = sous le réticule mais raté · orange = fausse marque · gris = cible hors réticule</text>' % half)
	var y := 70.0
	for r in rows:
		if r[0] == "title":
			out.append('<text x="14" y="%.0f" fill="#ddd" font-size="12">%s</text>' % [y + 11.0, _esc(r[1])])
			y += row_h
			continue
		var sw: Dictionary = r[1]
		var ok: bool = (int(sw["marks_correct"]) > 0) if _criterion == "mark" else (int(sw["hits"]) > 0)
		var lat = sw.get("first_mark_latency_ms")
		var label := "%.2f m/s %s%s · marques %d/%d · couv. %.0f %%%s" % [float(sw["speed"]), "OK" if ok else "raté",
			" (conf.)" if sw.get("confirm", false) else "", int(sw["marks_correct"]), int(sw["marks"]), 100.0 * float(sw["coverage"]),
			(" · 1re marque %.0f ms" % float(lat)) if lat != null else ""]
		out.append('<text x="40" y="%.0f" fill="%s">%s</text>' % [y + 10.0, "#9f9" if ok else "#f99", _esc(label)])
		out.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" fill="#333"/>' % [x0, y + 2.0, bar_w, row_h - 4.0])
		var px := func(local_x: float) -> float: return x0 + (clampf(-local_x, -half, half) + half) / (2.0 * half) * bar_w
		for seg in sw.get("missed_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			out.append('<rect x="%.1f" y="%.0f" width="%.1f" height="%.0f" fill="#d33"/>' % [minf(a, b), y + 2.0, maxf(absf(b - a), 1.0), row_h - 4.0])
		for seg in sw.get("detected_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			out.append('<rect x="%.1f" y="%.0f" width="%.1f" height="%.0f" fill="#3c3"/>' % [minf(a, b), y + 2.0, maxf(absf(b - a), 1.0), row_h - 4.0])
		for fx in sw.get("false_positive_path_x", []):
			var a: float = x0 + (clampf(float(fx), -half, half) + half) / (2.0 * half) * bar_w
			out.append('<rect x="%.1f" y="%.0f" width="2" height="%.0f" fill="#fa0"/>' % [a, y, row_h])
		out.append('<line x1="%.0f" y1="%.0f" x2="%.0f" y2="%.0f" stroke="#fff" stroke-width="1"/>' % [x0 + bar_w * 0.5, y, x0 + bar_w * 0.5, y + row_h])
		y += row_h
	out.append('</svg>')
	return "\n".join(out)


static func _esc(t: String) -> String:
	return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


## Show the SVG « fiche » in game (CanvasLayer, closed by show_sheet(false) / a click).
func show_sheet(visible_sheet: bool = true) -> void:
	hide_sheet()
	if not visible_sheet or svg_text == "":
		return
	var img := Image.new()
	if img.load_svg_from_string(svg_text, 1.0) != OK:
		push_warning("TriggerBench: SVG rendering failed")
		return
	_sheet_layer = CanvasLayer.new()
	_sheet_layer.layer = 50
	var bg := ColorRect.new()
	bg.color = Color(0, 0, 0, 0.7)
	bg.set_anchors_preset(Control.PRESET_FULL_RECT)
	bg.gui_input.connect(func(ev: InputEvent) -> void:
		if ev is InputEventMouseButton and ev.pressed:
			hide_sheet())
	_sheet_layer.add_child(bg)
	var scroll := ScrollContainer.new()
	scroll.set_anchors_preset(Control.PRESET_FULL_RECT)
	scroll.offset_left = 20
	scroll.offset_top = 20
	scroll.offset_right = -20
	scroll.offset_bottom = -20
	scroll.mouse_filter = Control.MOUSE_FILTER_PASS
	var tex := TextureRect.new()
	tex.texture = ImageTexture.create_from_image(img)
	tex.stretch_mode = TextureRect.STRETCH_KEEP
	tex.mouse_filter = Control.MOUSE_FILTER_PASS
	scroll.add_child(tex)
	_sheet_layer.add_child(scroll)
	game.add_child(_sheet_layer)


func hide_sheet() -> void:
	if _sheet_layer:
		_sheet_layer.queue_free()
		_sheet_layer = null


func _face_player() -> void:
	var to_player := -_target.position
	to_player.y = 0.0
	if to_player.length_squared() > 1e-6:
		_target.rotation.y = atan2(to_player.x, to_player.z)


func _lock_camera() -> void:
	var cam_pos := _player.camera.global_position
	_player.look_at_point(Vector3(cam_pos.x, _aim_y(), cam_pos.z - _distance))


## Reticle ray → is the target under the reticle, where on its body (local x, y), which zone.
func _ground_truth() -> Array:
	var cam := _player.camera
	var from := cam.global_position
	var to := from - cam.global_transform.basis.z * 500.0
	var query := PhysicsRayQueryParameters3D.create(from, to, RAY_MASK, [_player.get_rid()])
	var hit := _player.get_world_3d().direct_space_state.intersect_ray(query)
	if hit and hit.collider is Node and hit.collider.is_in_group("hitbox") and hit.collider.get_meta("enemy") == _target:
		var local: Vector3 = _target.to_local(hit.position)
		return [true, String(hit.collider.get_meta("zone")), local.x, local.y]
	return [false, "", 0.0, 0.0]


func _physics_process(delta: float) -> void:
	if not running:
		return
	_lock_camera()
	match _phase:
		"settle":
			if bool(config["wait_client"]) and not (game.bridge and game.bridge.get_stats()["client_connected"]):
				_emit("benchmark : en attente de l'algo (client AimBridge)…")
				return
			_timer -= delta
			if _timer <= 0.0:
				_begin_pass()
		"pause":
			_target.position = Vector3(-_half_travel * _dir, 0.0, -_distance)
			_face_player()
			_timer -= delta
			if _timer <= 0.0:
				_begin_pass()
		"cross":
			_x += _speed * delta * _dir
			_target.position = Vector3(_x, 0.0, -_distance)
			_face_player()
			var now := Time.get_ticks_usec()
			var gt := _ground_truth()
			_ticks.append([now, gt[0], gt[1], gt[2], gt[3], _x])
			if bool(config["oracle"]):
				_player.request_fire_from_assist()
				if gt[0]:
					_marks.append([now, now, 0])
			if absf(_x) > _half_travel:
				_end_pass()


func _on_mark(frame_id: int, capture_usec: int, _point_px: Vector2) -> void:
	if running and _phase == "cross":
		_marks.append([Time.get_ticks_usec(), capture_usec, frame_id])


func _on_target_hit(enemy: Node, zone: String, _damage: int, killed: bool) -> void:
	if running and _phase == "cross" and enemy == _target:
		_pass_stats["hits"] += 1
		if zone == "head":
			_pass_stats["headshots"] += 1
		if killed:
			_pass_stats["killed"] = true


func _on_shot_fired(_origin: Vector3, _hit: Vector3, _hit_something: bool) -> void:
	if running and _phase == "cross":
		_pass_stats["shots"] += 1
