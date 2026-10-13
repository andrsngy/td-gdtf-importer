// MIT License Copyright (c) 2026 andrasnagy.xyz
// Volumetric beam in haze. The cone mesh only bounds the volume: from outside the
// front faces are used (the march starts where the ray enters the cone, so the
// depth test against the scene is correct), from inside the beam the back faces.
// Every fragment ray-marches the camera ray through the cone in object space and
// sums the light scattered at each sample: the gobo transmission at that point of
// the beam cross-section, falling off with the beam's area.
uniform sampler2D sGobo;
uniform vec4 uBeam;    // x length (m), y lens radius (m), z tan(half angle incl. prism spread), w haze density
uniform vec4 uColor;   // rgb = colour * intensity * flux
uniform vec4 uCut;     // x enable floor cut, y floor height (world), z ray-march steps (0 = 64)

in vec3 vObj;
flat in vec3 vCamObj;
flat in mat4 vWorld;
layout(location = 0) out vec4 fragColor[TD_NUM_COLOR_BUFFERS];

void main() {
	TDCheckDiscard();
	if (dot(uColor.rgb, uColor.rgb) < 1e-8) discard;
	mat4 world = vWorld;
	vec3 ro = vCamObj;
	vec3 rd = vObj - ro;
	float tEnd = length(rd);
	rd /= tEnd;

	float L = uBeam.x, r0 = uBeam.y, k = uBeam.z;
	float R = r0 + L * k;
	float dc = -ro.z;
	bool inside = dc > 0.0 && dc < L && length(ro.xy) < r0 + dc * k;
	if (inside == gl_FrontFacing) discard;
	vec3 bmin = vec3(-R, -R, -L), bmax = vec3(R, R, 0.0);
	vec3 inv = 1.0 / (rd + vec3(1e-7));
	vec3 t0 = (bmin - ro) * inv, t1 = (bmax - ro) * inv;
	vec3 tmn = min(t0, t1), tmx = max(t0, t1);
	float ta, tb;
	if (inside) {
		ta = 0.0;
		tb = tEnd;
	} else {
		ta = tEnd;                                        // entry point on the front face
		tb = min(min(tmx.x, tmx.y), tmx.z);               // leave the bounding box
	}
	if (tb <= ta) discard;

	int N = uCut.z > 0.5 ? int(min(uCut.z, 128.0)) : 64;  // fewer steps: cheaper, a little grainier
	float dt = (tb - ta) / float(N);
	float jitter = fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453);
	float rRef = r0 + k;                 // beam radius 1 m from the lens
	vec3 acc = vec3(0.0);
	// world scale of the object space (fixtures may be scaled)
	float scale = length(world[0].xyz);
	for (int i = 0; i < N; i++) {
		vec3 p = ro + rd * (ta + (float(i) + jitter) * dt);
		float d = -p.z;
		if (d <= 0.0 || d > L) continue;
		float rad = r0 + d * k;
		vec2 uv = p.xy / rad;
		if (dot(uv, uv) > 1.0) continue;
		if (uCut.x > 0.5 && (world * vec4(p, 1.0)).y < uCut.y) continue;
		vec3 g = texture(sGobo, uv * 0.5 + 0.5).rgb;
		float fall = min((rRef * rRef) / (rad * rad), 6.0);   // irradiance ~ 1 / beam area
		// beam profile: a brighter core fading to soft edges defines each beam in the haze
		float r2 = dot(uv, uv);
		float core = mix(1.0, (1.0 - r2) * (1.0 - r2), 0.75);
		acc += g * fall * core;
	}
	vec3 col = uColor.rgb * acc * dt * scale * uBeam.w * 4.0;  // 4 = scattering calibration
	// soft response: a single beam reaches a clearly visible level quickly, but one beam can
	// never blow out (with the screen blending, many beams build up to bright, not white-out)
	col = vec3(1.0) - exp(-col);
	fragColor[0] = TDOutputSwizzle(vec4(col, 1.0));
}
