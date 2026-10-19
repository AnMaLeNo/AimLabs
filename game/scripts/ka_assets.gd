class_name KAAssets
extends RefCounted
## Access to the Kingdom Archives models fetched by tools/fetch_assets.py
## (game/assets/ka/manifest.json + glb + webp textures). The glb files carry no
## textures: materials are rebuilt here from the manifest exactly like the site's
## viewer does (DF = albedo, NM = normal, ORM = occlusion/roughness/metallic, EM = emission).

const ROOT := "res://assets/ka/"
const MANIFEST := ROOT + "manifest.json"

static var _manifest: Dictionary = {}
static var _manifest_loaded := false
static var _materials: Dictionary = {}  # cache key -> Material


static func available() -> bool:
	return not manifest().is_empty()


static func manifest() -> Dictionary:
	if not _manifest_loaded:
		_manifest_loaded = true
		if FileAccess.file_exists(MANIFEST):
			var txt := FileAccess.get_file_as_string(MANIFEST)
			var parsed: Variant = JSON.parse_string(txt)
			if parsed is Dictionary:
				_manifest = parsed
		if _manifest.is_empty():
			push_warning("KAAssets: %s missing, run `python3 tools/fetch_assets.py`" % MANIFEST)
	return _manifest


static func agents() -> Dictionary:
	return manifest().get("agents", {})


static func agents_fp() -> Dictionary:
	return manifest().get("agents_fp", {})


static func weapons() -> Dictionary:
	return manifest().get("weapons", {})


## Slugs of the agents, sorted by display name.
static func agent_slugs() -> Array[String]:
	var out: Array[String] = []
	var a := agents()
	for s: String in a.keys():
		out.append(s)
	out.sort_custom(func(x: String, y: String) -> bool: return String(a[x]["name"]) < String(a[y]["name"]))
	return out


static func entry_exists(entry: Dictionary) -> bool:
	return entry.has("glb") and ResourceLoader.exists(ROOT + String(entry["glb"]))


## Instantiate a manifest entry (agent, FP arms, weapon…) with its materials applied.
static func instantiate(entry: Dictionary) -> Node3D:
	if not entry_exists(entry):
		push_warning("KAAssets: model %s not imported" % entry.get("glb", "?"))
		return null
	var scene: PackedScene = load(ROOT + String(entry["glb"]))
	var node: Node3D = scene.instantiate()
	apply_materials(node, entry.get("materials", []))
	return node


static func apply_materials(root: Node, mats: Array) -> void:
	var by_name := {}
	for m: Dictionary in mats:
		by_name[String(m["name"])] = m
	for mi: MeshInstance3D in root.find_children("*", "MeshInstance3D", true, false):
		mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON
		for s in mi.mesh.get_surface_count():
			var src := mi.mesh.surface_get_material(s)
			var mat_name := src.resource_name if src else ""
			if by_name.has(mat_name):
				var built := _material_for(by_name[mat_name])
				if built:
					mi.set_surface_override_material(s, built)


static func _tex(path: String) -> Texture2D:
	var full := ROOT + path
	if ResourceLoader.exists(full):
		return load(full)
	return null


static func _material_for(m: Dictionary) -> Material:
	var key := JSON.stringify(m)
	if _materials.has(key):
		return _materials[key]
	var typ := String(m.get("type", "PBR"))
	var mat := StandardMaterial3D.new()
	mat.resource_name = String(m.get("name", ""))
	match typ:
		"Tritium":
			# Glowing sight dots on the weapons.
			var c := Color(0.2, 1.0, 0.4)
			if m.has("Color"):
				var cc: Dictionary = m["Color"]
				c = Color(float(cc.get("R", 0.2)), float(cc.get("G", 1.0)), float(cc.get("B", 0.4)))
			mat.albedo_color = c
			mat.emission_enabled = true
			mat.emission = c
			mat.emission_energy_multiplier = float(m.get("EmissiveIntensity", 2.0))
		"Eye":
			var df := _tex(String(m.get("DF", "")))
			if df:
				mat.albedo_texture = df
			mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR
			mat.alpha_scissor_threshold = 0.5
			mat.roughness = 0.2
		"Disabled", "DisabledOnVFX":
			mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
			mat.albedo_color = Color(1, 1, 1, 0)
			mat.no_depth_test = false
		_:
			if not m.has("DF"):
				return null  # custom shader on the site; keep the glb's plain material
			var df := _tex(String(m["DF"]))
			if df:
				mat.albedo_texture = df
			if m.has("NM"):
				var nm := _tex(String(m["NM"]))
				if nm:
					mat.normal_enabled = true
					mat.normal_texture = nm
			if m.has("ORM"):
				var orm := _tex(String(m["ORM"]))
				if orm:
					mat.ao_enabled = true
					mat.ao_texture = orm
					mat.ao_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_RED
					mat.roughness_texture = orm
					mat.roughness_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_GREEN
					mat.metallic_texture = orm
					mat.metallic_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_BLUE
			mat.metallic = float(m.get("Metallic", 0.0))  # three.js default
			mat.roughness = float(m.get("Roughness", 1.0))
			# three.js: an emissiveMap only glows when an emissive colour is set.
			if m.has("EM") and m.has("Emissive"):
				var em := _tex(String(m["EM"]))
				if em:
					var e: Dictionary = m["Emissive"]
					var c2: Dictionary = e.get("Color", {})
					mat.emission_enabled = true
					mat.emission_texture = em
					mat.emission = Color(float(c2.get("R", 1)), float(c2.get("G", 1)), float(c2.get("B", 1)))
					mat.emission_energy_multiplier = float(e.get("Intensity", 1.0))
			if typ == "AgentGlass" or typ == "TejoArmGlass" or typ == "TranslucentPBR":
				mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
				mat.albedo_color = Color(1, 1, 1, 0.6)
	_materials[key] = mat
	return mat


## Copy the animations of [param src] into [param dst] (library [param lib]), rewriting the
## track paths so they drive [param dst]'s skeleton. Tracks for bones the skeleton does not
## have are dropped. Used for the shared first-person weapon animations (FP_Classic.glb).
static func retarget_animations(src: AnimationPlayer, dst: AnimationPlayer, skeleton: Skeleton3D, lib: String) -> void:
	var skel_path := dst.get_node(dst.root_node).get_path_to(skeleton)
	var library := AnimationLibrary.new()
	for anim_name in src.get_animation_list():
		var a := src.get_animation(anim_name).duplicate() as Animation
		var i := a.get_track_count() - 1
		while i >= 0:
			var p := a.track_get_path(i)
			var bone := p.get_concatenated_subnames()
			if bone == "" or skeleton.find_bone(bone) < 0:
				a.remove_track(i)
			else:
				a.track_set_path(i, NodePath("%s:%s" % [skel_path, bone]))
			i -= 1
		library.add_animation(anim_name, a)
	if dst.has_animation_library(lib):
		dst.remove_animation_library(lib)
	dst.add_animation_library(lib, library)


## Load the AnimationPlayer of an external animation glb (no mesh inside).
static func load_animation_player(glb_rel: String) -> AnimationPlayer:
	var full := ROOT + glb_rel
	if not ResourceLoader.exists(full):
		return null
	var inst: Node = (load(full) as PackedScene).instantiate()
	var players := inst.find_children("*", "AnimationPlayer", true, false)
	if players.is_empty():
		inst.free()
		return null
	var ap: AnimationPlayer = players[0]
	ap.owner = null
	ap.get_parent().remove_child(ap)
	inst.free()
	return ap


## Pick the "character select idle" loop of a third-person agent.
static func pick_idle(ap: AnimationPlayer) -> String:
	var best := ""
	for n in ap.get_animation_list():
		var l := String(n).to_lower()
		if l.begins_with("cs_") and l.contains("idle") and not l.contains("hood") and not l.contains("mask") and not l.contains("fidget") and not l.contains("lookaround") and not l.contains("look_around"):
			if best == "" or l.length() < best.length():
				best = String(n)
	if best == "":
		for n in ap.get_animation_list():
			if String(n).to_lower().contains("idle") or String(n).to_lower().contains("charselect"):
				return String(n)
	return best
