class_name AlgoRunner
extends Node
## Launches the player's aim-assist programs ("algos") from inside the game: scans a
## folder for *.py files, runs one at a time with stdout/stderr piped into a log, and
## kills it on stop / switch / quit. The algo talks to the game through the AimBridge
## shared memory like any external program would.

signal changed
signal log_line(text: String)

const MAX_LOG_LINES := 300

var algos_dir := ""
var python_cmd := ""
var entries: Array[Dictionary] = [] ## { "name": String, "path": String, "desc": String }
var current_path := ""
var pid := -1
var exit_code := 0
var log_lines: PackedStringArray = []

var _stdio: FileAccess
var _stderr: FileAccess
var _started_usec := 0


func setup(dir: String, shm_name: String, python: String = "") -> void:
	algos_dir = dir
	python_cmd = python if python != "" else OS.get_environment("AIMLABS_PYTHON")
	if python_cmd == "":
		python_cmd = "python" if OS.get_name() == "Windows" else "python3"
	var sdk_dir := _repo_root().path_join("sdk/python")
	var sep := ";" if OS.get_name() == "Windows" else ":"
	var pythonpath := OS.get_environment("PYTHONPATH")
	OS.set_environment("PYTHONPATH", sdk_dir if pythonpath == "" else sdk_dir + sep + pythonpath)
	OS.set_environment("PYTHONUNBUFFERED", "1")
	OS.set_environment("AIMLABS_SHM", shm_name)
	refresh()


static func _repo_root() -> String:
	if OS.has_feature("editor") or DirAccess.dir_exists_absolute(ProjectSettings.globalize_path("res://").path_join("../sdk")):
		return ProjectSettings.globalize_path("res://").path_join("..").simplify_path()
	return OS.get_executable_path().get_base_dir()


static func default_dir() -> String:
	return _repo_root().path_join("algos")


func refresh() -> void:
	entries.clear()
	var dir := DirAccess.open(algos_dir)
	if dir == null:
		_log("dossier introuvable : %s" % algos_dir)
		changed.emit()
		return
	var names: PackedStringArray = []
	for f in dir.get_files():
		if f.get_extension() == "py" and not f.begins_with("_"):
			names.append(f)
	names.sort()
	for f in names:
		var path := algos_dir.path_join(f)
		entries.append({ "name": f, "path": path, "desc": _first_doc_line(path) })
	changed.emit()


static func _first_doc_line(path: String) -> String:
	var fa := FileAccess.open(path, FileAccess.READ)
	if fa == null:
		return ""
	var text := fa.get_as_text().strip_edges().left(2000)
	for quote in ['"""', "'''"]:
		if text.begins_with(quote):
			var end := text.find(quote, 3)
			var doc := text.substr(3, end - 3 if end > 0 else 200).strip_edges()
			return doc.get_slice("\n", 0).strip_edges()
	return ""


func find_entry(name: String) -> Dictionary:
	for e in entries:
		if e["name"] == name or e["path"] == name:
			return e
	return {}


func is_running() -> bool:
	return pid > 0 and OS.is_process_running(pid)


func current_name() -> String:
	return current_path.get_file()


func start(path: String, extra_args: PackedStringArray = []) -> bool:
	stop()
	current_path = path
	log_lines.clear()
	var argv := PackedStringArray([path])
	argv.append_array(extra_args)
	_log("$ %s %s" % [python_cmd, " ".join(argv)])
	var info := OS.execute_with_pipe(python_cmd, argv, false)
	if info.is_empty():
		_log("impossible de lancer %s (python introuvable ?)" % python_cmd)
		pid = -1
		changed.emit()
		return false
	pid = info["pid"]
	_stdio = info["stdio"]
	_stderr = info["stderr"]
	exit_code = 0
	_started_usec = Time.get_ticks_usec()
	changed.emit()
	return true


func stop() -> void:
	if pid > 0:
		if OS.is_process_running(pid):
			OS.kill(pid)
		_drain_pipes()
		_log("arrêté")
	pid = -1
	_stdio = null
	_stderr = null
	changed.emit()


func restart() -> void:
	if current_path != "":
		start(current_path)


func uptime_sec() -> float:
	return (Time.get_ticks_usec() - _started_usec) / 1e6 if pid > 0 else 0.0


func status_text() -> String:
	if current_path == "":
		return "aucun algo"
	if is_running():
		return "%s  ● en cours (%.0f s)" % [current_name(), uptime_sec()]
	if exit_code != 0:
		return "%s  ✖ terminé, code %d" % [current_name(), exit_code]
	return "%s  ○ arrêté" % current_name()


func _process(_delta: float) -> void:
	if pid <= 0:
		return
	_drain_pipes()
	if not OS.is_process_running(pid):
		exit_code = OS.get_process_exit_code(pid)
		_drain_pipes()
		_log("processus terminé (code %d)" % exit_code)
		pid = -1
		_stdio = null
		_stderr = null
		changed.emit()


func _drain_pipes() -> void:
	for fa in [_stdio, _stderr]:
		if fa == null:
			continue
		for _i in range(200):
			var line: String = fa.get_line()
			if line == "" and fa.get_error() != OK:
				break
			if line == "":
				break
			_log(line)


func _log(text: String) -> void:
	log_lines.append(text)
	if log_lines.size() > MAX_LOG_LINES:
		log_lines = log_lines.slice(log_lines.size() - MAX_LOG_LINES)
	log_line.emit(text)


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST or what == NOTIFICATION_EXIT_TREE:
		stop()
