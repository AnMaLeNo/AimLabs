class_name Enemy
extends Node3D
## Static humanoid target. Uses Kenney's CC0 "Blocky Characters" (2.7 units tall,
## scaled to ~1.8 m). Hitboxes: head (layer enemy_head) and body/arms (layer enemy_body).

signal damaged(zone: String, damage: int, hp_left: int)
signal died()
signal respawned()

const CHARACTER_SCENES: Array[String] = [
	"res://assets/characters/character-a.glb",
	"res://assets/characters/character-b.glb",
	"res://assets/characters/character-c.glb",
	"res://assets/characters/character-d.glb",
	"res://assets/characters/character-e.glb",
	"res://assets/characters/character-f.glb",
	"res://assets/characters/character-g.glb",
	"res://assets/characters/character-h.glb",
]
const MODEL_SCALE := 1.8 / 2.7
const LAYER_ENEMY_BODY := 1 << 2
const LAYER_ENEMY_HEAD := 1 << 3

@export var max_hp := 100
@export var respawn_delay := 3.0
@export var skin_index := -1  # -1 = random

var hp: int
var alive := true

var _model: Node3D
var _anim: AnimationPlayer
var _meshes: Array[MeshInstance3D] = []
var _hitboxes: Array[StaticBody3D] = []
var _flash_material: StandardMaterial3D
var _flash_tween: Tween


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
	var idx := skin_index if skin_index >= 0 else randi() % CHARACTER_SCENES.size()
	var scene: PackedScene = load(CHARACTER_SCENES[idx % CHARACTER_SCENES.size()])
	_model = scene.instantiate()
	_model.scale = Vector3.ONE * MODEL_SCALE
	add_child(_model)
	for mi in _model.find_children("*", "MeshInstance3D", true, false):
		_meshes.append(mi)
	var players := _model.find_children("*", "AnimationPlayer", true, false)
	if players.size() > 0:
		_anim = players[0]
		for anim_name in ["idle", "static"]:
			if _anim.has_animation(anim_name):
				_anim.get_animation(anim_name).loop_mode = Animation.LOOP_LINEAR
		_play_idle()


func _play_idle() -> void:
	if _anim == null:
		return
	if _anim.has_animation("idle"):
		_anim.play("idle")
		_anim.seek(randf() * _anim.current_animation_length, true)


## Boxes in model space (before MODEL_SCALE), measured from the glb (tools/measure_model.gd).
func _build_hitboxes() -> void:
	_add_hitbox("body", LAYER_ENEMY_BODY, Vector3(-0.4, 0.0, -0.3), Vector3(0.4, 1.9, 0.3))
	_add_hitbox("arm", LAYER_ENEMY_BODY, Vector3(0.4, 0.8, -0.3), Vector3(0.8, 1.9, 0.1))
	_add_hitbox("arm", LAYER_ENEMY_BODY, Vector3(-0.8, 0.8, -0.3), Vector3(-0.4, 1.9, 0.1))
	_add_hitbox("head", LAYER_ENEMY_HEAD, Vector3(-0.4, 1.9, -0.4), Vector3(0.4, 2.7, 0.4))


func _add_hitbox(zone: String, layer: int, bmin: Vector3, bmax: Vector3) -> void:
	var body := StaticBody3D.new()
	body.name = "Hitbox_%s_%d" % [zone, _hitboxes.size()]
	body.collision_layer = layer
	body.collision_mask = 0
	body.add_to_group("hitbox")
	body.set_meta("enemy", self)
	body.set_meta("zone", zone)
	var shape := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = (bmax - bmin) * MODEL_SCALE
	shape.shape = box
	shape.position = (bmin + bmax) * 0.5 * MODEL_SCALE
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
	if _anim and _anim.has_animation("die"):
		_anim.play("die")
	var timer := get_tree().create_timer(respawn_delay)
	timer.timeout.connect(respawn)


func respawn() -> void:
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
	return to_global(Vector3(0.0, 2.3, 0.0) * MODEL_SCALE)


func body_position() -> Vector3:
	return to_global(Vector3(0.0, 1.0, 0.0) * MODEL_SCALE)
