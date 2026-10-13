// MIT License Copyright (c) 2026 andrasnagy.xyz
// Volumetric beam: pass object-space position, the camera in object space and
// the object->world matrix to the ray marcher (TDCameraIndex is vertex-only).
out vec3 vObj;
flat out vec3 vCamObj;
flat out mat4 vWorld;

void main() {
	vObj = P;
	int ci = TDCameraIndex();
	vec3 camW = uTDMats[ci].camInverse[3].xyz;
	vCamObj = (uTDMats[ci].worldInverse * vec4(camW, 1.0)).xyz;
	vWorld = uTDMats[ci].world;
	vec4 worldSpacePos = TDDeform(P);
	gl_Position = TDWorldToProj(worldSpacePos);
}
