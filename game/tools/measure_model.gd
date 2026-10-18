extends SceneTree
# Usage: godot --headless --path game -s tools/measure_model.gd
# Prints the model-space AABB of every mesh of the character model.

func _init() -> void:
	var scene: PackedScene = load("res://assets/characters/character-a.glb")
	var inst := scene.instantiate()
	_walk(inst, Transform3D.IDENTITY, "")
	var ap := inst.find_children("*", "AnimationPlayer", true, false)
	if ap.size() > 0:
		print("animations: ", ap[0].get_animation_list())
	quit()

func _walk(n: Node, xf: Transform3D, indent: String) -> void:
	if n is Node3D:
		xf = xf * (n as Node3D).transform
	var info := ""
	if n is MeshInstance3D:
		var aabb: AABB = xf * (n as MeshInstance3D).get_aabb()
		info = " AABB min=%s max=%s" % [aabb.position, aabb.end]
	print(indent, n.name, " (", n.get_class(), ")", info)
	for c in n.get_children():
		_walk(c, xf, indent + "  ")
