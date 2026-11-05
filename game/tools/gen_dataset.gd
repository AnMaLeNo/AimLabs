extends Node
## Detector training data: renders the plan written by tools/detector/gen_plan.py.
##
##   godot --path game --resolution 640x360 res://tools/gen_dataset.tscn -- --plan=<plan.json> --out=<dir> [--start=0] [--count=N]
##
## One plan entry = one square crop of the game screen (the crop the sniper pipeline feeds to the network: the camera
## is the player's one with the field of view narrowed to the crop, so a pixel here is a pixel of the real screen).
## Per sample, written to <dir>/:
##   <id>_c.png   "game" samples: the real game world (sky, grid ground, sun, fog), RGB, no HUD
##   <id>_f.png   all agents on a transparent background (RGBA, lit by the sample's own sky/sun): the pixels of the
##                agents are exactly the alpha channel; "photo" samples are composited over a photograph from it
##   <id>_m<k>.png  one RGBA image per agent, that agent alone (only when the sample has 2+ agents): the visible part
##                of each one is found by subtracting the nearer agents
## The labels are not guessed from a model: they come from the alpha channel of these renders.

const SLOT_BIT0 := 10  # render layer of agent slot k = bit (SLOT_BIT0 + k); layer 1 (bit 0) = everything

var _plan: Array = []
var _out := ""
var _world: World
var _vp: SubViewport
var _cam: Camera3D
var _sun: DirectionalLight3D
var _wenv: WorldEnvironment
var _ground: Node3D
var _ground_mat: ShaderMaterial
var _sky_mat: ProceduralSkyMaterial
var _fg_env: Environment
var _fg_sky: ProceduralSkyMaterial
var _holder: Node3D
var _cache := {}  # "slug|neutral" -> Enemy
var _defaults := {}


func _ready() -> void:
	var args := {}
	for a in OS.get_cmdline_user_args():
		var kv := a.split("=", true, 1)
		args[kv[0]] = kv[1] if kv.size() > 1 else "1"
	if not args.has("--plan") or not args.has("--out"):
		printerr("usage: -- --plan=<plan.json> --out=<dir> [--start=N] [--count=N]")
		get_tree().quit(2)
		return
	_out = String(args["--out"])
	DirAccess.make_dir_recursive_absolute(_out)
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(String(args["--plan"])))
	if not (parsed is Dictionary) or not (parsed as Dictionary).has("samples"):
		printerr("gen_dataset: bad plan file")
		get_tree().quit(2)
		return
	_plan = (parsed as Dictionary)["samples"]
	var start := int(args.get("--start", 0))
	var count := int(args.get("--count", _plan.size() - start))
	_plan = _plan.slice(start, mini(start + count, _plan.size()))
	_build_scene(int((parsed as Dictionary).get("size", 480)))
	_run.call_deferred()


func _build_scene(size: int) -> void:
	_world = World.new()
	_world.name = "World"
	_world.map = "range"  # lighting + sky + grid ground exactly as in the game; the lone target is removed below
	add_child(_world)
	var old := _world.get_node_or_null("Enemies")
	if old:
		old.queue_free()
	_world.enemies.clear()
	_world.target = null
	_world.mover = null
	_sun = _world.get_node("Sun")
	_wenv = _world.get_node("WorldEnvironment")
	_ground = _world.get_node("Ground")
	_ground_mat = (_ground.get_node("GroundMesh") as MeshInstance3D).mesh.material
	_sky_mat = _wenv.environment.sky.sky_material
	_defaults = {
		"sun_rot": _sun.rotation_degrees, "sun_energy": _sun.light_energy, "sun_color": _sun.light_color,
		"sky_top": _sky_mat.sky_top_color, "sky_horizon": _sky_mat.sky_horizon_color,
		"sky_gb": _sky_mat.ground_bottom_color, "sky_gh": _sky_mat.ground_horizon_color,
		"amb_energy": _wenv.environment.ambient_light_energy,
		"fog_color": _wenv.environment.fog_light_color, "fog_density": _wenv.environment.fog_density,
		# ground.gdshader uniform defaults (get_shader_parameter returns null for a parameter never set)
		"g_a": Color(0.20, 0.21, 0.22), "g_b": Color(0.16, 0.17, 0.18), "g_line": Color(0.42, 0.45, 0.50),
		"g_cell": 2.0, "g_strength": 0.55,
	}
	_holder = Node3D.new()
	_holder.name = "Agents"
	_world.add_child(_holder)
	# transparent-background environment for the agents-only renders: nothing is drawn but the agents, lit by a sky
	# (ambient + reflections) that the sample controls, so that a photo background can be matched
	_fg_sky = ProceduralSkyMaterial.new()
	var sky := Sky.new()
	sky.sky_material = _fg_sky
	_fg_env = Environment.new()
	_fg_env.background_mode = Environment.BG_COLOR
	_fg_env.background_color = Color(0, 0, 0, 0)
	_fg_env.sky = sky
	_fg_env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	_fg_env.reflected_light_source = Environment.REFLECTION_SOURCE_SKY
	_fg_env.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	_vp = SubViewport.new()
	_vp.size = Vector2i(size, size)
	_vp.own_world_3d = false
	_vp.world_3d = get_viewport().world_3d
	_vp.msaa_3d = Viewport.MSAA_4X  # as project.godot
	_vp.render_target_update_mode = SubViewport.UPDATE_DISABLED
	add_child(_vp)
	_cam = Camera3D.new()
	_cam.keep_aspect = Camera3D.KEEP_HEIGHT
	_cam.near = 0.02
	_cam.far = 3000.0
	_vp.add_child(_cam)
	_cam.make_current()


func _col(a: Variant, fallback: Color) -> Color:
	if a is Array and (a as Array).size() >= 3:
		return Color(float(a[0]), float(a[1]), float(a[2]))
	return fallback


func _enemy(slug: String, neutral: bool) -> Enemy:
	var key := "%s|%s" % [slug, neutral]
	if not _cache.has(key):
		var e := Enemy.new()
		e.name = "A_" + slug
		e.agent_slug = slug
		e.neutral_pose = neutral
		e.visible = false
		_holder.add_child(e)
		_cache[key] = e
	return _cache[key]


func _apply_game_env(d: Dictionary) -> void:
	var df := _defaults
	_sun.rotation_degrees = Vector3(float(d["sun_elev"]), float(d["sun_az"]), 0.0) if d.has("sun_elev") else df["sun_rot"]
	_sun.light_energy = float(d.get("sun_energy", df["sun_energy"]))
	_sun.light_color = _col(d.get("sun_color"), df["sun_color"])
	_sky_mat.sky_top_color = _col(d.get("sky_top"), df["sky_top"])
	_sky_mat.sky_horizon_color = _col(d.get("sky_horizon"), df["sky_horizon"])
	_sky_mat.ground_bottom_color = _col(d.get("sky_gb"), df["sky_gb"])
	_sky_mat.ground_horizon_color = _col(d.get("sky_gh"), df["sky_gh"])
	_wenv.environment.ambient_light_energy = float(d.get("amb_energy", df["amb_energy"]))
	_wenv.environment.fog_light_color = _col(d.get("fog_color"), df["fog_color"])
	_wenv.environment.fog_density = float(d.get("fog_density", df["fog_density"]))
	_ground_mat.set_shader_parameter("color_a", _col(d.get("g_a"), df["g_a"]))
	_ground_mat.set_shader_parameter("color_b", _col(d.get("g_b"), df["g_b"]))
	_ground_mat.set_shader_parameter("line_color", _col(d.get("g_line"), df["g_line"]))
	_ground_mat.set_shader_parameter("cell_size", float(d.get("g_cell", df["g_cell"])))
	_ground_mat.set_shader_parameter("line_strength", float(d.get("g_strength", df["g_strength"])))


func _apply_fg_env(d: Dictionary) -> void:
	_sun.rotation_degrees = Vector3(float(d.get("sun_elev", -50.0)), float(d.get("sun_az", 155.0)), 0.0)
	_sun.light_energy = float(d.get("sun_energy", 1.1))
	_sun.light_color = _col(d.get("sun_color"), Color(1.0, 0.96, 0.9))
	_fg_sky.sky_top_color = _col(d.get("sky_top"), _defaults["sky_top"])
	_fg_sky.sky_horizon_color = _col(d.get("sky_horizon"), _defaults["sky_horizon"])
	_fg_sky.ground_bottom_color = _col(d.get("sky_gb"), _defaults["sky_gb"])
	_fg_sky.ground_horizon_color = _col(d.get("sky_gh"), _defaults["sky_gh"])
	_fg_env.ambient_light_energy = float(d.get("amb_energy", 0.7))


func _render() -> Image:
	_vp.render_target_update_mode = SubViewport.UPDATE_ONCE
	await RenderingServer.frame_post_draw
	return _vp.get_texture().get_image()


func _save(img: Image, name: String) -> void:
	var err := img.save_png("%s/%s.png" % [_out, name])
	if err != OK:
		printerr("gen_dataset: cannot write %s (%s)" % [name, error_string(err)])


func _run() -> void:
	await get_tree().process_frame
	var t0 := Time.get_ticks_msec()
	var n := 0
	for s: Dictionary in _plan:
		await _do_sample(s)
		n += 1
		if n % 100 == 0:
			var el := (Time.get_ticks_msec() - t0) / 1000.0
			print("gen_dataset: %d/%d  %.1f s  (%.1f samples/s)" % [n, _plan.size(), el, n / maxf(el, 0.001)])
	print("gen_dataset: DONE %d samples in %.1f s" % [n, (Time.get_ticks_msec() - t0) / 1000.0])
	get_tree().quit()


func _do_sample(s: Dictionary) -> void:
	var id := String(s["id"])
	var cam: Dictionary = s["cam"]
	_cam.position = Vector3(0.0, float(cam["eye"]), 0.0)
	_cam.rotation = Vector3(deg_to_rad(float(cam["pitch_deg"])), 0.0, 0.0)
	_cam.fov = float(cam["fov"])
	for e: Enemy in _cache.values():
		e.visible = false
	var placed: Array[Enemy] = []
	var agents: Array = s["agents"]
	for k in agents.size():
		var a: Dictionary = agents[k]
		var e := _enemy(String(a["slug"]), bool(a.get("neutral", true)))
		e.position = Vector3(float(a["x"]), 0.0, float(a["z"]))
		e.rotation_degrees = Vector3(0.0, float(a["rot_deg"]), 0.0)
		e.visible = true
		if e._anim != null and e._idle != "":
			var len := e._anim.current_animation_length
			e._anim.seek(float(a.get("anim_t", 0.0)) * len, true)
		for mi: MeshInstance3D in e._meshes:
			mi.layers = 1 | (1 << (SLOT_BIT0 + k))
		placed.append(e)
	var env: Dictionary = s.get("env", {})
	var mode := String(s["mode"])
	_cam.cull_mask = 1
	if mode == "game":
		_apply_game_env(env)
		_cam.environment = null
		_ground.visible = true
		_vp.transparent_bg = false
		var img := await _render()
		_save(img, id + "_c")
	if not agents.is_empty():
		_apply_fg_env(env)
		_cam.environment = _fg_env
		_ground.visible = false
		_vp.transparent_bg = true
		_save(await _render(), id + "_f")
		if agents.size() > 1:
			for k in agents.size():
				_cam.cull_mask = 1 << (SLOT_BIT0 + k)
				_save(await _render(), "%s_m%d" % [id, k])
			_cam.cull_mask = 1
		_ground.visible = true
	_cam.environment = null
