extends SceneTree
## Compare, for a target in the pose it is displayed in, the points the game uses to say « visible » (Enemy.visibility_points:
## hitbox capsules, measured on the REST pose) with where its bones really are (current animation pose).
##   godot --headless --path game --script res://tools/vis_probe.gd -- [--agent=jett]
## Answers: can a detector see a hand or an arm before the game calls the target visible?

func _initialize() -> void:
	var slug := "jett"
	for a in OS.get_cmdline_user_args():
		if a.begins_with("--agent="):
			slug = a.substr(8)
	var e := Enemy.new()
	e.agent_slug = slug
	e.neutral_pose = true
	root.add_child(e)
	await process_frame
	await process_frame
	await physics_frame
	await process_frame
	var skel: Skeleton3D = e._skeleton
	var pts := e.visibility_points(Vector3.RIGHT)
	var vx := [INF, -INF]
	var vy := [INF, -INF]
	for p in pts:
		vx = [minf(vx[0], p.x), maxf(vx[1], p.x)]
		vy = [minf(vy[0], p.y), maxf(vy[1], p.y)]
	print("agent %s, animation « %s »" % [slug, e._idle])
	print("points de visibilité du jeu : x de %.2f à %.2f m, y de %.2f à %.2f m" % [vx[0], vx[1], vy[0], vy[1]])
	for pose in ["rest", "anim"]:
		var bx := [INF, -INF, "", ""]
		var by := [INF, -INF, "", ""]
		for i in skel.get_bone_count():
			var xf: Transform3D = skel.get_bone_global_rest(i) if pose == "rest" else skel.get_bone_global_pose(i)
			var p: Vector3 = e.to_local(skel.global_transform * xf.origin)
			if p.x < bx[0]:
				bx[0] = p.x
				bx[2] = skel.get_bone_name(i)
			if p.x > bx[1]:
				bx[1] = p.x
				bx[3] = skel.get_bone_name(i)
			if p.y < by[0]:
				by[0] = p.y
				by[2] = skel.get_bone_name(i)
			if p.y > by[1]:
				by[1] = p.y
				by[3] = skel.get_bone_name(i)
		print("os, pose %s : x de %.2f (%s) à %.2f (%s) m ; y de %.2f (%s) à %.2f (%s) m" % [pose, bx[0], bx[2], bx[1], bx[3], by[0], by[2], by[1], by[3]])
	for name in ["L_Hand", "R_Hand", "L_Elbow", "R_Elbow", "L_Shoulder", "R_Shoulder", "Head"]:
		var i := skel.find_bone(name)
		if i >= 0:
			var r: Vector3 = e.to_local(skel.global_transform * skel.get_bone_global_rest(i).origin)
			var a: Vector3 = e.to_local(skel.global_transform * skel.get_bone_global_pose(i).origin)
			print("  %-11s repos (%.2f, %.2f)   affiché (%.2f, %.2f)" % [name, r.x, r.y, a.x, a.y])
	var box := AABB()
	var first := true
	for mi: MeshInstance3D in e.find_children("*", "MeshInstance3D", true, false):
		var b: AABB = e.global_transform.affine_inverse() * mi.global_transform * mi.get_aabb()
		box = b if first else box.merge(b)
		first = false
	print("boîte englobante des maillages (pose de repos) : x de %.2f à %.2f m, y de %.2f à %.2f m" % [box.position.x, box.end.x, box.position.y, box.end.y])
	quit()
