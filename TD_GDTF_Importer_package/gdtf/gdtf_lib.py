# MIT License Copyright (c) 2026 andrasnagy.xyz
"""
gdtf_lib - dependency-free GDTF reader, model converter and GDTF Share client.

Used by the TouchDesigner gdtf_fixture / dmx_out components, but has no `td`
dependency so it can be tested from plain Python.
"""
import io
import json
import math
import os
import re
import struct
import zipfile
import http.cookiejar
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SHARE_URL = 'https://gdtf-share.com/apis/public'

GEOMETRY_TAGS = {
	'Geometry', 'Axis', 'FilterBeam', 'FilterColor', 'FilterGobo', 'FilterShaper',
	'Beam', 'MediaServerLayer', 'MediaServerCamera', 'MediaServerMaster', 'Display',
	'GeometryReference', 'Laser', 'WiringObject', 'Inventory', 'Structure', 'Support',
	'Magnet',
}


# --------------------------------------------------------------------------- values

def parse_dmx(value, nbytes):
	"""GDTF DMX value 'v/n' or 'v/ns' -> int at nbytes resolution."""
	if value is None or value in ('', 'None'):
		return None
	m = re.match(r'^\s*(\d+)\s*/\s*(\d+)\s*(s?)\s*$', value)
	if not m:
		try:
			return int(float(value))
		except ValueError:
			return None
	v, n, shift = int(m.group(1)), int(m.group(2)), bool(m.group(3))
	if n == nbytes:
		return v
	if n > nbytes:
		return v >> (8 * (n - nbytes))
	diff = nbytes - n
	if shift:
		out = v
		for _ in range(diff):
			out = (out << 8) | (v & 0xFF)
		return out
	return v << (8 * diff)


def parse_matrix(text):
	"""'{a,b,c,d}{...}{...}{...}' -> 16 floats row-major (translation in col 3)."""
	if not text:
		return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
	rows = re.findall(r'\{([^}]*)\}', text)
	vals = []
	for r in rows[:4]:
		vals.extend(float(x) for x in r.split(',')[:4])
	if len(vals) == 12:
		vals += [0, 0, 0, 1]
	if len(vals) != 16:
		return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
	return vals


def parse_matrix3(text):
	"""Facet 'Rotation' '{a,b,c}{d,e,f}{g,h,i}' -> 9 floats (GDTF stores the 2D offset in the third row)."""
	rows = re.findall(r'\{([^}]*)\}', text or '')
	vals = []
	for r in rows[:3]:
		vals.extend(float(x) for x in r.split(',')[:3])
	return vals if len(vals) == 9 else [1, 0, 0, 0, 1, 0, 0, 0, 1]


def xyY_to_rgb(text):
	"""CIE 'x,y,Y' -> normalised linear sRGB tuple."""
	try:
		x, y, Y = [float(v) for v in text.split(',')[:3]]
	except Exception:
		return (1.0, 1.0, 1.0)
	if y <= 0:
		return (0.0, 0.0, 0.0)
	X = x / y
	Z = (1 - x - y) / y
	r = 3.2406 * X - 1.5372 - 0.4986 * Z
	g = -0.9689 * X + 1.8758 + 0.0415 * Z
	b = 0.0557 * X - 0.2040 + 1.0570 * Z
	r, g, b = max(r, 0), max(g, 0), max(b, 0)
	m = max(r, g, b, 1e-6)
	return (r / m, g / m, b / m)


# --------------------------------------------------------------------------- parse

def _f(el, name, default=0.0):
	try:
		return float(el.get(name, default))
	except (TypeError, ValueError):
		return default


def _geometry(el):
	g = {
		'tag': el.tag,
		'name': el.get('Name', ''),
		'model': el.get('Model', ''),
		'matrix': parse_matrix(el.get('Position')),
		'children': [],
	}
	if el.tag == 'Beam':
		g['beamangle'] = _f(el, 'BeamAngle', 25.0)
		g['fieldangle'] = _f(el, 'FieldAngle', g['beamangle'])
		g['beamradius'] = _f(el, 'BeamRadius', 0.05)
		g['beamtype'] = el.get('BeamType', 'Wash')
		g['colortemp'] = _f(el, 'ColorTemperature', 6000.0)
	if el.tag == 'GeometryReference':
		g['ref'] = el.get('Geometry', '')
		g['breaks'] = [
			{'break': int(b.get('DMXBreak', '1')), 'offset': int(b.get('DMXOffset', '1') or 1)}
			for b in el.findall('Break')
		]
	for c in el:
		if c.tag in GEOMETRY_TAGS:
			g['children'].append(_geometry(c))
	return g


def _find_geometry(tree, name):
	if tree['name'] == name:
		return tree
	for c in tree['children']:
		r = _find_geometry(c, name)
		if r:
			return r
	return None


def _walk(tree):
	yield tree
	for c in tree['children']:
		yield from _walk(c)


def parse(path):
	"""Parse a .gdtf file into a plain dict."""
	with zipfile.ZipFile(path) as z:
		root = ET.fromstring(z.read('description.xml'))
	ft = root.find('FixtureType')
	info = {
		'file': os.path.abspath(path),
		'name': ft.get('Name', ''),
		'longname': ft.get('LongName', ''),
		'manufacturer': ft.get('Manufacturer', ''),
		'description': ft.get('Description', ''),
		'fixturetypeid': ft.get('FixtureTypeID', ''),
		'dataversion': root.get('DataVersion', ''),
	}

	wheels = {}
	for w in ft.findall('Wheels/Wheel'):
		slots = []
		for sl in w.findall('Slot'):
			facets = []
			for fc in sl.findall('Facet'):
				m = parse_matrix3(fc.get('Rotation'))
				facets.append({'color': xyY_to_rgb(fc.get('Color', '0.3127,0.3290,100')), 'matrix': m})
			slots.append({
				'name': sl.get('Name', ''),
				'color': xyY_to_rgb(sl.get('Color', '0.3127,0.3290,100')),
				'media': sl.get('MediaFileName', '') or '',
				'facets': facets,
			})
		wheels[w.get('Name')] = slots
	info['wheels'] = wheels

	info['attributes'] = {
		a.get('Name'): {'unit': a.get('PhysicalUnit', 'None'), 'feature': a.get('Feature', '')}
		for a in ft.findall('AttributeDefinitions/Attributes/Attribute')
	}

	models = {}
	for m in ft.findall('Models/Model'):
		models[m.get('Name')] = {
			'file': m.get('File', ''),
			'primitive': m.get('PrimitiveType', 'Undefined'),
			'length': _f(m, 'Length'), 'width': _f(m, 'Width'), 'height': _f(m, 'Height'),
		}
	info['models'] = models

	geos = [_geometry(g) for g in ft.find('Geometries') if g.tag in GEOMETRY_TAGS] \
		if ft.find('Geometries') is not None else []
	info['geometries'] = geos

	modes = []
	for mode in ft.findall('DMXModes/DMXMode'):
		modes.append(_parse_mode(mode, geos))
	info['modes'] = modes
	return info


def _parse_channel(ch):
	offs = ch.get('Offset', 'None')
	if not offs or offs == 'None':
		return None
	offsets = [int(o) for o in offs.split(',') if o.strip()]
	nbytes = len(offsets)
	functions = []
	attribute = ''
	for lc in ch.findall('LogicalChannel'):
		attribute = attribute or lc.get('Attribute', '')
		for cf in lc.findall('ChannelFunction'):
			fn = {
				'name': cf.get('Name', ''),
				'attribute': cf.get('Attribute', ''),
				'dmxfrom': parse_dmx(cf.get('DMXFrom', '0/1'), nbytes) or 0,
				'default': parse_dmx(cf.get('Default', '0/1'), nbytes) or 0,
				'physfrom': _f(cf, 'PhysicalFrom', 0.0),
				'physto': _f(cf, 'PhysicalTo', 1.0),
				'wheel': cf.get('Wheel', ''),
				'realfade': _f(cf, 'RealFade', 0.0),
				'realaccel': _f(cf, 'RealAcceleration', 0.0),
				'sets': [],
			}
			for cs in cf.findall('ChannelSet'):
				d = parse_dmx(cs.get('DMXFrom', ''), nbytes)
				if d is None:
					continue
				fn['sets'].append({
					'name': cs.get('Name', ''),
					'dmxfrom': d,
					'slot': int(cs.get('WheelSlotIndex', '0') or 0),
					'physfrom': _f(cs, 'PhysicalFrom', 0.0) if cs.get('PhysicalFrom') else None,
					'physto': _f(cs, 'PhysicalTo', 0.0) if cs.get('PhysicalTo') else None,
				})
			functions.append(fn)
	functions.sort(key=lambda f: f['dmxfrom'])
	maxv = (1 << (8 * nbytes)) - 1
	for i, fn in enumerate(functions):
		fn['dmxto'] = functions[i + 1]['dmxfrom'] - 1 if i + 1 < len(functions) else maxv
		for j, s in enumerate(fn['sets']):
			s['dmxto'] = fn['sets'][j + 1]['dmxfrom'] - 1 if j + 1 < len(fn['sets']) else fn['dmxto']
	default = functions[0]['default'] if functions else 0
	initial = ch.get('InitialFunction', '')
	for fn in functions:
		if initial.endswith('.' + fn['name']):
			default = fn['default']
	return {
		'geometry': ch.get('Geometry', ''),
		'attribute': attribute,
		'offsets': offsets,
		'bytes': nbytes,
		'max': maxv,
		'default': default,
		'break': int(ch.get('DMXBreak', '1') if ch.get('DMXBreak', '1').isdigit() else 1),
		'functions': functions,
	}


def _parse_mode(mode, geos):
	root_name = mode.get('Geometry', '')
	root = next((g for g in geos if g['name'] == root_name), geos[0] if geos else None)
	# geometry references in this mode's tree: referenced geometry -> list of instances
	refs = {}
	if root:
		for g in _walk(root):
			if g['tag'] == 'GeometryReference':
				refs.setdefault(g['ref'], []).append(g)
	channels = []
	for ch in mode.findall('DMXChannels/DMXChannel'):
		c = _parse_channel(ch)
		if not c:
			continue
		in_root = root and _find_geometry(root, c['geometry'])
		templ = None
		if not in_root:
			for rname, insts in refs.items():
				tg = next((g for g in geos if g['name'] == rname), None)
				if tg and _find_geometry(tg, c['geometry']):
					templ = insts
					break
		if templ:
			for inst in templ:
				off = inst['breaks'][0]['offset'] if inst['breaks'] else 1
				cc = dict(c)
				cc['offsets'] = [o + off - 1 for o in c['offsets']]
				cc['instance'] = inst['name']
				channels.append(cc)
		else:
			c['instance'] = ''
			channels.append(c)
	channels.sort(key=lambda c: c['offsets'][0])
	footprint = max((max(c['offsets']) for c in channels), default=0)
	return {'name': mode.get('Name', ''), 'geometry': root['name'] if root else '',
			'channels': channels, 'footprint': footprint}


def function_at(channel, value):
	"""Return (function, channelset or None, physical value) for a DMX value."""
	fns = channel['functions']
	if not fns:
		return None, None, 0.0
	fn = fns[0]
	for f in fns:
		if f['dmxfrom'] <= value:
			fn = f
	span = max(fn['dmxto'] - fn['dmxfrom'], 1)
	t = min(max((value - fn['dmxfrom']) / span, 0.0), 1.0)
	phys = fn['physfrom'] + t * (fn['physto'] - fn['physfrom'])
	cs = None
	for s in fn['sets']:
		if s['dmxfrom'] <= value <= s['dmxto']:
			cs = s
	if cs is not None and cs.get('physfrom') is not None and cs.get('physto') is not None:
		# a channel set with its own physical range wins (e.g. 'No rotation' = 0 exactly,
		# where the function-wide interpolation gives a slow drift that never settles)
		sspan = max(cs['dmxto'] - cs['dmxfrom'], 1)
		st = min(max((value - cs['dmxfrom']) / sspan, 0.0), 1.0)
		phys = cs['physfrom'] + st * (cs['physto'] - cs['physfrom'])
	return fn, cs, phys


# --------------------------------------------------------------------------- models

def _read_3ds(data):
	"""Minimal .3ds reader -> (vertices, triangles)."""
	verts, tris = [], []

	def walk(start, end):
		pos = start
		cur_base = None
		while pos + 6 <= end:
			cid, clen = struct.unpack_from('<HI', data, pos)
			if clen < 6 or pos + clen > end:
				break
			body = pos + 6
			if cid in (0x4D4D, 0x3D3D, 0x4100):
				walk(body, pos + clen)
			elif cid == 0x4000:
				nul = data.index(b'\0', body)
				walk(nul + 1, pos + clen)
			elif cid == 0x4110:
				n = struct.unpack_from('<H', data, body)[0]
				cur_base = len(verts)
				walk.base = cur_base
				for i in range(n):
					verts.append(struct.unpack_from('<3f', data, body + 2 + i * 12))
			elif cid == 0x4120:
				n = struct.unpack_from('<H', data, body)[0]
				base = getattr(walk, 'base', 0)
				for i in range(n):
					a, b, c, _ = struct.unpack_from('<4H', data, body + 2 + i * 8)
					tris.append((a + base, b + base, c + base))
			pos += clen
	walk(0, len(data))
	return verts, tris


def _read_glb(data):
	"""Minimal .glb reader (positions + indices, node transforms applied)."""
	magic, _, length = struct.unpack_from('<4sII', data, 0)
	if magic != b'glTF':
		raise ValueError('not a glb')
	pos = 12
	js, binchunk = None, b''
	while pos < length:
		clen, ctype = struct.unpack_from('<II', data, pos)
		chunk = data[pos + 8:pos + 8 + clen]
		if ctype == 0x4E4F534A:
			js = json.loads(chunk)
		elif ctype == 0x004E4942:
			binchunk = chunk
		pos += 8 + clen
	comp = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H', 5125: 'I', 5126: 'f'}
	ncomp = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}

	def accessor(i):
		a = js['accessors'][i]
		bv = js['bufferViews'][a['bufferView']]
		fmt = comp[a['componentType']]
		nc = ncomp[a['type']]
		size = struct.calcsize(fmt)
		stride = bv.get('byteStride') or size * nc
		off = bv.get('byteOffset', 0) + a.get('byteOffset', 0)
		out = []
		for k in range(a['count']):
			v = struct.unpack_from('<%d%s' % (nc, fmt), binchunk, off + k * stride)
			out.append(v if nc > 1 else v[0])
		return out

	def matmul(a, b):
		return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]

	def node_matrix(n):
		if 'matrix' in n:
			m = n['matrix']  # column-major
			return [m[c * 4 + r] for r in range(4) for c in range(4)]
		t = n.get('translation', [0, 0, 0])
		x, y, z, w = n.get('rotation', [0, 0, 0, 1])
		s = n.get('scale', [1, 1, 1])
		r = [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
			 2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
			 2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]
		return [r[0] * s[0], r[1] * s[1], r[2] * s[2], t[0],
				r[3] * s[0], r[4] * s[1], r[5] * s[2], t[1],
				r[6] * s[0], r[7] * s[1], r[8] * s[2], t[2],
				0, 0, 0, 1]

	verts, tris = [], []
	ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

	def visit(ni, parent):
		n = js['nodes'][ni]
		m = matmul(parent, node_matrix(n))
		if 'mesh' in n:
			for prim in js['meshes'][n['mesh']]['primitives']:
				if prim.get('mode', 4) != 4 or 'POSITION' not in prim['attributes']:
					continue
				base = len(verts)
				for p in accessor(prim['attributes']['POSITION']):
					verts.append(tuple(m[r * 4] * p[0] + m[r * 4 + 1] * p[1] + m[r * 4 + 2] * p[2] + m[r * 4 + 3]
									   for r in range(3)))
				idx = accessor(prim['indices']) if 'indices' in prim else list(range(len(verts) - base))
				for k in range(0, len(idx) - 2, 3):
					tris.append((idx[k] + base, idx[k + 1] + base, idx[k + 2] + base))
		for c in n.get('children', []):
			visit(c, m)

	scene = js['scenes'][js.get('scene', 0)] if js.get('scenes') else {'nodes': range(len(js['nodes']))}
	for ni in scene['nodes']:
		visit(ni, ident)
	# glTF is Y-up, GDTF is Z-up
	verts = [(x, -z, y) for (x, y, z) in verts]
	return verts, tris


def _write_obj(path, verts, tris):
	with open(path, 'w') as f:
		f.write('# converted by gdtf_lib\n')
		for v in verts:
			f.write('v %.6f %.6f %.6f\n' % v)
		for t in tris:
			f.write('f %d %d %d\n' % (t[0] + 1, t[1] + 1, t[2] + 1))


def extract_models(info, cachedir):
	"""Convert every model with a mesh file to .obj (meters, GDTF Z-up).

	Returns {model name: obj path}. Models without a usable mesh are absent and
	should be drawn as primitives from length/width/height."""
	os.makedirs(cachedir, exist_ok=True)
	out = {}
	with zipfile.ZipFile(info['file']) as z:
		names = z.namelist()
		for mname, m in info['models'].items():
			if not m['file']:
				continue
			candidates = []
			for folder, ext in (('models/gltf/', '.glb'), ('models/3ds/', '.3ds')):
				for n in names:
					if n.lower() == (folder + m['file'] + ext).lower():
						candidates.append((n, ext))
			for n, ext in candidates:
				try:
					data = z.read(n)
					verts, tris = _read_glb(data) if ext == '.glb' else _read_3ds(data)
				except Exception as e:
					print('gdtf_lib: failed to read', n, e)
					continue
				if not verts or not tris:
					continue
				verts = _fit(verts, m, ext)
				safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', mname) or 'model'
				p = os.path.join(cachedir, safe + '.obj')
				_write_obj(p, verts, tris)
				out[mname] = p
				break
	return out


def _fit(verts, m, ext):
	"""Bring model units to meters. 3ds files are often in mm; use model size as reference."""
	xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
	size = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
	target = [m['length'], m['width'], m['height']]
	ratios = [t / s for t, s in zip(target, size) if t > 0 and s > 1e-9]
	if not ratios:
		return verts
	r = sorted(ratios)[len(ratios) // 2]
	# snap to a unit conversion when close (keeps proportions of the mesh intact)
	for unit in (1.0, 0.001, 0.01, 0.0254, 0.1):
		if abs(math.log10(r / unit)) < 0.35:
			r = unit
			break
	if abs(r - 1.0) < 1e-9:
		return verts
	return [(x * r, y * r, z * r) for (x, y, z) in verts]


def extract_media(info, cachedir):
	"""Extract wheel slot images. Returns {wheel name: [png path or '' per slot]}."""
	os.makedirs(cachedir, exist_ok=True)
	out = {}
	with zipfile.ZipFile(info['file']) as z:
		entries = {}
		for n in z.namelist():
			if n.lower().startswith('wheels/') and not n.endswith('/'):
				base = n[len('wheels/'):]
				entries[base.lower()] = n
				entries[os.path.splitext(base)[0].lower()] = n
		for wname, slots in info['wheels'].items():
			paths = []
			for sl in slots:
				src = entries.get(sl['media'].lower()) if sl['media'] else None
				if not src:
					paths.append('')
					continue
				safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.splitext(os.path.basename(src))[0]) + '.png'
				dst = os.path.join(cachedir, safe)
				if not os.path.isfile(dst):
					with open(dst, 'wb') as f:
						f.write(z.read(src))
				paths.append(dst)
			out[wname] = paths
	return out


# --------------------------------------------------------------------------- share

def _ssl_context():
	"""TouchDesigner's Python points OpenSSL at its build machine's certificate file,
	so HTTPS fails with CERTIFICATE_VERIFY_FAILED; use certifi or the system bundle."""
	try:
		import certifi
		return ssl.create_default_context(cafile=certifi.where())
	except Exception:
		pass
	for path in ('/etc/ssl/cert.pem', '/etc/ssl/certs/ca-certificates.crt'):
		if os.path.isfile(path):
			return ssl.create_default_context(cafile=path)
	return ssl.create_default_context()


class ShareClient:
	def __init__(self, user, password):
		self.user, self.password = user, password
		self.jar = http.cookiejar.CookieJar()
		self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
												  urllib.request.HTTPSHandler(context=_ssl_context()))
		self.opener.addheaders = [('User-Agent', 'TouchDesigner-gdtf_fixture/1.0')]

	def login(self):
		data = urllib.parse.urlencode({'user': self.user, 'password': self.password}).encode()
		with self.opener.open(SHARE_URL + '/login.php', data=data, timeout=30) as r:
			res = json.loads(r.read().decode('utf-8', 'replace'))
		if not res.get('result'):
			raise RuntimeError('GDTF Share login failed: %s' % res.get('error'))
		return res

	def get_list(self):
		with self.opener.open(SHARE_URL + '/getList.php', timeout=120) as r:
			res = json.loads(r.read().decode('utf-8', 'replace'))
		if not res.get('result'):
			raise RuntimeError('GDTF Share list failed: %s' % res.get('error'))
		return res.get('list', [])

	def download(self, rid, path):
		with self.opener.open(SHARE_URL + '/downloadFile.php?rid=%s' % rid, timeout=120) as r:
			data = r.read()
		if not data.startswith(b'PK'):
			raise RuntimeError('GDTF Share download failed: %s' % data[:200])
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, 'wb') as f:
			f.write(data)
		return path


def share_filename(entry):
	clean = lambda s: re.sub(r'[^A-Za-z0-9_.-]+', '_', str(s))
	return '%s@%s@%s.gdtf' % (clean(entry.get('manufacturer')), clean(entry.get('fixture')),
							  clean(entry.get('revision')))
