class_name Enemy
extends Node3D
## Static humanoid target: a Kingdom Archives agent (third-person model, character-select
## idle loop). Hitboxes are capsules built from the skeleton (head / torso / arms / legs):
## head on layer enemy_head, everything else on enemy_body.

signal damaged(zone: String, damage: int, hp_left: int)
signal died()
signal respawned()

const LAYER_ENEMY_BODY := 1 << 2
const LAYER_ENEMY_HEAD := 1 << 3
## Bone segments -> (zone, radius). Bone names are shared by all 29 agents.
const SEGMENTS := [
	["Pelvis", "Neck", "body", 0.17],
	["L_Shoulder", "L_Elbow", "arm", 0.07],
	["L_Elbow", "L_Hand", "arm", 0.06],
	["R_Shoulder", "R_Elbow", "arm", 0.07],
	["R_Elbow", "R_Hand", "arm", 0.06],
	["Pelvis", "L_Knee", "body", 0.10],
	["L_Knee", "L_Foot", "body", 0.08],
	["Pelvis", "R_Knee", "body", 0.10],
	["R_Knee", "R_Foot", "body", 0.08],
]
const HEAD_RADIUS := 0.12

@export var agent_slug := ""
@export var max_hp := 100
@export var respawn_delay := 3.0
## Shooting range: neutral A-pose (the site's plain "Idle" clip) instead of the
## character-select idle; the site has no walk/run clips.
@export var neutral_pose := false

var hp: int
var alive := true
var display_name := ""

var _model: Node3D
var _skeleton: Skeleton3D
var _anim: AnimationPlayer
var _idle := ""
var _meshes: Array[MeshInstance3D] = []
var _hitboxes: Array[StaticBody3D] = []
var _flash_material: StandardMaterial3D
var _flash_tween: Tween
var _death_tween: Tween
var _head_local := Vector3(0, 1.7, 0)
var _chest_local := Vector3(0, 1.2, 0)


func _ready() -> void:
	hp = max_hp
	_build_model()
	_build_hitboxes()
	_flash_material = StandardMaterial3D.new()
	_flash_material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	_flash_material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	_flash_material.albedo_color = Color(1.0, 0.15, 0.1, 0.0)
	for m in _meshes:
		m.material_overlay = _flash_material


func _build_model() -> void:
	var entry: Dictionary = KAAssets.agents().get(agent_slug, {})
	display_name = String(entry.get("name", agent_slug))
	_model = KAAssets.instantiate(entry) if not entry.is_empty() else null
	if _model == null:
		_model = _placeholder()
	add_child(_model)
	for mi in _model.find_children("*", "MeshInstance3D", true, false):
		_meshes.append(mi)
	var skels := _model.find_children("*", "Skeleton3D", true, false)
	if skels.size() > 0:
		_skeleton = skels[0]
	var players := _model.find_children("*", "AnimationPlayer", true, false)
	if players.size() > 0:
		_anim = players[0]
		_idle = "Idle" if neutral_pose and _anim.has_animation("Idle") else ("" if neutral_pose else KAAssets.pick_idle(_anim))
		if _idle != "":
			_anim.get_animation(_idle).loop_mode = Animation.LOOP_LINEAR
		_play_idle()


func _placeholder() -> Node3D:
	var mi := MeshInstance3D.new()
	var capsule := CapsuleMesh.new()
	capsule.radius = 0.3
	capsule.height = 1.8
	mi.mesh = capsule
	mi.position.y = 0.9
	var root := Node3D.new()
	root.add_child(mi)
	return root


func _play_idle() -> void:
	if _anim == null or _idle == "":
		return
	_anim.play(_idle)
	_anim.seek(randf() * _anim.current_animation_length, true)


## Capsules between skeleton bones, measured on the rest pose (the idle barely moves).
func _build_hitboxes() -> void:
	if _skeleton == null:
		_add_capsule("body", LAYER_ENEMY_BODY, Vector3(0, 0.1, 0), Vector3(0, 1.5, 0), 0.25)
		_add_capsule("head", LAYER_ENEMY_HEAD, Vector3(0, 1.55, 0), Vector3(0, 1.8, 0), HEAD_RADIUS)
		return
	var skel_xf := _model.global_transform.affine_inverse() * _skeleton.global_transform
	for seg: Array in SEGMENTS:
		var a: Variant = _bone_pos(String(seg[0]), skel_xf)
		var b: Variant = _bone_pos(String(seg[1]), skel_xf)
		if a == null or b == null:
			continue
		_add_capsule(String(seg[2]), LAYER_ENEMY_BODY, a, b, float(seg[3]))
	# hips: the leg capsules start at the Pelvis bone (~1.23 m) and leave the coat/hips uncovered
	# (bench visual probe: silhouette 0.65 m wide at 1.0 m, hitbox 0.3 m) → one capsule as wide as the coat
	var pelvis: Variant = _bone_pos("Pelvis", skel_xf)
	if pelvis != null:
		_add_capsule("body", LAYER_ENEMY_BODY, pelvis, pelvis - Vector3(0, 0.3, 0), 0.3)
	var head: Variant = _bone_pos("Head", skel_xf)
	if head != null:
		var top: Vector3 = head + Vector3(0, 0.16, 0)
		_add_capsule("head", LAYER_ENEMY_HEAD, head - Vector3(0, 0.02, 0), top, HEAD_RADIUS)
		_head_local = head + Vector3(0, 0.03, 0)  # eye level; +0.07 grazed the top of the skull (bench, visual probe)
	var chest: Variant = _bone_pos("Spine3", skel_xf)
	if chest != null:
		_chest_local = chest


func _bone_pos(bone: String, skel_xf: Transform3D) -> Variant:
	var i := _skeleton.find_bone(bone)
	if i < 0:
		return null
	return skel_xf * _skeleton.get_bone_global_rest(i).origin


func _add_capsule(zone: String, layer: int, a: Vector3, b: Vector3, radius: float) -> void:
	var body := StaticBody3D.new()
	body.name = "Hitbox_%s_%d" % [zone, _hitboxes.size()]
	body.collision_layer = layer
	body.collision_mask = 0
	body.add_to_group("hitbox")
	body.set_meta("enemy", self)
	body.set_meta("zone", zone)
	var shape := CollisionShape3D.new()
	var capsule := CapsuleShape3D.new()
	capsule.radius = radius
	capsule.height = maxf(a.distance_to(b) + radius * 2.0, radius * 2.0)
	shape.shape = capsule
	var dir := (b - a).normalized()
	var xf := Transform3D.IDENTITY
	xf.origin = (a + b) * 0.5
	if dir.length() > 0.5:
		var up := Vector3.FORWARD if absf(dir.dot(Vector3.UP)) > 0.99 else Vector3.UP
		var x := up.cross(dir).normalized()
		xf.basis = Basis(x, dir, x.cross(dir).normalized())
	shape.transform = xf
	body.add_child(shape)
	add_child(body)
	_hitboxes.append(body)


## Apply damage. Returns true if this hit killed the target.
func take_hit(zone: String, damage: int, _point: Vector3) -> bool:
	if not alive:
		return false
	hp = maxi(hp - damage, 0)
	damaged.emit(zone, damage, hp)
	_flash()
	if hp == 0:
		_die()
		return true
	return false


func _flash() -> void:
	if _flash_tween:
		_flash_tween.kill()
	_flash_material.albedo_color.a = 0.65
	_flash_tween = create_tween()
	_flash_tween.tween_property(_flash_material, "albedo_color:a", 0.0, 0.18)


func _die() -> void:
	alive = false
	_set_hitboxes_enabled(false)
	died.emit()
	if _anim:
		_anim.pause()
	if _death_tween:
		_death_tween.kill()
	_death_tween = create_tween().set_parallel(true)
	_death_tween.tween_property(_model, "rotation:x", -PI * 0.5, 0.35).set_ease(Tween.EASE_IN)
	_death_tween.tween_property(_model, "position:y", -0.6, 2.0).set_delay(1.0)
	get_tree().create_timer(respawn_delay).timeout.connect(respawn)


func respawn() -> void:
	if _death_tween:
		_death_tween.kill()
	_model.rotation = Vector3.ZERO
	_model.position = Vector3.ZERO
	hp = max_hp
	alive = true
	_set_hitboxes_enabled(true)
	_play_idle()
	respawned.emit()


func _set_hitboxes_enabled(enabled: bool) -> void:
	for body in _hitboxes:
		var zone: String = body.get_meta("zone")
		body.collision_layer = (LAYER_ENEMY_HEAD if zone == "head" else LAYER_ENEMY_BODY) if enabled else 0


## World-space centre of the head hitbox (handy for tests and aim tools).
func head_position() -> Vector3:
	return to_global(_head_local)


func body_position() -> Vector3:
	return to_global(_chest_local)
