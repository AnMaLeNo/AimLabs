shader_type spatial;

uniform vec4 color_a : source_color = vec4(0.20, 0.21, 0.22, 1.0);
uniform vec4 color_b : source_color = vec4(0.16, 0.17, 0.18, 1.0);
uniform vec4 line_color : source_color = vec4(0.42, 0.45, 0.50, 1.0);
uniform float cell_size = 2.0;
uniform float line_strength = 0.55;

varying vec3 world_pos;

void vertex() {
	world_pos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
}

void fragment() {
	vec2 p = world_pos.xz / cell_size;
	vec2 cell = floor(p);
	float checker = mod(cell.x + cell.y, 2.0);
	vec2 f = fract(p);
	vec2 d = fwidth(p) * 1.5;
	vec2 g = smoothstep(vec2(0.0), d, min(f, 1.0 - f));
	float line = 1.0 - min(g.x, g.y);
	vec3 col = mix(color_a.rgb, color_b.rgb, checker);
	col = mix(col, line_color.rgb, line * line_strength);
	ALBEDO = col;
	ROUGHNESS = 0.92;
	METALLIC = 0.0;
}
