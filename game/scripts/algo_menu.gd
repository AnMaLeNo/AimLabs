class_name AlgoMenu
extends PanelContainer
## Pause menu (Esc): pick an algo from the algos folder, start / stop / switch it without
## leaving the game, read its output, change the game mode.

var runner: AlgoRunner
var game: Node

var _list: ItemList
var _desc: Label
var _status: Label
var _log: RichTextLabel
var _dir_label: Label
var _start_btn: Button
var _stop_btn: Button
var _mode_btn: OptionButton
var _agent_btn: OptionButton
var _weapon_btn: OptionButton
var _map_btn: OptionButton
var _target_btn: OptionButton
var _pattern_btn: OptionButton
var _size_label: Label
var _size_box: SpinBox
var _speed_box: SpinBox
var _distance_box: SpinBox
var _range_row: HBoxContainer
var _bench_row: HBoxContainer
var _bench_btn: Button
var _bench_label: Label
var _bridge_label: Label
var _log_dirty := true


func setup(p_game: Node, p_runner: AlgoRunner) -> void:
	game = p_game
	runner = p_runner
	_build()
	runner.changed.connect(_refresh)
	runner.log_line.connect(func(_t: String) -> void: _log_dirty = true)
	_refresh()


func _build() -> void:
	mouse_filter = Control.MOUSE_FILTER_STOP
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.09, 0.1, 0.13, 0.96)
	style.border_color = Color(0.35, 0.4, 0.5)
	style.set_border_width_all(1)
	style.set_corner_radius_all(6)
	style.set_content_margin_all(14)
	add_theme_stylebox_override("panel", style)
	var root := VBoxContainer.new()
	root.add_theme_constant_override("separation", 8)
	add_child(root)

	var title := Label.new()
	title.text = "AimLabs — Algos"
	title.add_theme_font_size_override("font_size", 22)
	root.add_child(title)

	var dir_row := HBoxContainer.new()
	root.add_child(dir_row)
	_dir_label = Label.new()
	_dir_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_dir_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_dir_label.add_theme_font_size_override("font_size", 12)
	dir_row.add_child(_dir_label)
	var refresh_btn := Button.new()
	refresh_btn.text = "Rafraîchir"
	refresh_btn.pressed.connect(runner.refresh)
	dir_row.add_child(refresh_btn)
	var open_btn := Button.new()
	open_btn.text = "Dossier"
	open_btn.pressed.connect(func() -> void: OS.shell_show_in_file_manager(runner.algos_dir))
	dir_row.add_child(open_btn)

	var columns := HBoxContainer.new()
	columns.size_flags_vertical = Control.SIZE_EXPAND_FILL
	columns.add_theme_constant_override("separation", 12)
	root.add_child(columns)

	var left := VBoxContainer.new()
	left.custom_minimum_size.x = 260
	left.size_flags_vertical = Control.SIZE_EXPAND_FILL
	columns.add_child(left)
	_list = ItemList.new()
	_list.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_list.item_selected.connect(_on_selected)
	_list.item_activated.connect(func(_i: int) -> void: _start_selected())
	left.add_child(_list)
	_desc = Label.new()
	_desc.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_desc.custom_minimum_size.y = 48
	_desc.add_theme_font_size_override("font_size", 12)
	left.add_child(_desc)
	var btns := HBoxContainer.new()
	left.add_child(btns)
	_start_btn = Button.new()
	_start_btn.text = "Lancer"
	_start_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_start_btn.pressed.connect(_start_selected)
	btns.add_child(_start_btn)
	_stop_btn = Button.new()
	_stop_btn.text = "Arrêter"
	_stop_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_stop_btn.pressed.connect(runner.stop)
	btns.add_child(_stop_btn)

	var right := VBoxContainer.new()
	right.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	right.size_flags_vertical = Control.SIZE_EXPAND_FILL
	columns.add_child(right)
	_status = Label.new()
	_status.add_theme_font_size_override("font_size", 14)
	right.add_child(_status)
	_bridge_label = Label.new()
	_bridge_label.add_theme_font_size_override("font_size", 12)
	_bridge_label.modulate = Color(0.8, 0.8, 0.8)
	right.add_child(_bridge_label)
	_log = RichTextLabel.new()
	_log.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_log.scroll_following = true
	_log.selection_enabled = true
	_log.add_theme_font_size_override("normal_font_size", 12)
	right.add_child(_log)

	var bottom := HBoxContainer.new()
	_build_map_rows(root)
	root.add_child(bottom)
	var agent_label := Label.new()
	agent_label.text = "Personnage :"
	bottom.add_child(agent_label)
	_agent_btn = OptionButton.new()
	var agents := KAAssets.agents()
	for slug in KAAssets.agent_slugs():
		_agent_btn.add_item(String(agents[slug]["name"]))
		_agent_btn.set_item_metadata(_agent_btn.item_count - 1, slug)
		if slug == game.agent_slug:
			_agent_btn.select(_agent_btn.item_count - 1)
	_agent_btn.item_selected.connect(func(i: int) -> void: game.set_agent(String(_agent_btn.get_item_metadata(i))))
	bottom.add_child(_agent_btn)
	var weapon_label := Label.new()
	weapon_label.text = "Arme :"
	bottom.add_child(weapon_label)
	_weapon_btn = OptionButton.new()
	var weapons := KAAssets.weapons()
	var wslugs: Array = weapons.keys()
	wslugs.sort_custom(func(x: String, y: String) -> bool: return String(weapons[x]["name"]) < String(weapons[y]["name"]))
	for slug: String in wslugs:
		if not weapons[slug].has("fp"):
			continue
		var w: Dictionary = weapons[slug]
		var st: Dictionary = w["fp"].get("settings", {})
		_weapon_btn.add_item("%s (%s, %.2f/s)" % [w["name"], "auto" if bool(st.get("AutomaticWeapon", false)) else "semi", float(st.get("FireRate", 0.0))])
		_weapon_btn.set_item_metadata(_weapon_btn.item_count - 1, slug)
		if slug == game.weapon_slug:
			_weapon_btn.select(_weapon_btn.item_count - 1)
	_weapon_btn.item_selected.connect(func(i: int) -> void: game.set_weapon(String(_weapon_btn.get_item_metadata(i))))
	bottom.add_child(_weapon_btn)
	var mode_label := Label.new()
	mode_label.text = "Mode :"
	bottom.add_child(mode_label)
	_mode_btn = OptionButton.new()
	for m in game.MODES.keys():
		_mode_btn.add_item(m)
	_mode_btn.item_selected.connect(func(i: int) -> void: game.set_mode(_mode_btn.get_item_text(i)))
	bottom.add_child(_mode_btn)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bottom.add_child(spacer)
	var hint := Label.new()
	hint.text = "L'algo continue de jouer pendant le menu · Échap ou clic hors du menu : reprendre"
	hint.add_theme_font_size_override("font_size", 12)
	hint.modulate = Color(0.8, 0.8, 0.8)
	bottom.add_child(hint)
	var resume := Button.new()
	resume.text = "Reprendre"
	resume.pressed.connect(func() -> void: game.player.set_mouse_captured(true))
	bottom.add_child(resume)


const PATTERN_LABELS := {"none": "Immobile", "line": "Gauche-droite", "square": "Carré", "circle": "Cercle"}
const SIZE_LABELS := {"none": "Taille (m) :", "line": "Distance du trajet (m) :", "square": "Côté du carré (m) :", "circle": "Rayon du cercle (m) :"}


## Map selector + shooting-range settings (target agent, movement, size, speed, distance).
func _build_map_rows(root: VBoxContainer) -> void:
	var world: World = game.world
	var map_row := HBoxContainer.new()
	map_row.add_theme_constant_override("separation", 8)
	root.add_child(map_row)
	var map_label := Label.new()
	map_label.text = "Map :"
	map_row.add_child(map_label)
	_map_btn = OptionButton.new()
	_map_btn.add_item("Vitrine (tous les agents, toutes les armes)")
	_map_btn.set_item_metadata(0, "showcase")
	_map_btn.add_item("Stand de tir (une cible mobile)")
	_map_btn.set_item_metadata(1, "range")
	_map_btn.select(1 if world.map == "range" else 0)
	_map_btn.item_selected.connect(func(i: int) -> void:
		world.set_map(String(_map_btn.get_item_metadata(i)))
		_range_row.visible = world.map == "range"
		_bench_row.visible = world.map == "range")
	map_row.add_child(_map_btn)

	_range_row = HBoxContainer.new()
	_range_row.add_theme_constant_override("separation", 8)
	_range_row.visible = world.map == "range"
	root.add_child(_range_row)
	var target_label := Label.new()
	target_label.text = "Cible :"
	_range_row.add_child(target_label)
	_target_btn = OptionButton.new()
	var agents := KAAssets.agents()
	for slug in KAAssets.agent_slugs():
		_target_btn.add_item(String(agents[slug]["name"]))
		_target_btn.set_item_metadata(_target_btn.item_count - 1, slug)
		if slug == world.target_slug:
			_target_btn.select(_target_btn.item_count - 1)
	_target_btn.item_selected.connect(func(i: int) -> void: world.set_target_agent(String(_target_btn.get_item_metadata(i))))
	_range_row.add_child(_target_btn)
	var pattern_label := Label.new()
	pattern_label.text = "Mouvement :"
	_range_row.add_child(pattern_label)
	_pattern_btn = OptionButton.new()
	for p in TargetMover.PATTERNS:
		_pattern_btn.add_item(String(PATTERN_LABELS.get(p, p)))
		_pattern_btn.set_item_metadata(_pattern_btn.item_count - 1, p)
		if p == world.pattern:
			_pattern_btn.select(_pattern_btn.item_count - 1)
	_pattern_btn.item_selected.connect(func(i: int) -> void:
		world.set_pattern(String(_pattern_btn.get_item_metadata(i)))
		_size_label.text = String(SIZE_LABELS.get(world.pattern, "Taille (m) :")))
	_range_row.add_child(_pattern_btn)
	_size_label = Label.new()
	_size_label.text = String(SIZE_LABELS.get(world.pattern, "Taille (m) :"))
	_range_row.add_child(_size_label)
	_size_box = _spin(0.5, 30.0, 0.5, world.pattern_size, func(v: float) -> void: world.set_pattern_size(v))
	_range_row.add_child(_size_box)
	var speed_label := Label.new()
	speed_label.text = "Vitesse (m/s) :"
	_range_row.add_child(speed_label)
	_speed_box = _spin(0.0, 20.0, 0.25, world.pattern_speed, func(v: float) -> void: world.set_pattern_speed(v))
	_range_row.add_child(_speed_box)
	var dist_label := Label.new()
	dist_label.text = "Distance (m) :"
	_range_row.add_child(dist_label)
	_distance_box = _spin(2.0, 60.0, 1.0, world.range_distance, func(v: float) -> void: world.set_range_distance(v))
	_range_row.add_child(_distance_box)

	_bench_row = HBoxContainer.new()
	_bench_row.add_theme_constant_override("separation", 8)
	_bench_row.visible = world.map == "range"
	root.add_child(_bench_row)
	_bench_btn = Button.new()
	_bench_btn.text = "Benchmark tir auto"
	_bench_btn.tooltip_text = ("Caméra figée, la cible balaie le réticule sur ± 2 largeurs ; la vitesse limite est trouvée par\n"
		+ "dichotomie (on double tant que ça touche, puis on resserre à 10 %), confirmée, puis la cible recule\n"
		+ "de 5 m. Mesure l'algo en cours (ou toi, à la souris). Résultat : HUD, console, bench_results/*.json.")
	_bench_btn.pressed.connect(func() -> void:
		if game.bench.running:
			game.bench.stop()
		else:
			game.bench.out_path = AlgoRunner._repo_root().path_join("bench_results/trigger_%s_%s.json" % [
				runner.current_name() if runner.is_running() else "human",
				Time.get_datetime_string_from_system().replace(":", "-")])
			if not game.bench.finished.is_connected(_on_bench_finished):
				game.bench.finished.connect(_on_bench_finished)
			game.bench.start({ "settle": 1.0 })
			game.player.set_mouse_captured(true))
	_bench_row.add_child(_bench_btn)
	_bench_label = Label.new()
	_bench_label.text = "Lance d'abord l'algo à mesurer, puis le benchmark."
	_bench_row.add_child(_bench_label)


func _spin(min_v: float, max_v: float, step: float, value: float, on_change: Callable) -> SpinBox:
	var box := SpinBox.new()
	box.min_value = min_v
	box.max_value = max_v
	box.step = step
	box.value = value
	box.custom_minimum_size.x = 90
	box.value_changed.connect(on_change)
	return box


func _on_bench_finished(_r: Dictionary) -> void:
	game.player.set_mouse_captured(false)
	game.bench.show_sheet()


func _selected_entry() -> Dictionary:
	var sel := _list.get_selected_items()
	if sel.is_empty() or sel[0] >= runner.entries.size():
		return {}
	return runner.entries[sel[0]]


func _on_selected(_index: int) -> void:
	var e := _selected_entry()
	_desc.text = String(e.get("desc", ""))
	_update_buttons()


func _start_selected() -> void:
	var e := _selected_entry()
	if not e.is_empty():
		runner.start(e["path"])


func _refresh() -> void:
	var selected_path := String(_selected_entry().get("path", ""))
	if selected_path == "":
		selected_path = runner.current_path
	_dir_label.text = runner.algos_dir
	_dir_label.tooltip_text = runner.algos_dir
	_list.clear()
	for i in runner.entries.size():
		var e: Dictionary = runner.entries[i]
		var running: bool = runner.is_running() and runner.current_path == e["path"]
		_list.add_item(("● " if running else "    ") + e["name"])
		if e["path"] == selected_path:
			_list.select(i)
			_desc.text = e["desc"]
	if runner.entries.is_empty():
		_desc.text = "Aucun .py dans ce dossier."
	_update_buttons()
	_log_dirty = true


func _update_buttons() -> void:
	var e := _selected_entry()
	var running: bool = runner.is_running()
	_start_btn.disabled = e.is_empty()
	_start_btn.text = "Relancer" if running and e.get("path", "") == runner.current_path else ("Basculer" if running else "Lancer")
	_stop_btn.disabled = not running


func _process(_delta: float) -> void:
	if not visible:
		return
	_status.text = runner.status_text()
	if game.bridge:
		var s: Dictionary = game.bridge.get_stats()
		_bridge_label.text = "Pont : %s · %s · %d cmd (%d refusées) · latence %.1f ms" % [
			game.mode_name, "client connecté" if s["client_connected"] else "pas de client",
			s["cmds_applied"], s["cmds_dropped"], s["avg_latency_ms"]]
		var idx := _mode_btn.get_item_index(_mode_btn.get_selected_id())
		if idx < 0 or _mode_btn.get_item_text(idx) != game.mode_name:
			for i in _mode_btn.item_count:
				if _mode_btn.get_item_text(i) == game.mode_name:
					_mode_btn.select(i)
	else:
		_bridge_label.text = "Pont désactivé (--bridge=off)"
	if _log_dirty:
		_log_dirty = false
		_log.text = "\n".join(runner.log_lines)
	if game.bench:
		_bench_btn.text = "Arrêter le benchmark" if game.bench.running else "Benchmark tir auto"
		if game.bench.status_text() != "":
			_bench_label.text = game.bench.status_text()
