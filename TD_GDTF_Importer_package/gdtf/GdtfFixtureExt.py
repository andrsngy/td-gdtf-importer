# MIT License Copyright (c) 2026 andrasnagy.xyz
"""
GdtfFixtureExt - extension for the gdtf_fixture component.

Loads a GDTF profile (GDTF Share or a local folder), builds the 3D geometry
and the optical system (wheels, gobo compositor, volumetric beams, gobo
projecting spotlights), creates one parameter per DMX channel on the
'Channels' page, simulates the fixture every frame (gdtf_sim) and exposes
DmxData() for the dmx_out collector.
"""
import json
import math
import os

ABOUT = [('License', 'License', 'MIT License Copyright (c) 2026 andrasnagy.xyz')]
import re
import threading
import time

IMAGE_KINDS = ('gobo', 'anim')
MAX_LIGHTS = 2
# fx overlay: colour mixing attribute -> level 0..1 for an RGB colour
COLOR_MIX = {
	'ColorSub_C': lambda r, g, b: 1.0 - r, 'ColorSub_M': lambda r, g, b: 1.0 - g, 'ColorSub_Y': lambda r, g, b: 1.0 - b,
	'ColorAdd_R': lambda r, g, b: r, 'ColorAdd_G': lambda r, g, b: g, 'ColorAdd_B': lambda r, g, b: b,
	'ColorAdd_W': lambda r, g, b: 0.0,
	'ColorRGB_Red': lambda r, g, b: r, 'ColorRGB_Green': lambda r, g, b: g, 'ColorRGB_Blue': lambda r, g, b: b,
}
# channels the fx overlay can change; every other channel goes out as its parameter value
OVERLAY_ATTRS = frozenset(('Pan', 'Tilt', 'Dimmer') + tuple(COLOR_MIX))
# visual simulation budget per frame, shared by all fixtures (DMX output is not affected:
# it is computed from the parameters every frame). Over budget, a fixture that ran last
# frame waits one frame, so a big rig previews at a lower rate instead of stalling.
SIM_BUDGET_MS = 10.0


def attr_function(ch, attrs):
	"""The channel function for an attribute (or any of several). Mode-dependent
	functions (e.g. a MegaPointe's Zoom in beam / spot mode) all start at the same DMX
	value, which leaves all but one with an empty range: take the widest."""
	if isinstance(attrs, str):
		attrs = (attrs,)
	cands = [f for f in ch['functions'] if f['attribute'] in attrs]
	return max(cands, key=lambda f: f['dmxto'] - f['dmxfrom']) if cands else None


def _budget():
	import sys
	b = getattr(sys, '_gdtf_sim_budget', None)
	if b is None or b['frame'] != absTime.frame:
		b = {'frame': absTime.frame, 'used': 0.0}
		sys._gdtf_sim_budget = b
	return b


BEAM_DRAW_PRIORITY = 1  # volumetric cones must not draw at the default 0 for the ray-march to composite
PAN_HOLD_WEIGHT = 4e-8  # look-at: cost of a degree of pan travel, vs beam error in radians
PAN_HOLD_MAXERR = 3e-4  # look-at: never accept more than ~1 degree of aim error to hold pan


def _lib():
	return op('gdtf_lib').module


def _sim():
	return op('gdtf_sim').module


def _parname(prefix, text, used):
	base = prefix + re.sub(r'[^a-z0-9]', '', text.lower())[:18]
	name, i = base, 2
	while name in used:
		name = '%s%d' % (base, i)
		i += 1
	used.add(name)
	return name


def _safe(text):
	return re.sub(r'[^A-Za-z0-9_]', '_', text) or 'x'


def _norm(text):
	"""Lower case letters and digits only, for searching."""
	return re.sub(r'[^0-9a-z]+', '', str(text).lower())


class GdtfFixtureExt:
	def __init__(self, ownerComp):
		self.ownerComp = ownerComp
		self.catalog = []
		self.chanmap = {}       # value par name -> channel dict
		self.setmap = {}        # menu par name -> value par name
		self._job = None
		self._profile = None
		self._simobj = None
		self._lastFacets = None
		self._imageWheels, self._colorWheels = [], []
		self._beamops = {}
		self._fx = None
		self._loadCatalog(quiet=True)
		self._buildChanmap()
		self._restoreWheels()
		self._ensureAbout()

	def _ensureAbout(self):
		"""License page on every fixture, including ones created later."""
		comp = self.ownerComp
		if any(pg.name == 'About' for pg in comp.customPages):
			return
		pg = comp.appendCustomPage('About')
		for name, label, value in ABOUT:
			p = pg.appendStr(name, label=label)[0]
			p.default = p.val = value
			p.readOnly = True

	# ------------------------------------------------------------------ paths / profile
	@property
	def RootFolder(self):
		"""Folder holding gdtf/ (library, cache, code): the rig's Rootfolder parameter when it
		has one (the folder of its .tox in a shared package), else the project folder."""
		rig = self._rig()
		f = rig.par.Rootfolder.eval() if hasattr(rig.par, 'Rootfolder') else ''
		if not f:
			return project.folder
		return f if os.path.isabs(f) else os.path.join(project.folder, f)

	def Resolve(self, path):
		"""An absolute path for a stored one: relative paths are under RootFolder, and an
		absolute path that no longer exists (a project moved to another machine) is rebased
		onto RootFolder from its 'gdtf/' part."""
		if not path:
			return ''
		path = str(path)
		if not os.path.isabs(path):
			return os.path.join(self.RootFolder, path)
		if os.path.exists(path):
			return path
		norm = path.replace('\\', '/')
		i = norm.rfind('/gdtf/')
		if i >= 0:
			moved = os.path.join(self.RootFolder, norm[i + 1:])
			if os.path.exists(moved):
				return moved
		return path

	def Relative(self, path):
		"""A path as stored: relative to RootFolder when it lies inside it."""
		if not path:
			return ''
		path = self.Resolve(path)
		root = os.path.abspath(self.RootFolder)
		ap = os.path.abspath(path)
		if ap == root or ap.startswith(root + os.sep):
			return os.path.relpath(ap, root).replace(os.sep, '/')
		return path

	def _mapPaths(self, info, fn):
		"""info with its file / model / wheel media paths passed through fn."""
		out = dict(info)
		if out.get('file'):
			out['file'] = fn(out['file'])
		out['objs'] = {k: fn(v) for k, v in (info.get('objs') or {}).items()}
		out['media'] = {k: [fn(v) if v else v for v in vs] for k, vs in (info.get('media') or {}).items()}
		return out

	@property
	def LibraryFolder(self):
		f = self.ownerComp.par.Folder.eval() or os.path.join('gdtf', 'library')
		return self.Resolve(f)

	@property
	def CacheFolder(self):
		return os.path.join(self.RootFolder, 'gdtf', 'cache')

	@property
	def Profile(self):
		"""The parsed GDTF profile. Not saved in the project (it was the bulk of the .toe):
		it comes from the shared cache file next to the extracted models, or is parsed
		from Gdtffile again; fixtures of the same type share one copy in memory."""
		if self._profile is None:
			self._profile = self._loadProfile()
		return self._profile

	def _profileCachePath(self, gdtf):
		return os.path.join(self.CacheFolder, re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.basename(gdtf)[:-5]),
							'profile.json')

	def _loadProfile(self):
		import sys
		shared = sys.__dict__.setdefault('_gdtf_profiles', {})
		dat = self.ownerComp.op('profile')
		legacy = dat.text if dat is not None else ''
		if legacy.strip():  # a profile saved inside the project by an older version: move it out
			info = json.loads(legacy)
			self._writeProfileCache(info)
			dat.text = ''
			gdtf = self.Resolve(info.get('file', ''))
		else:
			gdtf = self.Resolve(self.ownerComp.par.Gdtffile.eval())
			if not gdtf or not os.path.isfile(gdtf):
				return None
			info = None
		key = (os.path.abspath(gdtf), os.path.getmtime(gdtf) if os.path.isfile(gdtf) else 0)
		if key in shared:
			return shared[key]
		if info is None:
			cache = self._profileCachePath(gdtf)
			if os.path.isfile(cache):
				with open(cache, encoding='utf-8') as f:
					info = self._mapPaths(json.load(f), self.Resolve)  # cached paths are stored relative
				info['file'] = gdtf
			if info is None or info.get('source_mtime') != key[1]:
				lib = _lib()
				info = lib.parse(gdtf)
				cdir = os.path.dirname(cache)
				info['objs'] = lib.extract_models(info, cdir)
				info['media'] = lib.extract_media(info, os.path.join(cdir, 'wheels'))
				self._writeProfileCache(info)
		for mode in info['modes']:
			self._assignParNames(mode)
		shared[key] = info
		return info

	def _writeProfileCache(self, info):
		gdtf = self.Resolve(info.get('file', ''))
		if not gdtf:
			return
		path = self._profileCachePath(gdtf)
		if os.path.isfile(gdtf):
			info['source_mtime'] = os.path.getmtime(gdtf)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, 'w', encoding='utf-8') as f:
			json.dump(self._mapPaths(info, self.Relative), f)  # relative, so the folder can move

	def _assignParNames(self, mode):
		"""The value / function parameter names of a mode's channels - the same names
		_buildChannelPars gives them, so a reloaded profile maps onto the existing
		parameters without rebuilding."""
		used = set()
		for ch in mode['channels']:
			attr = ch['attribute'] or 'channel'
			ch['par'] = _parname('C%03d' % ch['offsets'][0], attr, used)
			entries = self._menuEntries(ch)
			if len(entries) > 1:
				ch['setpar'] = _parname('S%03d' % ch['offsets'][0], attr, used)
				ch['entries'] = [[e[0], e[2], e[3]] for e in entries]
			else:
				ch.pop('setpar', None)
				ch.pop('entries', None)

	def _setProfile(self, info):
		import sys
		self._profile = info
		dat = self.ownerComp.op('profile')
		if dat is not None and dat.text:
			dat.text = ''
		gdtf = self.Resolve(info.get('file', ''))
		if gdtf and os.path.isfile(gdtf):
			if not os.path.isfile(self._profileCachePath(gdtf)):
				self._writeProfileCache(info)
			key = (os.path.abspath(gdtf), os.path.getmtime(gdtf))
			sys.__dict__.setdefault('_gdtf_profiles', {}).setdefault(key, info)

	@property
	def Mode(self):
		prof = self.Profile
		if not prof or not prof['modes']:
			return None
		name = self.ownerComp.par.Mode.eval()
		return next((m for m in prof['modes'] if m['name'] == name), prof['modes'][0])

	def _status(self, msg):
		self.ownerComp.par.Status = msg
		if msg.startswith('Error'):
			debug(msg)

	def _credentials(self):
		p = self.ownerComp.par
		return (p.Shareuser.eval() or os.environ.get('GDTF_SHARE_USER', ''),
				p.Sharepassword.eval() or os.environ.get('GDTF_SHARE_PASSWORD', ''))

	# ------------------------------------------------------------------ catalogue
	def RefreshList(self):
		"""Fetch the full catalogue from GDTF Share, or rescan the local folder."""
		if self.ownerComp.par.Source.eval() == 'folder':
			self._loadCatalog()
			self._status('Folder: %d fixtures' % len(self.catalog))
			return
		user, pw = self._credentials()
		if not user or not pw:
			self._status('Enter your GDTF Share user + password (free account at gdtf-share.com)')
			return
		lib = _lib()
		listfile = os.path.join(self.RootFolder, 'gdtf', 'share_list.json')

		def work(result):
			c = lib.ShareClient(user, pw)
			c.login()
			lst = c.get_list()
			os.makedirs(os.path.dirname(listfile), exist_ok=True)
			with open(listfile, 'w') as f:
				json.dump(lst, f)
			result['count'] = len(lst)

		self._runJob('Downloading the GDTF Share fixture list...', work, '_onListDone')

	def _onListDone(self, result):
		self._loadCatalog()
		self._status('GDTF Share: %d profiles from %d manufacturers' % (
			result.get('count', 0), len({c['manufacturer'] for c in self.catalog})))

	def _loadCatalog(self, quiet=False):
		cat = []
		if self.ownerComp.par.Source.eval() == 'share':
			listfile = os.path.join(self.RootFolder, 'gdtf', 'share_list.json')
			if os.path.isfile(listfile):
				try:
					with open(listfile) as f:
						entries = json.load(f)
					if self.ownerComp.par.Latestonly.eval():
						latest = {}
						for e in entries:
							k = (str(e.get('manufacturer', '')).strip().lower(), str(e.get('fixture', '')).strip().lower())
							if k not in latest or str(e.get('lastModified', '')) > str(latest[k].get('lastModified', '')):
								latest[k] = e
						entries = list(latest.values())
					for e in entries:
						modes = ', '.join('%s (%s)' % (m.get('name'), m.get('dmxfootprint')) for m in e.get('modes', []) or [])
						cat.append({
							'source': 'share', 'key': 'rid:%s' % e.get('rid'),
							'manufacturer': str(e.get('manufacturer', '')).strip() or '?',
							'fixture': str(e.get('fixture', '')).strip(),
							'revision': str(e.get('revision', '')), 'entry': e,
							'label': '%s  [%s]  %s' % (e.get('fixture'), e.get('revision'), modes[:70]),
						})
				except Exception as ex:
					if not quiet:
						self._status('Error reading share list: %s' % ex)
		folder = self.LibraryFolder
		if os.path.isdir(folder):
			for fn in sorted(os.listdir(folder)):
				if not fn.lower().endswith('.gdtf'):
					continue
				parts = fn[:-5].split('@')
				man = parts[0].replace('_', ' ') if len(parts) > 1 else 'Local'
				fix = parts[1].replace('_', ' ') if len(parts) > 1 else parts[0]
				rev = parts[2] if len(parts) > 2 else ''
				cat.append({'source': 'folder', 'key': os.path.join(folder, fn), 'manufacturer': man,
							'fixture': fix, 'revision': rev, 'label': '%s  [%s]  (downloaded)' % (fix, rev)})
		self.catalog = cat
		self._makerSize = {}
		for c in cat:
			self._makerSize[c['manufacturer']] = self._makerSize.get(c['manufacturer'], 0) + 1
		self.UpdateMenus()

	def UpdateMenus(self):
		"""Fill the Manufacturer / Fixture menus with the catalogue entries that match the
		searches: Search over everything, Find Manufacturer only in the maker's name, Find
		Fixture only in the fixture's name. Words match in any order, ignoring case,
		spaces and punctuation ('mega pointe' finds 'MegaPointe')."""
		p = self.ownerComp.par

		def words(name):
			return [_norm(w) for w in (getattr(p, name).eval() if hasattr(p, name) else '').split() if _norm(w)]

		allw, manw, fixw = words('Search'), words('Searchman'), words('Searchfix')

		def match(c):
			man, fix = _norm(c['manufacturer']), _norm(c['fixture'])
			return (all(w in man for w in manw) and all(w in fix for w in fixw)
					and all(w in man + fix + _norm(c['revision']) for w in allw))

		items = [c for c in self.catalog if match(c)]
		counts = {}
		for c in items:
			counts[c['manufacturer']] = counts.get(c['manufacturer'], 0) + 1
		mans = sorted(counts, key=str.lower)
		p.Manufacturer.menuNames = mans
		p.Manufacturer.menuLabels = ['%s  (%d)' % (m, counts[m]) for m in mans]
		man = p.Manufacturer.eval()
		if mans and man not in mans:
			# the best maker for the search: its name starts with it, then the most matches
			q = ''.join(manw)
			# (a tie goes to the bigger maker: the original rather than a re-upload)
			size = getattr(self, '_makerSize', {})
			man = min(mans, key=lambda m: (not _norm(m).startswith(q), -counts[m], -size.get(m, 0), len(m), m.lower()))
			p.Manufacturer = man
		fixtures = sorted([c for c in items if c['manufacturer'] == man],
						  key=lambda c: (c['fixture'].lower(), c['revision']))
		# the field shows the menu name, so the names are readable ('Robin Forte [v1.2]');
		# Load turns one back into its catalogue key (a GDTF Share id or a file path)
		names, self._fixtureKeys = [], {}
		for c in fixtures:
			base = '%s [%s]%s' % (c['fixture'], c['revision'], ' (downloaded)' if c['source'] == 'folder' else '')
			name, k = base, 2
			while name in self._fixtureKeys:  # the same fixture and revision uploaded twice
				name, k = '%s (%d)' % (base, k), k + 1
			self._fixtureKeys[name] = c['key']
			names.append(name)
		p.Fixture.menuNames = names
		p.Fixture.menuLabels = [c['label'] for c in fixtures]
		if names and p.Fixture.eval() not in names:
			p.Fixture = names[0]
		p.Matches = ('%d manufacturers / %d fixtures' % (len(mans), len(items)) if items
					 else 'nothing found' if self.catalog else 'no list yet: press Refresh List')

	# ------------------------------------------------------------------ loading
	def Load(self, key=None):
		key = key or self.ownerComp.par.Fixture.eval()
		key = getattr(self, '_fixtureKeys', {}).get(key, key)  # a menu name -> its catalogue key
		if not key:
			self._status('Select a fixture first')
			return
		lib = _lib()
		cache = self.CacheFolder
		user, pw = self._credentials()
		if key.startswith('rid:'):
			entry = next((c['entry'] for c in self.catalog if c['key'] == key), None)
			if not entry:
				self._status('Fixture not in the catalogue - press Refresh List')
				return
			path = os.path.join(self.LibraryFolder, lib.share_filename(entry))
		else:
			path, entry = key, None

		def work(result):
			if entry and not os.path.isfile(path):
				c = lib.ShareClient(user, pw)
				c.login()
				c.download(entry['rid'], path)
			info = lib.parse(path)
			cdir = os.path.join(cache, re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.basename(path)[:-5]))
			info['objs'] = lib.extract_models(info, cdir)
			info['media'] = lib.extract_media(info, os.path.join(cdir, 'wheels'))
			result['info'] = info

		self._runJob('Loading %s ...' % os.path.basename(path), work, '_onLoadDone')

	def LoadFile(self, path):
		self.Load(self.Resolve(path))

	def _onLoadDone(self, result):
		self.ApplyProfile(result['info'])

	def ApplyProfile(self, info, mode=None):
		"""Use a parsed profile (from Load, or shared by an importer so 200 fixtures do
		not parse the same file 200 times) in the given mode (default: the first)."""
		self._setProfile(info)
		p = self.ownerComp.par
		p.Gdtffile = self.Relative(info['file'])  # relative to RootFolder, so the rig can move
		names = [m['name'] for m in info['modes']]
		p.Mode.menuNames = names
		p.Mode.menuLabels = ['%s  (%d ch)' % (m['name'], m['footprint']) for m in info['modes']]
		p.Mode = mode if mode in names else (names[0] if names else '')
		p.Fixturename = '%s %s' % (info['manufacturer'], info['name'])
		self.Rebuild()
		self._status('Loaded %s %s - %d modes' % (info['manufacturer'], info['name'], len(names)))

	def OnModeChange(self):
		"""Rebuild for a mode picked in the menu (not again for one ApplyProfile just built)."""
		if self.ownerComp.par.Mode.eval() != getattr(self, '_builtMode', None):
			self.Rebuild()

	def _runJob(self, msg, func, done):
		if self._job and self._job['thread'].is_alive():
			self._status('Busy - wait for the current job')
			return
		self._status(msg)
		result = {}

		def target():
			try:
				func(result)
			except Exception as e:
				result['error'] = '%s: %s' % (type(e).__name__, e)

		th = threading.Thread(target=target, daemon=True)
		self._job = {'thread': th, 'result': result, 'done': done}
		th.start()
		run('args[0].ext.GdtfFixtureExt._poll()', self.ownerComp, delayFrames=5, delayRef=op.TDResources)

	def _poll(self):
		job = self._job
		if not job:
			return
		if job['thread'].is_alive():
			run('args[0].ext.GdtfFixtureExt._poll()', self.ownerComp, delayFrames=5, delayRef=op.TDResources)
			return
		self._job = None
		if 'error' in job['result']:
			self._status('Error: ' + job['result']['error'])
			return
		getattr(self, job['done'])(job['result'])

	# ------------------------------------------------------------------ build
	def Rebuild(self):
		"""Rebuild channel parameters, geometry and optics for the current mode."""
		self._builtMode = self.ownerComp.par.Mode.eval()
		self.MarkDirty()
		self._buildChannelPars()
		self._buildChanmap()
		self._buildOptics()
		self._buildGeometry()
		self._simobj = None
		self._lastFacets = None
		self.Tick()
		self._writeChannelTable()
		# new spot lights must show up in the render TOPs' light lists
		for r in self._rig().findChildren(type=renderTOP, maxDepth=1):
			if r.par.lights.expr:
				r.par.lights.expr = r.par.lights.expr

	def _buildChannelPars(self):
		comp = self.ownerComp
		page = next((pg for pg in comp.customPages if pg.name == 'Channels'), None)
		# a rebuild in the same mode (Body Detail, Beam Cones, ...) keeps the values
		keep = {}
		if page and comp.fetch('gdtf_values_mode', None) == comp.par.Mode.eval():
			keep = {par.name: par.eval() for par in page.pars}
		if page:
			page.destroy()
		comp.store('gdtf_values_mode', comp.par.Mode.eval())
		mode = self.Mode
		comp.par.Footprint = mode['footprint'] if mode else 0
		if not mode:
			return
		page = comp.appendCustomPage('Channels')
		used = set()
		for ch in mode['channels']:
			attr = ch['attribute'] or 'channel'
			inst = ch.get('instance') or ''
			label_attr = ('%s %s' % (inst, attr)).strip()
			offs = '%d-%d' % (ch['offsets'][0], ch['offsets'][-1]) if ch['bytes'] > 1 else str(ch['offsets'][0])
			vname = _parname('C%03d' % ch['offsets'][0], attr, used)
			p = page.appendInt(vname, label='%s  %s%s' % (offs, label_attr, '' if ch['bytes'] == 1 else ' (%dbit)' % (8 * ch['bytes'])))[0]
			p.min, p.max = 0, ch['max']
			p.clampMin = p.clampMax = True
			p.normMin, p.normMax = 0, ch['max']
			p.default = ch['default']
			p.val = keep.get(vname, ch['default'])
			ch['par'] = vname
			ch.pop('setpar', None)
			entries = self._menuEntries(ch)
			if len(entries) > 1:
				sname = _parname('S%03d' % ch['offsets'][0], attr, used)
				m = page.appendMenu(sname, label='      ↳ function')[0]
				m.menuNames = [e[0] for e in entries]
				m.menuLabels = [e[1] for e in entries]
				if keep.get(sname) in m.menuNames:
					m.val = keep[sname]
				ch['setpar'] = sname
				ch['entries'] = [[e[0], e[2], e[3]] for e in entries]
		prof = self.Profile
		for i, m in enumerate(prof['modes']):
			if m['name'] == mode['name']:
				prof['modes'][i] = mode
		self._setProfile(prof)

	def _menuEntries(self, ch):
		"""[(menu name, label, dmxfrom, dmxto)] - one entry per channel set, else per function."""
		out, seen = [], set()
		for fn in ch['functions']:
			rng = '%g..%g' % (fn['physfrom'], fn['physto'])
			items = [(s['dmxfrom'], s['dmxto'], s['name']) for s in fn['sets']] or [(fn['dmxfrom'], fn['dmxto'], '')]
			for lo, hi, sname in items:
				if lo in seen:
					continue
				seen.add(lo)
				label = '%s: %s' % (fn['name'], sname) if sname else '%s  (%s)' % (fn['name'], rng)
				out.append((str(lo), '%s  [%d-%d]' % (label, lo, hi), lo, hi))
		out.sort(key=lambda e: e[2])
		return out

	def _buildChanmap(self):
		self.chanmap, self.setmap = {}, {}
		mode = self.Mode
		if not mode:
			return
		for ch in mode['channels']:
			if 'par' in ch and hasattr(self.ownerComp.par, ch['par']):
				self.chanmap[ch['par']] = ch
				if ch.get('setpar'):
					self.setmap[ch['setpar']] = ch['par']
		self._boAttrs = self._blackoutAttrs()
		self._ovChans = None
		rig = self._rig()
		self._bo = bool(rig.par.Blackout.eval()) if hasattr(rig.par, 'Blackout') else False
		# the patch inversions only apply to fixtures that have the axis
		attrs = {f['attribute'] for ch in self.chanmap.values() for f in ch['functions']}
		p = self.ownerComp.par
		if hasattr(p, 'Invertpan'):
			p.Invertpan.enable = 'Pan' in attrs
			p.Inverttilt.enable = 'Tilt' in attrs

	def _buildGeometry(self):
		comp = self.ownerComp
		if comp.par.parentshortcut.eval() != 'Fixture':
			comp.par.parentshortcut = 'Fixture'  # the beam materials refer to it by this
		for c in comp.findChildren(tags=['gdtfgeo'], maxDepth=1):
			c.destroy()
		prof, mode = self.Profile, self.Mode
		if not prof or not mode:
			return
		root = next((g for g in prof['geometries'] if g['name'] == mode['geometry']), None)
		if not root:
			return
		space = comp.create(geometryCOMP, 'gdtf_space')
		space.tags.add('gdtfgeo')
		for c in list(space.children):
			c.destroy()
		space.par.rx = -90  # GDTF is Z-up, TouchDesigner is Y-up
		space.nodeX, space.nodeY = 0, -300
		self._nbeams = 0
		self._keptBeams = 0
		simple = hasattr(comp.par, 'Bodydetail') and comp.par.Bodydetail.eval() == 'simple'
		# geometries that move on their own: they stay separate COMPs in Simple mode
		self._axisGeos = {ch['geometry'] for ch in mode['channels']
						  if (ch.get('attribute') or '') in ('Pan', 'Tilt', 'PanRotate', 'TiltRotate')}
		self._makeGeo(space, root, prof, simple=simple)

	@staticmethod
	def _mul16(a, b):
		"""Product of two row-major 4x4 GDTF matrices (flat lists)."""
		return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]

	def _keep(self, g, simple, limit):
		"""Simple mode keeps a geometry as its own COMP only when it moves (an axis) or
		draws a beam cone (the first Beamcones beams); Full mode keeps every geometry."""
		if not simple or g['name'] in self._axisGeos:
			return True
		if g['tag'] == 'Beam' and g.get('beamtype', 'Wash') not in ('None', 'Glow') \
				and (not limit or self._keptBeams < limit):
			self._keptBeams += 1
			return True
		return False

	def _modelSop(self, container, g, prof, name):
		"""The SOP for a geometry's model (a file, or a primitive of its size), or None."""
		model = prof['models'].get(g['model'])
		objpath = prof.get('objs', {}).get(g['model'])
		if objpath and os.path.isfile(objpath):
			sop = container.create(fileinSOP, name)
			# an expression through Resolve, so the model is found again when the folder moves
			sop.par.file.expr = 'parent.Fixture.ext.GdtfFixtureExt.Resolve(%r)' % self.Relative(objpath)
			return sop
		if model and (model['length'] or model['width'] or model['height']):
			L, W, H = max(model['length'], 0.01), max(model['width'], 0.01), max(model['height'], 0.005)
			prim = model['primitive']
			if prim in ('Cylinder', 'Conventional1_1', 'Base1_1') or g['tag'] == 'Beam':
				sop = container.create(tubeSOP, name)
				sop.par.orient = 'z'
				sop.par.rad1 = sop.par.rad2 = max(L, W) / 2
				sop.par.height = H
				sop.par.cap = True
			elif prim == 'Sphere':
				sop = container.create(sphereSOP, name)
				sop.par.radx, sop.par.rady, sop.par.radz = L / 2, W / 2, H / 2
			else:
				sop = container.create(boxSOP, name)
				sop.par.sizex, sop.par.sizey, sop.par.sizez = L, W, H
			return sop
		return None

	def _makeGeo(self, parentComp, g, prof, simple=False, rel=None, parts=None):
		"""One geometry and its children. A geometry Simple mode does not keep goes into
		'parts' (model + matrix relative to the nearest kept geometry) instead of
		becoming a COMP; the kept one merges them into a single mesh."""
		if g['tag'] == 'GeometryReference':
			ref = next((x for x in prof['geometries'] if x['name'] == g.get('ref')), None)
			if not ref:
				return
			g = dict(ref, matrix=g['matrix'], name=g['name'], children=ref['children'])
		limit = int(self.ownerComp.par.Beamcones.eval()) if hasattr(self.ownerComp.par, 'Beamcones') else 0
		matrix = self._mul16(rel, g['matrix']) if rel else g['matrix']
		if parts is not None and not self._keep(g, simple, limit):
			parts.append((g, matrix))
			for c in g['children']:
				self._makeGeo(parentComp, c, prof, simple, matrix, parts)
			return
		if parts is None and simple:
			self._keep(g, simple, limit)  # the root is always kept; count it if it is a beam

		geo = parentComp.create(geometryCOMP, 'g_' + _safe(g['name']))
		for c in list(geo.children):
			c.destroy()
		geo.nodeX = 200 * (len(parentComp.children) - 1)
		geo.nodeY = -200
		geo.tags.add('gdtfgeo')
		# the matrix includes any merged geometries between this one and its kept parent
		geo.store('gdtf', {'name': g['name'], 'tag': g['tag'], 'matrix': matrix})
		self._applyMatrix(geo, matrix)
		geo.par.material = self.ownerComp.op('mat_body')

		sop = self._modelSop(geo, g, prof, 'model')
		if sop:
			sop.render = sop.display = True
		if g['tag'] == 'Beam':
			self._makeBeam(geo, g, sop)
		mine = []
		for c in g['children']:
			self._makeGeo(geo, c, prof, simple, None, mine)
		if mine:
			self._mergeParts(geo, sop, mine, prof)

	def _mergeParts(self, geo, own, parts, prof):
		"""Merge the collapsed geometries' models (each moved by its matrix) and the
		geometry's own model into one rendered SOP: one draw instead of many COMPs."""
		merge = geo.create(mergeSOP, 'merged')
		merge.nodeX, merge.nodeY = 400, 0
		if own is not None:
			own.render = own.display = False
			merge.inputConnectors[0].connect(own)
		for i, (g, m16) in enumerate(parts):
			sop = self._modelSop(geo, g, prof, 'part%d' % i)
			if sop is None:
				continue
			sop.nodeX, sop.nodeY = 0, -120 * (i + 1)
			xf = geo.create(transformSOP, 'xf%d' % i)
			xf.nodeX, xf.nodeY = 200, -120 * (i + 1)
			xf.inputConnectors[0].connect(sop)
			sc, r, t = self._gdtfMatrix(m16).decompose()
			xf.par.tx, xf.par.ty, xf.par.tz = t
			xf.par.rx, xf.par.ry, xf.par.rz = r
			xf.par.sx, xf.par.sy, xf.par.sz = sc
			merge.inputConnectors[len(merge.inputs)].connect(xf)
		merge.render = merge.display = True

	def _makeBeam(self, geo, g, lens):
		fx = self.ownerComp
		geo.tags.add('gdtfbeam')
		btype = g.get('beamtype', 'Wash')
		geo.store('beam', {
			'angle': g.get('beamangle', 25.0), 'field': g.get('fieldangle', 25.0),
			'radius': max(g.get('beamradius', 0.05), 0.005), 'type': btype,
			'colortemp': g.get('colortemp', 6500.0),
		})
		lensmat = geo.create(constantMAT, 'mat_lens')
		lensmat.nodeX = 200
		if lens:
			geo.par.material = lensmat
		if btype in ('None', 'Glow'):
			return
		# multi-cell fixtures: every cell is simulated and its lens glows, but only the
		# first Beamcones cells draw a volumetric cone (0 = all) - they are the render cost
		limit = int(fx.par.Beamcones.eval()) if hasattr(fx.par, 'Beamcones') else 0
		if limit and self._nbeams >= limit:
			self._nbeams += 1
			return
		# volumetric cone: the mesh only bounds the volume, the shader ray-marches it
		cone = geo.create(geometryCOMP, 'beam_cone')
		for c in list(cone.children):
			c.destroy()
		cone.nodeX, cone.nodeY = 0, -200
		cone.tags.add('gdtfcone')
		tube = cone.create(tubeSOP, 'cone')
		tube.par.orient = 'z'
		tube.par.rows, tube.par.cols = 2, 24
		tube.par.cap = True
		tube.render = tube.display = True
		mat = cone.create(glslMAT, 'mat_beam')
		for d in list(mat.docked):
			d.destroy()
		# next to this code: in the fixture, or in a rig's shared fixture_code component -
		# that one by the rig's parent shortcut, so fixtures can sit at any depth
		vs, ps = op('shader_beam_vertex'), op('shader_beam_pixel')
		rig = vs.parent().parent()
		if vs.parent().name == 'fixture_code' and rig.par.parentshortcut.eval():
			sc = rig.par.parentshortcut.eval()
			mat.par.vdat.expr = "parent.%s.op('fixture_code/shader_beam_vertex')" % sc
			mat.par.pdat.expr = "parent.%s.op('fixture_code/shader_beam_pixel')" % sc
		else:
			mat.par.vdat, mat.par.pdat = vs, ps
		mat.seq.sampler.numBlocks = 1
		mat.par.sampler0name = 'sGobo'
		mat.par.sampler0top = fx.op('optics/gobo_out')
		mat.seq.vec.numBlocks = 3
		for i, n in enumerate(('uBeam', 'uColor', 'uCut')):
			mat.par['vec%dname' % i] = n
		# parent.Fixture: the fixture COMP's own shortcut, so it can be moved or grouped
		mat.par.vec2valuex.expr = "parent.Fixture.par.Floorcut"
		mat.par.vec2valuey.expr = "parent.Fixture.par.Floory"
		mat.par.vec2valuez.expr = "getattr(parent.Fixture.par, 'Beamsteps', 64)"
		mat.par.blending = True
		# screen blending (1 - (1 - a)(1 - b)): overlapping beams add up softly to bright
		# instead of clipping into a white wall
		mat.par.srcblend = 'omdcol'
		mat.par.destblend = 'one'
		mat.par.depthwriting = False
		mat.par.cullface = 'neither'  # shader keeps front faces from outside, back faces from inside
		mat.nodeX = 200
		cone.par.material = mat
		cone.par.render.expr = "parent.Fixture.par.Showbeam and parent.Fixture.par.Visualize"
		cone.par.drawpriority = BEAM_DRAW_PRIORITY

		if self._nbeams < MAX_LIGHTS:
			lt = geo.create(lightCOMP, 'spot')
			lt.tags.add('gdtflight')
			lt.par.tx = lt.par.ty = lt.par.tz = 0  # a new Light COMP defaults to tz=10
			lt.par.lighttype = 'cone'
			lt.par.projmaptype = 'spot'
			lt.par.projmap = fx.op('optics/gobo_out')
			lt.par.attenuated = True
			lt.par.attenuationstart, lt.par.attenuationend = 0, 30
			lt.par.attenuationexp = 2
			lt.par.shadowtype = 'off'
			lt.nodeX, lt.nodeY = 200, -200
		self._nbeams += 1

	def _wheelLayout(self):
		"""Which wheels go to the compositor: up to 4 image wheels and 3 colour wheels."""
		prof, mode = self.Profile, self.Mode
		images, colors = [], []
		if not prof or not mode:
			return images, colors
		sim = _sim().FixtureSim(prof, mode, lambda g: [])
		for key, wname in sim.wheelkeys.items():
			fam = re.match(r'^[A-Za-z]+', key).group(0)
			kind = _sim().KIND.get(fam)
			if kind == 'color' and wname and len(colors) < 3:
				colors.append({'key': key, 'wheel': wname})
			elif kind in IMAGE_KINDS and len(images) < 4 and (wname or kind == 'anim'):
				images.append({'key': key, 'wheel': wname, 'kind': kind})
		return images, colors

	def _restoreWheels(self):
		try:
			self._imageWheels, self._colorWheels = self._wheelLayout()
		except Exception:
			self._imageWheels, self._colorWheels = [], []

	def _buildOptics(self):
		"""Wheel slot images -> A/B switches -> gobo compositor -> blur -> gobo_out."""
		opt = self.ownerComp.op('optics')
		keep = {'white', 'gobo_comp', 'gobo_blur', 'gobo_out', 'facets', 'facetcol',
				'facets_callbacks', 'facetcol_callbacks', 'gobo_pixel'}
		for c in list(opt.children):
			if c.name not in keep:
				c.destroy()
		comp = opt.op('gobo_comp')
		self._imageWheels, self._colorWheels = self._wheelLayout()
		prof = self.Profile
		media = prof.get('media', {}) if prof else {}
		for w, iw in enumerate(self._imageWheels):
			paths = media.get(iw['wheel'], []) if iw['wheel'] else []
			sa = opt.create(switchTOP, 'w%d_a' % w)
			sb = opt.create(switchTOP, 'w%d_b' % w)
			sa.nodeX, sa.nodeY = -200, -300 * w
			sb.nodeX, sb.nodeY = -200, -300 * w - 130
			for s, path in enumerate(paths or ['']):
				if path:
					t = opt.create(moviefileinTOP, 'w%d_s%02d' % (w, s))
					self._still(t)
					t.par.file.expr = 'parent.Fixture.ext.GdtfFixtureExt.Resolve(%r)' % self.Relative(path)
					t.nodeX, t.nodeY = -560 - 150 * (s % 8), -300 * w - 110 * (s // 8)
				else:
					t = opt.op('white')
				sa.inputConnectors[s].connect(t)
				sb.inputConnectors[s].connect(t)
			comp.inputConnectors[2 * w].connect(sa)
			comp.inputConnectors[2 * w + 1].connect(sb)
		for i in range(2 * len(self._imageWheels), 8):
			comp.inputConnectors[i].connect(opt.op('white'))

	@staticmethod
	def _gdtfMatrix(m16):
		# GDTF matrix is row-major with translation in column 3; tdu.Matrix takes column-major values
		return tdu.Matrix([m16[r * 4 + c] for c in range(4) for r in range(4)])

	@staticmethod
	def _still(movie):
		"""A wheel slot is a still image: a Movie File In TOP 'plays' it every frame by
		default, which kept every gobo compositor - and the render - cooking."""
		movie.par.playmode = 'specify'
		movie.par.index.expr = ''
		movie.par.index = 0
		movie.par.play = False

	def _applyMatrix(self, geo, m16, extra=None):
		m = self._gdtfMatrix(m16)
		if extra is not None:
			m = m * extra
		s, r, t = m.decompose()
		geo.par.tx, geo.par.ty, geo.par.tz = t
		geo.par.rx, geo.par.ry, geo.par.rz = r
		geo.par.sx, geo.par.sy, geo.par.sz = s

	# ------------------------------------------------------------------ parameters
	def OnValueChange(self, par):
		if par.name in self.setmap:
			vpar = getattr(self.ownerComp.par, self.setmap[par.name])
			ch = self.chanmap[vpar.name]
			entry = next((e for e in ch.get('entries', []) if e[0] == par.eval()), None)
			# only jump when the value is outside the chosen range (the menu also follows the value)
			if entry and not entry[1] <= int(vpar.eval()) <= entry[2]:
				vpar.val = entry[1]
		elif par.name in self.chanmap:
			ch = self.chanmap[par.name]
			if ch.get('setpar'):
				self._syncMenu(ch, int(par.eval()))
			# the table is only a view: rewriting it on every channel change cost a pixel
			# fixture ms per channel during colour fades - Tick writes it at most twice a second
			self._tableDirty = True

	def UpdateVisuals(self):
		self._writeChannelTable()

	def ResetDefaults(self):
		for name, ch in self.chanmap.items():
			getattr(self.ownerComp.par, name).val = ch['default']

	def SetChannel(self, attribute, value, normalized=True):
		"""Set every channel with this attribute, e.g. SetChannel('Dimmer', 1.0)."""
		for name, ch in self.chanmap.items():
			if ch['attribute'] == attribute:
				getattr(self.ownerComp.par, name).val = round(value * ch['max']) if normalized else int(value)

	def _syncMenu(self, ch, v):
		p = getattr(self.ownerComp.par, ch['setpar'])
		best = None
		for name, lo, hi in ch.get('entries', []):
			if lo <= v:
				best = name
		if best is not None and p.eval() != best:
			p.val = best

	def _writeChannelTable(self):
		t = self.ownerComp.op('channels')
		t.clear()
		t.appendRow(['dmx', 'par', 'geometry', 'attribute', 'value', 'function', 'set', 'physical'])
		lib = _lib()
		addr = self.ownerComp.par.Address.eval()
		for name, ch in self.chanmap.items():
			v = int(getattr(self.ownerComp.par, name).eval())
			fn, cs, phys = lib.function_at(ch, v)
			if fn:
				t.appendRow([addr + ch['offsets'][0] - 1, name, ch['geometry'], ch['attribute'], v,
							 fn['name'], cs['name'] if cs else '', round(phys, 3)])

	# ------------------------------------------------------------------ simulation
	def _geos(self):
		return {g.fetch('gdtf', {}).get('name'): g for g in self.ownerComp.findChildren(tags=['gdtfgeo'])}

	def _ensureSim(self):
		if self._simobj is not None:
			return self._simobj
		prof, mode = self.Profile, self.Mode
		if not prof or not mode or not self.chanmap:
			return None
		geos = self._geos()
		beams = self.ownerComp.findChildren(tags=['gdtfbeam'])
		names = {b: b.fetch('gdtf')['name'] for b in beams}

		def beams_for(gname):
			g = geos.get(gname)
			if g is None:
				return list(names.values())
			sub = [n for b, n in names.items() if b == g or b.path.startswith(g.path + '/')]
			return sub or list(names.values())

		self._simobj = _sim().FixtureSim(prof, mode, beams_for)
		self._beamops = {n: b for b, n in names.items()}
		self._geocache = geos
		return self._simobj

	def Tick(self):
		"""Called every frame by the 'tick' Execute DAT."""
		if not getattr(self, '_rigTicked', False) and (absTime.frame + self.ownerComp.id) % 30 == 0:
			self._updateNextFree()  # a rig ticker does this round-robin instead
		if getattr(self, '_tableDirty', False) and absTime.frame - getattr(self, '_tableAt', -99) >= 30:
			self._tableDirty, self._tableAt = False, absTime.frame
			self._writeChannelTable()
		sim = self._ensureSim()
		if sim is None:
			return
		self._aim(sim)
		p = self.ownerComp.par
		if hasattr(p, 'Visualize') and not p.Visualize.eval():
			self._idleKey = None  # DMX only: no simulation, beam or light
			return
		# nothing touched since an idle frame: a parameter change, an fx overlay change or
		# a rebuild sets _dirty (the parexec DAT, SetFx / ClearFx)
		if not getattr(self, '_dirty', True) and getattr(self, '_idleKey', None) is not None \
				and self._idleKey[0] == id(sim):
			self._sleep()
			return
		base = self._baseValues()
		over = self._fx is not None or getattr(self, '_bo', False)
		touched = OVERLAY_ATTRS | self._boAttrs if over else OVERLAY_ATTRS
		values = [(self._outValue(ch, base[ch['par']]) if over and ch['attribute'] in touched
				   else base[ch['par']]) if ch.get('par') in base else ch['default'] for ch in sim.channels]
		# idle: same output as last frame and the simulation had settled - nothing to redo
		key = (id(sim), tuple(values))
		if key == getattr(self, '_idleKey', None):
			self._dirty = False
			self._sleep()
			return
		budget = _budget()
		# over budget: wait (stays dirty). A rig ticker starts each frame with a different
		# fixture, so all get turns; a stand-alone fixture runs every other frame at worst.
		strict = getattr(self, '_rigTicked', False)
		if budget['used'] > SIM_BUDGET_MS and (strict or not getattr(self, '_waited', False)):
			self._waited = True
			self._skipped = getattr(self, '_skipped', 0) + 1
			return
		self._waited = False
		self._dirty = False
		t0 = time.perf_counter()
		dt = min(max(absTime.stepSeconds, 1e-4) * (1 + getattr(self, '_skipped', 0)), 0.1)
		self._skipped = 0
		st = sim.update(values, dt)
		self._idleKey = key if sim.settled() else None

		for gname, ax in st['axes'].items():
			g = self._geocache.get(gname)
			if g is None or not g.valid:
				self._simobj = None
				return
			r = tdu.Matrix()
			r.rotate(ax.get('Tilt', 0.0), 0, ax.get('Pan', 0.0))
			self._applyMatrix(g, g.fetch('gdtf')['matrix'], r)

		optic = self._applyOptics(st['optics'])
		keys = self.__dict__.setdefault('_beamKeys', {})
		for name, bst in st['beams'].items():
			b = self._beamops.get(name)
			if b is not None and b.valid:
				key = (repr(bst), id(optic))
				if keys.get(name) != key:  # unchanged beams keep their ~30 parameters as they are
					keys[name] = key
					self._applyBeam(b, bst, optic)
		budget['used'] += 1000.0 * (time.perf_counter() - t0)
		import sys
		work = sys.__dict__.setdefault('_gdtf_rig_work', {})  # per rig: its render gate watches this count
		rig = self._rig().path
		work[rig] = work.get(rig, 0) + 1

	# ------------------------------------------------------------------ fx overlay
	def SetFx(self, pan=0.0, tilt=0.0, dimmer=0.0, dimmerMix=0.0, multiply=False, color=(1.0, 1.0, 1.0),
			  colorMix=0.0):
		"""Overlay from a show controller, applied on top of the channel parameters for the
		simulation and DMX output (the parameters themselves are left alone): pan / tilt
		offsets in degrees, the Dimmer channel blended towards 'dimmer' (0..1, or a factor
		on the parameter value when multiply) by dimmerMix, and the colour mixing channels
		(CMY or RGB(W)) blended towards the RGB 'color' (0..1) by colorMix. Call every
		frame; an overlay not refreshed for a few frames lapses."""
		r, g, b = (min(max(float(c), 0.0), 1.0) for c in color)
		fx = (float(pan), float(tilt), float(dimmer), min(max(float(dimmerMix), 0.0), 1.0),
			  bool(multiply), (r, g, b), min(max(float(colorMix), 0.0), 1.0), absTime.frame)
		if self._fx is None or self._fx[:-1] != fx[:-1]:
			self._overlayChanged()
		self._fx = fx

	def ClearFx(self):
		if self._fx is not None:
			self._overlayChanged()
		self._fx = None

	def FxDirections(self):
		"""(tilt sign, pan sign): the sign of a tilt offset that raises the beam (world +Y)
		and of a pan offset that swings it to the left as seen from the fixture."""
		model = self._beamModel() if self._ensureSim() is not None else None
		if model is None or not model[0]:
			return 1.0, 1.0
		axes, links, B, Ms, fwd = model
		x = [self._dmxToAngle(fn, int(par.eval())) for _, _, par, fn in axes]
		o, d = fwd(x)
		signs = {}
		for k, (_, attr, _, _) in enumerate(axes):
			hi, lo = list(x), list(x)
			hi[k] += 2.0
			lo[k] -= 2.0
			dh, dl = fwd(hi)[1], fwd(lo)[1]
			if attr == 'Tilt':
				signs[attr] = 1.0 if dh.y >= dl.y else -1.0
			else:  # left = counter-clockwise seen from above, around the beam
				signs[attr] = 1.0 if (d.x * dh.z - d.z * dh.x) <= (d.x * dl.z - d.z * dl.x) else -1.0
		return signs.get('Tilt', 1.0), signs.get('Pan', 1.0)

	def BeamDirection(self, panOffset=0.0, tiltOffset=0.0):
		"""World direction of the beam with pan / tilt offsets (degrees) on top of the
		channel values - as the fx overlay would set them (clamped to the axis ranges).
		None when the fixture has no pan / tilt."""
		cache = getattr(self, '_bmCache', None)
		if cache is None or cache[0] != absTime.frame:
			cache = self._bmCache = (absTime.frame, self._beamModel() if self._ensureSim() is not None else None)
		model = cache[1]
		if model is None or not model[0]:
			return None
		axes, links, B, Ms, fwd = model
		x = []
		for _, attr, par, fn in axes:
			lo, hi = sorted((fn['physfrom'], fn['physto']))
			a = self._dmxToAngle(fn, int(par.eval())) + (panOffset if attr == 'Pan' else tiltOffset)
			x.append(min(max(a, lo), hi))
		return fwd(x)[1]

	def SetBlackout(self, on):
		"""Rig master blackout: the light channels go out as 0 (DMX and preview) while
		on; the channel parameters keep their values for when it is released."""
		on = bool(on)
		if on != getattr(self, '_bo', False):
			self._bo = on
			self._overlayChanged()

	def _blackoutAttrs(self):
		"""The channels a blackout zeroes: the dimmers, or - for a fixture without one (a
		white-only blinder) - its colour / white emitters."""
		attrs = {ch['attribute'] for ch in self.chanmap.values()}
		if 'Dimmer' in attrs:
			return {'Dimmer'}
		return {a for a in attrs if a.startswith(('ColorAdd_', 'ColorRGB_'))}

	def _outValue(self, ch, v):
		"""Channel value after the master blackout and the fx overlay."""
		if getattr(self, '_bo', False) and ch['attribute'] in self._boAttrs:
			return 0
		fx = self._fx
		if fx is None:
			return v
		pan, tilt, dim, mix, mult, rgb, cmix, frame = fx
		if absTime.frame - frame > 3:
			self._fx = None
			self._overlayChanged()
			return v
		a = ch['attribute']
		if a in ('Pan', 'Tilt'):
			off = pan if a == 'Pan' else tilt
			fn = attr_function(ch, a)
			if fn and off:
				lo, hi = sorted((fn['physfrom'], fn['physto']))
				return self._angleToDmx(fn, min(max(self._dmxToAngle(fn, v) + off, lo), hi))
		elif a == 'Dimmer' and mix > 0.0:
			target = v * dim if mult else dim * ch['max']
			return int(round(min(max(v + (target - v) * mix, 0), ch['max'])))
		elif a in COLOR_MIX and cmix > 0.0:
			fn = attr_function(ch, a)
			if fn:
				t = COLOR_MIX[a](*rgb)
				target = fn['dmxfrom'] + t * (fn['dmxto'] - fn['dmxfrom'])
				return int(round(min(max(v + (target - v) * cmix, 0), ch['max'])))
		return v

	# ------------------------------------------------------------------ look at
	def _aim(self, sim):
		"""Point the beam at the 'Lookat' object. A fixture with Pan/Tilt gets its
		Pan/Tilt channels set (so DMX output follows); one without rotates its body."""
		st = getattr(self, '_aimState', None)
		if st is None or st['sim'] is not sim:
			st = self._aimState = {'sim': sim, 'key': None, 'x': None, 'body': None}
		p = self.ownerComp.par
		tgt = p.Lookat.eval() if hasattr(p, 'Lookat') else None
		if tgt is None or not getattr(tgt, 'valid', False) or not hasattr(tgt, 'worldTransform'):
			g = st['body']
			if g is not None and g.valid:
				self._applyMatrix(g, g.fetch('gdtf')['matrix'])
			st['body'] = st['key'] = st['x'] = None
			return

		model = self._beamModel()
		if model is None:
			return
		axes, links, B, Ms, fwd = model
		P = tgt.worldTransform * tdu.Position(0, 0, 0)
		key = (round(P.x, 5), round(P.y, 5), round(P.z, 5), tuple(round(v, 6) for v in B.vals), len(axes))
		if key == st['key']:
			return
		st['key'] = key

		if not axes:
			self._aimBody(st, links, B, Ms, P)
			return

		def residual(x):
			o, d = fwd(x)
			w = P - o
			lw, ld = max(w.length(), 1e-9), max(d.length(), 1e-9)
			return [w.x / lw - d.x / ld, w.y / lw - d.y / ld, w.z / lw - d.z / ld]

		bounds = [tuple(sorted((fn['physfrom'], fn['physto']))) for _, _, _, fn in axes]
		current = [self._dmxToAngle(fn, int(par.eval())) for _, _, par, fn in axes]
		x = self._aimSolve(residual, st['x'] or current, current, bounds, [a[1] for a in axes])
		st['x'] = x
		for (ci, attr, par, fn), a in zip(axes, x):
			v = self._angleToDmx(fn, a)
			if int(par.eval()) != v:
				par.val = v

	def _beamModel(self):
		"""(axes, links, B, Ms, fwd) for the first beam. axes: [(chain index, attribute,
		value par, function)] of the Pan/Tilt channels that move it; links: geometries from
		the topmost axis (or the root geometry) down to the beam; B: world matrix above
		links; Ms: their GDTF matrices; fwd(angles) -> (origin, direction) in world space."""
		cached = getattr(self, '_modelCache', None)
		if cached is not None and cached[0] is self._simobj and all(g.valid for g in cached[2]):
			_, axes, links, Ms, rots = cached
			return self._beamModelFor(axes, links, Ms, rots)
		beam = next((b for b in self._beamops.values() if b.valid), None)
		space = self.ownerComp.op('gdtf_space')
		if beam is None or space is None:
			return None
		chain, g = [], beam
		while g is not None and g != space and g != self.ownerComp:
			chain.append(g)
			g = g.parent()
		chain.reverse()
		index = {g.id: i for i, g in enumerate(chain)}

		p = self.ownerComp.par
		axes = []
		for name, ch in self.chanmap.items():
			fn = attr_function(ch, ('Pan', 'Tilt'))
			g = self._geocache.get(ch['geometry'])
			if fn and g is not None and g.valid and g.id in index:
				axes.append((index[g.id], fn['attribute'], getattr(p, name), fn))
		axes.sort(key=lambda a: (a[0], a[1]))
		i0 = axes[0][0] if axes else 0
		links = chain[i0:]
		Ms = [self._gdtfMatrix(g.fetch('gdtf')['matrix']) for g in links]
		rots = {}
		for k, (ci, attr, par, fn) in enumerate(axes):
			rots.setdefault(ci - i0, {})[attr] = k
		# the chain only changes when the fixture is rebuilt (which makes a new simulation)
		self._modelCache = (self._simobj, axes, links, Ms, rots)
		return self._beamModelFor(axes, links, Ms, rots)

	@staticmethod
	def _beamModelFor(axes, links, Ms, rots):
		B = links[0].parent().worldTransform  # not moved by the axes / body rotation

		def fwd(x):
			m = B
			for k, M in enumerate(Ms):
				m = m * M
				rot = rots.get(k)
				if rot:
					r = tdu.Matrix()
					r.rotate(x[rot['Tilt']] if 'Tilt' in rot else 0.0, 0, x[rot['Pan']] if 'Pan' in rot else 0.0)
					m = m * r
			return m * tdu.Position(0, 0, 0), m * tdu.Vector(0, 0, -1)

		return axes, links, B, Ms, fwd

	def _aimSolve(self, residual, seed, current, bounds, attrs):
		"""Axis angles that zero the residual. Finds one exact pose (warm start, else a
		coarse grid), then refines every equivalent pose - pan +-360, or pan +-180 with the
		tilt mirrored - and keeps the one with the least travel from the current position."""
		def err(x):
			return sum(c * c for c in residual(x))

		tol = 2e-5  # ~0.25 degree
		pi = attrs.index('Pan') if 'Pan' in attrs else None
		ti = attrs.index('Tilt') if 'Tilt' in attrs else None

		def family(x):
			"""x refined, plus its refined equivalents that lie (about) within range."""
			out = [x]
			if pi is None:
				return out
			shifts = [(dp, 1) for dp in (-360, 360)]
			if ti is not None:
				shifts += [(dp, -1) for dp in (-540, -180, 180, 540)]
			for dp, sign in shifts:
				c = list(x)
				c[pi] += dp
				if ti is not None:
					c[ti] *= sign
				# the mirrored pose is only approximate when the beam sits off the axes
				if all(lo - 15 <= v <= hi + 15 for v, (lo, hi) in zip(c, bounds)):
					out.append(self._aimRefine(residual, c, bounds))
			return out

		def travel(c):
			return max(abs(a - b) for a, b in zip(c, current))

		x = self._aimRefine(residual, seed, bounds)
		cands = family(x)  # at an end stop the warm pose fails but its mirror does not
		if all(err(c) >= tol for c in cands):
			seeds = [[]]
			for lo, hi in bounds:  # ~45 degree grid over each axis range
				steps = max(1, int(round((hi - lo) / 45.0)))
				seeds = [s + [lo + (hi - lo) * i / steps] for s in seeds for i in range(steps + 1)]
			seeds.sort(key=err)
			x = min((self._aimRefine(residual, s, bounds) for s in seeds[:6]), key=err)
			cands = family(x)
		exact = [c for c in cands if err(c) < tol]
		if not exact:
			return min(cands, key=err)  # target out of reach: the closest the axes can get
		# least travel wins; a small bias keeps the warm pose on near-ties
		x = min(exact, key=lambda c: travel(c) - (1.0 if c is cands[0] else 0.0))
		if pi is None:
			return x
		# Near straight down/up pan barely moves the beam, so an exact solve swings the
		# yoke around for nothing. Penalise pan travel slightly: elsewhere this costs a
		# negligible aim error, near the pole it holds pan and lets tilt pass through 0.
		w = math.sqrt(PAN_HOLD_WEIGHT)

		def held(c):
			return residual(c) + [w * (c[pi] - current[pi])]

		xh = self._aimRefine(held, x, bounds)
		return xh if err(xh) < PAN_HOLD_MAXERR else x

	@staticmethod
	def _aimRefine(residual, x, bounds, iters=25):
		"""Damped Gauss-Newton on the axis angles (degrees), clamped to the axis ranges."""
		x = [min(max(v, lo), hi) for v, (lo, hi) in zip(x, bounds)]
		n, lam, h = len(x), 1e-3, 1e-2
		r = residual(x)
		e = sum(c * c for c in r)
		for _ in range(iters):
			J = []
			for i in range(n):
				xh = list(x)
				xh[i] += h
				rh = residual(xh)
				J.append([(a - b) / h for a, b in zip(rh, r)])
			A = [[sum(J[i][k] * J[j][k] for k in range(len(r))) for j in range(n)] for i in range(n)]
			g = [sum(J[i][k] * r[k] for k in range(len(r))) for i in range(n)]
			while True:
				for i in range(n):
					A[i][i] *= 1.0 + lam
				if n == 1:
					dx = [-g[0] / A[0][0]] if A[0][0] else [0.0]
				else:
					det = A[0][0] * A[1][1] - A[0][1] * A[1][0]
					dx = [-(A[1][1] * g[0] - A[0][1] * g[1]) / det, -(A[0][0] * g[1] - A[1][0] * g[0]) / det] if det else [0.0, 0.0]
				for i in range(n):
					A[i][i] /= 1.0 + lam
				xn = [min(max(v + d, lo), hi) for v, d, (lo, hi) in zip(x, dx, bounds)]
				rn = residual(xn)
				en = sum(c * c for c in rn)
				if en < e or lam > 1e6:
					break
				lam *= 10
			if en >= e:
				break
			step = max(abs(a - b) for a, b in zip(xn, x))
			x, r, e, lam = xn, rn, en, max(lam / 10, 1e-6)
			if step < 1e-4:
				break
		return x

	def _aimBody(self, st, links, B, Ms, P):
		"""No pan/tilt: rotate the root geometry so the beam points at P."""
		top = links[0]
		inv = B * Ms[0]
		inv.invert()
		Pl = inv * P
		rest = tdu.Matrix()
		for M in Ms[1:]:
			rest = rest * M
		o0, d0 = rest * tdu.Position(0, 0, 0), rest * tdu.Vector(0, 0, -1)
		d0.normalize()
		E = tdu.Matrix()
		for _ in range(4):  # the lens sits off the pivot, so re-aim from the rotated origin
			w = Pl - E * o0
			if w.length() < 1e-6:
				break
			w.normalize()
			axis = d0.cross(w)
			ang = math.degrees(math.atan2(axis.length(), d0.dot(w)))
			E = tdu.Matrix()
			if axis.length() > 1e-9:
				E.rotateOnAxis(axis, ang)
			elif d0.dot(w) < 0:
				perp = d0.cross(tdu.Vector(1, 0, 0))
				E.rotateOnAxis(perp if perp.length() > 1e-6 else d0.cross(tdu.Vector(0, 1, 0)), 180)
		self._applyMatrix(top, top.fetch('gdtf')['matrix'], E)
		st['body'] = top

	@staticmethod
	def _angleToDmx(fn, a):
		span = fn['physto'] - fn['physfrom']
		t = min(max((a - fn['physfrom']) / span, 0.0), 1.0) if span else 0.0
		return int(round(fn['dmxfrom'] + t * (fn['dmxto'] - fn['dmxfrom'])))

	@staticmethod
	def _dmxToAngle(fn, v):
		t = min(max((v - fn['dmxfrom']) / max(fn['dmxto'] - fn['dmxfrom'], 1), 0.0), 1.0)
		return fn['physfrom'] + t * (fn['physto'] - fn['physfrom'])

	def _applyOptics(self, o):
		"""Push wheel / prism / iris / blade state to the gobo compositor (only when it
		changed - fx usually move pan / tilt / dimmer only)."""
		last = getattr(self, '_lastOptics', None)
		if last is not None and last[0] == o and last[2] is self._simobj:
			return last[1]
		optic = self._pushOptics(o)
		self._lastOptics = (o, optic, self._simobj)
		return optic

	def _pushOptics(self, o):
		opt = self.ownerComp.op('optics')
		comp = opt.op('gobo_comp')
		prof = self.Profile
		media = prof.get('media', {})
		U = {}
		for w in range(4):
			U['uW%d' % w] = (0, 0, 0, 0)
			U['uWA%d' % w] = (1, 1, 1, 0)
			U['uWB%d' % w] = (1, 1, 1, 0)
		for w, iw in enumerate(self._imageWheels):
			ws = o['wheels'].get(iw['key'])
			if not ws:
				continue
			slots = prof['wheels'].get(iw['wheel'], []) if iw['wheel'] else []
			paths = media.get(iw['wheel'], []) if iw['wheel'] else []
			anim = iw['kind'] == 'anim'
			active = ws.get('active', True)
			if anim:
				# the etched disc turns through the beam; wheel position is its rotation
				U['uW%d' % w] = (1 if active else 0, ws['insert'], math.radians(ws['pos'] * 360.0 / max(ws['slots'], 1) + ws['rot']), 1)
			else:
				U['uW%d' % w] = (1, ws['t'], math.radians(ws['rot']), 0)
			ia, ib = ws['a'], ws['b']
			if anim:
				# an animation wheel is one etched disc: use the slot that carries its image
				img = next((i for i, pth in enumerate(paths) if pth), None)
				ia = ib = img if img is not None else 0
			for tag, idx in (('A', ia), ('B', ib)):
				col, mode_ = (1, 1, 1), 0
				if idx < len(slots):
					sl = slots[idx]
					col = sl['color']
					if idx < len(paths) and paths[idx]:
						mode_ = 1
					elif not re.search(r'open|empty', sl['name'] or 'open', re.I):
						mode_ = 2 + idx + 7 * w  # procedural stand-in, the profile has no image
				U['uW%s%d' % (tag, w)] = (col[0], col[1], col[2], mode_)
			sa, sb = opt.op('w%d_a' % w), opt.op('w%d_b' % w)
			if sa and sb:
				sa.par.index, sb.par.index = ia, ib
		for c in range(3):
			U['uCA%d' % c], U['uCB%d' % c] = (1, 1, 1, 0), (1, 1, 1, 0)
		for c, cw in enumerate(self._colorWheels):
			ws = o['wheels'].get(cw['key'])
			slots = prof['wheels'].get(cw['wheel'], [])
			if not ws or not slots:
				continue
			ca, cb = slots[ws['a'] % len(slots)]['color'], slots[ws['b'] % len(slots)]['color']
			U['uCA%d' % c] = (ca[0], ca[1], ca[2], ws['t'])
			U['uCB%d' % c] = (cb[0], cb[1], cb[2], 1)

		# prism: the first prism wheel that currently has facets in the beam
		facets, prot = [], 0.0
		for ws in o['wheels'].values():
			if ws['family'] == 'Prism' and ws.get('facets'):
				facets, prot = ws['facets'], ws['rot']
				break
		spread, frows = 0.0, []
		for f in facets[:16]:
			m = f['matrix']
			sx = math.hypot(m[0], m[1]) or 1.0
			frows.append([m[6], m[7], sx, 0.0] + list(f['color']))
			spread = max(spread, math.hypot(m[6], m[7]))
		if frows != self._lastFacets:
			self._lastFacets = frows
			self.ownerComp.store('facets', [r[:4] for r in frows] or [[0, 0, 1, 0]])
			self.ownerComp.store('facetcol', [r[4:7] + [1.0] for r in frows] or [[1, 1, 1, 1]])
			opt.op('facets').cook(force=True)
			opt.op('facetcol').cook(force=True)
		U['uPrism'] = (len(frows), math.radians(prot), 1 if frows else 0, 0)
		U['uIris'] = (o['iris'], 0, 0, 0)
		bl = o['blades']
		U['uBladeA'] = tuple(b[0] for b in bl)
		U['uBladeB'] = tuple(b[1] for b in bl)
		U['uBladeRot'] = tuple(math.radians(b[2]) for b in bl)
		domain = 1.0 + spread
		beam0 = next(iter(self._beamops.values()), None)
		binfo = beam0.fetch('beam') if beam0 is not None and beam0.valid else {'angle': 25, 'field': 25}
		soft = min(max(1.0 - binfo['angle'] / max(binfo['field'], 1e-3), 0.03), 0.6)
		soft = min(soft + 0.5 * o['frost'], 0.9)
		U['uOptic'] = (domain, soft, math.radians(o['shaperrot']), 0)
		for name, v in U.items():
			i = self._vecIndex(comp, name)
			comp.par['vec%dvaluex' % i], comp.par['vec%dvaluey' % i] = v[0], v[1]
			comp.par['vec%dvaluez' % i], comp.par['vec%dvaluew' % i] = v[2], v[3]
		opt.op('gobo_blur').par.size = 1.0 + 60.0 * o['frost'] + 28.0 * abs(o['focus'] - 0.5)
		return {'domain': domain, 'zoom': o['zoom'], 'frost': o['frost']}

	def _vecIndex(self, comp, name):
		cache = comp.fetch('vecidx', None)
		if cache is None or name not in cache:
			cache = {comp.par['vec%dname' % i].eval(): i for i in range(comp.seq.vec.numBlocks)}
			if name not in cache:
				i = comp.seq.vec.numBlocks
				comp.seq.vec.numBlocks = i + 1
				comp.par['vec%dname' % i] = name
				cache[name] = i
			comp.store('vecidx', cache)
		return cache[name]

	def _applyBeam(self, b, st, optic):
		fx = self.ownerComp
		info = b.fetch('beam')
		angle = optic['zoom'] if optic['zoom'] else info['angle']
		angle = min(angle * (1.0 + 0.9 * optic['frost']), 170.0)
		k = math.tan(math.radians(angle / 2.0)) * optic['domain']
		lamp = _sim().kelvin_rgb(info.get('colortemp') or 6500.0)
		ref = _sim().kelvin_rgb(6500.0)
		rgb = [st['rgb'][i] * lamp[i] / ref[i] for i in range(3)]
		m = max(rgb + [1.0])
		rgb = [c / m for c in rgb]
		inten = st['intensity'] * fx.par.Brightness.eval()
		L = fx.par.Beamlength.eval()
		cone = b.op('beam_cone')
		if cone:
			r0 = info['radius']
			tube = cone.op('cone')
			lit = inten * max(rgb) > 1e-3  # a dark (or black-coloured) beam is not drawn: no ray-marching
			if tube.render != lit:
				tube.render = lit
			tube.par.rad1 = r0
			tube.par.rad2 = r0 + L * k
			tube.par.height = L
			tube.par.tz = -L / 2
			mat = cone.op('mat_beam')
			mat.par.vec0valuex, mat.par.vec0valuey, mat.par.vec0valuez, mat.par.vec0valuew = L, r0, k, fx.par.Haze.eval()
			mat.par.vec1valuex, mat.par.vec1valuey, mat.par.vec1valuez = [c * inten for c in rgb]
		lens = b.op('mat_lens')
		glow = min(1.0, 0.06 + inten)
		lens.par.colorr, lens.par.colorg, lens.par.colorb = [c * glow for c in rgb]
		lt = b.op('spot')
		if lt:
			lt.par.cr, lt.par.cg, lt.par.cb = rgb
			lt.par.dimmer = inten * fx.par.Lightlevel.eval()
			full = 2.0 * math.degrees(math.atan(k))
			lt.par.coneangle = full
			lt.par.projangle = full
			lt.par.conedelta = 2.0

	# ------------------------------------------------------------------ DMX
	def _sleep(self):
		"""Settled and nothing to aim at: a rig ticker skips this fixture until something
		wakes it (MarkDirty, an fx overlay change)."""
		self._sleeping = self.ownerComp.par.Lookat.eval() is None if hasattr(self.ownerComp.par, 'Lookat') else True

	def MarkDirty(self):
		"""A parameter changed (or a rebuild): re-read the channel values, simulate and
		recompute the DMX bytes."""
		self._dirty = True
		self._sleeping = False
		self._dmxCache = None
		self._base = None
		self._baseBytes = None
		self._beamKeys = {}  # fixture parameters (brightness, haze ...) feed every beam

	def _overlayChanged(self):
		"""Only the fx overlay changed: the channel values stay, the overlaid ones and
		the output are recomputed."""
		self._dirty = True
		self._sleeping = False
		self._dmxCache = None

	def _baseValues(self):
		"""{value par name: int} of every channel, read once per parameter change."""
		base = getattr(self, '_base', None)
		if base is None:
			p = self.ownerComp.par
			base = self._base = {name: int(getattr(p, name).eval()) for name in self.chanmap if hasattr(p, name)}
		return base

	def DmxData(self):
		"""(universe, address, [bytes]) for this fixture's footprint. Cached until
		MarkDirty - the same tuple object comes back while nothing changed."""
		fx = self._fx
		if fx is not None and absTime.frame - fx[-1] > 3:  # the overlay lapsed
			self._fx = None
			self._overlayChanged()
		cache = getattr(self, '_dmxCache', None)
		if cache is not None:
			return cache
		self._dmxCache = self._dmxData()
		return self._dmxCache

	def _dmxData(self):
		"""The parameter values' bytes are kept until a parameter changes; with an fx
		overlay only the overlaid channels (pan, tilt, dimmer, colour) are redone."""
		p = self.ownerComp.par
		cached = getattr(self, '_baseBytes', None)
		if cached is None or self._base is None:
			base = self._baseValues()
			cached = self._baseBytes = (int(p.Universe.eval()), int(p.Address.eval()),
										self._packBytes(base, False))
		if self._fx is None and not getattr(self, '_bo', False):
			return cached
		return cached[0], cached[1], self._packBytes(self._baseValues(), True, list(cached[2]))

	def _packBytes(self, base, overlay, data=None):
		p = self.ownerComp.par
		fp = int(p.Footprint.eval())
		if data is None:
			data = [0] * fp
		invert = {'Pan': hasattr(p, 'Invertpan') and p.Invertpan.eval(),
				  'Tilt': hasattr(p, 'Inverttilt') and p.Inverttilt.eval()}
		if overlay:
			chans = getattr(self, '_ovChans', None)
			if chans is None:  # the channels an fx overlay / blackout can change, once per build
				chans = self._ovChans = [(n, ch) for n, ch in self.chanmap.items()
										 if ch['attribute'] in OVERLAY_ATTRS or ch['attribute'] in self._boAttrs]
		else:
			chans = self.chanmap.items()
		for name, ch in chans:
			v = base.get(name, ch['default'])
			if overlay:
				v = self._outValue(ch, v)
			if invert.get(ch['attribute']):
				# patch inversion: only what goes out, so the preview keeps the intended move
				fn = attr_function(ch, ch['attribute'])
				lo, hi = (fn['dmxfrom'], fn['dmxto']) if fn else (0, ch['max'])
				if lo <= v <= hi:
					v = lo + hi - v
			n = ch['bytes']
			for i, off in enumerate(ch['offsets']):
				if 0 < off <= fp:
					data[off - 1] = (v >> (8 * (n - 1 - i))) & 0xFF
		return data

	def _patchedFixtures(self):
		"""Fixtures sharing this fixture's DMX output (the patch it belongs to)."""
		d = self.ownerComp.par.Dmxout.eval() if hasattr(self.ownerComp.par, 'Dmxout') else None
		if d is not None and hasattr(d.ext, 'DmxOutExt'):
			return d.ext.DmxOutExt.Fixtures()
		return [f for f in self._rig().findChildren(tags=['gdtffixture'], maxDepth=3)
				if hasattr(f.ext, 'GdtfFixtureExt')]

	def _rig(self):
		"""The rig this fixture belongs to: the nearest parent with a dmx_out or shared
		fixture_code (fixtures can sit in group COMPs), else its parent."""
		rig = getattr(self, '_rigOp', None)
		if rig is not None and rig.valid:
			return rig
		p = self.ownerComp.parent()
		while p is not None and p.path != '/':
			if p.op('dmx_out') is not None or p.op('fixture_code') is not None:
				break
			p = p.parent()
		self._rigOp = p if p is not None and p.path != '/' else self.ownerComp.parent()
		return self._rigOp

	def NextFreeAddress(self):
		"""(universe, address, overlapping fixture names): the slot right after this
		fixture's footprint (rolling over to the next universe past 512)."""
		me = self.ownerComp
		uni = int(me.par.Universe.eval())
		start = int(me.par.Address.eval())
		end = start + int(me.par.Footprint.eval()) - 1
		overlaps = []
		for f in self._patchedFixtures():
			if f == me or int(f.par.Universe.eval()) != uni:
				continue
			a = int(f.par.Address.eval())
			if a <= end and start <= a + max(int(f.par.Footprint.eval()), 1) - 1:
				overlaps.append(f.name)
		nxt = end + 1
		if nxt > 512:
			uni, nxt = uni + 1 + (nxt - 513) // 512, (nxt - 513) % 512 + 1
		return uni, nxt, overlaps

	def _updateNextFree(self):
		p = self.ownerComp.par
		if not hasattr(p, 'Nextfree'):
			return
		uni, addr, overlaps = self.NextFreeAddress()
		text = 'U%d.%03d' % (uni, addr)
		if overlaps:
			text += '  ! overlaps ' + ', '.join(overlaps)
		if p.Nextfree.eval() != text:
			p.Nextfree.val = text

	def Patch(self, universe, address):
		self.ownerComp.par.Universe = universe
		self.ownerComp.par.Address = address
