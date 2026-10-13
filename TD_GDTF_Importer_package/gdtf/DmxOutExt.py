# MIT License Copyright (c) 2026 andrasnagy.xyz
"""
DmxOutExt - collects every gdtf_fixture (tag 'gdtffixture') under Searchroot
whose Dmxout parameter is empty or points at this component, merges their
DMX data into 512-slot universes and feeds a DMX Out CHOP (Art-Net / sACN).
"""


class DmxOutExt:
	def __init__(self, ownerComp):
		self.ownerComp = ownerComp
		self.universes = {}   # universe -> list[512]
		self.changed = True   # Collect: did any byte source change since the last call
		self.conflicts = []

	def Fixtures(self):
		"""Fixtures feeding this output, by patch (computed once per frame)."""
		cache = getattr(self, '_fixtures', None)
		if cache is not None and cache[0] == absTime.frame:
			return cache[1]
		out = self._findFixtures()
		self._fixtures = (absTime.frame, out)
		return out

	def _findFixtures(self):
		root = self.ownerComp.par.Searchroot.eval() or self.ownerComp.parent()
		out = []
		for f in root.findChildren(tags=['gdtffixture']):
			target = f.par.Dmxout.eval() if hasattr(f.par, 'Dmxout') else None
			if target in (None, self.ownerComp) and hasattr(f.ext, 'GdtfFixtureExt'):
				out.append(f)
		return sorted(out, key=lambda f: (f.par.Universe.eval(), f.par.Address.eval()))

	def Collect(self):
		"""Rebuild universe buffers. Called from the script CHOP every cook."""
		fixtures = self.Fixtures()
		datas = []
		for f in fixtures:
			try:
				datas.append(f.ext.GdtfFixtureExt.DmxData())
			except Exception as e:
				datas.append(e)
		# every fixture handed back its cached bytes: the universes are unchanged
		last = getattr(self, '_lastDatas', None)
		if last is not None and len(last) == len(datas) and all(a is b for a, b in zip(last, datas)):
			self.changed = False
			return self.universes
		self.changed = True
		self._lastDatas = datas
		unis, owner, conflicts = {}, {}, []
		for f, d in zip(fixtures, datas):
			if isinstance(d, Exception):
				conflicts.append('%s: %s' % (f.path, d))
				continue
			u, addr, data = d
			buf = unis.setdefault(u, [0] * 512)
			for i, b in enumerate(data):
				slot = addr - 1 + i
				if slot >= 512:
					conflicts.append('%s overflows universe %d' % (f.name, u))
					break
				prev = owner.get((u, slot))
				if prev and prev != f.name:
					conflicts.append('U%d.%03d %s overlaps %s' % (u, slot + 1, f.name, prev))
				owner[(u, slot)] = f.name
				buf[slot] = b
		self.universes = dict(sorted(unis.items()))
		self.conflicts = sorted(set(conflicts))
		return self.universes

	def Routing(self, dat):
		"""Fill the DMX Out CHOP routing table (one row per universe channel)."""
		dat.clear()
		dat.appendRow(['channel', 'net', 'subnet', 'universe'])
		sacn = self.ownerComp.par.Interface.eval() == 'sacn'
		offset = int(self.ownerComp.par.Universeoffset.eval())
		for u in self.universes:
			n = u + offset
			if sacn:
				dat.appendRow(['u%d' % u, 0, 0, max(n, 1)])
			else:
				pa = max(n - 1, 0)  # universe 1 -> Art-Net port address 0
				dat.appendRow(['u%d' % u, (pa >> 8) & 0x7F, (pa >> 4) & 0xF, pa & 0xF])

	def Patch(self, dat):
		dat.clear()
		dat.appendRow(['fixture', 'id', 'type', 'mode', 'universe', 'address', 'footprint', 'end'])
		for f in self.Fixtures():
			p = f.par
			fp = int(p.Footprint.eval())
			fid = p.Fixtureid.eval() if hasattr(p, 'Fixtureid') else ''
			dat.appendRow([f.name, fid, p.Fixturename.eval(), p.Mode.eval(), p.Universe.eval(), p.Address.eval(),
						   fp, p.Address.eval() + fp - 1])
		for c in self.conflicts:
			dat.appendRow(['CONFLICT', '', c, '', '', '', '', ''])

	def AutoPatch(self, universe=1, address=1):
		"""Patch all collected fixtures sequentially without gaps."""
		u, a = universe, address
		for f in sorted(self.Fixtures(), key=lambda f: f.name):
			fp = max(int(f.par.Footprint.eval()), 1)
			if a + fp - 1 > 512:
				u, a = u + 1, 1
			f.par.Universe, f.par.Address = u, a
			a += fp
