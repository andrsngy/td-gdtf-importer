// MIT License Copyright (c) 2026 andrasnagy.xyz
// Gobo / optics compositor. Output = light transmission (rgb) of the fixture's
// optical system as seen at the front lens, in a domain of +-uDomain beam radii.
//
// inputs: 2*w = slot A, 2*w+1 = slot B of image wheel w (w = 0..3)
uniform vec4 uW0, uW1, uW2, uW3;          // x active, y scroll t, z rotation (rad), w kind (0 gobo, 1 animation)
uniform vec4 uWA0, uWA1, uWA2, uWA3;      // slot A colour rgb, w mode (0 open, 1 image, >=2 procedural seed)
uniform vec4 uWB0, uWB1, uWB2, uWB3;      // slot B
uniform vec4 uCA0, uCB0, uCA1, uCB1;      // colour wheels: A rgb + t, B rgb + active
uniform vec4 uCA2, uCB2;
uniform vec4 uPrism;                      // x facets, y rotation (rad), z active
uniform vec4 uIris;                       // x aperture 0..1
uniform vec4 uBladeA, uBladeB, uBladeRot; // per blade insertion A/B (0..1) and rotation (rad)
uniform vec4 uOptic;                      // x domain, y edge softness, z shaper rotation (rad)
uniform vec4 uFacet[16];                  // xy offset, z scale
uniform vec4 uFacetCol[16];               // rgb

out vec4 fragColor;

vec2 rot2(vec2 p, float a) { float c = cos(a), s = sin(a); return vec2(c * p.x - s * p.y, s * p.x + c * p.y); }

float hash(float n) { return fract(sin(n) * 43758.5453123); }

// procedural stand-in for gobo slots the profile names but ships no image for
float procGobo(vec2 p, float seed) {
	float r = length(p), a = atan(p.y, p.x);
	float k = floor(mod(seed, 6.0));
	float n = 3.0 + floor(hash(seed) * 6.0);
	if (k < 1.0) return step(0.0, sin(a * n)) * step(r, 0.95);                      // spokes
	if (k < 2.0) return step(0.5, fract(r * (2.0 + n * 0.5)));                       // rings
	if (k < 3.0) { vec2 g = fract(p * (2.0 + n * 0.6)) - 0.5; return step(length(g), 0.28); } // dots
	if (k < 4.0) return step(abs(p.y), 0.25 + 0.15 * sin(a * n));                    // bar
	if (k < 5.0) return step(0.5, fract((a / 6.2831853 + r) * n));                   // swirl
	return step(r, 0.35 + 0.25 * step(0.0, sin(a * n)));                             // star
}

vec3 slotSample(int i, vec2 p, vec4 info) {
	if (dot(p, p) > 1.0) return vec3(0.0);          // wheel metal around the slot
	if (info.w < 0.5) return info.rgb;              // open hole (with filter colour)
	if (info.w < 1.5) {
		vec4 c;
		// constant sampler indices keep every backend happy
		if (i == 0) c = texture(sTD2DInputs[0], p * 0.5 + 0.5);
		else if (i == 1) c = texture(sTD2DInputs[1], p * 0.5 + 0.5);
		else if (i == 2) c = texture(sTD2DInputs[2], p * 0.5 + 0.5);
		else if (i == 3) c = texture(sTD2DInputs[3], p * 0.5 + 0.5);
		else if (i == 4) c = texture(sTD2DInputs[4], p * 0.5 + 0.5);
		else if (i == 5) c = texture(sTD2DInputs[5], p * 0.5 + 0.5);
		else if (i == 6) c = texture(sTD2DInputs[6], p * 0.5 + 0.5);
		else c = texture(sTD2DInputs[7], p * 0.5 + 0.5);
		return c.rgb * c.a * info.rgb;              // white = light passes, transparent = blocked
	}
	return vec3(procGobo(p, info.w)) * info.rgb;
}

vec3 imageWheel(int w, vec4 W, vec4 A, vec4 B, vec2 p) {
	if (W.x < 0.5) return vec3(1.0);
	if (W.w > 0.5) {
		// animation wheel: a large etched disc beside the beam, rotating through it
		vec2 c = vec2(0.0, 1.9);
		vec2 q = rot2(p - c, W.z) / 2.8;
		vec3 g = A.w > 0.5 ? slotSample(2 * w, q, vec4(1.0, 1.0, 1.0, A.w)) : vec3(0.5 + 0.5 * step(0.0, sin(q.x * 40.0 + q.y * 25.0)));
		return mix(vec3(1.0), g, W.y);              // y = insertion
	}
	float gap = 2.3;                                 // slot pitch in beam radii (slots are separated by metal)
	vec2 pa = rot2(p + vec2(W.y * gap, 0.0), W.z);
	vec2 pb = rot2(p - vec2((1.0 - W.y) * gap, 0.0), W.z);
	return slotSample(2 * w, pa, A) + slotSample(2 * w + 1, pb, B);
}

vec3 colorWheel(vec4 A, vec4 B, vec2 p) {
	if (B.w < 0.5) return vec3(1.0);
	float edge = 1.0 - 2.0 * A.w;                  // next filter slides in from the side -> split colours
	float k = smoothstep(edge - 0.02, edge + 0.02, p.x);
	return mix(A.rgb, B.rgb, k);
}

float blade(int i, vec2 p) {
	float ins = uBladeA[i] + uBladeB[i];
	if (ins <= 0.0) return 1.0;
	float ang = float(i) * 1.5707963 + uOptic.z + uBladeRot[i];
	vec2 q = rot2(p, -ang);
	float edgeY = 1.0 - 2.0 * mix(uBladeA[i], uBladeB[i], q.x * 0.5 + 0.5);
	return 1.0 - smoothstep(edgeY - 0.01, edgeY + 0.01, q.y);
}

vec3 optics(vec2 p) {
	float r = length(p);
	float aperture = 1.0 - smoothstep(1.0 - uOptic.y, 1.0, r);
	float iris = 1.0 - smoothstep(uIris.x - 0.015, uIris.x + 0.015, r);
	if (aperture * iris <= 0.0) return vec3(0.0);
	vec3 t = vec3(aperture * iris);
	for (int i = 0; i < 4; i++) t *= blade(i, p);
	t *= colorWheel(uCA0, uCB0, p);
	t *= colorWheel(uCA1, uCB1, p);
	t *= colorWheel(uCA2, uCB2, p);
	t *= imageWheel(0, uW0, uWA0, uWB0, p);
	t *= imageWheel(1, uW1, uWA1, uWB1, p);
	t *= imageWheel(2, uW2, uWA2, uWB2, p);
	t *= imageWheel(3, uW3, uWA3, uWB3, p);
	return t;
}

void main() {
	vec2 p = (vUV.st * 2.0 - 1.0) * uOptic.x;
	vec3 c;
	if (uPrism.z > 0.5 && uPrism.x > 0.5) {
		c = vec3(0.0);
		int n = int(uPrism.x);
		for (int i = 0; i < 16; i++) {
			if (i >= n) break;
			// each facet images the whole gobo, displaced; the prism rotates the pattern of copies
			vec2 off = rot2(uFacet[i].xy, uPrism.y);
			vec2 q = rot2(p - off, -uPrism.y) / max(uFacet[i].z, 0.05);
			c += optics(q) * uFacetCol[i].rgb;
		}
		c /= float(n);
	} else {
		c = optics(p);
	}
	fragColor = TDOutputSwizzle(vec4(c, 1.0));
}
