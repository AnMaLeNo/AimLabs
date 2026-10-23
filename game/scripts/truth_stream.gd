extends Node
## Training aid (information option "truth"): the game's ground truth, sent as one JSON datagram per snapshot to
## 127.0.0.1:<port> (UDP), at most <hz> per second. Forward it wherever the program under construction runs.
## NOT available in the "real" information profile.
##
## { "t": game clock in µs (same clock as the bridge's time_usec), "frame": rendered frame counter, "fps",
##   "player": {pos, yaw, pitch, fov}, "stats": {shots, hits, headshots, kills},
##   "targets": [{ "id", "alive", "pos": [x, y, z], "distance", "screen": [x1, y1, x2, y2] | null }],   (window pixels)
##   "reticle": { "on_target", "target", "zone" },
##   "bench": { "phase", "speed", "distance" } | null }

const RAY_MASK := (1 << 2) | (1 << 3) | (1 << 0)

var game: Node
var enabled := false
var port := 7790
var hz := 240.0

var _peer := PacketPeerUDP.new()
var _acc := 0.0


func setup(p_game: Node, p_port: int, p_hz: float) -> void:
	game = p_game
	port = p_port
	hz = maxf(p_hz, 1.0)
	_peer.set_dest_address("127.0.0.1", port)


func _process(delta: float) -> void:
	if not enabled or game == null:
		return
	_acc += delta
	if _acc < 1.0 / hz:
		return
	_acc = 0.0
	_peer.put_packet(JSON.stringify(_snapshot()).to_utf8_buffer())


func _snapshot() -> Dictionary:
	var player: Player = game.player
	var cam: Camera3D = player.camera
	var targets := []
	var enemies: Array = game.world.enemies
	for i in enemies.size():
		var e: Enemy = enemies[i]
		var feet: Vector3 = e.global_position
		var head: Vector3 = e.head_position() + Vector3(0.0, 0.12, 0.0)
		var right: Vector3 = cam.global_transform.basis.x
		var pts: Array[Vector3] = [feet + right * 0.35, feet - right * 0.35, head + right * 0.18, head - right * 0.18]
		var visible := true
		var mn := Vector2(INF, INF)
		var mx := Vector2(-INF, -INF)
		for p in pts:
			if cam.is_position_behind(p):
				visible = false
				break
			var s := cam.unproject_position(p)
			mn = mn.min(s)
			mx = mx.max(s)
		targets.append({ "id": i, "alive": e.alive, "pos": [feet.x, feet.y, feet.z],
			"distance": cam.global_position.distance_to(feet),
			"screen": [mn.x, mn.y, mx.x, mx.y] if visible else null })
	var from := cam.global_position
	var query := PhysicsRayQueryParameters3D.create(from, from - cam.global_transform.basis.z * 500.0, RAY_MASK, [player.get_rid()])
	var hit := player.get_world_3d().direct_space_state.intersect_ray(query)
	var reticle := { "on_target": false, "target": -1, "zone": "" }
	if hit and hit.collider is Node and hit.collider.is_in_group("hitbox"):
		reticle = { "on_target": true, "target": enemies.find(hit.collider.get_meta("enemy")), "zone": String(hit.collider.get_meta("zone")) }
	var bench = null
	if game.bench and game.bench.running:
		bench = { "phase": game.bench._phase, "speed": game.bench._speed, "distance": game.bench._distance }
	var hud: HUD = game.hud
	return { "t": Time.get_ticks_usec(), "frame": Engine.get_frames_drawn(), "fps": Engine.get_frames_per_second(),
		"player": { "pos": [player.global_position.x, player.global_position.y, player.global_position.z],
			"yaw": player.yaw_deg, "pitch": player.pitch_deg, "fov": cam.fov },
		"stats": { "shots": hud.shots, "hits": hud.hits, "headshots": hud.headshots, "kills": hud.kills },
		"targets": targets, "reticle": reticle, "bench": bench }
