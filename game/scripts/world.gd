class_name World
extends Node3D
## Flat world with two maps:
##   showcase : every Kingdom Archives agent standing in a line facing the spawn, every
##              weapon laid on the ground in front of them;
##   range    : shooting range — one chosen agent (neutral pose) moving along a chosen
##              pattern (line / square / circle, see TargetMover) to test aim algorithms.

signal targets_rebuilt(enemies: Array)
signal map_changed(map: String)

const MAPS: Array[String] = ["showcase", "range"]

@export var line_distance := 12.0  # metres in front of the spawn
@export var agent_spacing := 2.0
@export var weapons_distance := 6.0
@export var weapon_spacing := 1.1

const LAYER_WORLD := 1 << 0
const GROUND_SIZE := 6000.0

var enemies: Array[Enemy] = []
var assets_missing := false

var map := "showcase"
# Shooting range settings (applied live through the setters below).
var range_distance := 10.0   # metres between the spawn and the pattern centre
var target_slug := "jett"
var pattern := "line"
var pattern_size := 4.0
var pattern_speed := 2.0
var target: Enemy
var mover: TargetMover


func _ready() -> void:
	_build_lighting()
	_build_ground()
	_build_map()


func set_map(name: String) -> bool:
	if not MAPS.has(name):
		push_warning("Unknown map '%s' (maps: %s)" % [name, ", ".join(MAPS)])
		return false
	if name == map and not enemies.is_empty():
		return true
	map = name
	_build_map()
	return true


func set_target_agent(slug: String) -> bool:
	if not KAAssets.agents().has(slug):
		push_warning("Unknown agent '%s'" % slug)
		return false
	target_slug = slug
	if map == "range":
		_build_map()
	return true


func set_pattern(p: String) -> bool:
	if not TargetMover.PATTERNS.has(p):
		push_warning("Unknown pattern '%s' (patterns: %s)" % [p, ", ".join(TargetMover.PATTERNS)])
		return false
	pattern = p
	if mover:
		mover.configure(pattern, pattern_size, pattern_speed)
	return true


func set_pattern_size(v: float) -> void:
	pattern_size = maxf(v, 0.0)
	if mover:
		mover.configure(pattern, pattern_size, pattern_speed)


func set_pattern_speed(v: float) -> void:
	pattern_speed = maxf(v, 0.0)
	if mover:
		mover.configure(pattern, pattern_size, pattern_speed)


func set_range_distance(v: float) -> void:
	range_distance = maxf(v, 1.0)
	if mover:
		mover.recentre(Vector3(0.0, 0.0, -range_distance))


func _build_map() -> void:
	for n in ["Enemies", "Weapons"]:
		var old := get_node_or_null(n)
		if old:
			remove_child(old)
			old.queue_free()
	enemies.clear()
	target = null
	mover = null
	match map:
		"range":
			_build_range()
		_:
			_build_enemies()
			_build_weapons()
	map_changed.emit(map)


## One agent in a neutral pose, moving along the chosen pattern and always facing the spawn.
func _build_range() -> void:
	var holder := Node3D.new()
	holder.name = "Enemies"
	add_child(holder)
	assets_missing = KAAssets.agent_slugs().is_empty()
	target = Enemy.new()
	target.name = "Target_%s" % target_slug
	target.agent_slug = target_slug
	target.neutral_pose = true
	target.position = Vector3(0.0, 0.0, -range_distance)
	mover = TargetMover.new()
	mover.name = "Mover"
	mover.face_point = Vector3.ZERO
	target.add_child(mover)
	holder.add_child(target)
	mover.configure(pattern, pattern_size, pattern_speed)
	enemies.append(target)
	targets_rebuilt.emit(enemies)


func _build_lighting() -> void:
	var sun := DirectionalLight3D.new()
	sun.name = "Sun"
	sun.rotation_degrees = Vector3(-52.0, 155.0, 0.0)  # light comes from behind the player
	sun.light_energy = 1.1
	sun.light_color = Color(1.0, 0.96, 0.9)
	sun.shadow_enabled = true
	# Lights are culled by Camera3D.cull_mask too: the viewmodel camera only sees
	# Viewmodel.RENDER_LAYER, so the sun must be on that layer or the arms get sky ambient only.
	sun.layers = 1 | Viewmodel.RENDER_LAYER
	sun.directional_shadow_mode = DirectionalLight3D.SHADOW_PARALLEL_4_SPLITS
	sun.directional_shadow_max_distance = 120.0
	add_child(sun)

	var sky_mat := ProceduralSkyMaterial.new()
	sky_mat.sky_top_color = Color(0.10, 0.22, 0.52)
	sky_mat.sky_horizon_color = Color(0.50, 0.60, 0.76)
	sky_mat.ground_bottom_color = Color(0.22, 0.24, 0.26)
	sky_mat.ground_horizon_color = Color(0.60, 0.64, 0.68)
	var sky := Sky.new()
	sky.sky_material = sky_mat
	var env := Environment.new()
	env.background_mode = Environment.BG_SKY
	env.sky = sky
	env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	env.ambient_light_sky_contribution = 1.0
	env.ambient_light_energy = 0.7
	env.reflected_light_source = Environment.REFLECTION_SOURCE_SKY
	env.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	env.fog_enabled = true
	env.fog_light_color = Color(0.50, 0.60, 0.76)
	env.fog_density = 0.0012
	env.fog_sky_affect = 0.2
	var we := WorldEnvironment.new()
	we.name = "WorldEnvironment"
	we.environment = env
	add_child(we)


func _build_ground() -> void:
	var ground := StaticBody3D.new()
	ground.name = "Ground"
	ground.collision_layer = LAYER_WORLD
	ground.collision_mask = 0
	var shape := CollisionShape3D.new()
	shape.shape = WorldBoundaryShape3D.new()  # infinite plane y = 0
	ground.add_child(shape)
	var mesh := PlaneMesh.new()
	mesh.size = Vector2(GROUND_SIZE, GROUND_SIZE)
	mesh.subdivide_depth = 16
	mesh.subdivide_width = 16
	var mat := ShaderMaterial.new()
	mat.shader = load("res://scripts/ground.gdshader")
	mesh.material = mat
	var mi := MeshInstance3D.new()
	mi.name = "GroundMesh"
	mi.mesh = mesh
	mi.extra_cull_margin = 16384.0
	ground.add_child(mi)
	add_child(ground)


func _build_enemies() -> void:
	var holder := Node3D.new()
	holder.name = "Enemies"
	add_child(holder)
	var slugs := KAAssets.agent_slugs()
	assets_missing = slugs.is_empty()
	if assets_missing:
		slugs = ["?", "?", "?", "?", "?"]  # placeholders until the assets are fetched
	for i in slugs.size():
		var x := (i - (slugs.size() - 1) * 0.5) * agent_spacing
		var enemy := Enemy.new()
		enemy.name = "Enemy_%s" % slugs[i]
		enemy.agent_slug = slugs[i]
		enemy.position = Vector3(x, 0.0, -line_distance)
		# The models face +Z (the viewer looks at them from +Z), i.e. towards the spawn.
		holder.add_child(enemy)
		enemies.append(enemy)
	targets_rebuilt.emit(enemies)


## Every weapon of the site, lying flat on the ground in a row, grip towards the player.
func _build_weapons() -> void:
	var holder := Node3D.new()
	holder.name = "Weapons"
	add_child(holder)
	var weapons := KAAssets.weapons()
	var slugs: Array = weapons.keys()
	slugs.sort()
	for i in slugs.size():
		var entry: Dictionary = weapons[slugs[i]]
		var model := KAAssets.instantiate(entry)
		if model == null:
			continue
		var pivot := Node3D.new()
		pivot.name = "Weapon_%s" % slugs[i]
		pivot.position = Vector3((i - (slugs.size() - 1) * 0.5) * weapon_spacing, 0.0, -weapons_distance)
		holder.add_child(pivot)
		pivot.add_child(model)
		# Barrel along +X in the glb; lay the weapon on its side, barrel pointing away.
		model.rotation_degrees = Vector3(-90.0, 90.0, 0.0)
		var aabb := _model_aabb(model)
		model.position.y = -aabb.position.y + 0.005
		_strip_physics(model)


func _model_aabb(model: Node3D) -> AABB:
	var out := AABB()
	var first := true
	for mi: MeshInstance3D in model.find_children("*", "MeshInstance3D", true, false):
		var xf := model.global_transform.affine_inverse() * mi.global_transform
		var box := xf * mi.get_aabb()
		out = box if first else out.merge(box)
		first = false
	return out


func _strip_physics(model: Node3D) -> void:
	for ap: AnimationPlayer in model.find_children("*", "AnimationPlayer", true, false):
		ap.stop()


func respawn_all() -> void:
	for e in enemies:
		e.respawn()
