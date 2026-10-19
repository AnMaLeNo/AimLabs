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
	root.add_child(bottom)
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
