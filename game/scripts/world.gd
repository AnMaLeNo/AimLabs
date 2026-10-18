class_name World
extends Node3D
## Procedural sandbox: a quasi-infinite ground, scattered blocks and walls,
## and a set of static humanoid targets. Deterministic for a given seed.

signal targets_rebuilt(enemies: Array)

@export var world_seed := 42
@export var arena_half_size := 90.0
@export var spawn_clear_radius := 7.0
@export var block_count := 55
@export var wall_count := 14
@export var enemy_count := 24
@export var near_targets := 5  # a row of close targets in front of the spawn

const LAYER_WORLD := 1 << 0
const GROUND_SIZE := 6000.0
const BLOCK_COLORS: Array[Color] = [
	Color(0.55, 0.12, 0.10), Color(0.08, 0.25, 0.55), Color(0.80, 0.50, 0.08),
	Color(0.10, 0.38, 0.16), Color(0.28, 0.14, 0.50), Color(0.70, 0.70, 0.66),
	Color(0.05, 0.42, 0.45), Color(0.45, 0.26, 0.12),
]

var enemies: Array[Enemy] = []
var _rng := RandomNumberGenerator.new()
var _obstacles: Array[AABB] = []  # world-space boxes, used to keep targets out of blocks


func _ready() -> void:
	_rng.seed = world_seed
	_build_lighting()
	_build_ground()
	_build_obstacles()
	_build_enemies()


func _build_lighting() -> void:
	var sun := DirectionalLight3D.new()
	sun.name = "Sun"
	sun.rotation_degrees = Vector3(-48.0, 35.0, 0.0)
	sun.light_energy = 1.0
	sun.light_color = Color(1.0, 0.96, 0.9)
	sun.shadow_enabled = true
	sun.directional_shadow_mode = DirectionalLight3D.SHADOW_PARALLEL_4_SPLITS
	sun.directional_shadow_max_distance = 160.0
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
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color(0.62, 0.66, 0.72)
	env.ambient_light_energy = 0.45
	env.reflected_light_source = Environment.REFLECTION_SOURCE_DISABLED
	env.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	env.fog_enabled = true
	env.fog_light_color = Color(0.50, 0.60, 0.76)
	env.fog_density = 0.0016
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


func _build_obstacles() -> void:
	var holder := Node3D.new()
	holder.name = "Obstacles"
	add_child(holder)
	for i in block_count:
		var size := Vector3(_rng.randf_range(1.0, 6.0), _rng.randf_range(1.0, 5.0), _rng.randf_range(1.0, 6.0))
		var pos := _random_free_position(maxf(size.x, size.z))
		_add_box(holder, "Block%d" % i, pos, size, _rng.randf() * TAU if _rng.randf() < 0.3 else 0.0)
	for i in wall_count:
		var length := _rng.randf_range(8.0, 24.0)
		var size := Vector3(length, _rng.randf_range(2.5, 4.5), 0.6)
		var pos := _random_free_position(length * 0.5)
		_add_box(holder, "Wall%d" % i, pos, size, _rng.randf() * TAU)


func _add_box(parent: Node, box_name: String, pos: Vector3, size: Vector3, yaw: float) -> void:
	var body := StaticBody3D.new()
	body.name = box_name
	body.collision_layer = LAYER_WORLD
	body.collision_mask = 0
	body.position = Vector3(pos.x, size.y * 0.5, pos.z)
	body.rotation.y = yaw
	var shape := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = size
	shape.shape = box
	body.add_child(shape)
	var mesh := BoxMesh.new()
	mesh.size = size
	var mat := StandardMaterial3D.new()
	mat.albedo_color = BLOCK_COLORS[_rng.randi() % BLOCK_COLORS.size()]
	mat.roughness = 0.85
	mesh.material = mat
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	body.add_child(mi)
	parent.add_child(body)
	var radius := maxf(size.x, size.z) * 0.75  # conservative bound for rotated boxes
	_obstacles.append(AABB(Vector3(pos.x - radius, 0.0, pos.z - radius), Vector3(radius * 2.0, size.y, radius * 2.0)))


func _random_free_position(footprint: float) -> Vector3:
	for _attempt in 40:
		var p := Vector3(_rng.randf_range(-arena_half_size, arena_half_size), 0.0, _rng.randf_range(-arena_half_size, arena_half_size))
		if Vector2(p.x, p.z).length() < spawn_clear_radius + footprint:
			continue
		if not _overlaps_obstacle(p, footprint):
			return p
	return Vector3(arena_half_size, 0.0, arena_half_size)


func _overlaps_obstacle(p: Vector3, margin: float) -> bool:
	for box in _obstacles:
		if box.grow(margin).has_point(Vector3(p.x, 0.5, p.z)):
			return true
	return false


func _build_enemies() -> void:
	var holder := Node3D.new()
	holder.name = "Enemies"
	add_child(holder)
	for i in near_targets:
		var x := (i - (near_targets - 1) * 0.5) * 3.0
		_add_enemy(holder, Vector3(x, 0.0, -14.0), 0.0, i)
	for i in enemy_count:
		var p := _random_free_position(1.0)
		var yaw := _rng.randf() * TAU
		if _rng.randf() < 0.5:
			yaw = atan2(p.x, p.z) + PI  # face the spawn point
		_add_enemy(holder, p, yaw, -1)
	targets_rebuilt.emit(enemies)


func _add_enemy(parent: Node, pos: Vector3, yaw: float, skin: int) -> void:
	var enemy := Enemy.new()
	enemy.name = "Enemy%d" % enemies.size()
	enemy.position = pos
	enemy.rotation.y = yaw
	enemy.skin_index = skin
	parent.add_child(enemy)
	enemies.append(enemy)


func respawn_all() -> void:
	for e in enemies:
		e.respawn()
