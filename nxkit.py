#!/usr/bin/env python3
"""nxkit: build an nxnative (https://github.com/seedds/nxnative) iPhone IPA from a Windows PC, with only the Mac part on GitHub Actions.

The work is split three ways:
  PC     prepare  extract the game, rewrite its code modules, split its shaders (what nxnative does before it needs a Mac)
                  and put what the Mac needs in a small encrypted build kit; the game files (RomFS) stay on the PC
  cloud  build    on a GitHub macOS runner: the Metal shader libraries and the app (Xcode), packed as a "slim" IPA without the game files
  PC     merge    the slim IPA + the game files extracted on the PC → the finished IPA

Commands (run on the PC unless noted):
  setup --repo <owner>/<name> [--home DIR]   one time: tools, the nxnative checkout and the kit password (also stored as the repository's KIT_PASSWORD secret)
  game <NSP folder> -k <prod.keys> [options] the whole build: prepare → upload the kit → run the workflow → download → merge
  prepare / build / merge / encrypt / decrypt  the single steps (build runs on the Mac runner)

Everything that leaves the PC is encrypted with AES-256-GCM under the kit password, so a public repository's releases and artifacts show nothing readable.
"""
import argparse, getpass, json, os, secrets, shutil, struct, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'nxkit.local.json'     # home folder, repository and kit password (never committed: see .gitignore)
APP = 'Payload/DmIOS.app/'             # the app folder inside the IPA (dm/ios/tools/build_ipa.sh)
NEEDS_MAC = {   # what profile_guess.py shader_sources reports → why the build boots the game on a Mac
    'glslc_capture': 'it compiles shaders at run time, which the build captures by booting the game on a Mac GPU',
    'vk_capture': 'it generates Vulkan shaders at run time, which the build captures by booting the game on a Mac',
    'gl_capture': 'it puts GLSL shaders together at run time, which the build captures by booting the game on a Mac',
    'ue_decode': 'its Unreal shaders are decoded by booting the game on a Mac',
    'oodle_packs': 'its shader packs are decoded by the game\'s own Oodle in the Mac host',
    'ue_legacy': 'its Unreal paks are decoded by the game\'s own decoder in the Mac host',
}


class KitError(Exception):
    pass


def say(msg):
    print(f'[nxkit] {msg}', flush=True)


# ---- Configuration and tools ----
def load_config():
    if not CONFIG.is_file(): raise KitError(f'run setup first: python {Path(__file__).name} setup --repo <owner>/<name>')
    return json.loads(CONFIG.read_text())


def tool_env(home):
    """The PC's tool environment: the .NET SDK and temporary files under home (so nothing large goes to C:), applied to this process too: Windows finds a child's
    executable through this process's PATH, and tempfile picks its folder from TEMP"""
    home = Path(home)
    (home / 'tmp').mkdir(parents=True, exist_ok=True)
    env = {'TEMP': str(home / 'tmp'), 'TMP': str(home / 'tmp'), 'DOTNET_CLI_HOME': str(home / 'dotnet-home'), 'NUGET_PACKAGES': str(home / 'nuget'),
           'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_NOLOGO': '1', 'PIP_CACHE_DIR': str(home / 'pip-cache')}
    if (home / 'dotnet').is_dir():
        env['DOTNET_ROOT'] = str(home / 'dotnet')
        env['PATH'] = str(home / 'dotnet') + os.pathsep + os.environ.get('PATH', '')
    os.environ.update(env)
    tempfile.tempdir = None
    return dict(os.environ)


def run(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode: raise KitError(f'{Path(str(cmd[0])).name} {cmd[1] if len(cmd) > 1 else ""} failed (exit code {r.returncode})')
    return r


def gh(*args, capture=False):
    r = subprocess.run(['gh', *map(str, args)], capture_output=capture, text=True)
    if r.returncode: raise KitError(f'gh {" ".join(map(str, args[:2]))} failed: {(r.stderr or "").strip()[-400:]}')
    return r.stdout.strip() if capture else ''


def checkout(home, ref=None):
    """The nxnative checkout under home, at ref (default: the newest main); returns (repo path, commit)"""
    repo = Path(home) / 'nxnative'
    # The files exactly as on the Mac (no CRLF line endings, which Git for Windows writes by default): nxnative hashes its own sources (the shader chain version, build_src.sha256)
    if not (repo / '.git').is_dir(): run(['git', 'clone', '-q', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'https://github.com/seedds/nxnative.git', repo])
    run(['git', '-C', repo, 'config', 'core.autocrlf', 'false']); run(['git', '-C', repo, 'config', 'core.eol', 'lf'])
    run(['git', '-C', repo, 'fetch', '-q', 'origin'])
    run(['git', '-C', repo, 'reset', '-q', '--hard', ref or 'origin/main'])   # also drops chain_header's rewrite of dm/gfx/nvn/dmsh_chain.h from the last build
    commit = run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    return repo, commit


def load_nxnative(repo):
    """nxnative's NSP script as a module, with the modules main() would load, and the two Mac-only helpers replaced on Windows"""
    repo = Path(repo)
    for d in ('scripts', 'dm/games', 'dm/tools', 'dm/gfx/tools'):
        if str(repo / d) not in sys.path: sys.path.insert(0, str(repo / d))
    import build_ipa_from_nsp as B, game as gamemod, profile_guess as pg, nacp
    B.pg, B.nacp = pg, nacp   # main() sets these globals
    B.PROG.mode = B.progress_mode('auto')
    if os.name == 'nt':
        B.can_clone = lambda d: False   # APFS clones: macOS only (nx_extract then writes full copies)

        def build_nx_extract(repo, work, logs, env):   # the same build as B.build_nx_extract; the executable is nx_extract.exe on Windows
            nx = work / 'tools/nx_extract/nx_extract.exe'
            with tempfile.TemporaryDirectory(prefix='nx_extract_') as t:
                B.run(['dotnet', 'build', repo / 'dm/tools/nx_extract', '-c', 'Release', '-o', t, '-v', 'quiet', '-nologo'], logs / 'nx_extract_build.log', env=env)
                if not (Path(t) / 'nx_extract.exe').exists(): raise B.BuildError(f'nx_extract built without an executable (see {logs / "nx_extract_build.log"})')
                B.rmtree(nx.parent); shutil.copytree(t, nx.parent)
            return nx
        B.build_nx_extract = build_nx_extract
    return B, gamemod, pg, nacp


def games_dir(p):
    """A DM_GAMES_DIR value for p. dm/games/game.py splits the variable on ':', which a Windows drive letter has: there the path goes without its drive, and the
    working directory is moved to that drive, which every tool inherits"""
    p = Path(p).resolve()
    if os.name != 'nt': return str(p)
    os.chdir(p.anchor)
    return str(p)[len(p.drive):]


# ---- Encryption: AES-256-GCM in 8 MB chunks, key from the password with scrypt ----
MAGIC, CHUNK = b'NXKIT1\0\0', 8 << 20


def _key(password, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())


def encrypt(src, dst, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(16); aes = AESGCM(_key(password, salt))
    size, done, i = Path(src).stat().st_size, 0, 0
    with open(src, 'rb') as f, open(dst, 'wb') as o:
        o.write(MAGIC + salt)
        while True:
            chunk = f.read(CHUNK); done += len(chunk); last = done >= size
            nonce = os.urandom(12)
            ct = aes.encrypt(nonce, chunk, MAGIC + struct.pack('<QB', i, last))   # the index and the last flag are authenticated: no chunk can be moved or cut off
            o.write(struct.pack('<IB', len(ct), last) + nonce + ct)
            i += 1
            if last: break


def decrypt(src, dst, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    with open(src, 'rb') as f, open(dst, 'wb') as o:
        if f.read(8) != MAGIC: raise KitError(f'{src} is not an nxkit file')
        aes, i = AESGCM(_key(password, f.read(16))), 0
        while True:
            head = f.read(5)
            if len(head) < 5: raise KitError(f'{src} is cut off')
            n, last = struct.unpack('<IB', head); nonce = f.read(12)
            try: o.write(aes.decrypt(nonce, f.read(n), MAGIC + struct.pack('<QB', i, last)))
            except InvalidTag: raise KitError(f'{src}: wrong kit password, or the file is damaged') from None
            i += 1
            if last: break


# ---- PC: prepare the build kit ----
def prepare(repo, commit, nsp_dir, keys, kit_zip, package=None, app_name=None, language=None, bundle_id=None, show_fps=True):
    """nxnative's main() (scripts/build_ipa_from_nsp.py) up to the first step that needs a Mac, then the kit. Returns the merge info (also saved as <id>_build/nxkit.json)"""
    B, gamemod, pg, nacp = load_nxnative(repo)
    BuildError = B.BuildError
    nsp_dir, keys = Path(nsp_dir).resolve(), Path(keys).resolve()
    if not nsp_dir.is_dir(): raise KitError(f'not a folder: {nsp_dir}')
    if not keys.is_file(): raise KitError(f'no such file: {keys}')
    want_lang = B.language_code(language) if language else 'en-US'
    if not want_lang: raise KitError(f'unknown game language {language!r}')
    env = dict(os.environ); env.setdefault('USER', getpass.getuser()); env.setdefault('J', str(os.cpu_count() or 8))
    py = sys.executable

    app, fam = B.scan_nsps(nsp_dir)
    fam = B.decompress_nsz(nsp_dir, app, fam, keys, env)
    g = gamemod.by_title(app)
    alias = None if g else B.same_build(nsp_dir, app, fam, keys, repo, env)
    if alias: g = alias[0]; B.say(f'no game profile for {app:016X}, but its main build is game profile {g.id}\'s {alias[1]} ({g.get("name", "")})')
    if g is None: raise KitError(f'nxnative has no game profile for {app:016X}: only the games on its list can be built this way')
    B.say(f'game profile: {g.id} ({g.get("name", "")})')
    builds, need = g.builds(), g.words('shader_versions')
    have, derived = B.nsp_versions(fam, builds), False
    pkg_ver = package or max(have, key=B.vnum)
    if pkg_ver not in have: raise KitError(f'version {pkg_ver} is not in the folder, which holds {", ".join(sorted(have, key=B.vnum))}')
    if pkg_ver not in builds: derived = f'{pkg_ver} is not a version game profile {g.id} registers ({", ".join(f"{v} {b["display"]}" for v, b in builds.items())})'
    elif not all(v in have for v in need): derived = f'the folder has no {", ".join(v for v in need if v not in have)}, whose files game profile {g.id}\'s shader tables were made from'
    if alias and not derived: derived = f'title {app:016X} is not game profile {g.id}\'s title {g.title_id:016X}'
    if derived: B.say(f'[RED] {derived}: packing {pkg_ver} from a profile derived from it (the game may need the update the profile names to run well)')
    versions = [pkg_ver] if derived else need + ([pkg_ver] if pkg_ver not in need else [])
    work = nsp_dir / f'{g.id}_build'
    env.update(DM_GAME=g.id, DM_ROOT=str(work), WORK=str(work / 'tmp'))
    logs = work / 'logs'
    picks = B.pick_nsps(fam, have, versions)
    ex = work / 'extract'; ex.mkdir(parents=True, exist_ok=True); (work / 'tmp').mkdir(exist_ok=True)
    gb = sum({p.stat().st_size for b, u in picks.values() for p in (b, u) if p}) / 2 ** 30 * 2 + 4   # the extraction, then the finished IPA beside it
    if B.free_gb(work) < gb and not all((ex / v).exists() for v in versions): raise KitError(f'less than {gb:.0f} GB free on the drive of {work}')

    B.PROG.plan(5 + len(versions))
    B.PROG.stage('build nx_extract')
    nx = B.build_nx_extract(repo, work, logs, env)
    B.PROG.end()
    dirs = {v: B.extract(nx, keys, ex, v, *picks[v], None, logs, g) for v in versions}
    bad = [] if derived else [f'{v}: {x}' for v in versions for x in B.build_mismatch(dirs[v], v, g)]
    if bad:
        derived = f'not the builds game profile {g.id} registers: {"; ".join(bad)}'
        B.say(f'{derived}: packing {pkg_ver} from a profile derived from it')
        versions, dirs = [pkg_ver], {pkg_ver: dirs[pkg_ver]}
    if derived:
        pdir = nsp_dir / f'{app:016X}_build' / 'profile'
        disp = B.derive_profile(g, pdir / g.id, pkg_ver, dirs[pkg_ver], g.words('modules'), app)
        os.environ['DM_GAMES_DIR'] = env['DM_GAMES_DIR'] = games_dir(pdir)
        os.environ['DM_GAMES_FIRST'] = env['DM_GAMES_FIRST'] = '1'
        g = gamemod.load(g.id)
        B.say(f'derived game profile for {pkg_ver} ({disp}): {g.dir}')
    sv = versions if derived else need
    game = dirs[pkg_ver]
    src = pg.build_sources(game / 'ExeFS', game / 'RomFS', g.words('modules'))
    gfx = src['graphics'][0]
    on = {'glslc_capture': gfx == 'nvn', 'vk_capture': gfx == 'vulkan', 'gl_capture': gfx == 'gl', 'ue_decode': gfx == 'nvn', 'oodle_packs': gfx == 'nvn', 'ue_legacy': gfx == 'nvn'}
    mac = [why for k, why in NEEDS_MAC.items() if on[k] and src[k][0]]
    if mac: raise KitError(f'this game can\'t be built without a real Mac: {"; ".join(mac)}')
    if gfx == 'gl': raise KitError('OpenGL games are not supported by nxkit yet (their GLSL is spread over the RomFS and main)')

    aoc = B.extract_aoc(nx, keys, repo, work, app, fam, logs)
    base = next((dirs[v] for v in versions if picks[v][1] is None), dirs[versions[0]])
    app_name = app_name or nacp.name(base / 'control.nacp')
    if not app_name: raise KitError(f'no game name in {base / "control.nacp"}: give --app-name')
    bundle_id = bundle_id or f'org.nxnative.{app:016x}'
    app_version = nacp.version(game / 'control.nacp') or nacp.version(base / 'control.nacp')
    lang = B.game_language(want_lang, bool(language), nacp.languages(game / 'control.nacp') or nacp.languages(base / 'control.nacp'))
    B.say(f'app name: {app_name}; version: {app_version or "1.0"}; bundle ID: {bundle_id}; language: {lang}')

    patched = work / f'patched_{pkg_ver}'
    B.rewrite(py, repo, game / 'ExeFS', patched, env, logs, g)
    B.chain_header(py, repo, env, logs)
    if derived:
        B.provisional_prep(py, repo, env, logs, g)
        g = B.refine_after_rewrite(g, game, patched)
    if gfx == 'nvn':
        B.split_shaders(py, repo, [dirs[v] / 'RomFS' for v in sv], [dirs[v] / 'ExeFS/main' for v in sv], work / 'shader/pkg', env, logs, g, bool(derived), None)
    icon = next((d / 'icon.jpg' for d in (game, base) if (d / 'icon.jpg').exists()), None)

    # The kit: what the Mac steps read, without the game files
    B.PROG.stage('build kit')
    kit = work / 'kit'; B.rmtree(kit); kit.mkdir()
    shutil.copytree(patched, kit / 'patched')
    if derived: shutil.copytree(g.dir, kit / 'profile' / g.id)
    (kit / 'game/RomFS').mkdir(parents=True)
    for n in ('control.nacp',):
        if (game / n).exists(): shutil.copyfile(game / n, kit / 'game' / n)
    if icon: shutil.copyfile(icon, kit / 'game/icon.jpg')
    romfs = game / 'RomFS'
    keep = [romfs / g.get('romfs_marker')]   # build_ipa.sh checks the RomFS by it
    if gfx == 'vulkan':
        import vk_shaders
        keep += [Path(p) for p in vk_shaders.spirv_files(str(romfs))]   # what vk_shaders.py translates
    for p in keep:
        d = kit / 'game/RomFS' / p.relative_to(romfs); d.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(p, d)
    if gfx == 'nvn': shutil.copytree(work / 'shader/pkg', kit / 'shader/pkg')
    meta = dict(nxnative_commit=commit, game=g.id, title=f'{app:016X}', package=pkg_ver, gfx=gfx, derived=bool(derived), app_name=app_name, bundle_id=bundle_id,
                app_version=app_version or '', language=lang, show_fps=bool(show_fps), icon=bool(icon))
    (kit / 'meta.json').write_text(json.dumps(meta, indent=1))
    zip_dir(kit, kit_zip)
    B.rmtree(kit)
    B.PROG.end(f'{Path(kit_zip).stat().st_size / 2 ** 20:.0f} MB')
    info = dict(meta, romfs=str(romfs), aoc=str(aoc or ''), zip_store=g.get('zip_store') or '', ipa=str(nsp_dir / g.get('ipa')), work=str(work))
    (work / 'nxkit.json').write_text(json.dumps(info, indent=1))
    B.PROG.close()
    return info


def zip_dir(d, out):
    d = Path(d)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(d.rglob('*')):
            if p.is_file(): z.write(p, p.relative_to(d).as_posix())


# ---- Cloud (macOS): build the slim IPA from the kit ----
def build(repo, kit_dir, out_ipa):
    B, gamemod, pg, nacp = load_nxnative(repo)
    kit, out_ipa = Path(kit_dir).resolve(), Path(out_ipa).resolve()
    meta = json.loads((kit / 'meta.json').read_text())
    work = kit / 'work'; logs = work / 'logs'; (work / 'tmp').mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=os.pathsep.join(B.TOOL_PATHS + [os.environ.get('PATH', '/usr/bin:/bin')]))
    env.setdefault('USER', getpass.getuser()); env.setdefault('J', str(os.cpu_count() or 8))
    env.update(DM_GAME=meta['game'], DM_ROOT=str(work), WORK=str(work / 'tmp'))
    if meta['derived']:
        os.environ['DM_GAMES_DIR'] = env['DM_GAMES_DIR'] = str(kit / 'profile')
        os.environ['DM_GAMES_FIRST'] = env['DM_GAMES_FIRST'] = '1'
    g = gamemod.load(meta['game'])
    py = sys.executable
    xlate = repo / 'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate'
    B.check_prereqs(env, xlate)
    B.chain_header(py, repo, env, logs)
    sh = work / 'shader'
    try:
        if meta['gfx'] == 'vulkan':
            ios = B.build_vk_shaders(py, repo, [kit / 'game/RomFS'], sh, env, logs, g, reuse=False)
        else:
            shutil.copytree(kit / 'shader/pkg', sh / 'pkg')
            ios = B.build_shaders(py, repo, sh, xlate, env, logs, g, None, False)
        ipa_env = dict(env, GAME=g.id, W=str(work / 'ipa'), PATCHED=str(kit / 'patched'), ROMFS=str(kit / 'game/RomFS'), MSL_IOS=str(ios), GFXAPI=meta['gfx'], FONT_PY=py,
                       DM_DISPLAY_NAME=meta['app_name'], DM_BUNDLE_ID=meta['bundle_id'], AOC='', DM_XCODE_DERIVED_DATA=str(work / 'xcode-derived-data'), DM_TEAM_ID='0000000000',
                       DM_LANG=meta['language'], DM_SHOW_FPS='1' if meta['show_fps'] else '0')
        if meta['app_version']: ipa_env['DM_APP_VERSION'] = meta['app_version']
        if meta['icon']: ipa_env['DM_ICON'] = str(kit / 'game/icon.jpg')
        B.PROG.stage('app build')
        B.run(['bash', repo / 'dm/ios/tools/build_ipa.sh', out_ipa], logs / 'build_ipa.log', env=ipa_env)
        B.PROG.end(f'{out_ipa.stat().st_size / 2 ** 20:.0f} MB')
    except B.BuildError as e:
        raise KitError(str(e)) from None
    finally:
        B.PROG.close()


# ---- PC: merge the slim IPA with the game files ----
def merge(slim, romfs, aoc, store, out):
    """The slim IPA's entries (its stub RomFS left out), then the RomFS and the add-on content from the PC, as dm/ios/tools/pack_ipa.py packs them: Unix modes, names ending in
    a zip_store suffix stored, everything else deflated at level 6"""
    store = tuple(s.lower() for s in (store or '').split(':') if s)
    out = Path(out); tmp = out.with_name(out.name + '.partial')
    total = sum(p.stat().st_size for root in (romfs, aoc) if root for p in Path(root).rglob('*') if p.is_file())
    done, t0, shown = 0, time.time(), 0

    def add_tree(z, root, arc):
        nonlocal done, shown
        root = Path(root)
        for d, dns, fns in os.walk(root):
            dns.sort(); rel = Path(d).relative_to(root).as_posix()
            name = arc + ('' if rel == '.' else rel + '/')
            zi = zipfile.ZipInfo(name, date_time=dos_time(Path(d)))
            zi.create_system, zi.external_attr = 3, (0o40755 << 16) | 0x10
            z.writestr(zi, b'')
            for fn in sorted(fns):
                p = Path(d) / fn; size = p.stat().st_size
                zi = zipfile.ZipInfo(name + fn, date_time=dos_time(p))
                zi.create_system, zi.external_attr = 3, 0o100644 << 16
                zi.compress_type = zipfile.ZIP_STORED if fn.lower().endswith(store) else zipfile.ZIP_DEFLATED
                zi.compress_level = 6
                with open(p, 'rb') as f, z.open(zi, 'w', force_zip64=size >= 1 << 31) as o: shutil.copyfileobj(f, o, 16 << 20)
                done += size
                if time.time() - shown > 10:
                    shown = time.time(); say(f'merge: {done / 2 ** 30:.1f} of {total / 2 ** 30:.1f} GB')

    with zipfile.ZipFile(slim) as zin, zipfile.ZipFile(tmp, 'w', allowZip64=True) as z:
        for info in zin.infolist():
            if info.filename.startswith((APP + 'romfs/', APP + 'aoc/')): continue
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.create_system, zi.external_attr, zi.compress_type, zi.compress_level = info.create_system, info.external_attr, info.compress_type, 6
            if info.is_dir(): z.writestr(zi, b''); continue
            with zin.open(info) as f, z.open(zi, 'w', force_zip64=info.file_size >= 1 << 31) as o: shutil.copyfileobj(f, o, 16 << 20)
        add_tree(z, romfs, APP + 'romfs/')
        if aoc and Path(aoc).is_dir(): add_tree(z, aoc, APP + 'aoc/')
    tmp.replace(out)
    say(f'merged in {time.time() - t0:.0f} s: {out} ({out.stat().st_size / 2 ** 30:.2f} GB)')


def dos_time(p):
    t = time.localtime(max(p.stat().st_mtime, 315532800 + 86400))   # zip times start in 1980
    return t[:6]


# ---- PC: the whole build ----
def game_cmd(a):
    cfg = load_config(); home = Path(cfg['home'])
    tool_env(home)
    repo, commit = checkout(home, a.nxnative_ref)
    say(f'nxnative {commit[:10]}')
    out_dir = home / 'kits'; out_dir.mkdir(parents=True, exist_ok=True)
    tag = f'kit-{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}'
    kit_zip, kit_enc = out_dir / f'{tag}.zip', out_dir / f'{tag}.nxk'
    info = prepare(repo, commit, a.nsp_dir, a.keys, kit_zip, a.package, a.app_name, a.language, a.bundle_id, not a.no_fps)
    encrypt(kit_zip, kit_enc, cfg['password']); kit_zip.unlink()
    say(f'encrypted build kit: {kit_enc.stat().st_size / 2 ** 20:.0f} MB')
    slug = cfg['repo']
    try:
        gh('release', 'create', tag, kit_enc, '--repo', slug, '--prerelease', '--title', tag, '--notes', 'Encrypted nxkit build kit; the build deletes it.')
        gh('workflow', 'run', 'build-ipa.yml', '--repo', slug, '-f', f'kit_tag={tag}', '-f', f'nxnative_ref={commit}')
        say('cloud build started; waiting for GitHub to list it')
        run_id = None
        for _ in range(60):
            time.sleep(5)
            runs = json.loads(gh('run', 'list', '--repo', slug, '--workflow', 'build-ipa.yml', '--limit', '20', '--json', 'databaseId,displayTitle', capture=True))
            run_id = next((r['databaseId'] for r in runs if tag in r['displayTitle']), None)
            if run_id: break
        if not run_id: raise KitError('the cloud build did not start (see the Actions tab)')
        say(f'cloud build: https://github.com/{slug}/actions/runs/{run_id}')
        if subprocess.run(['gh', 'run', 'watch', str(run_id), '--repo', slug, '--exit-status', '--interval', '30']).returncode:
            fetch_logs(slug, run_id, home / 'logs' / tag, cfg['password'])
            raise KitError(f'the cloud build failed: https://github.com/{slug}/actions/runs/{run_id}')
        dl = home / 'downloads' / tag; B_rm(dl)
        gh('run', 'download', run_id, '--repo', slug, '--name', 'ipa', '--dir', dl)
        slim = dl / 'slim.ipa'
        decrypt(dl / 'slim.ipa.nxk', slim, cfg['password'])
        merge(slim, info['romfs'], info['aoc'], info['zip_store'], a.out or info['ipa'])
        B_rm(dl)
        for art in json.loads(gh('api', f'repos/{slug}/actions/runs/{run_id}/artifacts', capture=True))['artifacts']:
            gh('api', '-X', 'DELETE', f'repos/{slug}/actions/artifacts/{art["id"]}')
    finally:
        subprocess.run(['gh', 'release', 'delete', tag, '--repo', slug, '--yes', '--cleanup-tag'], capture_output=True)   # the workflow deletes it first; this covers a failed start
        kit_enc.unlink(missing_ok=True)
    say(f'done: {a.out or info["ipa"]}')
    if not a.keep_extracted:
        say(f'deleting the extracted game files: {Path(info["work"]) / "extract"}'); B_rm(Path(info['work']) / 'extract')


def fetch_logs(slug, run_id, dst, password):
    """A failed cloud build's encrypted nxnative logs (the artifact "logs") → dst, readable"""
    import tarfile
    try:
        gh('run', 'download', run_id, '--repo', slug, '--name', 'logs', '--dir', dst)
        decrypt(dst / 'logs.tgz.nxk', dst / 'logs.tgz', password)
        with tarfile.open(dst / 'logs.tgz') as t: t.extractall(dst, filter='data')
        (dst / 'logs.tgz.nxk').unlink(); (dst / 'logs.tgz').unlink()
        say(f'the cloud build\'s logs: {dst / "logs"}')
    except KitError as e:
        say(f'no build logs to download ({e})')


def B_rm(p):
    p = Path(p)
    if p.is_dir(): shutil.rmtree(p)
    elif p.exists(): p.unlink()


def setup_cmd(a):
    home = Path(a.home).resolve(); home.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    cfg.update(home=str(home), repo=a.repo)
    if not cfg.get('password') or a.new_password: cfg['password'] = secrets.token_urlsafe(32)
    env = tool_env(home)
    run([sys.executable, '-m', 'pip', 'install', '-q', 'lz4', 'capstone', 'fonttools', 'cryptography', 'nsz'])
    if not (home / 'dotnet' / ('dotnet.exe' if os.name == 'nt' else 'dotnet')).exists():
        say('installing the .NET 10 SDK into ' + str(home / 'dotnet'))
        if os.name == 'nt':
            script = home / 'dotnet-install.ps1'
            run(['powershell', '-NoProfile', '-Command', f"Invoke-WebRequest https://dot.net/v1/dotnet-install.ps1 -OutFile '{script}'"])
            run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Channel', '10.0', '-InstallDir', home / 'dotnet', '-NoPath'])
        else:
            raise KitError('install the .NET 10 SDK (https://dot.net)')
        tool_env(home)
    checkout(home)
    gh('auth', 'status', capture=True)
    gh('secret', 'set', 'KIT_PASSWORD', '--repo', a.repo, '--body', cfg['password'])
    CONFIG.write_text(json.dumps(cfg, indent=1))
    say(f'ready: tools in {home}, kit password saved in {CONFIG.name} and as {a.repo}\'s KIT_PASSWORD secret')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('setup', help='one-time setup on the PC')
    s.add_argument('--repo', required=True, help='your GitHub repository holding this workflow, owner/name')
    s.add_argument('--home', default=str(HERE / '.nxkit'), help='folder for the tools, nxnative and temporary files (pick a drive with free space)')
    s.add_argument('--new-password', action='store_true', help='make a new kit password')
    s = sub.add_parser('game', help='build one game\'s IPA (PC + cloud)')
    s.add_argument('nsp_dir', help='folder holding one game\'s NSPs (base game, update, DLC)')
    s.add_argument('-k', '--keys', required=True, help='path of prod.keys')
    s.add_argument('-p', '--package', help='version to pack, e.g. v589824 (default: the newest in the folder)')
    s.add_argument('-o', '--out', help='output IPA (default: in the NSP folder)')
    s.add_argument('--app-name'); s.add_argument('--bundle-id'); s.add_argument('-l', '--language')
    s.add_argument('--no-fps', action='store_true', help='hide the frame-rate counter')
    s.add_argument('--keep-extracted', action='store_true', help='keep the extracted game files (faster next build of the same game)')
    s.add_argument('--nxnative-ref', help='nxnative commit or branch (default: the newest main)')
    s = sub.add_parser('prepare', help='PC step: the build kit (unencrypted zip)')
    s.add_argument('repo'); s.add_argument('nsp_dir'); s.add_argument('-k', '--keys', required=True); s.add_argument('-o', '--out', required=True)
    s.add_argument('-p', '--package'); s.add_argument('--app-name'); s.add_argument('--bundle-id'); s.add_argument('-l', '--language')
    s = sub.add_parser('build', help='cloud step (macOS): the slim IPA from an unpacked kit')
    s.add_argument('repo'); s.add_argument('kit_dir'); s.add_argument('-o', '--out', required=True)
    s = sub.add_parser('merge', help='PC step: slim IPA + game files → IPA')
    s.add_argument('slim'); s.add_argument('info', help='<id>_build/nxkit.json from prepare'); s.add_argument('-o', '--out')
    for c in ('encrypt', 'decrypt'):
        s = sub.add_parser(c, help=f'{c} a file with the kit password (KIT_PASSWORD environment, else nxkit.local.json)')
        s.add_argument('src'); s.add_argument('dst')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'setup': setup_cmd(a)
        elif a.cmd == 'game': game_cmd(a)
        elif a.cmd == 'prepare':
            repo = Path(a.repo).resolve()
            commit = run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
            if CONFIG.is_file(): tool_env(load_config()['home'])
            prepare(repo, commit, a.nsp_dir, a.keys, Path(a.out).resolve(), a.package, a.app_name, a.language, a.bundle_id)
        elif a.cmd == 'build': build(Path(a.repo).resolve(), a.kit_dir, a.out)
        elif a.cmd == 'merge':
            info = json.loads(Path(a.info).read_text())
            merge(a.slim, info['romfs'], info['aoc'], info['zip_store'], a.out or info['ipa'])
        else:
            pw = os.environ.get('KIT_PASSWORD') or load_config()['password']
            (encrypt if a.cmd == 'encrypt' else decrypt)(a.src, a.dst, pw)
    except KitError as e:
        print(f'[nxkit] error: {e}', file=sys.stderr); return 1
    except Exception as e:
        if type(e).__name__ == 'BuildError': print(f'[nxkit] error: {e}', file=sys.stderr); return 1
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main())
