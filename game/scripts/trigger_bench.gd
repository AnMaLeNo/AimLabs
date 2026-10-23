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
	"speed_start": 2.0, "speed_min": 0.25, "speed_max": 150.0, "precision": 0.1, "confirm": 2,
	"width": 1.2, "travel_widths": 2.0, "heights": "chest", "criterion": "auto",
	"mark_tolerance": 0.03, "settle": 3.0, "pause": 0.3, "wait_client": false, "oracle": false,
	"fixed_speed": 0.0, "passes": 20,  # fixed_speed > 0: `passes` sweeps at that single speed (success rate instead of the speed search)
	"sniper": false, "max_shots": 1,  # sniper: one bullet per sweep — success = ≥ 1 hit with ≤ max_shots shots
	"truth": "auto",  # visual (silhouette under the reticle, what a vision algo sees) | hitbox | auto = visual when the probe works
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
var _fixed_hits := 0
var _fixed_done := 0
var _dist_entry: Dictionary = {}
var _phase := ""           # "settle" | "pause" | "cross" | "done"
var _timer := 0.0
var _x := 0.0
var _pass_stats := {}
var _ticks: Array = []     # per sweep: [usec, under(bool), zone, local_x, local_y, path_x]
var _vis_samples: Array = []  # per sweep, one per RENDERED frame: [usec, visible(bool), hitbox(bool), local_x≈-path_x]
const PROBE_LAYER := 1 << 19   # visual layer 20: the target's meshes, rendered alone by the probe
const PROBE_PX := 16
var _probe_vp: SubViewport
var _probe_cam: Camera3D
var _probe_ok := false
var _truth := "hitbox"
var _pending_hit := ""      # zone hit by the shot being resolved (target_hit fires before shot_fired)
var _oracle_fired := false
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
	_setup_probe()
	_truth = String(config["truth"])
	if _truth == "auto":
		_truth = "visual" if _probe_ok else "hitbox"
	elif _truth == "visual" and not _probe_ok:
		push_warning("TriggerBench: visual truth unavailable (headless), falling back to hitbox")
		_truth = "hitbox"
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
	if bool(config["sniper"]):
		_criterion = "hit"
	elif _criterion == "auto":
		_criterion = "mark" if (client or bool(config["oracle"]) or bool(config["wait_client"])) else "hit"
	result = {
		"algo": game.runner.current_name() if game.runner and game.runner.is_running() else ("oracle" if config["oracle"] else "human"),
		"weapon": game.weapon_slug, "agent": game.agent_slug, "target": _world.target_slug,
		"criterion": _criterion, "truth": _truth, "config": config.duplicate(),
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
	if _probe_vp:
		_probe_vp.queue_free()
		_probe_vp = null
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
	_speed = float(config["fixed_speed"]) if float(config["fixed_speed"]) > 0.0 else float(config["speed_start"])
	_fixed_hits = 0
	_fixed_done = 0
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
		"confirm": _confirm_left > 0, "shot_rows": [] }
	_pending_hit = ""
	_oracle_fired = false
	_ticks = []
	_vis_samples = []
	_marks = []
	_phase = "cross"
	var what := "confirmation %d/%d" % [int(config["confirm"]) - _confirm_left + 1, int(config["confirm"])] if _confirm_left > 0 else (
		"ok jusqu'à %.2f, raté dès %.2f" % [_lo, _hi] if _hi > 0.0 else "recherche de la limite")
	_emit("benchmark [%s, %s] : %.0f m, %.2f m/s — %s" % [_heights[_height_index], _criterion, _distance, _speed, what])


## Ground truth + algo marks → segments on the body, correctness, coverage, latency.
## « Visual truth » : a tiny SubViewport whose camera copies the player's camera (narrow FOV =
## the few pixels around the reticle) and renders ONLY the target's meshes on a transparent
## background. Centre pixel opaque ⇔ the target's silhouette is under the reticle. Compared
## with the hitbox ray this measures how far the hitboxes are from what YOLO actually sees.
func _setup_probe() -> void:
	_probe_ok = false
	if DisplayServer.get_name() == "headless":
		return
	for mi in _target._meshes:
		mi.layers |= PROBE_LAYER
	_probe_vp = SubViewport.new()
	_probe_vp.size = Vector2i(PROBE_PX, PROBE_PX)
	_probe_vp.transparent_bg = true
	_probe_vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	_probe_vp.own_world_3d = false
	_probe_vp.world_3d = _player.get_world_3d()
	_probe_cam = Camera3D.new()
	_probe_cam.cull_mask = PROBE_LAYER
	_probe_cam.keep_aspect = Camera3D.KEEP_HEIGHT
	_probe_vp.add_child(_probe_cam)
	add_child(_probe_vp)
	_probe_cam.make_current()
	if not RenderingServer.frame_post_draw.is_connected(_on_frame_drawn):
		RenderingServer.frame_post_draw.connect(_on_frame_drawn)
	_probe_ok = true


## Sampled once per rendered frame (the probe image only changes when a frame is drawn; on a
## slow renderer several physics ticks share one frame, so ticks must not be used here).
func _on_frame_drawn() -> void:
	if running and _phase == "cross" and _probe_ok:
		var vis := _probe_visible()
		_vis_samples.append([Time.get_ticks_usec(), vis, bool(_ground_truth()[0]), -_x])


func _process(delta: float) -> void:
	if running and _phase == "cross":
		# the target moves per RENDERED frame (not per 60 Hz physics tick): at 30 m/s a tick is a
		# 0.5 m jump, the whole silhouette — the limit would measure the tick rate, not the algo
		_x += _speed * delta * _dir
		_target.position = Vector3(_x, 0.0, -_distance)
		_face_player()
	if running and _probe_ok:
		var cam := _player.camera
		_probe_cam.global_transform = cam.global_transform
		_probe_cam.near = cam.near
		_probe_cam.far = cam.far
		var vh := float(_player.get_viewport().get_visible_rect().size.y)
		_probe_cam.fov = rad_to_deg(2.0 * atan(tan(deg_to_rad(cam.fov) * 0.5) * PROBE_PX / vh))


func _probe_visible() -> bool:
	if not _probe_ok:
		return false
	var img := _probe_vp.get_texture().get_image()
	if img == null or img.is_empty():
		return false
	return img.get_pixel(PROBE_PX / 2, PROBE_PX / 2).a > 0.5


## Truth timeline used for scoring: [usec, under(bool), local_x, hitbox(bool), zone, local_y].
## Visual truth → one entry per rendered frame (silhouette under the reticle, local_x ≈ -path_x);
## hitbox truth → one entry per physics tick (reticle ray).
func _events() -> Array:
	var ev := []
	if _truth == "visual":
		for v in _vis_samples:
			ev.append([int(v[0]), bool(v[1]), float(v[3]), bool(v[2]), "", 0.0])
	else:
		for t in _ticks:
			ev.append([int(t[0]), bool(t[1]), float(t[3]) if t[1] else -float(t[5]), bool(t[1]), String(t[2]), float(t[4])])
	return ev


func _nearest_event(ev: Array, usec: int) -> int:
	if ev.is_empty():
		return -1
	var lo := 0
	var hi := ev.size() - 1
	while lo < hi:
		var mid := (lo + hi) >> 1
		if int(ev[mid][0]) < usec:
			lo = mid + 1
		else:
			hi = mid
	if lo > 0 and absi(int(ev[lo - 1][0]) - usec) < absi(int(ev[lo][0]) - usec):
		lo -= 1
	return lo


static func _segments(ev: Array, pred: Callable) -> Array:
	var segs := []
	var cur := []
	for e in ev:
		if pred.call(e):
			if cur.is_empty():
				cur = [e[2], e[2]]
			else:
				cur[1] = e[2]
		elif not cur.is_empty():
			segs.append(cur)
			cur = []
	if not cur.is_empty():
		segs.append(cur)
	return segs


func _analyse_sweep() -> void:
	var tol := int(float(config["mark_tolerance"]) * 1e6)
	var ev := _events()
	var under_n := 0
	var first_under := -1
	for e in ev:
		if e[1]:
			under_n += 1
			if first_under < 0:
				first_under = int(e[0])
	var hitbox_ticks := 0
	for t in _ticks:
		if t[1]:
			hitbox_ticks += 1
	var vis_n := 0
	var vis_only := 0
	var hb_only := 0
	for v in _vis_samples:
		if v[1]:
			vis_n += 1
		if v[1] and not v[2]:
			vis_only += 1
		if v[2] and not v[1]:
			hb_only += 1
	var detected := {}  # event index → true
	var correct := 0
	var false_pos := 0
	var false_on_visual := 0
	var first_correct := -1
	var mark_rows := []
	for m in _marks:
		var cap := int(m[1])
		var when := cap if cap >= 0 else int(m[0])
		var idx := _nearest_event(ev, when)
		var ok: bool = idx >= 0 and bool(ev[idx][1]) and absi(int(ev[idx][0]) - when) <= tol
		var ti := _nearest_tick(when)
		var on_hb: bool = ti >= 0 and bool(_ticks[ti][1])
		var on_vis: bool = not on_hb and _visible_at(when)
		if ok:
			correct += 1
			detected[idx] = true
			if first_correct < 0 or int(m[0]) < first_correct:
				first_correct = int(m[0])
		else:
			false_pos += 1
			if on_vis:
				false_on_visual += 1
		mark_rows.append({ "frame_id": m[2], "capture_usec": cap, "recv_usec": m[0], "correct": ok, "on_visual_only": on_vis, "on_hitbox": on_hb,
			"local_x": ev[idx][2] if idx >= 0 else 0.0, "local_y": (_ticks[ti][4] if ti >= 0 else 0.0),
			"path_x": -float(ev[idx][2]) if idx >= 0 else 0.0 })
	# a detected event = under the reticle and a correct mark within `tol` of it
	var det_flags := []
	det_flags.resize(ev.size())
	det_flags.fill(false)
	for i in detected:
		var t0 := int(ev[i][0])
		var j: int = i
		while j >= 0 and absi(int(ev[j][0]) - t0) <= tol and ev[j][1]:
			det_flags[j] = true
			j -= 1
		j = i + 1
		while j < ev.size() and absi(int(ev[j][0]) - t0) <= tol and ev[j][1]:
			det_flags[j] = true
			j += 1
	var det_n := 0
	var tagged := []
	for i in ev.size():
		tagged.append([ev[i][0], ev[i][1], ev[i][2], det_flags[i]])
		if det_flags[i]:
			det_n += 1
	_pass_stats["truth"] = _truth
	_pass_stats["under_ticks"] = under_n          # truth samples with the target under the reticle
	_pass_stats["ticks"] = ev.size()
	_pass_stats["hitbox_ticks"] = hitbox_ticks
	_pass_stats["hitbox_segments"] = _segments(_events_hitbox(), func(e): return e[1])
	_pass_stats["visual_probe"] = _probe_ok
	_pass_stats["visual_samples"] = _vis_samples.size()
	_pass_stats["visual_samples_visible"] = vis_n
	_pass_stats["visual_samples_visible_only"] = vis_only
	_pass_stats["visual_samples_hitbox_only"] = hb_only
	_pass_stats["visual_segments"] = _vis_segments()
	_pass_stats["false_positives_on_visual"] = false_on_visual
	_pass_stats["marks"] = _marks.size()
	_pass_stats["marks_correct"] = correct
	_pass_stats["false_positives"] = false_pos
	_pass_stats["coverage"] = float(det_n) / under_n if under_n > 0 else 0.0
	_pass_stats["first_mark_latency_ms"] = (first_correct - first_under) / 1000.0 if first_correct >= 0 and first_under >= 0 else null
	_pass_stats["detected_segments"] = _segments(tagged, func(e): return e[1] and e[3])
	_pass_stats["missed_segments"] = _segments(tagged, func(e): return e[1] and not e[3])
	_pass_stats["mark_rows"] = mark_rows
	_pass_stats["false_positive_path_x"] = []
	for r in mark_rows:
		if not r["correct"]:
			_pass_stats["false_positive_path_x"].append(r["path_x"])


func _events_hitbox() -> Array:
	var ev := []
	for t in _ticks:
		ev.append([int(t[0]), bool(t[1]), float(t[3]) if t[1] else -float(t[5])])
	return ev


func _vis_segments() -> Array:
	var ev := []
	for v in _vis_samples:
		ev.append([int(v[0]), bool(v[1]), float(v[3])])
	return _segments(ev, func(e): return e[1])


## Visible-sample nearest in time to `usec` (within one physics tick).
func _visible_at(usec: int) -> bool:
	var best_vis := false
	var best_d := 1 << 40
	for v in _vis_samples:
		var d := absi(int(v[0]) - usec)
		if d < best_d:
			best_d = d
			best_vis = bool(v[1])
	return best_vis and best_d <= 20000


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
	if bool(config["sniper"]):
		return int(_pass_stats["hits"]) > 0 and int(_pass_stats["shots"]) <= int(config["max_shots"])
	return int(_pass_stats["hits"]) > 0


func _sweep_ok(sw: Dictionary) -> bool:
	if _criterion == "mark":
		return int(sw["marks_correct"]) > 0
	if bool(config["sniper"]):
		return int(sw["hits"]) > 0 and int(sw["shots"]) <= int(config["max_shots"])
	return int(sw["hits"]) > 0


func _end_pass() -> void:
	_analyse_sweep()
	if bool(config["sniper"]):
		var shots: int = _pass_stats["shots"]
		var hits: int = _pass_stats["hits"]
		_pass_stats["outcome"] = "no_shot" if shots == 0 else ("extra_shots" if shots > int(config["max_shots"]) else ("hit" if hits > 0 else "miss"))
		var first_under := -1
		for e in _events():
			if e[1]:
				first_under = int(e[0])
				break
		var rows: Array = _pass_stats["shot_rows"]
		_pass_stats["first_shot_delay_ms"] = (int(rows[0]["usec"]) - first_under) / 1000.0 if rows.size() > 0 and first_under >= 0 else null
	_dist_entry["sweeps"].append(_pass_stats)
	var hit := _sweep_scored()
	print("bench: [%s] %.0f m  %.2f m/s  %s  marques %d (%d justes, %d fausses)  couverture %.0f %%  tirs %d  touches %d%s%s" % [
		_heights[_height_index], _distance, _speed, "OK" if hit else "raté", _pass_stats["marks"], _pass_stats["marks_correct"],
		_pass_stats["false_positives"], 100.0 * float(_pass_stats["coverage"]), _pass_stats["shots"], _pass_stats["hits"],
		("  sniper: " + String(_pass_stats["outcome"])) if bool(config["sniper"]) else "",
		"  (confirmation)" if _confirm_left > 0 else ""])
	_dir = -_dir
	if float(config["fixed_speed"]) > 0.0:
		_fixed_hits += int(hit)
		_fixed_done += 1
		if _fixed_done >= int(config["passes"]):
			_lo = _speed if _fixed_hits > 0 else 0.0
			print("bench: vitesse fixe %.2f m/s → %d/%d réussites" % [_speed, _fixed_hits, _fixed_done])
			_end_distance()
		else:
			_next_sweep()
		return
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
				var ok_sw: bool = _sweep_ok(sw)
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


func _sniper_summary(dist_entry: Dictionary) -> Dictionary:
	var out := { "hit": 0, "miss": 0, "no_shot": 0, "extra_shots": 0 }
	var delays := []
	for sw in dist_entry["sweeps"]:
		if sw.has("outcome"):
			out[sw["outcome"]] += 1
		if sw.get("first_shot_delay_ms") != null:
			delays.append(float(sw["first_shot_delay_ms"]))
	delays.sort()
	out["first_shot_delay_ms_median"] = delays[delays.size() / 2] if delays.size() > 0 else null
	return out


func _end_distance() -> void:
	if bool(config["sniper"]):
		_dist_entry["sniper"] = _sniper_summary(_dist_entry)
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


## « Fiche » : one row per sweep (distance, speed, direction), a bar = the body sweep across the
## reticle in metres as seen by the player; green = detected, red = under the reticle but no
## correct mark, orange = false mark (claimed while the target was not under the reticle).
func build_svg() -> String:
	var w := float(config["width"])
	var half := _half_travel if _half_travel > 0.0 else w * 2.0
	var bar_w := 520.0
	var x0 := 560.0
	var row_h := 16.0
	var rows := []
	for h in result["heights"]:
		rows.append(["title", "Hauteur de visée : %s (réticule à y = %.2f m du sol)" % [h["height"], h["aim_y"]]])
		for d in h["distances"]:
			rows.append(["dist", "Distance %.0f m — vitesse limite trouvée : %.2f m/s%s" % [d["distance"], d["max_speed_hit"], _sniper_dist_label(d)], d])
			var sweeps: Array = d["sweeps"].duplicate()
			sweeps.sort_custom(func(a, b): return float(a["speed"]) < float(b["speed"]))
			for sw in sweeps:
				rows.append(["sweep", sw, d])
			rows.append(["axis"])
	var total_w := x0 + bar_w + 40.0
	var out := PackedStringArray()
	out.append("")  # <svg …> written at the end, once the height is known
	out.append('<rect width="100%" height="100%" fill="#14161c"/>')
	out.append('<text x="14" y="22" fill="#eee" font-size="16">AimLabs — benchmark tir auto : %s · arme %s · cible %s · critère %s · vérité %s</text>' % [
		_esc(String(result["algo"])), _esc(String(result["weapon"])), _esc(String(result["target"])), String(result["criterion"]),
		"silhouette visible" if String(result.get("truth", "")) == "visual" else "hitbox"])
	out.append('<text x="14" y="40" fill="#aaa">%s · résumé : %s</text>' % [_esc(String(result.get("started", ""))), _esc(String(result.get("summary", "")))])
	out.append('<text x="14" y="62" fill="#ddd">Chaque ligne = une traversée : distance · vitesse · sens (→ gauche→droite, ← droite→gauche, vu par le joueur) · résultat.</text>')
	out.append('<text x="14" y="78" fill="#ddd">La barre = le trajet de la cible (± %.1f m de part et d\'autre du réticule, échelle en mètres sous chaque groupe) ; le trait blanc = le réticule.</text>' % half)
	var ly := 96.0
	for item in [["#3c3", "vert : la cible était sous le réticule et l\'algo l\'a marquée (détectée)"],
			["#d33", "rouge : la cible était sous le réticule mais aucune marque (ratée)"],
			["#fa0", "orange : marque envoyée alors que la cible n\'était pas sous le réticule (fausse marque)"],
			["#333", "gris : cible hors du réticule, rien à détecter"],
			["#4af", "bleu (filet au-dessus de la barre) : silhouette visible de la cible sous le réticule (vérité visuelle = ce qu'un algo de vision peut voir)"],
			["#c6f", "violet (filet sous la barre) : hitbox de la cible sous le réticule (rayon physique ; l'écart avec le bleu = défaut de hitbox)"],
			["#0ff", "triangle cyan : balle tirée qui a touché (position de la cible à l'instant du tir) ; magenta : balle ratée"]]:
		out.append('<rect x="14" y="%.0f" width="22" height="10" fill="%s"/>' % [ly - 9.0, item[0]])
		out.append('<text x="42" y="%.0f" fill="#ccc">%s</text>' % [ly, item[1]])
		ly += 14.0
	var y := ly + 10.0
	var px := func(local_x: float) -> float: return x0 + (clampf(-local_x, -half, half) + half) / (2.0 * half) * bar_w
	for r in rows:
		if r[0] == "title":
			out.append('<text x="14" y="%.0f" fill="#fff" font-size="13" font-weight="bold">%s</text>' % [y + 12.0, _esc(r[1])])
			y += row_h + 4.0
			continue
		if r[0] == "dist":
			out.append('<text x="24" y="%.0f" fill="#ddd" font-size="12" font-weight="bold">%s</text>' % [y + 12.0, _esc(r[1])])
			y += row_h
			continue
		if r[0] == "axis":
			var step := 0.5 if half <= 3.0 else 1.0
			var m := -floorf(half / step) * step
			while m <= half + 1e-6:
				var ax: float = x0 + (m + half) / (2.0 * half) * bar_w
				out.append('<line x1="%.1f" y1="%.0f" x2="%.1f" y2="%.0f" stroke="#888"/>' % [ax, y, ax, y + 4.0])
				out.append('<text x="%.1f" y="%.0f" fill="#aaa" font-size="9" text-anchor="middle">%s</text>' % [ax, y + 14.0, "réticule" if absf(m) < 1e-6 else "%+.1f m" % m])
				m += step
			out.append('<text x="%.0f" y="%.0f" fill="#aaa" font-size="9">← à gauche du réticule (vu par le joueur)</text>' % [x0, y + 26.0])
			out.append('<text x="%.0f" y="%.0f" fill="#aaa" font-size="9" text-anchor="end">à droite du réticule →</text>' % [x0 + bar_w, y + 26.0])
			y += row_h + 22.0
			continue
		var sw: Dictionary = r[1]
		var d: Dictionary = r[2]
		var ok: bool = _sweep_ok(sw)
		var lat = sw.get("first_mark_latency_ms")
		var arrow := "→ gauche→droite" if float(sw.get("dir", 1.0)) > 0.0 else "← droite→gauche"
		var label := "%.0f m · %5.2f m/s · %s · %s%s · marques %d justes / %d · couverture %.0f %%%s%s" % [
			float(d["distance"]), float(sw["speed"]), arrow, "OK" if ok else "raté",
			" (confirmation)" if sw.get("confirm", false) else "", int(sw["marks_correct"]), int(sw["marks"]),
			100.0 * float(sw["coverage"]), (" · 1re marque %.0f ms" % float(lat)) if lat != null else "",
			_shot_label(sw)]
		out.append('<text x="40" y="%.0f" fill="%s">%s</text>' % [y + 11.0, "#9f9" if ok else "#f99", _esc(label)])
		out.append('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" fill="#333"/>' % [x0, y + 2.0, bar_w, row_h - 4.0])
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
		for seg in sw.get("visual_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			out.append('<rect x="%.1f" y="%.0f" width="%.1f" height="2" fill="#4af"/>' % [minf(a, b), y, maxf(absf(b - a), 1.0)])
		for seg in sw.get("hitbox_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			out.append('<rect x="%.1f" y="%.0f" width="%.1f" height="2" fill="#c6f"/>' % [minf(a, b), y + row_h - 2.0, maxf(absf(b - a), 1.0)])
		for sh in sw.get("shot_rows", []):
			var a: float = x0 + (clampf(float(sh["path_x"]), -half, half) + half) / (2.0 * half) * bar_w
			out.append('<polygon points="%.1f,%.0f %.1f,%.0f %.1f,%.0f" fill="%s"/>' % [a, y + 2.0, a - 5.0, y + row_h - 2.0, a + 5.0, y + row_h - 2.0, "#0ff" if sh["hit"] else "#f0f"])
		out.append('<line x1="%.0f" y1="%.0f" x2="%.0f" y2="%.0f" stroke="#fff" stroke-width="1"/>' % [x0 + bar_w * 0.5, y, x0 + bar_w * 0.5, y + row_h])
		var ax0 := x0 + bar_w + 6.0
		out.append('<text x="%.0f" y="%.0f" fill="#ccc" font-size="12">%s</text>' % [ax0, y + 12.0, "→" if float(sw.get("dir", 1.0)) > 0.0 else "←"])
		y += row_h
	out.append('</svg>')
	out[0] = '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" font-family="sans-serif" font-size="11">' % [int(total_w), int(y + 20.0)]
	return "\n".join(out)


func _sniper_dist_label(d: Dictionary) -> String:
	if not d.has("sniper"):
		return ""
	var sn: Dictionary = d["sniper"]
	var med = sn.get("first_shot_delay_ms_median")
	return " — sniper : %d touchés, %d ratés, %d sans tir, %d trop de balles%s" % [int(sn["hit"]), int(sn["miss"]), int(sn["no_shot"]), int(sn["extra_shots"]),
		(" · délai médian %.0f ms" % float(med)) if med != null else ""]


func _shot_label(sw: Dictionary) -> String:
	if not sw.has("outcome"):
		return ""
	var d = sw.get("first_shot_delay_ms")
	var names := { "hit": "TOUCHÉ", "miss": "RATÉ", "no_shot": "pas tiré", "extra_shots": "trop de balles (%d)" % int(sw["shots"]) }
	return " · sniper : %s%s" % [names[sw["outcome"]], (" à %.0f ms" % float(d)) if d != null else ""]


static func _esc(t: String) -> String:
	return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


## Bar of one sweep drawn with Godot (the in-game SVG renderer has no text support).
class SheetBar extends Control:
	var half := 2.4
	var sweep: Dictionary = {}
	func _draw() -> void:
		var w := size.x
		var h := size.y
		draw_rect(Rect2(0, 2, w, h - 4), Color("#333"))
		var px := func(local_x: float) -> float: return (clampf(-local_x, -half, half) + half) / (2.0 * half) * w
		for seg in sweep.get("missed_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			draw_rect(Rect2(minf(a, b), 2, maxf(absf(b - a), 1.0), h - 4), Color("#d33"))
		for seg in sweep.get("detected_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			draw_rect(Rect2(minf(a, b), 2, maxf(absf(b - a), 1.0), h - 4), Color("#3c3"))
		for fx in sweep.get("false_positive_path_x", []):
			var a: float = (clampf(float(fx), -half, half) + half) / (2.0 * half) * w
			draw_rect(Rect2(a, 0, 2, h), Color("#fa0"))
		for seg in sweep.get("visual_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			draw_rect(Rect2(minf(a, b), 0, maxf(absf(b - a), 1.0), 2), Color("#4af"))
		for seg in sweep.get("hitbox_segments", []):
			var a: float = px.call(float(seg[0]))
			var b: float = px.call(float(seg[1]))
			draw_rect(Rect2(minf(a, b), h - 2, maxf(absf(b - a), 1.0), 2), Color("#c6f"))
		for sh in sweep.get("shot_rows", []):
			var a: float = (clampf(float(sh["path_x"]), -half, half) + half) / (2.0 * half) * w
			var col := Color("#0ff") if sh["hit"] else Color("#f0f")
			draw_colored_polygon(PackedVector2Array([Vector2(a, 2), Vector2(a - 5, h - 2), Vector2(a + 5, h - 2)]), col)
		draw_line(Vector2(w * 0.5, 0), Vector2(w * 0.5, h), Color.WHITE, 1.0)


func _label(text: String, color: Color = Color("#ddd"), size_px: int = 13, bold: bool = false) -> Label:
	var l := Label.new()
	l.text = text
	l.add_theme_color_override("font_color", color)
	l.add_theme_font_size_override("font_size", size_px)
	if bold:
		l.add_theme_color_override("font_color", Color.WHITE)
	return l


## Show the « fiche » in game (CanvasLayer with real Labels + drawn bars; click to close).
func show_sheet(visible_sheet: bool = true) -> void:
	hide_sheet()
	if not visible_sheet or result.is_empty():
		return
	var half := _half_travel if _half_travel > 0.0 else float(config["width"]) * float(config["travel_widths"])
	_sheet_layer = CanvasLayer.new()
	_sheet_layer.layer = 50
	var bg := ColorRect.new()
	bg.color = Color(0.08, 0.09, 0.11, 0.97)
	bg.set_anchors_preset(Control.PRESET_FULL_RECT)
	_sheet_layer.add_child(bg)
	var scroll := ScrollContainer.new()
	scroll.set_anchors_preset(Control.PRESET_FULL_RECT)
	scroll.offset_left = 16
	scroll.offset_top = 12
	scroll.offset_right = -16
	scroll.offset_bottom = -12
	_sheet_layer.add_child(scroll)
	var root := VBoxContainer.new()
	root.add_theme_constant_override("separation", 3)
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(root)
	root.add_child(_label("Benchmark tir auto : %s · arme %s · cible %s · critère %s · vérité %s" % [result["algo"], result["weapon"], result["target"], result["criterion"], "silhouette visible" if result.get("truth", "") == "visual" else "hitbox"], Color.WHITE, 18))
	root.add_child(_label("%s · résumé : %s" % [result.get("started", ""), result.get("summary", "")], Color("#aaa"), 12))
	root.add_child(_label("Chaque ligne = une traversée : distance · vitesse · sens (→ gauche→droite, ← droite→gauche, vu par le joueur) · résultat. La barre = le trajet de la cible (± %.1f m autour du réticule), le trait blanc = le réticule." % half, Color("#ccc"), 12))
	for item in [["#3c3", "vert : cible sous le réticule et marquée par l'algo (détectée)"], ["#d33", "rouge : cible sous le réticule mais aucune marque (ratée)"],
			["#fa0", "orange : marque envoyée alors que la cible n'était pas sous le réticule (fausse marque)"], ["#333", "gris : cible hors du réticule"],
			["#4af", "bleu (filet au-dessus de la barre) : silhouette visible de la cible sous le réticule (vérité visuelle = ce qu'un algo de vision peut voir)"],
			["#c6f", "violet (filet sous la barre) : hitbox de la cible sous le réticule (rayon physique ; l'écart avec le bleu = défaut de hitbox)"],
			["#0ff", "triangle cyan : balle tirée qui a touché (position de la cible à l'instant du tir) ; triangle magenta : balle ratée"]]:
		var row := HBoxContainer.new()
		var sw := ColorRect.new()
		sw.color = Color(item[0])
		sw.custom_minimum_size = Vector2(22, 12)
		row.add_child(sw)
		row.add_child(_label(item[1], Color("#ccc"), 12))
		root.add_child(row)
	var close := Button.new()
	close.text = "Fermer la fiche (clic)   —   fichier : %s" % (out_path.get_basename() + ".svg" if out_path != "" else "non enregistré")
	close.pressed.connect(hide_sheet)
	root.add_child(close)
	for h in result["heights"]:
		root.add_child(_label("Hauteur de visée : %s (réticule à y = %.2f m du sol)" % [h["height"], h["aim_y"]], Color.WHITE, 14, true))
		for d in h["distances"]:
			root.add_child(_label("   Distance %.0f m — vitesse limite trouvée : %.2f m/s%s" % [d["distance"], d["max_speed_hit"], _sniper_dist_label(d)], Color("#eee"), 13, true))
			var sweeps: Array = d["sweeps"].duplicate()
			sweeps.sort_custom(func(a, b): return float(a["speed"]) < float(b["speed"]))
			for sw in sweeps:
				var ok: bool = _sweep_ok(sw)
				var lat = sw.get("first_mark_latency_ms")
				var arrow := "→ gauche→droite" if float(sw.get("dir", 1.0)) > 0.0 else "← droite→gauche"
				var row := HBoxContainer.new()
				row.add_theme_constant_override("separation", 10)
				var text := "%.0f m · %5.2f m/s · %s · %s%s · marques %d justes / %d · couverture %.0f %%%s%s" % [
					float(d["distance"]), float(sw["speed"]), arrow, "OK" if ok else "raté", " (conf.)" if sw.get("confirm", false) else "",
					int(sw["marks_correct"]), int(sw["marks"]), 100.0 * float(sw["coverage"]), (" · 1re marque %.0f ms" % float(lat)) if lat != null else "",
					_shot_label(sw)]
				var l := _label(text, Color("#9f9") if ok else Color("#f99"), 12)
				l.custom_minimum_size.x = 620
				row.add_child(l)
				var bar := SheetBar.new()
				bar.half = half
				bar.sweep = sw
				bar.custom_minimum_size = Vector2(480, 16)
				row.add_child(bar)
				row.add_child(_label("→" if float(sw.get("dir", 1.0)) > 0.0 else "←", Color("#ccc"), 14))
				root.add_child(row)
			var axis := HBoxContainer.new()
			axis.add_theme_constant_override("separation", 10)
			var spacer := Control.new()
			spacer.custom_minimum_size.x = 620
			axis.add_child(spacer)
			axis.add_child(_label("← à gauche du réticule : −%.1f m  ·  trait blanc = réticule (0)  ·  +%.1f m : à droite →" % [half, half], Color("#aaa"), 11))
			root.add_child(axis)
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
			# sniper: the weapon must be ready again (Operator: 1.67 s between shots) before the next sweep
			if _timer <= 0.0 and not (bool(config["sniper"]) and _player.fire_cooldown > 0.0):
				_begin_pass()
		"cross":
			var now := Time.get_ticks_usec()
			var gt := _ground_truth()
			_ticks.append([now, gt[0], gt[1], gt[2], gt[3], _x])
			if bool(config["oracle"]):
				var under_now: bool = _visible_at(now) if _truth == "visual" else gt[0]
				if bool(config["sniper"]):
					if under_now and not _oracle_fired:
						_player.request_fire_from_assist()
						_oracle_fired = true
				else:
					_player.request_fire_from_assist()
				if under_now:
					_marks.append([now, now, 0])
			if absf(_x) > _half_travel:
				_end_pass()


func _on_mark(frame_id: int, capture_usec: int, _point_px: Vector2) -> void:
	if running and _phase == "cross":
		_marks.append([Time.get_ticks_usec(), capture_usec, frame_id])


func _on_target_hit(enemy: Node, zone: String, _damage: int, killed: bool) -> void:
	if running and _phase == "cross" and enemy == _target:
		_pass_stats["hits"] += 1
		_pending_hit = zone
		if zone == "head":
			_pass_stats["headshots"] += 1
		if killed:
			_pass_stats["killed"] = true


func _on_shot_fired(_origin: Vector3, _hit: Vector3, _hit_something: bool) -> void:
	if running and _phase == "cross":
		_pass_stats["shots"] += 1
		var now := Time.get_ticks_usec()
		var gt := _ground_truth()
		_pass_stats["shot_rows"].append({ "usec": now, "hit": _pending_hit != "", "zone": _pending_hit,
			"path_x": _x, "local_x": gt[2] if gt[0] else -_x, "visible": _visible_at(now) })
		_pending_hit = ""
