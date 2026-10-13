# MIT License Copyright (c) 2026 andrasnagy.xyz
"""
FixtureLabelsExt - fixture labels for the GDTF rig's 3D preview.

Lives on /lights/gdtf_rig/fixture_labels (Base COMP). Collects every fixture of the
rig into the 'fixture_info' table (ID, name, mode, universe, address, footprint),
draws ID / DMX address / name / mode next to each fixture by projecting it through
the render camera, adds a patch legend, and saves printable snapshots.

Input:  in1  - the rig render (passed through when labels are off)
Output: out1 - render with labels
"""
import os
import datetime

COLS = ['id', 'fixture', 'name', 'mode', 'universe', 'address', 'footprint', 'end', 'dmx', 'path']
GLYPH_W = 0.79  # advance of Menlo per font-size unit in the Text TOP
LINE_H = 1.25

class FixtureLabelsExt:
	def __init__(self, ownerComp):
		self.ownerComp = ownerComp

	# ---------------------------------------------------------------- fixtures
	def Rig(self):
		return self.ownerComp.par.Rig.eval() or self.ownerComp.parent()

	def Fixtures(self):
		rig = self.Rig()
		return [f for f in rig.findChildren(type=geometryCOMP, maxDepth=1)
				if hasattr(f.par, 'Address') and hasattr(f.par, 'Universe')]

	def FixtureId(self, f):
		if hasattr(f.par, 'Fixtureid'):
			return int(f.par.Fixtureid.eval())
		digits = ''.join(c for c in f.name if c.isdigit())
		return int(digits) if digits else 0

	def NumberIds(self):
		"""Number fixtures 1..N in patch order (universe, address)."""
		fx = sorted(self.Fixtures(), key=lambda f: (int(f.par.Universe.eval()), int(f.par.Address.eval()), f.name))
		for i, f in enumerate(fx, 1):
			if hasattr(f.par, 'Fixtureid'):
				f.par.Fixtureid = i

	def CookInfo(self, dat):
		"""fixture_info: one row per fixture, sorted by ID."""
		dat.clear()
		dat.appendRow(COLS)
		for f in sorted(self.Fixtures(), key=self.FixtureId):
			p = f.par
			u, a = int(p.Universe.eval()), int(p.Address.eval())
			fp = int(p.Footprint.eval()) if hasattr(p, 'Footprint') else 0
			name = p.Fixturename.eval() if hasattr(p, 'Fixturename') else ''
			mode = p.Mode.eval() if hasattr(p, 'Mode') else ''
			dat.appendRow([self.FixtureId(f), f.name, name or f.name, mode, u, a, fp,
						   a + max(fp, 1) - 1, 'U%d.%03d' % (u, a), f.path])

	# ------------------------------------------------------------------ labels
	def _size(self):
		return float(self.ownerComp.par.Labelsize.eval()) * self.ownerComp.op('in1').height / 720.0

	def CookSpec(self, dat):
		"""labels_spec: x, y (bottom-left, pixels), text rows for the Text TOPs."""
		absTime.frame  # follow moving fixtures / camera
		par = self.ownerComp.par
		info = self.ownerComp.op('fixture_info')
		src = self.ownerComp.op('in1')
		w, h = src.width, src.height
		cam = par.Camera.eval()
		size = self._size()
		lh, cw = size * LINE_H, size * GLYPH_W
		rows = [['x', 'y', 'text']]
		placed = []  # label boxes already used, to stack overlapping labels
		if cam:
			M = cam.projection(w, h) * cam.worldTransform.getInverse()
			for r in range(1, info.numRows):
				f = op(info[r, 'path'].val)
				if f is None:
					continue
				p = M * (f.worldTransform * tdu.Position(0, 0, 0))
				if p.z < -1 or p.z > 1:
					continue  # behind the camera / clipped
				lines = []
				if par.Showid:
					lines.append('#' + info[r, 'id'].val)
				if par.Showaddress:
					lines.append(info[r, 'dmx'].val)
				if par.Showname:
					lines.append(info[r, 'name'].val)
				if par.Showmode and info[r, 'mode'].val:
					lines.append(info[r, 'mode'].val)
				if not lines:
					continue
				bw, bh = max(len(l) for l in lines) * cw, len(lines) * lh
				x = min(max((p.x * 0.5 + 0.5) * w - bw / 2, 4), w - bw - 4)
				ytop = (p.y * 0.5 + 0.5) * h - float(par.Labeloffset.eval()) * h / 720.0
				moved = True
				while moved:
					moved = False
					for (bx, by, bbw, bbh) in placed:
						if x < bx + bbw + 6 and bx < x + bw + 6 and ytop > by - bbh - 4 and by > ytop - bh - 4:
							ytop = by - bbh - 8
							moved = True
				placed.append((x, ytop, bw, bh))
				cx = x + bw / 2  # each line centred under the fixture
				for i, l in enumerate(lines):
					rows.append([int(cx - len(l) * cw / 2), int(ytop - (i + 1) * lh), l])
		if par.Showlegend and info.numRows > 1:
			table = [('ID', 'DMX', 'FIXTURE', 'MODE')] + [
				('#' + info[r, 'id'].val, info[r, 'dmx'].val, info[r, 'name'].val, info[r, 'mode'].val)
				for r in range(1, info.numRows)]
			widths = [max(len(t[c]) for t in table) for c in range(4)]
			for i, t in enumerate(table):
				text = '  '.join(t[c].ljust(widths[c]) for c in range(4))
				rows.append([12, int(12 + (len(table) - 1 - i) * lh), text])
		dat.clear()
		dat.appendRows(rows)

	def CookBackground(self, dat):
		"""labels_bgspec: a solid block behind every text line so labels print legibly."""
		spec = self.ownerComp.op('labels_spec')
		cw = self._size() * GLYPH_W
		dat.clear()
		dat.appendRow(['x', 'y', 'text'])
		for r in range(1, spec.numRows):
			n = len(spec[r, 'text'].val)
			dat.appendRow([int(int(spec[r, 'x']) - cw * 0.5), spec[r, 'y'], '█' * (n + 1)])

	# ---------------------------------------------------------------- snapshot
	def SaveSnapshot(self):
		folder = self.ownerComp.par.Snapshotfolder.eval() or 'snapshots'
		if not os.path.isabs(folder):  # relative to the rig's Root Folder (the package folder)
			rig = self.Rig()
			root = rig.par.Rootfolder.eval() if hasattr(rig.par, 'Rootfolder') else ''
			folder = os.path.join(root or project.folder, folder)
		os.makedirs(folder, exist_ok=True)
		path = os.path.join(folder, 'rig_labels_%s.png' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
		src = self.ownerComp.op('labeled')
		src.cook(force=True)
		src.save(path)
		debug('saved fixture label snapshot', path)
		return path
