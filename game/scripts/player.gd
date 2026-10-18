class_name Player
extends CharacterBody3D
## First-person controller: movement, mouse look and hitscan shooting.
##
## Look input is funnelled through [method add_look_delta] with a [enum LookSource]
## so that the game can tell apart the physical mouse from an aim-assist program
## (part 2 of the project: the "AimBridge").

enum LookSource { REAL, ASSIST }

signal shot_fired(origin: Vector3, hit_point: Vector3, hit_something: bool)
signal target_hit(enemy: Node, zone: String, damage: int, killed: bool)
signal mouse_capture_changed(captured: bool)

@export_group("Movement")
@export var walk_speed := 6.0
@export var sprint_speed := 9.5
@export var ground_accel := 12.0
@export var air_accel := 2.5
@export var jump_velocity := 5.0

@export_group("Look")
## Degrees of rotation per pixel of mouse movement (0.022 ≈ CS:GO at sens 1.0).
@export var sensitivity_deg_per_px := 0.07
@export var max_pitch_deg := 89.0

@export_group("Weapon")
@export var rounds_per_minute := 650.0
@export var damage_body := 34
@export var damage_arm := 20
@export var damage_head := 100
@export var max_range := 500.0
@export var recoil_deg_per_shot := 0.35
@export var recoil_recovery_deg_per_s := 6.0

@onready var head: Node3D = $Head
@onready var camera: Camera3D = $Head/Camera3D
@onready var weapon: Node3D = $Head/Camera3D/Weapon
@onready var muzzle: Marker3D = $Head/Camera3D/Weapon/Muzzle
@onready var muzzle_flash: Node3D = $Head/Camera3D/Weapon/Muzzle/Flash

var gravity: float = ProjectSettings.get_setting("physics/3d/default_gravity")
var yaw_deg := 0.0
var pitch_deg := 0.0
var recoil_pending_deg := 0.0
var fire_cooldown := 0.0
var mouse_captured := false

## Pixels of look input received this frame, per source (for the HUD / bridge).
var look_delta_this_frame := { LookSource.REAL: Vector2.ZERO, LookSource.ASSIST: Vector2.ZERO }
var fire_requested_by_assist := false

var _weapon_rest_pos: Vector3
var _bob_time := 0.0
var _flash_timer := 0.0

const HITBOX_MASK := (1 << 2) | (1 << 3)  # enemy_body | enemy_head
const WORLD_MASK := 1 << 0
const RAY_MASK := HITBOX_MASK | WORLD_MASK


func _ready() -> void:
	_weapon_rest_pos = weapon.position
	muzzle_flash.visible = false
	yaw_deg = rad_to_deg(rotation.y)


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and mouse_captured:
		add_look_delta(event.relative, LookSource.REAL)
	elif event.is_action_pressed("toggle_mouse"):
		set_mouse_captured(not mouse_captured)
	elif event is InputEventMouseButton and event.pressed and not mouse_captured:
		set_mouse_captured(true)
		get_viewport().set_input_as_handled()


## Rotate the view by [param delta_px] pixels of mouse movement (x = right, y = down).
func add_look_delta(delta_px: Vector2, source: LookSource) -> void:
	look_delta_this_frame[source] += delta_px
	yaw_deg -= delta_px.x * sensitivity_deg_per_px
	pitch_deg = clampf(pitch_deg - delta_px.y * sensitivity_deg_per_px, -max_pitch_deg, max_pitch_deg)
	yaw_deg = wrapf(yaw_deg, -180.0, 180.0)
	_apply_view_rotation()


## Point the camera at a world position (used by tests / future bridge helpers).
func look_at_point(world_pos: Vector3) -> void:
	var from := camera.global_position
	var dir := (world_pos - from).normalized()
	yaw_deg = rad_to_deg(atan2(-dir.x, -dir.z))
	pitch_deg = rad_to_deg(asin(clampf(dir.y, -1.0, 1.0)))
	_apply_view_rotation()


func _apply_view_rotation() -> void:
	rotation.y = deg_to_rad(yaw_deg)
	head.rotation.x = deg_to_rad(pitch_deg)


func set_mouse_captured(captured: bool) -> void:
	if captured == mouse_captured:
		return
	mouse_captured = captured
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED if captured else Input.MOUSE_MODE_VISIBLE
	mouse_capture_changed.emit(captured)


func _physics_process(delta: float) -> void:
	var input_dir := Vector2.ZERO
	if mouse_captured:
		input_dir = Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	var wish_dir := (transform.basis * Vector3(input_dir.x, 0.0, input_dir.y)).normalized()
	var sprinting := mouse_captured and Input.is_action_pressed("sprint") and input_dir.y < 0.0
	var target_speed := sprint_speed if sprinting else walk_speed

	if not is_on_floor():
		velocity.y -= gravity * delta
	elif mouse_captured and Input.is_action_just_pressed("jump"):
		velocity.y = jump_velocity

	var horizontal := Vector3(velocity.x, 0.0, velocity.z)
	var accel := ground_accel if is_on_floor() else air_accel
	horizontal = horizontal.lerp(wish_dir * target_speed, clampf(accel * delta, 0.0, 1.0))
	velocity.x = horizontal.x
	velocity.z = horizontal.z
	move_and_slide()

	_update_weapon_motion(delta, horizontal.length(), sprinting)


func _process(delta: float) -> void:
	fire_cooldown = maxf(fire_cooldown - delta, 0.0)
	_recover_recoil(delta)

	var wants_fire := (mouse_captured and Input.is_action_pressed("fire")) or fire_requested_by_assist
	fire_requested_by_assist = false
	if wants_fire and fire_cooldown <= 0.0:
		_fire()
		fire_cooldown = 60.0 / rounds_per_minute

	if _flash_timer > 0.0:
		_flash_timer -= delta
		if _flash_timer <= 0.0:
			muzzle_flash.visible = false

	look_delta_this_frame[LookSource.REAL] = Vector2.ZERO
	look_delta_this_frame[LookSource.ASSIST] = Vector2.ZERO


## Request one shot on behalf of an aim-assist program (consumed next frame).
func request_fire_from_assist() -> void:
	fire_requested_by_assist = true


func _fire() -> void:
	var from := camera.global_position
	var dir := -camera.global_transform.basis.z
	var to := from + dir * max_range
	var space := get_world_3d().direct_space_state
	var query := PhysicsRayQueryParameters3D.create(from, to, RAY_MASK, [get_rid()])
	query.collide_with_areas = false
	var result := space.intersect_ray(query)

	var hit_point := to
	var hit_something := false
	if result:
		hit_point = result.position
		hit_something = true
		var collider: Object = result.collider
		if collider is Node and collider.is_in_group("hitbox"):
			var enemy: Node = collider.get_meta("enemy")
			var zone: String = collider.get_meta("zone")
			var dmg := _damage_for_zone(zone)
			var killed: bool = enemy.take_hit(zone, dmg, hit_point)
			target_hit.emit(enemy, zone, dmg, killed)
		else:
			_spawn_impact(hit_point, result.normal)

	shot_fired.emit(muzzle.global_position, hit_point, hit_something)
	_spawn_tracer(muzzle.global_position, hit_point)

	recoil_pending_deg += recoil_deg_per_shot
	pitch_deg = clampf(pitch_deg + recoil_deg_per_shot, -max_pitch_deg, max_pitch_deg)
	_apply_view_rotation()
	weapon.position = _weapon_rest_pos + Vector3(0.0, 0.01, 0.06)
	muzzle_flash.visible = true
	muzzle_flash.rotation.z = randf() * TAU
	_flash_timer = 0.04


func _damage_for_zone(zone: String) -> int:
	match zone:
		"head":
			return damage_head
		"arm":
			return damage_arm
		_:
			return damage_body


func _recover_recoil(delta: float) -> void:
	if recoil_pending_deg <= 0.0:
		return
	var step := minf(recoil_pending_deg, recoil_recovery_deg_per_s * delta)
	recoil_pending_deg -= step
	pitch_deg = clampf(pitch_deg - step, -max_pitch_deg, max_pitch_deg)
	_apply_view_rotation()


func _update_weapon_motion(delta: float, speed: float, sprinting: bool) -> void:
	if is_on_floor() and speed > 0.5:
		_bob_time += delta * (14.0 if sprinting else 10.0)
	var bob_amount := clampf(speed / walk_speed, 0.0, 1.5) * 0.008
	var bob := Vector3(sin(_bob_time) * bob_amount, absf(cos(_bob_time)) * bob_amount, 0.0)
	var real_look: Vector2 = look_delta_this_frame[LookSource.REAL]
	var sway := Vector3(-real_look.x, real_look.y, 0.0) * 0.0004
	var target := _weapon_rest_pos + bob + sway
	weapon.position = weapon.position.lerp(target, clampf(delta * 12.0, 0.0, 1.0))
	var target_fov := 82.0 if sprinting else 75.0
	camera.fov = lerpf(camera.fov, target_fov, clampf(delta * 6.0, 0.0, 1.0))


func _spawn_tracer(from: Vector3, to: Vector3) -> void:
	var length := from.distance_to(to)
	if length < 0.2:
		return
	var mesh := BoxMesh.new()
	mesh.size = Vector3(0.015, 0.015, length)
	var mat := StandardMaterial3D.new()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	mat.albedo_color = Color(1.0, 0.85, 0.4, 0.9)
	mesh.material = mat
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	get_tree().current_scene.add_child(mi)
	mi.global_position = (from + to) * 0.5
	mi.look_at(to, Vector3.UP if absf(((to - from).normalized()).dot(Vector3.UP)) < 0.99 else Vector3.FORWARD)
	var tween := mi.create_tween()
	tween.tween_property(mat, "albedo_color:a", 0.0, 0.08)
	tween.tween_callback(mi.queue_free)


func _spawn_impact(point: Vector3, normal: Vector3) -> void:
	var mesh := SphereMesh.new()
	mesh.radius = 0.04
	mesh.height = 0.08
	mesh.radial_segments = 8
	mesh.rings = 4
	var mat := StandardMaterial3D.new()
	mat.albedo_color = Color(0.1, 0.1, 0.1)
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mesh.material = mat
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	get_tree().current_scene.add_child(mi)
	mi.global_position = point + normal * 0.01
	get_tree().create_timer(8.0).timeout.connect(mi.queue_free)
