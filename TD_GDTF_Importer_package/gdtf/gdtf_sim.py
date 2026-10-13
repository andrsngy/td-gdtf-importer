# MIT License Copyright (c) 2026 andrasnagy.xyz
"""
gdtf_sim - turns DMX values into physical fixture state, frame by frame.

Interprets GDTF attributes (Annex A naming) the way a real fixture behaves:
movement with acceleration/speed limits (RealFade / RealAcceleration),
wheels that physically scroll between slots (split colours, gobo slide-in),
gobo index / rotation / shake, animation wheels, prisms with facets from the
profile, iris, frost, focus, zoom, framing blades, all strobe types and
additive / subtractive / CTO colour mixing.

No TouchDesigner dependency - testable from plain Python.
"""
import math
import random
import re

import gdtf_lib

WHEEL_RE = re.compile(r'^(Color|Gobo|AnimationWheel|Prism|Effects)(\d*)(.*)$')
KIND = {'Color': 'color', 'Gobo': 'gobo', 'Effects': 'gobo', 'AnimationWheel': 'anim', 'Prism': 'prism'}

ADD_COLORS = {
	'R': (1, 0, 0), 'G': (0, 1, 0), 'B': (0, 0, 1), 'W': (1, 1, 1), 'WW': (1, 0.82, 0.55),
	'CW': (0.82, 0.9, 1), 'A': (1, 0.55, 0), 'RY': (1, 0.35, 0), 'GY': (0.55, 1, 0),
	'L': (0.65, 1, 0), 'Lime': (0.65, 1, 0), 'C': (0, 1, 1), 'M': (1, 0, 1), 'Y': (1, 1, 0),
	'UV': (0.25, 0, 0.6), 'Indigo': (0.25, 0, 0.6), 'Red': (1, 0, 0), 'Green': (0, 1, 0),
	'Blue': (0, 0, 1), 'White': (1, 1, 1), 'Amber': (1, 0.55, 0), 'Cyan': (0, 1, 1),
	'Magenta': (1, 0, 1), 'Yellow': (1, 1, 0), 'Orange': (1, 0.4, 0),
}


def kelvin_rgb(k):
	"""Approximate normalised RGB of a black body (Tanner Helland fit)."""
	t = max(1000.0, min(40000.0, k)) / 100.0
	r = 255.0 if t <= 66 else 329.698727446 * ((t - 60) ** -0.1332047592)
	g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * ((t - 60) ** -0.0755148492)
	b = 255.0 if t >= 66 else (0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
	c = [min(max(x, 0.0), 255.0) / 255.0 for x in (r, g, b)]
	m = max(c)
	return [x / m for x in c]


class Mover:
	"""Second-order follower: accelerates to vmax, decelerates onto target."""

	def __init__(self, value=0.0, vmax=1e9, accel=1e9, wrap=None):
		self.pos, self.vel = float(value), 0.0
		self.vmax, self.accel, self.wrap = vmax, accel, wrap

	def step(self, target, dt):
		if self.wrap:  # shortest way round a wheel
			d = (target - self.pos) % self.wrap
			if d > self.wrap / 2:
				d -= self.wrap
		else:
			d = target - self.pos
		if self.vmax >= 1e8:
			self.pos, self.vel = (self.pos + d), 0.0
		else:
			vdes = math.copysign(min(self.vmax, math.sqrt(2 * self.accel * abs(d))), d)
			dv = vdes - self.vel
			self.vel += math.copysign(min(abs(dv), self.accel * dt), dv)
			move = self.vel * dt
			if abs(move) >= abs(d):
				move, self.vel = d, 0.0
			self.pos += move
		if self.wrap:
			self.pos %= self.wrap
		return self.pos

	def spin(self, speed, dt):
		self.vel = speed
		self.pos += speed * dt
		if self.wrap:
			self.pos %= self.wrap
		return self.pos


def _speed(fn, default_time, rng):
	"""(vmax, accel) in physical units/s from the profile's RealFade / RealAcceleration."""
	t = fn.get('realfade') or default_time
	vmax = abs(rng) / t if t > 0 else 1e9
	a = fn.get('realaccel') or (t * 0.25 if t > 0 else 0)
	accel = vmax / a if a > 0 else 1e9
	return vmax, accel


class FixtureSim:
	def __init__(self, info, mode, beams_for):
		"""beams_for(geometry name) -> list of beam names fed by that geometry."""
		self.info, self.mode, self.beams_for = info, mode, beams_for
		self.channels = mode['channels']
		self.t = 0.0
		self.rand = random.Random(1)
		self.movers = {}
		self.phase = {}
		self.wheelkeys = self._wheelKeys()
		# a pixel channel names its geometry reference instance (P01, P02 ...): it drives that pixel
		self.beamnames = sorted({b for ch in self.channels for b in beams_for(ch.get('instance') or ch['geometry'])})
		# colour mixing: a beam takes it from the most specific geometry that has colour
		# channels (its own pixel), not also from zones above it (a background colour would
		# otherwise be added to every pixel)
		self.colorowner = {}
		for ch in self.channels:
			if (ch['attribute'] or '').startswith(('ColorAdd_', 'ColorRGB_', 'ColorSub_')):
				key = ch.get('instance') or ch['geometry']
				bs = beams_for(key)
				for b in bs:
					if b not in self.colorowner or len(bs) < self.colorowner[b][1]:
						self.colorowner[b] = (key, len(bs))

	def _wheelKeys(self):
		keys = {}
		for ch in self.channels:
			for fn in ch['functions']:
				m = WHEEL_RE.match(fn['attribute'])
				if not m or (m.group(1) == 'Color' and not m.group(2)):
					continue
				key = m.group(1) + (m.group(2) or '1')
				if fn.get('wheel') and fn['wheel'] in self.info['wheels']:
					keys.setdefault(key, fn['wheel'])
				elif key not in keys:
					keys[key] = None
		# functions without a Wheel reference: match by wheel name (e.g. 'AnimationWheel', 'Prism1')
		hint = {'AnimationWheel': 'anim', 'Prism': 'prism', 'Gobo': 'gobo', 'Effects': 'effect', 'Color': 'colo'}
		used = {w for w in keys.values() if w}
		for key, w in list(keys.items()):
			if w:
				continue
			fam, num = re.match(r'^([A-Za-z]+?)(\d+)$', key).groups()
			cands = [n for n in self.info['wheels'] if hint[fam] in n.lower() and n not in used]
			cands.sort(key=lambda n: (num not in n, n))
			keys[key] = cands[0] if cands else None
			if keys[key]:
				used.add(keys[key])
		return keys

	def _mover(self, key, value, vmax=1e9, accel=1e9, wrap=None):
		mv = self.movers.get(key)
		if mv is None:
			mv = self.movers[key] = Mover(value, vmax, accel, wrap)
		mv.vmax, mv.accel = vmax, accel
		mv.wrap = wrap
		return mv

	def _strobe(self, key, kind, hz, dt):
		"""Shutter transmission 0..1 for strobe-type functions."""
		self.live = True
		hz = max(hz, 0.05)
		ph = self.phase.get(key, 0.0) + hz * dt
		self.phase[key] = ph
		f = ph % 1.0
		flash = min(0.5, 0.025 * hz)  # ~25 ms flash, like a real LED / discharge strobe
		if 'RandomPulse' in kind or 'Random' in kind:
			nxt = self.phase.setdefault(key + '_next', 0.0)
			if self.t >= nxt:
				self.phase[key + '_on'] = self.t
				self.phase[key + '_next'] = self.t + self.rand.expovariate(hz)
			return 1.0 if self.t - self.phase.get(key + '_on', -9) < 0.025 else 0.0
		if 'PulseOpen' in kind:
			return f
		if 'PulseClose' in kind:
			return 1.0 - f
		if 'Pulse' in kind:
			return math.sin(math.pi * f) ** 2
		return 1.0 if f < flash else 0.0

	def update(self, values, dt):
		"""values: list of DMX ints in channel order. Returns the physical state."""
		self.t += dt
		self.live = False  # set by anything that changes with time alone (strobe, shake, random)
		info = self.info
		axes = {}
		beams = {b: {'dimmer': None, 'add': [0.0, 0.0, 0.0], 'hasadd': False, 'sub': [0.0, 0.0, 0.0],
					 'shutter': 1.0, 'cto': None, 'hsb': [None, None, None], 'tint': 0.0}
				 for b in self.beamnames}
		optics = {'zoom': None, 'focus': 0.5, 'frost': 0.0, 'iris': 1.0,
				  'blades': [[0.0, 0.0, 0.0] for _ in range(4)], 'shaperrot': 0.0, 'wheels': {}}
		wheel_cmd = {}  # key -> {'slot': (mode, value), 'rot': (mode, value)}

		for ch, v in zip(self.channels, values):
			fn, cs, phys = gdtf_lib.function_at(ch, v)
			if fn is None:
				continue
			a = fn['attribute']
			rng = fn['physto'] - fn['physfrom']
			norm = (phys - fn['physfrom']) / rng if rng else (1.0 if fn['physfrom'] > 0 else 0.0)
			cid = '%s.%d' % (ch['geometry'], ch['offsets'][0])
			tb = self.beams_for(ch.get('instance') or ch['geometry'])

			# ---- position
			if a in ('Pan', 'Tilt'):
				vm, ac = _speed(fn, 2.6 if a == 'Pan' else 1.8, rng or 540)
				mv = self._mover('axis.%s.%s' % (ch['geometry'], a), phys, vm, ac * 1.0)
				axes.setdefault(ch['geometry'], {})[a] = mv.step(phys, dt)
				continue
			if a in ('PanRotate', 'TiltRotate'):
				base = 'axis.%s.%s' % (ch['geometry'], a[:-6])
				mv = self._mover(base, 0.0, 1e9, 1e9)
				if abs(phys) > 1e-3:
					axes.setdefault(ch['geometry'], {})[a[:-6]] = mv.spin(phys, dt)
				continue

			# ---- intensity / shutter
			if a == 'Dimmer':
				vm, ac = _speed(fn, 0.0, 1.0)
				val = self._mover('dim.' + cid, norm, vm, ac).step(norm, dt)
				for b in tb:
					beams[b]['dimmer'] = val
				continue
			if a.startswith('Shutter') and re.match(r'^Shutter\d*$', a):
				open_ = 1.0 if phys > 0.5 else 0.0
				if cs and re.search(r'open', cs['name'], re.I):
					open_ = 1.0
				elif cs and re.search(r'clos', cs['name'], re.I):
					open_ = 0.0
				for b in tb:
					beams[b]['shutter'] *= open_
				continue
			if a.startswith('Shutter') or a.startswith('Strobe'):
				if fn['physfrom'] == fn['physto'] == 0:
					hz = 1 + 20 * norm  # profile gives no frequency: spread 1..21 Hz over the range
				elif phys <= 0.01:
					continue  # 0 Hz strobe = shutter open
				else:
					hz = phys
				val = self._strobe('st.' + cid + a, a, hz, dt)
				for b in tb:
					beams[b]['shutter'] *= val
				continue

			# ---- colour mixing
			if a.startswith(('ColorAdd_', 'ColorRGB_', 'ColorSub_')):
				key = ch.get('instance') or ch['geometry']
				tb = [b for b in tb if self.colorowner.get(b, (key,))[0] == key]
			if a.startswith('ColorAdd_') or a.startswith('ColorRGB_'):
				col = ADD_COLORS.get(a.split('_', 1)[1])
				if col:
					val = self._mover('col.' + cid, norm, *_speed(fn, 0.0, 1.0)).step(norm, dt)
					for b in tb:
						beams[b]['hasadd'] = True
						for i in range(3):
							beams[b]['add'][i] += col[i] * val
				continue
			if a.startswith('ColorSub_'):
				k = a.split('_', 1)[1]
				val = self._mover('col.' + cid, norm, *_speed(fn, 0.15, 1.0)).step(norm, dt)
				for b in tb:
					if k in ('C', 'M', 'Y'):
						i = 'CMY'.index(k)
						beams[b]['sub'][i] = max(beams[b]['sub'][i], val)
					elif k in ('R', 'G', 'B'):  # subtractive filter that lets R/G/B through
						for i, kk in enumerate('RGB'):
							if kk != k:
								beams[b]['sub'][i] = max(beams[b]['sub'][i], val)
				continue
			if a in ('CTO', 'CTC', 'CTB'):
				unit = info.get('attributes', {}).get(a, {}).get('unit', '')
				for b in tb:
					beams[b]['cto'] = ('K', phys) if (unit == 'ColorTemperature' or phys > 500) else (a, norm)
				continue
			if a.startswith('HSB_'):
				i = {'HSB_Hue': 0, 'HSB_Saturation': 1, 'HSB_Brightness': 2}.get(a)
				if i is not None:
					for b in tb:
						beams[b]['hsb'][i] = phys if i == 0 else norm
				continue
			if a == 'Tint':
				for b in tb:
					beams[b]['tint'] = phys
				continue

			# ---- beam shaping
			if a == 'Zoom' or a.startswith('Zoom'):
				if a == 'Zoom':
					vm, ac = _speed(fn, 1.2, rng or 40)
					optics['zoom'] = self._mover('zoom', phys, vm, ac).step(phys, dt)
				continue
			if re.match(r'^Focus\d*$', a):
				optics['focus'] = self._mover('focus', norm, *_speed(fn, 0.8, 1.0)).step(norm, dt)
				continue
			if re.match(r'^Frost\d*$', a):
				optics['frost'] = max(optics['frost'], self._mover('frost' + a, norm, *_speed(fn, 0.25, 1.0)).step(norm, dt))
				continue
			if a.startswith('Frost'):  # FrostPulse... -> pulsing frost
				optics['frost'] = max(optics['frost'], self._strobe('fr' + cid, 'Pulse', max(phys, 0.5), dt))
				continue
			if a == 'Iris':
				openstart = any(re.search(r'open', s['name'], re.I) and s['dmxfrom'] == fn['dmxfrom'] for s in fn['sets'])
				# GDTF Iris is a ratio of the open diameter; profiles that start at 'open' count down
				if rng and 0.0 <= min(fn['physfrom'], fn['physto']) and max(fn['physfrom'], fn['physto']) <= 1.0:
					ap = phys  # the profile gives the open-diameter ratio directly
				else:
					ap = 1.0 - norm if openstart else norm
				ap = max(ap, 0.06)
				optics['iris'] = self._mover('iris', ap, *_speed(fn, 0.3, 1.0)).step(ap, dt)
				continue
			if a.startswith('Iris'):  # IrisStrobe / IrisPulse...
				optics['iris'] = 0.15 + 0.85 * self._strobe('ir' + cid, a, max(phys, 0.5), dt)
				continue
			m = re.match(r'^Blade(?:Soft)?(\d)(A|B|Rot)$', a)
			if m:
				i = int(m.group(1)) - 1
				if 0 <= i < 4:
					j = {'A': 0, 'B': 1, 'Rot': 2}[m.group(2)]
					optics['blades'][i][j] = phys if j == 2 else norm
				continue
			if a == 'ShaperRot':
				optics['shaperrot'] = phys
				continue

			# ---- wheels
			m = WHEEL_RE.match(a)
			if m and not (m.group(1) == 'Color' and not m.group(2)):
				fam, n, suffix = m.group(1), m.group(2) or '1', m.group(3)
				key = fam + n
				cmd = wheel_cmd.setdefault(key, {'fam': fam})
				slot = cs['slot'] if cs and cs.get('slot') else None
				if slot is None and fn['sets'] and suffix in ('', 'SelectSpin', 'SelectShake', 'SelectEffects'):
					nearest = min((s for s in fn['sets'] if s.get('slot')), default=None,
								  key=lambda s: abs(s['dmxfrom'] - v))
					slot = nearest['slot'] if nearest else None
				if suffix in ('', 'SelectEffects'):
					if fam == 'AnimationWheel':
						# insertion: a ranged function inserts progressively; a fixed one only via a named set
						named_out = bool(cs and re.search(r'open|out|off|no ', cs['name'], re.I))
						if rng:
							cmd['insert'] = 0.0 if named_out else norm
						else:
							cmd['insert'] = 1.0 if (cs and not named_out and slot) else 0.0
						cmd['fn'] = fn
						continue
					if slot is not None:
						cmd['slot'] = ('select', slot)
						# continuous / index ranges: the wheel travels from this slot towards the next one
						if cs and re.search(r'index|scroll|contin|linear|split', fn['name'], re.I) and cs['dmxto'] > cs['dmxfrom']:
							frac = (v - cs['dmxfrom']) / float(cs['dmxto'] - cs['dmxfrom'] + 1)
							cmd['slot'] = ('select', slot + frac)
					cmd['fn'] = fn
				elif suffix == 'SelectSpin':
					if slot is not None:
						cmd['slot'] = ('select', slot)
					cmd['rot'] = ('rotate', phys)
					cmd['fn'] = fn
				elif suffix == 'SelectShake':
					if slot is not None:
						cmd['slot'] = ('select', slot)
					cmd['rot'] = ('shake', max(phys, 0.5))
					cmd['fn'] = fn
				elif suffix == 'WheelIndex':
					cmd['slot'] = ('angle', phys)
				elif suffix in ('WheelSpin', 'Spin'):
					cmd['slot'] = ('spin', phys)
					if fam == 'AnimationWheel':
						cmd['insert'] = 1.0
				elif suffix == 'WheelShake':
					cmd['slotshake'] = max(phys, 0.5)
				elif suffix in ('WheelRandom', 'WheelAudio', 'Audio', 'RandomFrequency'):
					cmd['slot'] = ('random', max(phys, 0.2))
				elif suffix in ('Pos', 'IndexRotate'):
					cmd['rot'] = ('index', phys)
				elif suffix in ('PosRotate', 'Rotate'):
					cmd['rot'] = ('rotate', phys)
				elif suffix == 'PosShake':
					cmd['rot'] = ('shake', max(phys, 0.5))
				continue

		for key, cmd in wheel_cmd.items():
			optics['wheels'][key] = self._wheel(key, cmd, dt)

		# ---- resolve colours per beam
		for b, st in beams.items():
			rgb = [min(c, 1.0) for c in st['add']] if st['hasadd'] else [1.0, 1.0, 1.0]
			rgb = [rgb[i] * (1.0 - 0.97 * st['sub'][i]) for i in range(3)]
			if st['cto']:
				kind, val = st['cto']
				if kind == 'K':
					warm = kelvin_rgb(val)
				else:
					warm = kelvin_rgb(6500 - 3300 * val) if kind in ('CTO', 'CTC') else kelvin_rgb(6500 + 6000 * val)
				rgb = [rgb[i] * warm[i] for i in range(3)]
			h, s, br = st['hsb']
			if h is not None or s is not None:
				import colorsys
				r2, g2, b2 = colorsys.hsv_to_rgb(((h or 0) % 360) / 360.0, 1.0 if s is None else s, 1.0)
				rgb = [rgb[0] * r2, rgb[1] * g2, rgb[2] * b2]
			if st['tint']:
				t = max(-1.0, min(1.0, st['tint']))
				rgb = [rgb[0] * (1 - 0.3 * max(-t, 0)), rgb[1] * (1 - 0.4 * max(t, 0)), rgb[2] * (1 - 0.3 * max(-t, 0))]
			dim = 1.0 if st['dimmer'] is None else st['dimmer']
			if br is not None:
				dim *= br
			st['rgb'] = rgb
			st['intensity'] = dim * st['shutter']
		return {'axes': axes, 'beams': beams, 'optics': optics}

	def settled(self):
		"""True when nothing will change until the DMX values do: every mover has
		arrived (a spinning one keeps its speed) and nothing ran on time alone."""
		return not self.live and all(m.vel == 0.0 for m in self.movers.values())

	def _wheel(self, key, cmd, dt):
		fam = cmd['fam']
		wname = self.wheelkeys.get(key)
		if cmd.get('fn') and cmd['fn'].get('wheel') in self.info['wheels']:
			wname = cmd['fn']['wheel']
		slots = self.info['wheels'].get(wname, []) if wname else []
		n = max(len(slots), 1)
		fn = cmd.get('fn') or {}
		vm, ac = _speed(fn, 0.0, 1.0)
		# a real wheel moves ~ one slot in 60-120 ms, a full turn in about a second
		vmax = 12.0 if vm >= 1e8 else max(vm, 1.0)
		mv = self._mover('wheel.' + key, 0.0, vmax, vmax * 8, wrap=float(n))
		mode, val = cmd.get('slot', ('select', 1))
		if mode == 'select':
			pos = mv.step(float(val - 1), dt)
		elif mode == 'angle':
			pos = mv.step((val / 360.0 * n) % n, dt)
		elif mode == 'spin':
			pos = mv.spin(val / 360.0 * n, dt)
		else:  # random
			self.live = True
			nxt = self.phase.get(key + '_rnext', 0.0)
			if self.t >= nxt:
				self.phase[key + '_rslot'] = self.rand.randrange(n)
				self.phase[key + '_rnext'] = self.t + 1.0 / val
			pos = mv.step(float(self.phase.get(key + '_rslot', 0)), dt)
		if cmd.get('slotshake'):
			self.live = True
			pos = (pos + 0.12 * math.sin(2 * math.pi * cmd['slotshake'] * self.t)) % n

		rot = 0.0
		rmode, rval = cmd.get('rot', ('index', 0.0))
		rm = self._mover('rot.' + key, 0.0, 400.0, 1500.0)
		if rmode == 'index':
			rot = rm.step(rval, dt)
		elif rmode == 'rotate':
			rot = rm.spin(rval, dt)
		else:
			self.live = True
			rm.step(0.0, dt)
			rot = 25.0 * math.sin(2 * math.pi * rval * self.t)

		ia = int(math.floor(pos)) % n
		ib = (ia + 1) % n
		t = pos - math.floor(pos)
		out = {'family': fam, 'kind': KIND[fam], 'wheel': wname, 'slots': n, 'pos': pos,
			   'a': ia, 'b': ib, 't': t, 'rot': rot, 'insert': cmd.get('insert', 1.0)}
		if fam == 'AnimationWheel':
			out['active'] = cmd.get('insert', 0.0) > 0.01 or mode == 'spin'
		else:
			out['active'] = True
		if fam == 'Prism':
			out['facets'] = slots[int(round(pos)) % n]['facets'] if slots else []
		return out
