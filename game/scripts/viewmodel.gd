class_name Viewmodel
extends Node3D
## First-person view rebuilt from the Kingdom Archives viewer: the agent's FP arms rig,
## the held weapon on the R_WeaponMaster bone (rotated 90° Y like the site), the weapon's
## own skeleton animated by GN_<weapon>.glb, the arms by FP_<weapon>.glb, every clip
## retimed to the site's SequenceLength. The rig is re-anchored every frame so that its
## "Camera" bone coincides with the player camera (the viewer parents the camera to that bone).

signal weapon_changed(slug: String, name: String)

const WEAPON_BONE := "R_WeaponMaster"
const CAMERA_BONE := "Camera"
## Objects drawn by the dedicated viewmodel camera only.
const RENDER_LAYER := 1 << 1

var agent_slug := ""
var weapon_slug := ""
var fire_rate := 6.75          # shots per second (site FireRate)
var automatic := false         # site AutomaticWeapon
var reload_length := 1.75
var equip_length := 0.75

var _camera: Camera3D
var _rig: Node3D
var _skeleton: Skeleton3D
var _anim: AnimationPlayer
var _weapon: Node3D
var _weapon_skel: Skeleton3D
var _weapon_anim: AnimationPlayer
var _weapon_attach: BoneAttachment3D
var _muzzle: Node3D
var _cam_bone := -1
var _cam_rest := Transform3D.IDENTITY
var _fp_anims := {}  # Translatable (Fire/Equip/Reload/Inspect/Idle) -> {name, seq, weapon_anim}
var _busy_until := 0.0
var _idle_name := ""


func setup(camera: Camera3D) -> void:
	_camera = camera


func _process(_delta: float) -> void:
	if _rig == null or _skeleton == null or _cam_bone < 0 or _camera == null:
		return
	# Viewer: camera is a child of the Camera bone, looking along +X at load time.
	var bone_now := _skeleton.get_bone_global_pose(_cam_bone)
	var look_x := Transform3D(Basis(Vector3.UP, -PI * 0.5), _cam_rest.origin)
	_rig.global_transform = _camera.global_transform * look_x.affine_inverse() * _cam_rest * bone_now.affine_inverse()
	if _idle_name != "" and _anim and not _anim.is_playing():
		_anim.play(_idle_name)
	if _weapon_anim and not _weapon_anim.is_playing() and _weapon_anim.has_animation("Idle"):
		_weapon_anim.play("Idle")


func set_agent(slug: String) -> bool:
	var entry: Dictionary = KAAssets.agents_fp().get(slug + "-fp", {})
	if entry.is_empty():
		push_warning("Viewmodel: no first-person model for '%s'" % slug)
		return false
	var rig := KAAssets.instantiate(entry)
	if rig == null:
		return false
	if _rig:
		_rig.queue_free()
	agent_slug = slug
	_rig = rig
	add_child(_rig)
	_skeleton = _rig.find_children("*", "Skeleton3D", true, false)[0]
	_anim = _ensure_player(_rig)
	_cam_bone = _skeleton.find_bone(CAMERA_BONE)
	_cam_rest = _skeleton.get_bone_global_rest(_cam_bone) if _cam_bone >= 0 else Transform3D.IDENTITY
	for mi: MeshInstance3D in _rig.find_children("*", "MeshInstance3D", true, false):
		mi.layers = RENDER_LAYER
		mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		mi.extra_cull_margin = 4.0
	_weapon = null
	_weapon_attach = null
	if weapon_slug != "":
		set_weapon(weapon_slug)
	return true


func set_weapon(slug: String) -> bool:
	var w: Dictionary = KAAssets.weapons().get(slug, {})
	var fp: Dictionary = w.get("fp", {})
	if w.is_empty() or fp.is_empty():
		push_warning("Viewmodel: no first-person data for weapon '%s'" % slug)
		return false
	if _rig == null or _skeleton == null:
		weapon_slug = slug
		return true
	var model := KAAssets.instantiate(fp if fp.has("glb") else w)
	if model == null:
		return false
	if _weapon_attach:
		_weapon_attach.queue_free()
	weapon_slug = slug
	var st: Dictionary = fp.get("settings", {})
	fire_rate = float(st.get("FireRate", 6.75))
	automatic = bool(st.get("AutomaticWeapon", false))
	reload_length = float(st.get("ReloadLength", 1.75))
	equip_length = float(st.get("EquipLength", 0.75))

	_weapon_attach = BoneAttachment3D.new()
	_weapon_attach.name = "WeaponAttach"
	_skeleton.add_child(_weapon_attach)
	_weapon_attach.bone_name = WEAPON_BONE
	_weapon = model
	_weapon_attach.add_child(_weapon)
	var fp_st: Variant = st.get("FP")
	var fprot: Dictionary = {"X": 0, "Y": 90, "Z": 0}
	if fp_st is Dictionary and fp_st.has("Rotation"):
		fprot = fp_st["Rotation"]
	# three.js Euler XYZ (intrinsic) == Godot EULER_ORDER_XYZ.
	_weapon.basis = Basis.from_euler(Vector3(deg_to_rad(float(fprot.get("X", 0))), deg_to_rad(float(fprot.get("Y", 90))), deg_to_rad(float(fprot.get("Z", 0)))), EULER_ORDER_XYZ)
	for mi: MeshInstance3D in _weapon.find_children("*", "MeshInstance3D", true, false):
		mi.layers = RENDER_LAYER
		mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		mi.extra_cull_margin = 4.0
	for n: String in fp.get("fp_exclusive", []):
		var hidden := _weapon.find_child(n.trim_suffix("mo") + "_mo", true, false)
		if hidden:
			hidden.visible = false
	var skels := _weapon.find_children("*", "Skeleton3D", true, false)
	_weapon_skel = skels[0] if skels.size() > 0 else null
	_weapon_anim = _ensure_player(_weapon)
	_apply_constraints(fp.get("constraints", []))
	_muzzle = null
	if _weapon_skel and _weapon_skel.find_bone("MuzzlePoint") >= 0:
		var ba := BoneAttachment3D.new()
		_weapon_skel.add_child(ba)
		ba.bone_name = "MuzzlePoint"
		_muzzle = ba
	_load_animations(fp)
	weapon_changed.emit(slug, String(w.get("name", slug)))
	play("Equip")
	return true


## Most FP rigs and weapon glbs ship without clips (the viewer loads them from separate
## FP_/GN_ glbs), hence without an AnimationPlayer: create one rooted at the model.
static func _ensure_player(root: Node) -> AnimationPlayer:
	var players := root.find_children("*", "AnimationPlayer", true, false)
	if players.size() > 0:
		return players[0]
	var ap := AnimationPlayer.new()
	ap.name = "AnimationPlayer"
	root.add_child(ap)
	ap.root_node = ap.get_path_to(root)
	return ap


## Site "ObjectToBone" constraints: parent a static sub-mesh (magazine, bullet…) to a bone
## of the weapon skeleton. Node names in the glb lose their "ao"/"mo" suffix dot.
func _apply_constraints(constraints: Array) -> void:
	if _weapon_skel == null:
		return
	for c: Dictionary in constraints:
		if String(c.get("Type", "")) != "ObjectToBone":
			continue
		var src_name := String(c["SourceObject"])
		var src: Node3D = _weapon.find_child(src_name.left(-2) + "_" + src_name.right(2), false, false)
		if src == null or _weapon_skel.find_bone(String(c["Bone"])) < 0:
			continue
		var ba := BoneAttachment3D.new()
		_weapon_skel.add_child(ba)
		ba.bone_name = String(c["Bone"])
		src.get_parent().remove_child(src)
		ba.add_child(src)
		var o: Dictionary = c.get("Offset", {})
		var r: Dictionary = c.get("Rotation", {})
		src.position = Vector3(float(o.get("X", 0)), float(o.get("Y", 0)), float(o.get("Z", 0)))
		src.basis = Basis.from_euler(Vector3(deg_to_rad(float(r.get("X", 0))), deg_to_rad(float(r.get("Y", 0))), deg_to_rad(float(r.get("Z", 0)))), EULER_ORDER_XYZ)
		if c.has("Scale"):
			var sc: Dictionary = c["Scale"]
			src.scale = Vector3(float(sc.get("X", 1)), float(sc.get("Y", 1)), float(sc.get("Z", 1)))


func _load_animations(fp: Dictionary) -> void:
	_fp_anims.clear()
	_idle_name = ""
	# Arms: FP_<weapon>.glb retargeted onto this agent's skeleton.
	var arms_glb := ""
	for a: Dictionary in fp.get("fp_animations", []):
		if a.has("glb"):
			arms_glb = String(a["glb"])
	if arms_glb != "" and _anim:
		var src := KAAssets.load_animation_player(arms_glb)
		if src:
			KAAssets.retarget_animations(src, _anim, _skeleton, "weapon")
			src.free()
	for a: Dictionary in fp.get("fp_animations", []):
		var key := String(a.get("Translatable", a["Name"]))
		_fp_anims[key] = {"name": "weapon/" + String(a["Name"]), "seq": float(a.get("SequenceLength", -1.0)), "seq_gn": float(a.get("SequenceLengthGN", a.get("SequenceLength", -1.0))), "weapon_anim": String(a.get("WeaponAnimName", ""))}
		if bool(a.get("IsHeldDefault", false)) or key == "Idle":
			_idle_name = "weapon/" + String(a["Name"])
	if _idle_name != "" and _anim and _anim.has_animation(_idle_name):
		_anim.get_animation(_idle_name).loop_mode = Animation.LOOP_LINEAR
	# Weapon: GN_<weapon>.glb onto the weapon's own skeleton.
	var gun_glb := ""
	for a: Dictionary in fp.get("animations", []):
		if a.has("glb"):
			gun_glb = String(a["glb"])
	if gun_glb != "" and _weapon_anim and _weapon_skel:
		var src := KAAssets.load_animation_player(gun_glb)
		if src:
			KAAssets.retarget_animations(src, _weapon_anim, _weapon_skel, "gn")
			src.free()
	if _weapon_anim and _weapon_anim.has_animation("Idle"):
		_weapon_anim.get_animation("Idle").loop_mode = Animation.LOOP_LINEAR


## Play a site keybind clip: "Fire", "Equip", "Reload", "Inspect".
func play(action: String) -> void:
	if not _fp_anims.has(action) or _anim == null:
		return
	var a: Dictionary = _fp_anims[action]
	var seq: float = a["seq"]
	if _anim.has_animation(a["name"]):
		var clip := _anim.get_animation(a["name"])
		var speed := clip.length / seq if seq > 0.0 else 1.0
		_anim.play(a["name"], 0.0, speed)
	var wa := String(a["weapon_anim"])
	if _weapon_anim:
		var gn_name := "gn/" + wa
		if _weapon_anim.has_animation(gn_name):
			var clip2 := _weapon_anim.get_animation(gn_name)
			var seq_gn: float = a["seq_gn"]
			_weapon_anim.play(gn_name, 0.0, clip2.length / seq_gn if seq_gn > 0.0 else 1.0)
		elif _weapon_anim.has_animation(wa):
			_weapon_anim.play(wa)


func muzzle_position() -> Vector3:
	if _muzzle:
		return _muzzle.global_position
	if _weapon:
		return _weapon.global_position
	return global_position


func fire_interval() -> float:
	return 1.0 / maxf(fire_rate, 0.01)
