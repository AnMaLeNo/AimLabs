class_name World
extends Node3D
## Flat test range: an infinite ground, every Kingdom Archives agent standing in a line
## facing the spawn, and every weapon laid on the ground in front of them.

signal targets_rebuilt(enemies: Array)

@export var line_distance := 12.0  # metres in front of the spawn
@export var agent_spacing := 2.0
@export var weapons_distance := 6.0
@export var weapon_spacing := 1.1

const LAYER_WORLD := 1 << 0
const GROUND_SIZE := 6000.0

var enemies: Array[Enemy] = []
var assets_missing := false


func _ready() -> void:
	_build_lighting()
	_build_ground()
	_build_enemies()
	_build_weapons()


func _build_lighting() -> void:
	var sun := DirectionalLight3D.new()
	sun.name = "Sun"
	sun.rotation_degrees = Vector3(-52.0, 155.0, 0.0)  # light comes from behind the player
	sun.light_energy = 1.1
	sun.light_color = Color(1.0, 0.96, 0.9)
	sun.shadow_enabled = true
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
