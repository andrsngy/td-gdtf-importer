# MIT License Copyright (c) 2026 andrasnagy.xyz
"""
ShowFxExt - extension for the show_control component.

Runs simple console style effects on the gdtf_fixture components (tag
'gdtffixture') under Searchroot whose names match Fixtures. Each frame it sends
every fixture an overlay through GdtfFixtureExt.SetFx: pan / tilt offsets around
the fixture's own position (its channel parameters, or its Look At aim) and a
dimmer level. Two layers run together:

- Position FX moves the beams. Its Gate can light a fixture only while its beam
  travels one way (e.g. only while tilting down), measured on the fixture's real
  geometry, so 'down' means towards the floor however the fixture is hung.
- Dimmer FX modulates the intensity.

Phases advance with Bpm; Spread offsets the fixtures across the cycle. Switching
an effect, a gate or Active crossfades over Fade seconds.

The pixel map layer samples an animated texture (map_out) at each fixture's U V
place (Fixtures list, or a top view of the rig) and lets the pixel drive the
dimmer, the colour and / or the position. map_ui shows the texture with every
fixture's pixel and name.

Presets store the fixtures' own channel values per family (Color, Focus / Zoom,
Position - the latter with each fixture's Look At target) in the 'presets' DAT
as JSON, and recall them with a crossfade. The Chain list plays steps that each
call up to one preset per family, with a fade and a hold time.
"""
import json
import math
import random

TAU = 2.0 * math.pi


def _smooth(t):
	t = min(max(t, 0.0), 1.0)
	return t * t * (3.0 - 2.0 * t)


# --------------------------------------------------------------------------- position fx
# f(ph, k) -> (pan, tilt, gate): ph is the fixture's cycle phase 0..1, k its place in
# the group -1..1. pan / tilt are -1..1 (scaled by Pansize / Tiltsize; +tilt raises the
# beam, +pan swings it left); gate is the preset's own dimmer gate 0..1 or None.

def _fall(ph, k):
	# lit drop from high to the base position, accelerating like gravity; dark return
	duty = 0.6
	if ph < duty:
		s = ph / duty
		return 0.0, 1.0 - s * s, 1.0
	return 0.0, _smooth((ph - duty) / (1.0 - duty)), 0.0


def _rise(ph, k):
	duty = 0.6
	if ph < duty:
		s = ph / duty
		return 0.0, 1.0 - (1.0 - s) * (1.0 - s), 1.0
	return 0.0, 1.0 - _smooth((ph - duty) / (1.0 - duty)), 0.0


def _bounce(ph, k):
	# a ball dropped from the top: ever lower hops from the base, lit near the ground
	hops = [0.55 ** n for n in range(6)]
	durs = [math.sqrt(h) for h in hops]  # hop time grows with sqrt(height)
	t = ph * sum(durs)
	for h, d in zip(hops, durs):
		if t < d:
			u = t / d
			y = h * 4.0 * u * (1.0 - u)
			return 0.0, y, 1.0 - _smooth(y / 0.3)
		t -= d
	return 0.0, 0.0, 1.0


POSITION_FX = {
	'off':       ('Off', None),
	'fall':      ('Fall (lit only while falling)', _fall),
	'rise':      ('Rise (lit only while rising)', _rise),
	'tiltwave':  ('Tilt Wave', lambda ph, k: (0.0, math.sin(TAU * ph), None)),
	'pansweep':  ('Pan Sweep', lambda ph, k: (math.sin(TAU * ph), 0.0, None)),
	'circle':    ('Circle', lambda ph, k: (math.cos(TAU * ph), math.sin(TAU * ph), None)),
	'figure8':   ('Figure 8', lambda ph, k: (math.sin(TAU * ph), math.sin(2.0 * TAU * ph), None)),
	'ballyhoo':  ('Ballyhoo', lambda ph, k: (0.7 * math.sin(TAU * ph) + 0.3 * math.sin(3.0 * TAU * ph + 1.0),
										   0.6 * math.sin(2.0 * TAU * ph + 0.5) + 0.4 * math.cos(TAU * ph), None)),
	'fan':       ('Fan In / Out', lambda ph, k: (k * (0.5 - 0.5 * math.cos(TAU * ph)), 0.0, None)),
	'bounce':    ('Bounce', _bounce),
}

GATES = [
	('preset', 'Preset'),
	('none', 'Always On'),
	('down', 'Only While Tilting Down'),
	('up', 'Only While Tilting Up'),
	('left', 'Only While Panning Left'),
	('right', 'Only While Panning Right'),
]


# --------------------------------------------------------------------------- dimmer fx
# f(ph, i, cycle) -> 0..1 for fixture index i in the given cycle

def _hash(*a):
	return random.Random(hash(a)).random()


DIMMER_FX = {
	'none':    ('None', None),
	'steady':  ('Steady (Level)', lambda ph, i, c: 1.0),
	'pulse':   ('Pulse', lambda ph, i, c: 0.5 - 0.5 * math.cos(TAU * ph)),
	'chase':   ('Chase', lambda ph, i, c: (1.0 - ph) ** 3),
	'saw':     ('Saw Down', lambda ph, i, c: 1.0 - ph),
	'square':  ('On / Off', lambda ph, i, c: 1.0 if ph < 0.5 else 0.0),
	'strobe':  ('Strobe', lambda ph, i, c: 1.0 if ph < 0.15 else 0.0),
	'random':  ('Random On / Off', lambda ph, i, c: 1.0 if _hash(i, c) < 0.5 else 0.0),
	'sparkle': ('Sparkle', lambda ph, i, c: 1.0 if ph < 0.25 and _hash(i, c, 's') < 0.35 else 0.0),
}


# --------------------------------------------------------------------------- pixel map
MAP_SOURCES = [
	('noise', 'Noise'),
	('sweep', 'Sweep Bar'),
	('radial', 'Radial Rings'),
	('rainbow', 'Color Cycle'),
	('external', 'External TOP'),
]

MAP_DIMMER = {
	'max': ('Max RGB', lambda r, g, b, a: max(r, g, b)),
	'luma': ('Luminance', lambda r, g, b, a: 0.2126 * r + 0.7152 * g + 0.0722 * b),
	'r': ('Red', lambda r, g, b, a: r),
	'g': ('Green', lambda r, g, b, a: g),
	'b': ('Blue', lambda r, g, b, a: b),
	'a': ('Alpha', lambda r, g, b, a: a),
}

MAP_UI_SIZE = 512


# --------------------------------------------------------------------------- presets
# 'full' holds every channel (but the control ones) and comes first: in a chain step it
# is recalled before the family presets, which can then override part of it
PRESET_FAMILIES = [('full', 'All Channels'), ('color', 'Color'), ('focus', 'Focus / Zoom'), ('position', 'Position')]
AIM_FAMILIES = ('full', 'position')  # these also store each fixture's Look At target
# pixel map state in presets: 'full' keeps all of it (and each fixture's map U V);
# color / position keep whether the map drove that family, plus the texture if it did
MAP_TEXTURE = ('Mapsource', 'Maptop', 'Mapbeats', 'Maptintr', 'Maptintg', 'Maptintb', 'Mapres', 'Mapdimsource')
MAP_FULL = ('Mapactive', 'Mapdimmer', 'Mapcolor', 'Mapposition') + MAP_TEXTURE
MAP_DRIVE = {'color': 'Mapcolor', 'position': 'Mapposition'}
# fx state in presets: 'full' keeps all of it, 'position' the position effect if one ran.
# Bpm and the crossfade time are show-wide controls and never stored.
FX_POSITION = ('Posfx', 'Gate', 'Gatedelay', 'Gatefade', 'Posbeats', 'Spread', 'Pansize', 'Tiltsize', 'Mirror', 'Symmetry')
FX_FULL = ('Active', 'Posfx', 'Gate', 'Gatedelay', 'Gatefade', 'Dimfx', 'Level', 'Dimmermode', 'Posbeats',
		   'Dimbeats', 'Spread', 'Pansize', 'Tiltsize', 'Mirror', 'Symmetry')
# extra rows in preset_view: preset key -> fixture column label
PRESET_SECTIONS = {'pixelmap': '(pixel map)', 'fx': '(fx)'}
NO_PRESET = 'none'


def in_family(attr, family):
	if family == 'full':
		# lamp / reset / fixture control commands are never stored: recalling one could
		# switch a lamp off or reset a fixture in the middle of a show
		return not attr.startswith(('Control', 'LampControl', 'Reset', 'Function'))
	return preset_family(attr) == family


def preset_family(attr):
	"""The colour / focus / position family a channel attribute belongs to, or None."""
	if attr in ('Pan', 'Tilt'):
		return 'position'
	if attr.startswith(('Zoom', 'Focus')):
		return 'focus'
	if attr.startswith(('Color', 'CTO', 'CTC', 'CTB')) and not attr.endswith(('MSpeed', 'WheelSpin')):
		return 'color'
	return None


def fades(attr):
	"""Continuous channels crossfade; wheels, modes and other stepped ones switch."""
	return (attr in ('Pan', 'Tilt', 'Zoom', 'Dimmer', 'Iris', 'ShaperRot') or attr.endswith('Pos')
			or attr.startswith(('ColorSub', 'ColorAdd', 'ColorRGB', 'CTO', 'CTC', 'CTB', 'Focus', 'Frost', 'Blade')))


class ShowFxExt:
	def __init__(self, ownerComp):
		self.ownerComp = ownerComp
		self.posPhase = 0.0    # cycles
		self.dimPhase = 0.0
		self._from = None      # settings faded out of
		self._fade = 1.0       # 0..1 progress of the crossfade
		self._last = None      # settings of the previous frame
		self._touched = set()  # fixture paths holding an overlay
		self._dirs = {}        # fixture path -> (tilt sign, pan sign, frame)
		self._taps = []
		self._markers = None   # map_ui marker layout last built
		self._drag = None      # fixture path being dragged in map_ui
		self._mapres = (1, 1)
		self._fades = {}       # (fixture path, par name) -> (par, from, to, start, seconds)
		self._gates = {}       # fixture path -> dimmer gate state (delay / fade)
		self._chainStep = -1
		self._chainStart = 0.0
		self._chainWasOn = False

	# ------------------------------------------------------------------ fixtures
	def Fixtures(self):
		"""Controlled fixtures, in phase order: the Fixtures list when any row names a
		fixture (unticked rows skipped), otherwise every fixture matching the name pattern.
		Kept for half a second (a big rig has hundreds); list edits refresh it."""
		cache = getattr(self, '_fixCache', None)
		if cache is not None and 0 <= absTime.frame - cache[0] < 30:
			return cache[1]
		out = self._findFixtures()
		self._fixCache = (absTime.frame, out)
		return out

	def _findFixtures(self):
		blocks = [b for b in self.ownerComp.seq.Fix.blocks if b.par.Fixture.eval() is not None]
		if not blocks:
			return self.RigFixtures()
		out = []
		for blk in blocks:
			f = blk.par.Fixture.eval()
			if blk.par.On.eval() and f not in out and hasattr(f.ext, 'GdtfFixtureExt'):
				out.append(f)
		return out

	def RigFixtures(self):
		"""Every fixture under Searchroot whose name matches the Name Pattern, by name."""
		p = self.ownerComp.par
		root = p.Searchroot.eval() or self.ownerComp.parent()
		pattern = p.Fixtures.eval().strip() or '*'
		out = [f for f in root.findChildren(tags=['gdtffixture'])
			   if hasattr(f.ext, 'GdtfFixtureExt') and any(tdu.match(pat, [f.name]) for pat in pattern.split())]
		return sorted(out, key=lambda f: f.name)

	def FillList(self):
		"""Fill the Fixtures list with every fixture the name pattern finds."""
		self._fixCache = None
		fixtures = self.RigFixtures()
		seq = self.ownerComp.seq.Fix
		seq.numBlocks = max(len(fixtures), 1)
		for blk, f in zip(seq.blocks, fixtures):
			blk.par.Fixture = f.path
			blk.par.On = True

	def MapPlaces(self, fixtures):
		"""{fixture path: (u, v)} - the list's U V, or a top view when the list is unused."""
		blocks = [b for b in self.ownerComp.seq.Fix.blocks if b.par.Fixture.eval() is not None]
		if not blocks:
			return self._topView(fixtures)
		return {b.par.Fixture.eval().path: (b.par.Uvx.eval(), b.par.Uvy.eval()) for b in blocks}

	def AutoPlace(self):
		"""Write a top view of the rig (stage left = left, downstage = bottom) into the list."""
		self._fixCache = None
		if not any(b.par.Fixture.eval() is not None for b in self.ownerComp.seq.Fix.blocks):
			self.FillList()
		blocks = [b for b in self.ownerComp.seq.Fix.blocks if b.par.Fixture.eval() is not None]
		places = self._topView([b.par.Fixture.eval() for b in blocks])
		for b in blocks:
			b.par.Uvx, b.par.Uvy = places[b.par.Fixture.eval().path]

	@staticmethod
	def _topView(fixtures):
		pos = {f.path: f.worldTransform * tdu.Position(0, 0, 0) for f in fixtures}
		if not pos:
			return {}
		xs, zs = [q.x for q in pos.values()], [q.z for q in pos.values()]
		span = max(max(xs) - min(xs), max(zs) - min(zs), 1e-6)
		cx, cz = (max(xs) + min(xs)) / 2.0, (max(zs) + min(zs)) / 2.0
		k = 0.8 / span  # keep a 10% margin
		return {path: (0.5 + (q.x - cx) * k, 0.5 - (q.z - cz) * k) for path, q in pos.items()}

	def _sampler(self):
		"""(sample(u, v) -> (r, g, b, a), resolution) of map_out, read back once a frame."""
		top = self.ownerComp.op('map_out')
		arr = top.numpyArray(delayed=True) if top is not None else None
		if arr is None or not arr.size:
			return (lambda u, v: (0.0, 0.0, 0.0, 0.0)), (1, 1)
		h, w = arr.shape[0], arr.shape[1]

		def sample(u, v):
			px = arr[min(max(int(v * h), 0), h - 1), min(max(int(u * w), 0), w - 1)]
			return float(px[0]), float(px[1]), float(px[2]), float(px[3])
		return sample, (w, h)

	def ClearList(self):
		self._fixCache = None
		seq = self.ownerComp.seq.Fix
		seq.numBlocks = 1  # a sequence keeps at least one block
		seq.blocks[0].par.Fixture = ''
		seq.blocks[0].par.On = True
		seq.blocks[0].par.Uvx = seq.blocks[0].par.Uvy = 0.5

	def _directions(self, f):
		# re-measured now and then: they follow the fixture's base position
		d = self._dirs.get(f.path)
		if d is None or absTime.frame - d[2] > 30:
			t, pn = f.ext.GdtfFixtureExt.FxDirections()
			d = self._dirs[f.path] = (t, pn, absTime.frame)
		return d[0], d[1]

	# ------------------------------------------------------------------ controls
	def Tap(self):
		now = absTime.seconds
		self._taps = [t for t in self._taps if now - t < 2.5] + [now]
		if len(self._taps) >= 2:
			iv = [b - a for a, b in zip(self._taps, self._taps[1:])][-4:]
			self.ownerComp.par.Bpm = round(60.0 / (sum(iv) / len(iv)), 1)
			self.Restart()

	def Restart(self):
		self.posPhase = self.dimPhase = 0.0

	def Stop(self):
		"""Drop the overlay on every fixture immediately."""
		for f in self.Fixtures():
			f.ext.GdtfFixtureExt.ClearFx()
		self._touched.clear()

	# ------------------------------------------------------------------ frame
	def _settings(self):
		p = self.ownerComp.par
		if not p.Active.eval():
			return ('off', 'none', 'none')
		return (p.Posfx.eval(), p.Gate.eval(), p.Dimfx.eval())

	def Tick(self):
		"""Called every frame by the 'tick' Execute DAT."""
		p = self.ownerComp.par
		dt = min(max(absTime.stepSeconds, 0.0), 0.1)
		beat = p.Bpm.eval() / 60.0 * dt
		clock = getattr(self, 'beatClock', None)  # beats from outside (a show locked to music)
		if clock is not None:
			self.posPhase = clock / max(p.Posbeats.eval(), 0.01)
			self.dimPhase = clock / max(p.Dimbeats.eval(), 0.01)
		else:
			self.posPhase += beat / max(p.Posbeats.eval(), 0.01)
			self.dimPhase += beat / max(p.Dimbeats.eval(), 0.01)
		self._runChain()
		self._runFades()

		cur = self._settings()
		if self._last is not None and cur != self._last:
			self._from = self._last
			self._fade = 0.0
		self._last = cur
		fade = p.Fade.eval()
		self._fade = 1.0 if fade <= 0 else min(self._fade + dt / fade, 1.0)
		w = _smooth(self._fade)
		if w >= 1.0:
			self._from = None

		fixtures = self.Fixtures()
		paths = {f.path for f in fixtures}
		for path in self._touched - paths:
			o = op(path)
			if o is not None and hasattr(o.ext, 'GdtfFixtureExt'):
				o.ext.GdtfFixtureExt.ClearFx()
		self._touched &= paths

		mapping = p.Mapactive.eval() and (p.Mapdimmer.eval() or p.Mapcolor.eval() or p.Mapposition.eval())
		idle = cur == ('off', 'none', 'none') and self._from is None and not mapping
		# the map view follows the texture live while it drives fixtures; otherwise a few
		# times a second is plenty (with hundreds of fixtures this is the costly part)
		if not idle or self._markers is None:  # list edits reset _markers (parexec)
			places = self.MapPlaces(fixtures)
			sample, res = self._sampler()
			samples = {f.path: sample(*places.get(f.path, (0.5, 0.5))) for f in fixtures}
			self._updateMapUI(fixtures, places, samples, res)

		if idle:
			if self._touched:
				self.Stop()
			return

		n = len(fixtures)
		for i, f in enumerate(fixtures):
			a = self._evaluate(cur, i, n, f)
			if self._from is not None:
				b = self._evaluate(self._from, i, n, f)
				a = tuple(bv + (av - bv) * w for av, bv in zip(a, b))
			pan, tilt, g, gmix, dv, dmix = a
			if gmix > 0.0:
				g = self._shapeGate(f.path, g, dt)
			else:
				self._gates.pop(f.path, None)
			dim = (1.0 + (g - 1.0) * gmix) * (1.0 + (dv - 1.0) * dmix)
			mix = max(gmix, dmix)
			color, cmix = (1.0, 1.0, 1.0), 0.0
			if mapping:
				r, g, b, al = samples[f.path]
				if p.Mapposition.eval():  # red -> pan, green -> tilt, 0.5 = no offset
					pan += 2.0 * r - 1.0
					tilt += 2.0 * g - 1.0
				if p.Mapdimmer.eval():
					v = MAP_DIMMER.get(p.Mapdimsource.eval(), MAP_DIMMER['max'])[1](r, g, b, al)
					dim, mix = (dim * v if mix > 0.0 else v), 1.0
				if p.Mapcolor.eval():
					m = max(r, g, b)
					# with the dimmer mapped too, brightness goes there and the colour stays full
					color = (r / m, g / m, b / m) if p.Mapdimmer.eval() and m > 1e-4 else (r, g, b)
					cmix = 1.0
			ts, ps = self._directions(f)
			f.ext.GdtfFixtureExt.SetFx(pan * p.Pansize.eval() * ps, tilt * p.Tiltsize.eval() * ts,
									   dim * p.Level.eval(), mix, p.Dimmermode.eval() == 'multiply',
									   color, cmix)
			self._touched.add(f.path)

	def OpenMap(self):
		self.ownerComp.op('map_ui').openViewer(borders=True)

	def MapMouse(self, event, u, v):
		"""Map View mouse: 'press' picks the nearest fixture square, 'drag' moves it
		(snapped to the centre of the pixel under the mouse), 'release' drops it."""
		if event == 'release':
			self._drag = None
			self._markers = None  # redraw the borders
			return
		res = getattr(self, '_mapres', (1, 1))
		if event == 'press':
			self._drag = None
			fixtures = self.Fixtures()
			places = self.MapPlaces(fixtures)
			if not places:
				return
			path, (pu, pv) = min(places.items(), key=lambda kv: (kv[1][0] - u) ** 2 + (kv[1][1] - v) ** 2)
			reach = max(1.5 / res[0], 24.0 / MAP_UI_SIZE)  # within the square, or 24 view pixels
			if abs(pu - u) > reach or abs(pv - v) > reach:
				return
			if not any(b.par.Fixture.eval() is not None for b in self.ownerComp.seq.Fix.blocks):
				self.AutoPlace()  # the list was unused: positions need rows to live in
			self._drag = path
			self._markers = None
			return
		if event == 'drag' and getattr(self, '_drag', None):
			w, h = res
			col, row = min(max(int(u * w), 0), w - 1), min(max(int(v * h), 0), h - 1)
			for b in self.ownerComp.seq.Fix.blocks:
				f = b.par.Fixture.eval()
				if f is not None and f.path == self._drag:
					b.par.Uvx, b.par.Uvy = (col + 0.5) / w, (row + 0.5) / h

	def _updateMapUI(self, fixtures, places, samples, res):
		"""A square on each fixture's pixel (filled with its colour, yellow while dragged)
		in map_ui, and its name and pixel drawn into the map_text image (map_labels /
		map_shadow tables). Squares are moved, not recreated, so a drag is not lost."""
		ui = self.ownerComp.op('map_ui')
		if ui is None:
			return
		self._mapres = res
		w, h = res
		drag = getattr(self, '_drag', None)
		layout = tuple((f.name, places.get(f.path)) for f in fixtures) + (res, drag)
		cw, ch = MAP_UI_SIZE / float(w), MAP_UI_SIZE / float(h)
		if layout != self._markers:
			self._markers = layout
			names = {'px_' + f.name for f in fixtures}
			for c in ui.findChildren(tags=['mapmarker'], depth=1):
				if c.valid and c.name not in names:
					c.destroy()
			labels, shadow = self.ownerComp.op('map_labels'), self.ownerComp.op('map_shadow')
			for t in (labels, shadow):
				t.clear()
				t.appendRow(['x', 'y', 'text'])
			for i, f in enumerate(fixtures):
				u, v = places.get(f.path, (0.5, 0.5))
				col, row = min(max(int(u * w), 0), w - 1), min(max(int(v * h), 0), h - 1)
				sq = ui.op('px_' + f.name)
				if sq is None:
					sq = ui.create(containerCOMP, 'px_' + f.name)
					sq.tags.add('mapmarker')
					sq.nodeX, sq.nodeY = 0, -150 * i - 200
					sq.par.bgalpha = 1
					sq.par.clickthrough = True  # clicks reach map_ui, which does the dragging
					for side in ('leftborder', 'rightborder', 'topborder', 'bottomborder'):
						sq.par[side] = 'bordera'
				size = max(cw, 10.0)
				x, y = col * cw + cw / 2.0 - size / 2.0, row * ch + ch / 2.0 - size / 2.0
				sq.par.x, sq.par.y = x, y
				sq.par.w = sq.par.h = size
				hot = f.path == drag
				sq.par.borderar, sq.par.borderag, sq.par.borderab = (1, 0.85, 0) if hot else (1, 1, 1)
				sq.par.borderaalpha = 1
				text = '%s  px %d,%d' % (f.name, col, row)
				lx = x + size + 6 if x + size + 6 + 140 <= MAP_UI_SIZE else x - 146  # keep it in view
				ly = min(max(y + size / 2.0 - 5, 4), MAP_UI_SIZE - 14)
				labels.appendRow([int(lx), int(ly), text])
				shadow.appendRow([int(lx) + 1, int(ly) - 1, text])
		for f in fixtures:
			sq = ui.op('px_' + f.name)
			if sq is not None:
				r, g, b, a = samples.get(f.path, (0.0, 0.0, 0.0, 0.0))
				sq.par.bgcolorr, sq.par.bgcolorg, sq.par.bgcolorb = r, g, b

	# ------------------------------------------------------------------ presets
	def Presets(self):
		"""{family: {name: {'values': {fixture path: {par: value}}, 'lookat': {path: target}}}}"""
		try:
			d = json.loads(self.ownerComp.op('presets').text or '{}')
		except ValueError:
			d = {}
		for fam, _ in PRESET_FAMILIES:
			d.setdefault(fam, {})
		# stored paths are absolute: map them onto this rig when it was moved or shared
		for fam in d.values():
			for pr in fam.values():
				if not isinstance(pr, dict):
					continue
				for k in ('values', 'lookat'):
					if isinstance(pr.get(k), dict):
						pr[k] = {self._here(p): v for p, v in pr[k].items()}
				if isinstance(pr.get('lookat'), dict):
					pr['lookat'] = {p: self._here(t) if t else t for p, t in pr['lookat'].items()}
				pm = pr.get('pixelmap')
				if isinstance(pm, dict) and isinstance(pm.get('uv'), dict):
					pm['uv'] = {self._here(p): v for p, v in pm['uv'].items()}
		return d

	def _here(self, path):
		"""A stored operator path, found again under this rig when it moved or was copied:
		the longest trailing part of the path that exists below the rig."""
		rig = self.ownerComp.parent()
		if not path or str(path).startswith(rig.path + '/'):
			return path
		parts = [s for s in str(path).split('/') if s]
		for i in range(1, len(parts)):
			o = rig.op('/'.join(parts[i:]))
			if o is not None:
				return o.path
		return path

	def _savePresets(self, d):
		self.ownerComp.op('presets').text = json.dumps(d, indent=1, sort_keys=True)
		self.UpdatePresetMenus(d)

	def UpdatePresetMenus(self, d=None):
		"""Fill the menu tables the preset and chain menus read."""
		d = d or self.Presets()
		every = self.ownerComp.op('preset_menu_all')
		every.clear()
		every.appendRow(['name', 'label'])
		for fam, label in PRESET_FAMILIES:
			t = self.ownerComp.op('preset_menu_' + fam)
			t.clear()
			t.appendRow(['name', 'label'])
			t.appendRow([NO_PRESET, '-'])
			for name in sorted(d[fam], key=str.lower):
				t.appendRow([name, name])
				every.appendRow(['%s:%s' % (fam, name), '%s: %s' % (label, name)])
		# a table menu can keep what it read while its table was empty: re-apply them
		menus = [self.ownerComp.par.Presetrecall] + [getattr(b.par, fam.capitalize())
													  for b in self.ownerComp.seq.Step.blocks
													  for fam, _ in PRESET_FAMILIES]
		for par in menus:
			src = par.menuSource  # re-assigning the same string does not re-read it
			par.menuSource = ''
			par.menuSource = src

	def _status(self, msg):
		self.ownerComp.par.Presetstatus = msg

	def RecordPreset(self, family=None, name=None):
		"""Store the controlled fixtures' current values of one family under a name."""
		p = self.ownerComp.par
		family = family or p.Presetfamily.eval()
		name = (name if name is not None else p.Presetname.eval()).strip()
		if not name:
			self._status('Type a preset name first')
			return
		values, lookat = {}, {}
		for f in self.Fixtures():
			e = f.ext.GdtfFixtureExt
			v = {n: int(getattr(f.par, n).eval()) for n, ch in e.chanmap.items()
				 if in_family(ch['attribute'], family)}
			if v:
				values[f.path] = v
				if family in AIM_FAMILIES and hasattr(f.par, 'Lookat'):
					t = f.par.Lookat.eval()
					lookat[f.path] = t.path if t is not None else ''
		if not values:
			self._status('No controlled fixture has %s channels' % family)
			return
		d = self.Presets()
		verb = 'Updated' if name in d[family] else 'Recorded'
		pm, fx = self._mapState(family), self._fxState(family)
		d[family][name] = {'values': values, 'lookat': lookat}
		if pm is not None:
			d[family][name]['pixelmap'] = pm
		if fx is not None:
			d[family][name]['fx'] = fx
		self._savePresets(d)
		self._status('%s %s "%s" (%d fixtures, %d channels%s%s)' % (
			verb, family, name, len(values), sum(len(v) for v in values.values()), self._mapNote(family, pm),
			self._fxNote(family, fx)))
		if (family, name) == self._selected():
			self.ShowPresetTable()

	def RecallPreset(self, family, name, fade=None):
		"""Crossfade the stored fixtures to a preset over 'fade' seconds."""
		if not name or name == NO_PRESET:
			return
		pr = self.Presets()[family].get(name)
		if pr is None:
			self._status('No %s preset "%s"' % (family, name))
			return
		fade = self.ownerComp.par.Presetfade.eval() if fade is None else fade
		now = absTime.seconds
		missing = []
		self._applyMapState(family, pr.get('pixelmap'))
		self._applyFxState(family, pr.get('fx'))
		for path, vals in pr['values'].items():
			f = op(path)
			if f is None or not hasattr(f.ext, 'GdtfFixtureExt'):
				continue
			chanmap = f.ext.GdtfFixtureExt.chanmap
			target = pr.get('lookat', {}).get(path, '') if family in AIM_FAMILIES else ''
			if target and op(target) is None:
				missing.append(target.split('/')[-1])
				target = ''  # the aim object is gone: leave Look At off rather than broken
			if family in AIM_FAMILIES and path in pr.get('lookat', {}) and hasattr(f.par, 'Lookat'):
				f.par.Lookat = target
			for parname, v in vals.items():
				ch = chanmap.get(parname)
				if ch is None:
					continue  # the fixture's mode changed since recording
				# an aimed fixture still gets its recorded pan / tilt, then Look At re-solves
				# from that pose: the same pose while the target has not moved
				reaim = bool(target) and ch['attribute'] in ('Pan', 'Tilt')
				par = getattr(f.par, parname)
				if fade > 0 and fades(ch['attribute']):
					self._fades[(path, parname)] = (par, int(par.eval()), int(v), now, fade, reaim)
				else:
					self._fades.pop((path, parname), None)
					par.val = int(v)
					if reaim:
						self._reaim(f)
		self._status('Recalled %s "%s"%s%s%s' % (family, name, self._mapNote(family, pr.get('pixelmap')),
											   self._fxNote(family, pr.get('fx')),
											   ' - Look At target missing: ' + ', '.join(sorted(set(missing))) if missing else ''))

	# ------------------------------------------------------------------ presets: pixel map
	def _mapState(self, family):
		"""The pixel map state a preset of this family keeps (None: the family has none)."""
		p = self.ownerComp.par
		if family == 'full':
			st = {n: self._mapParValue(n) for n in MAP_FULL}
			st['uv'] = {b.par.Fixture.eval().path: [b.par.Uvx.eval(), b.par.Uvy.eval()]
						for b in self.ownerComp.seq.Fix.blocks if b.par.Fixture.eval() is not None}
			return st
		drive = MAP_DRIVE.get(family)
		if drive is None:
			return None
		uses = bool(p.Mapactive.eval() and getattr(p, drive).eval())
		st = {drive: uses}
		if uses:
			st.update({n: self._mapParValue(n) for n in MAP_TEXTURE})
		return st

	def _mapParValue(self, name):
		par = getattr(self.ownerComp.par, name)
		if par.style in ('TOP', 'COMP', 'OBJ', 'OP', 'CHOP', 'DAT', 'SOP', 'MAT', 'Menu', 'StrMenu', 'Str'):
			return par.val if par.style not in ('Menu', 'StrMenu') else par.eval()
		v = par.eval()
		return bool(v) if par.style == 'Toggle' else v

	def _applyMapState(self, family, st):
		if not st:
			return
		p = self.ownerComp.par
		if family == 'full':
			for n in MAP_FULL:
				if n in st:
					setattr(p, n, st[n])
			uv = st.get('uv', {})
			for b in self.ownerComp.seq.Fix.blocks:
				f = b.par.Fixture.eval()
				if f is not None and f.path in uv:
					b.par.Uvx, b.par.Uvy = uv[f.path]
			return
		drive = MAP_DRIVE.get(family)
		if drive is None or drive not in st:
			return
		setattr(p, drive, bool(st[drive]))  # off: the preset's own values show again
		if st[drive]:
			p.Mapactive = True
			for n in MAP_TEXTURE:
				if n in st:
					setattr(p, n, st[n])

	# ------------------------------------------------------------------ presets: fx
	def _fxState(self, family):
		"""The fx settings a preset of this family keeps (None: the family has none)."""
		p = self.ownerComp.par
		if family == 'full':
			return {n: self._mapParValue(n) for n in FX_FULL if hasattr(p, n)}
		if family != 'position':
			return None
		uses = bool(p.Active.eval() and p.Posfx.eval() != 'off')
		st = {'Posfx': p.Posfx.eval() if uses else 'off'}
		if uses:
			st.update({n: self._mapParValue(n) for n in FX_POSITION if hasattr(p, n)})
		return st

	def _applyFxState(self, family, st):
		"""Set the stored fx; changes blend over the FX crossfade (Fade)."""
		if not st:
			return
		p = self.ownerComp.par
		names = FX_FULL if family == 'full' else FX_POSITION
		for n in names:
			if n in st and hasattr(p, n):
				setattr(p, n, st[n])
		if family == 'position' and st.get('Posfx', 'off') != 'off':
			p.Active = True

	@staticmethod
	def _fxNote(family, st):
		if not st:
			return ''
		if family == 'full':
			if not st.get('Active'):
				return ', fx off'
			used = [v for v in (st.get('Posfx'), st.get('Dimfx')) if v not in (None, 'off', 'none')]
			return ', fx: %s' % (' + '.join(used) or 'none')
		return ', fx: %s' % st['Posfx'] if st.get('Posfx', 'off') != 'off' else ''

	@staticmethod
	def _mapNote(family, st):
		if not st:
			return ''
		if family == 'full':
			if not st.get('Mapactive'):
				return ', pixel map off'
			drives = [n[3:].lower() for n in ('Mapdimmer', 'Mapcolor', 'Mapposition') if st.get(n)]
			return ', pixel map: %s' % (', '.join(drives) or 'nothing driven')
		drive = MAP_DRIVE.get(family)
		return ', from pixel map' if st.get(drive) else ''

	def _selected(self):
		fam, _, name = self.ownerComp.par.Presetrecall.eval().partition(':')
		return fam, name

	def RecallSelected(self):
		fam, name = self._selected()
		if name:
			self.RecallPreset(fam, name)

	def UpdateSelected(self):
		"""Overwrite the selected preset with the controlled fixtures' current values."""
		fam, name = self._selected()
		if name:
			self.RecordPreset(fam, name)

	def RenameSelected(self):
		"""Rename the selected preset to the Name field; chain steps follow."""
		fam, name = self._selected()
		new = self.ownerComp.par.Presetname.eval().strip()
		d = self.Presets()
		if not name or name not in d.get(fam, {}):
			return
		if not new or new == name:
			self._status('Type the new name in Name first')
			return
		if new in d[fam]:
			self._status('A %s preset "%s" already exists' % (fam, new))
			return
		# note the steps first: once the menus refresh the old name is no longer valid
		steps = [getattr(b.par, fam.capitalize()) for b in self.ownerComp.seq.Step.blocks
				 if getattr(b.par, fam.capitalize()).eval() == name]
		d[fam][new] = d[fam].pop(name)
		self._savePresets(d)
		for par in steps:
			par.val = new
		self.ownerComp.par.Presetrecall = '%s:%s' % (fam, new)
		self._status('Renamed %s "%s" to "%s"' % (fam, name, new))

	def DeleteSelected(self):
		fam, name = self._selected()
		d = self.Presets()
		if d.get(fam, {}).pop(name, None) is not None:
			self._savePresets(d)
			self._status('Deleted %s "%s"' % (fam, name))
			self.ShowPresetTable()

	def ShowPresetTable(self):
		"""Fill preset_view with the selected preset: one row per stored channel. The
		value column is editable (Save Edits writes it back); deleting a row drops that
		channel from the preset. 'meaning' is what the value does, for reference."""
		t = self.ownerComp.op('preset_view')
		t.clear()
		t.appendRow(['fixture', 'parameter', 'channel', 'value', 'meaning'])
		fam, name = self._selected()
		pr = self.Presets().get(fam, {}).get(name)
		if pr is None:
			return
		for path, vals in sorted(pr['values'].items()):
			f = op(path)
			e = f.ext.GdtfFixtureExt if f is not None and hasattr(f.ext, 'GdtfFixtureExt') else None
			lib = f.op('gdtf_lib').module if e is not None else None
			look = pr.get('lookat', {}).get(path)
			if look is not None:
				t.appendRow([path, 'Lookat', 'Look At', look, 'aim target (empty = off)'])
			for parname, v in sorted(vals.items(), key=lambda kv: kv[0]):
				ch = e.chanmap.get(parname) if e is not None else None
				meaning = ''
				if ch is not None:
					fn, cs, phys = lib.function_at(ch, int(v))
					meaning = ('%s: %s' % (fn['name'], cs['name']) if cs and cs['name'] else
							   '%s %s' % (fn['name'], ('%.2f' % phys).rstrip('0').rstrip('.'))) if fn else ''
				t.appendRow([path, parname, ch['attribute'] if ch else '(not on fixture)', int(v), meaning])
		pm = pr.get('pixelmap') or {}
		for key, label in PRESET_SECTIONS.items():
			sect = pr.get(key) or {}
			for n in (MAP_FULL if key == 'pixelmap' else FX_FULL):
				if n in sect:
					par = getattr(self.ownerComp.par, n)
					v = sect[n]
					t.appendRow([label, n, label.strip('()').title().replace('Fx', 'FX'), int(v) if isinstance(v, bool) else v,
								 par.label + (' (0 = off, 1 = on)' if par.style == 'Toggle' else '')])
		for path, (u, v) in sorted(pm.get('uv', {}).items()):
			t.appendRow([path, 'Mapuv', 'Map U V', '%g %g' % (u, v), 'pixel map place (u v)'])

	def SaveTableEdits(self):
		"""Write preset_view back into the selected preset."""
		fam, name = self._selected()
		d = self.Presets()
		if name not in d.get(fam, {}):
			self._status('Select a preset first')
			return
		t = self.ownerComp.op('preset_view')
		values, lookat, pm = {}, {}, {}
		sections = {key: {} for key in PRESET_SECTIONS}
		sections['pixelmap'] = pm
		section_of = {label: key for key, label in PRESET_SECTIONS.items()}
		for r in range(1, t.numRows):
			path, parname, val = t[r, 'fixture'].val, t[r, 'parameter'].val, t[r, 'value'].val.strip()
			if parname == 'Lookat':
				lookat[path] = val
				continue
			if parname == 'Mapuv':
				try:
					uv = [min(max(float(x), 0.0), 1.0) for x in val.split()]
					if len(uv) != 2:
						raise ValueError
					pm.setdefault('uv', {})[path] = uv
				except ValueError:
					self._status('Row %d: U V must be two numbers, e.g. "0.25 0.5"' % r)
					return
				continue
			if path in section_of:
				sect = sections[section_of[path]]
				par = getattr(self.ownerComp.par, parname, None)
				if par is None:
					continue
				try:
					if par.style == 'Toggle':
						sect[parname] = bool(int(float(val)))
					elif par.style in ('Float', 'Int', 'RGB', 'XY'):
						sect[parname] = float(val) if par.style != 'Int' else int(float(val))
					elif par.isMenu and val not in par.menuNames:
						self._status('Row %d: %s must be one of %s' % (r, par.label, ', '.join(par.menuNames)))
						return
					else:
						sect[parname] = val
				except ValueError:
					self._status('Row %d: "%s" is not valid for %s' % (r, val, par.label))
					return
				continue
			try:
				v = int(round(float(val)))
			except ValueError:
				self._status('Row %d: "%s" is not a number' % (r, val))
				return
			f = op(path)
			ch = f.ext.GdtfFixtureExt.chanmap.get(parname) if f is not None and hasattr(f.ext, 'GdtfFixtureExt') else None
			if ch is not None:
				v = min(max(v, 0), ch['max'])
			values.setdefault(path, {})[parname] = v
		d[fam][name] = {'values': values, 'lookat': lookat}
		for key, sect in sections.items():
			if sect:
				d[fam][name][key] = sect
		self._savePresets(d)
		self.ShowPresetTable()
		self._status('Saved edits to %s "%s" (%d channels)' % (fam, name, sum(len(v) for v in values.values())))

	def EditSelected(self):
		self.ShowPresetTable()
		self.ownerComp.op('preset_view').openViewer()

	@staticmethod
	def _reaim(f):
		"""Make the fixture's Look At solve again, starting from its current pan / tilt."""
		st = getattr(f.ext.GdtfFixtureExt, '_aimState', None)
		if st:
			st['key'] = st['x'] = None

	def _runFades(self):
		now = absTime.seconds
		for key, (par, a, b, t0, dur, reaim) in list(self._fades.items()):
			if not par.valid:
				del self._fades[key]
				continue
			t = (now - t0) / dur
			if t >= 1.0:
				par.val = b
				del self._fades[key]
				if reaim:
					self._reaim(par.owner)
			else:
				par.val = int(round(a + (b - a) * _smooth(t)))

	# ------------------------------------------------------------------ chain
	def _chainSeconds(self, v):
		p = self.ownerComp.par
		return v * 60.0 / max(p.Bpm.eval(), 1.0) if p.Chainunit.eval() == 'beats' else v

	def _chainSteps(self):
		return list(self.ownerComp.seq.Step.blocks)

	def GoStep(self, i):
		"""Enter chain step i: recall its presets with its fade."""
		steps = self._chainSteps()
		if not steps:
			return
		self._chainStep = i % len(steps)
		self._chainStart = absTime.seconds
		blk = steps[self._chainStep]
		fade = self._chainSeconds(blk.par.Fadetime.eval())
		names = []
		for fam, _ in PRESET_FAMILIES:
			name = getattr(blk.par, fam.capitalize()).eval()
			if name and name != NO_PRESET:
				self.RecallPreset(fam, name, fade)
				names.append(name)
		self.ownerComp.par.Chainstatus = 'Step %d / %d  %s' % (self._chainStep + 1, len(steps),
															   ' + '.join(names) or '(no presets)')

	def ChainRestart(self):
		self.GoStep(0)

	def ChainGo(self):
		self.GoStep(self._chainStep + 1)

	def _runChain(self):
		p = self.ownerComp.par
		on = p.Chainplay.eval()
		if on and not self._chainWasOn:
			self.GoStep(0)
		self._chainWasOn = on
		if not on or self._chainStep < 0:
			return
		steps = self._chainSteps()
		if not steps:
			return
		blk = steps[min(self._chainStep, len(steps) - 1)]
		length = self._chainSeconds(blk.par.Fadetime.eval() + blk.par.Hold.eval())
		if absTime.seconds - self._chainStart < length:
			return
		if self._chainStep + 1 < len(steps):
			self.GoStep(self._chainStep + 1)
		elif p.Chainloop.eval():
			self.GoStep(0)
		else:
			p.Chainplay = False
			p.Chainstatus = 'Finished (%d steps)' % len(steps)

	def _symmetry(self):
		"""'off', 'alternate' (every other fixture mirrored - the old Mirror toggle) or
		'center' (stage left mirrors stage right, timing from the centre out)."""
		p = self.ownerComp.par
		if hasattr(p, 'Symmetry'):
			return p.Symmetry.eval()
		return 'alternate' if p.Mirror.eval() else 'off'

	def _evaluate(self, settings, i, n, f=None):
		"""(pan -1..1, tilt -1..1, gate 0..1, gate mix, dimmer fx 0..1, dimmer fx mix) for
		fixture i of n. The gate is kept apart so its on delay / fade shape only it."""
		posname, gate, dimname = settings
		p = self.ownerComp.par
		sym = self._symmetry()
		k0 = (2.0 * i / (n - 1) - 1.0) if n > 1 else 0.0
		# center symmetry: timing by distance from the centre, so both sides move as mirror images
		off = p.Spread.eval() * (abs(k0) if sym == 'center' else i / max(n, 1))
		k = (2.0 * i / (n - 1) - 1.0) if n > 1 else 0.0
		pan = tilt = 0.0
		g, gmix, dv, dmix = 1.0, 0.0, 1.0, 0.0

		fx = POSITION_FX.get(posname, (None, None))[1]
		if fx is not None:
			ph = (self.posPhase + off) % 1.0
			pan, tilt, own = fx(ph, k)
			mirror = -1.0 if (sym == 'alternate' and i % 2) or (sym == 'center' and k0 < 0) else 1.0
			pan *= mirror

			def gate_at(q):
				if gate == 'preset':
					return fx(q, k)[2]
				if gate in ('down', 'up') and f is not None:
					return self._worldGate(f, fx, gate, q, k, mirror)
				return self._gate(fx, gate, q, k)

			gv = gate_at(ph)
			if gv is not None:
				g, gmix = gv, 1.0
				# fade out ahead of time: if the gate is shut one fade later, start closing now
				# so the light is gone by the time the beam turns
				ahead = p.Gatefade.eval() * p.Bpm.eval() / 60.0 / max(p.Posbeats.eval(), 0.01)
				if ahead > 0:
					g2 = gate_at((ph + min(ahead, 0.5)) % 1.0)
					if g2 is not None and g2 < 0.01:
						g = 0.0

		dfx = DIMMER_FX.get(dimname, (None, None))[1]
		if dfx is not None:
			t = self.dimPhase + off
			dv, dmix = dfx(t % 1.0, i, int(math.floor(t))), 1.0
		return pan, tilt, g, gmix, dv, dmix

	def _shapeGate(self, path, target, dt):
		"""The gate with its on delay (Gatedelay: lights only once it has been open that
		long) and fade (Gatefade: ramps instead of snapping; _evaluate already closes the
		gate one fade early, so the fade out ends when the beam turns)."""
		p = self.ownerComp.par
		st = self._gates.setdefault(path, {'v': 0.0, 'open': None})
		# counted in frame time (like the effect phases), so it stays in step with the effect
		if target > 0.01:
			st['open'] = (st['open'] or 0.0) + dt
			if st['open'] < p.Gatedelay.eval():
				target = 0.0
		else:
			st['open'] = None
		fade = p.Gatefade.eval()
		step = dt / fade if fade > 0 else 1.0
		v = st['v']
		v = min(v + step, target) if target > v else max(v - step, target)
		st['v'] = v
		return _smooth(v) if fade > 0 else v

	def _crossesPole(self, f):
		"""Can this fixture's tilt swing (Tilt Size around its base) pass straight down /
		up? Only then does the real-beam check matter. Cached for a second per fixture."""
		cache = self.__dict__.setdefault('_poleCache', {})
		hit = cache.get(f.path)
		if hit is not None and absTime.frame - hit[1] < 60:
			return hit[0]
		e = f.ext.GdtfFixtureExt
		tilt = next((ch for ch in e.chanmap.values() if ch['attribute'] == 'Tilt'), None)
		res = True
		if tilt is not None:
			fn = max((x for x in tilt['functions'] if x['attribute'] == 'Tilt'),
					 key=lambda x: x['dmxto'] - x['dmxfrom'], default=tilt['functions'][0])
			base = e._dmxToAngle(fn, int(getattr(f.par, tilt['par']).eval()))
			res = abs(base) < self.ownerComp.par.Tiltsize.eval() + 2.0
		cache[f.path] = (res, absTime.frame)
		return res

	def _worldGate(self, f, fx, gate, ph, k, mirror):
		"""Tilting down / up measured on the real beam: 1 while the fixture's beam heads
		towards the floor (or up) in world space, so a swing through straight down - where
		the tilt keeps going but the beam rises again - stays correct."""
		p = self.ownerComp.par
		ts, ps = self._directions(f)
		if not self._crossesPole(f):
			return self._gate(fx, gate, ph, k)  # the effect's own tilt direction is the beam's
		e = 0.01
		ys = []
		for q in ((ph - e) % 1.0, (ph + e) % 1.0):
			pan, tilt, _ = fx(q, k)
			d = f.ext.GdtfFixtureExt.BeamDirection(pan * mirror * p.Pansize.eval() * ps, tilt * p.Tiltsize.eval() * ts)
			if d is None:
				return self._gate(fx, gate, ph, k)  # no pan / tilt: fall back to the effect's own motion
			ys.append(d.y / max(d.length(), 1e-9))
		v = ys[0] - ys[1] if gate == 'down' else ys[1] - ys[0]
		return _smooth(v / 0.002 + 0.5)

	@staticmethod
	def _gate(fx, gate, ph, k):
		"""1 while the effect moves the beam the gated way, else 0 (soft around turns)."""
		if gate == 'none':
			return 1.0
		e = 0.01
		p0, t0, _ = fx((ph - e) % 1.0, k)
		p1, t1, _ = fx((ph + e) % 1.0, k)
		v = {'down': t0 - t1, 'up': t1 - t0, 'left': p1 - p0, 'right': p0 - p1}.get(gate)
		if v is None:
			return None
		return _smooth(v / 0.004 + 0.5)
