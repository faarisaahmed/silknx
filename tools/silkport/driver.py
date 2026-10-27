"""silknx convert: translate a BepInEx plugins folder into one Switch module."""
import json
import os
import re
import shutil
import sys

from .model import load_universe, sig_str
from .switchdb import SwitchDB
from .translator import Translator, cstr
from .harmony import HarmonyPass
from .types import cid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REF_DIRS = [os.path.join(ROOT, 'pcref', 'core'), os.path.join(ROOT, 'pcref', 'Managed')]
RT_DIR = os.path.join(ROOT, 'sdk', 'rt')
# Libraries shipped next to plugins that are part of BepInEx/the game, never translated.
SKIP_DLLS = {'0Harmony.dll', 'BepInEx.dll', 'MonoMod.RuntimeDetour.dll', 'MonoMod.Utils.dll', 'Mono.Cecil.dll'}


def find_dlls(folder):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in sorted(files):
            if f.endswith('.dll') and not f.startswith('._') and f not in SKIP_DLLS:
                out.append(os.path.join(root, f))
    return out


def implemented_shims():
    names = set()
    for f in os.listdir(RT_DIR):
        if f.endswith(('.cpp', '.h')):
            for m in re.finditer(r'\b(shim_[A-Za-z0-9_]+)\s*\(', open(os.path.join(RT_DIR, f)).read()):
                names.add(m.group(1))
    return names


def convert(plugins_dir, name='SilkNX', config_dir=None, install=False, only=None):
    dlls = find_dlls(plugins_dir)
    if only:
        dlls = [d for d in dlls if any(o.lower() in os.path.basename(d).lower() for o in only)]
    print(f'silkport: {len(dlls)} assemblies from {plugins_dir}')
    game = os.path.join(ROOT, 'game')
    switch = (os.path.join(game, 'exefs', 'main'),
              os.path.join(game, 'romfs', 'Data', 'Managed', 'Metadata', 'global-metadata.dat'))
    u, mods = load_universe(dlls, [os.path.join(ROOT, 'pcref', 'Managed')], switch=switch)
    print(f'  game types from the Switch build; PC copy: {"yes (optional extras enabled)" if u.has_pc else "not used"}')
    db = SwitchDB()
    tr = Translator(u, db, mods, implemented_shims())
    hp = HarmonyPass(tr)
    tr.harmony = hp

    plugins = []
    for asm in mods:
        tr.report.cur_asm = asm.name
        tr.report.items.setdefault(asm.name, [])
        hp.scan(asm)
        hp.scan_manual(asm)
        for td in asm.typedefs:
            for key, args, named in td.attrs:
                if key == 'BepInEx.BepInPlugin' and args:
                    plugins.append((asm, td, args))
    hp.report_unresolved()
    # roots: plugin types (ctor + messages) and patch methods
    for asm, td, args in plugins:
        info = tr.mod_type(tr.owner_sig(td, ()))
        ctor = td.method('.ctor', 0)
        if ctor is not None:
            tr.fn(ctor)
    for p in hp.patches:
        tr.fn(p.patch_md)
    # iterate: translating code instantiates types whose virtual methods must be translated too
    while True:
        tr.run()
        tr.touch_typerefs()
        for info in list(tr.mod_types.values()):
            info.finalized = False
            info.finalize()
        if not tr.queue:
            break
    hooks, hook_lines = hp.generate()
    while True:
        tr.run()
        tr.touch_typerefs()
        for info in list(tr.mod_types.values()):
            info.finalized = False
            info.finalize()
        if not tr.queue:
            break

    lines = [tr.emit()]
    lines += hook_lines
    lines.append('extern "C" const rt_plugin silk_plugins[] = {')
    for asm, td, args in plugins:
        info = tr.mod_type(tr.owner_sig(td, ()))
        ctor = td.method('.ctor', 0)
        guid, pname, ver = (list(args) + ['', '', ''])[:3]
        folder = os.path.relpath(os.path.dirname(asm.path), plugins_dir).replace(os.sep, '/')
        folder = '' if folder == '.' else folder
        lines.append(f'    {{"{cstr(guid)}", "{cstr(pname)}", "{cstr(ver)}", "{cstr(folder)}", &D_{info.cname}, '
                     f'{"(void*)&" + tr.fn(ctor) if ctor else "nullptr"}, {tr.asm_index(asm)}}},')
    lines.append('    {nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, -1}};')
    lines.append(f'extern "C" const uint32_t silk_plugin_count = {len(plugins)};')
    lines.append(f'extern "C" const uint32_t silk_mid_tostring = '
                 f'{tr.method_id(("virt", "System.Object", "ToString", "()"))};')
    lines.append(f'extern "C" const uint32_t silk_mid_movenext = '
                 f'{tr.method_id(("virt", "System.Collections.IEnumerator", "MoveNext", "()"))};')
    lines.append(f'extern "C" const uint32_t silk_mid_current = '
                 f'{tr.method_id(("virt", "System.Collections.IEnumerator", "get_Current", "()"))};')
    lines.append('extern "C" const char* const silk_asm_names[] = {' +
                 ', '.join(f'"{cstr(a.name)}"' for a in mods) + ', nullptr};')
    build = os.path.join(ROOT, 'build', name)
    os.makedirs(build, exist_ok=True)
    gen_cpp = os.path.join(build, 'silkport_gen.cpp')
    if os.environ.get('SILKNX_TRACE') == '2':
        lines = _trace_native_calls(lines, db)
    open(gen_cpp, 'w').write('\n'.join(lines))

    # assets + configs -> romfs
    romfs = os.path.join(build, 'romfs')
    shutil.rmtree(romfs, ignore_errors=True)
    base = os.path.join(romfs, 'Data', 'StreamingAssets', 'SilkNX')
    copied = set()
    for asm in mods:
        src = os.path.dirname(asm.path)
        rel = os.path.relpath(src, plugins_dir)
        if rel == '.' or rel in copied:
            continue
        copied.add(rel)
        dst = os.path.join(base, 'plugins', rel)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('*.dll', '*.pdb', '*.xml', '.DS_Store', '._*',
                                                                'Dumps', 'TextDumps', '*.bak*'),
                        dirs_exist_ok=True)
    if os.environ.get('SILKNX_NO_TEXTURES') != '1':
        from .textures import compress_pngs
        n, raw_mb, packed_mb = compress_pngs(base, build)
        if n:
            print(f'  textures: {n} PNGs -> ASTC ({raw_mb:.0f} MB -> {packed_mb:.0f} MB of texture memory)')
    for asm in mods:  # embedded resources (Assembly.GetManifestResourceStream)
        for rname, data in asm.manifest_resources():
            rdir = os.path.join(base, 'resources', asm.name)
            os.makedirs(rdir, exist_ok=True)
            open(os.path.join(rdir, rname), 'wb').write(data)
    if config_dir and os.path.isdir(config_dir):
        os.makedirs(os.path.join(base, 'config'), exist_ok=True)
        for asm, td, args in plugins:
            cfg = os.path.join(config_dir, f'{args[0]}.cfg')
            if os.path.exists(cfg):
                shutil.copy(cfg, os.path.join(base, 'config'))
    index = []
    if os.path.isdir(romfs):
        for root, dirs, files in os.walk(romfs):
            for f in files:
                full = os.path.join(root, f)
                index.append((os.path.relpath(full, romfs).replace(os.sep, '/'), os.path.getsize(full)))
    index.sort()
    extra = ['extern "C" const rt_romfs_file silk_romfs_files[] = {']
    extra += [f'    {{"{cstr(p)}", {sz}}},' for p, sz in index] + ['    {nullptr, 0}};',
              f'extern "C" const uint32_t silk_romfs_count = {len(index)};']
    images = sorted({t['image'][:-4] for t in db.types.values() if t['image'].endswith('.dll')})
    extra.append('extern "C" const char* const silk_image_names[] = {' +
                 ', '.join(f'"{cstr(i)}"' for i in images) + ', nullptr};')
    ver = ''
    meta = os.path.join(ROOT, 'game', 'romfs', 'Data', 'StreamingAssets', 'BuildMetadata.json')
    if os.path.exists(meta):
        ver = '1.0.' + str(json.load(open(meta)).get('revision', ''))
    extra.append(f'extern "C" const char silk_game_version[] = "{cstr(ver)}";')
    with open(gen_cpp, 'a') as f:
        f.write('\n'.join(extra) + '\n')
    write_report(tr, plugins, hooks, build)
    return tr, gen_cpp, hooks, romfs


def write_report(tr, plugins, hooks, build):
    path = os.path.join(build, 'report.txt')
    with open(path, 'w') as f:
        for asm_name, items in tr.report.items.items():
            errs = [t for s, t in items if s == 'error']
            warns = [t for s, t in items if s == 'warn']
            status = 'OK' if not errs else f'{len(errs)} problem(s)'
            f.write(f'== {asm_name}: {status}\n')
            for t in errs:
                f.write(f'   ERROR {t}\n')
            for t in warns:
                f.write(f'   warn  {t}\n')
        f.write(f'\n{len(plugins)} plugins, {len(hooks)} Harmony hooks, {len(tr.funcs)} translated methods\n')
        for h in hooks:
            f.write(f'   hook {h[0]} -> {h[2]} @ main+{h[1]:#x}\n')
    print(open(path).read())


def build(plugins, config=None, name='SilkNX', install=True, atmosphere=None, only=None):
    """Translate `plugins`, compile the Switch module and install/export it."""
    tr, gen_cpp, hooks, romfs = convert(plugins, name, config, install, only)
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    from silkbuild import build_module, Dump
    import platform_paths
    # runtime manifest -> rt_game.h + runtime hooks
    rtcfg = json.load(open(os.path.join(RT_DIR, 'rt.json')))
    dump = Dump()
    gen = os.path.join(ROOT, 'build', name, 'gen')
    os.makedirs(gen, exist_ok=True)
    hdr = ['// Generated from sdk/rt/rt.json. Do not edit.', '#pragma once', '#include <stdint.h>', 'namespace rva {']
    for m in rtcfg['methods']:
        hdr.append(f'constexpr uintptr_t {m["name"]} = {dump.method(m["method"], m.get("argc"), m.get("params")):#x};')
    nmeth = len(dump.types['System.String']['methods']) if hasattr(dump, 'types') else 0
    hdr += [f'constexpr uint32_t String_method_count = {nmeth};']
    hdr += ['}', 'namespace off {']
    for f in rtcfg['fields']:
        hdr.append(f'constexpr uintptr_t {f["name"]} = {dump.field(f["field"]):#x};')
    hdr += ['}']
    open(os.path.join(gen, 'rt_game.h'), 'w').write('\n'.join(hdr) + '\n')
    rt_hooks = [(h['name'], dump.method(h['method'], h.get('argc'), h.get('params'))) for h in rtcfg['hooks']]
    rt_srcs = sorted(os.path.join(RT_DIR, f) for f in os.listdir(RT_DIR) if f.endswith(('.cpp', '.S')))
    out = build_module(name, rt_srcs + [gen_cpp], rt_hooks + [(h[0], h[1]) for h in hooks],
                       include_dirs=[RT_DIR, gen],
                       extra_cflags=['-Wno-unused-label', '-Wno-unused-variable', '-Wno-unused-but-set-variable',
                                     '-Wno-unused-value', '-Wno-parentheses-equality', '-Wno-tautological-compare',
                                     '-Wno-self-assign'] + (['-DSILKNX_SELFTEST'] if os.environ.get('SILKNX_SELFTEST') else []) + (['-DSILKNX_AUTOTEST'] if os.environ.get('SILKNX_AUTOTEST') else []) + (['-DSILKNX_TRACE'] if os.environ.get('SILKNX_TRACE') else []) + (['-DSILKNX_TRACE_ALL'] if os.environ.get('SILKNX_TRACE') == '2' else []) + (['-fstack-protector-all', '-DSILKNX_STACKCHK'] if os.environ.get('SILKNX_STACKCHK') else []) + (['-DSILKNX_NOEH'] if os.environ.get('SILKNX_NOEH') else []) + ([f'-DSILKNX_SKIP_ONGUI="{os.environ["SILKNX_SKIP_ONGUI"]}"'] if os.environ.get('SILKNX_SKIP_ONGUI') else []),
                       romfs_dir=romfs if os.path.isdir(romfs) else None, install=False, verbose=False,
                       sdk_sources=('crt0.S', 'svc.S', 'runtime.cpp'))
    if install:
        dst = platform_paths.ryujinx_mod_dir(name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(out, dst)
        print(f'Installed into Ryujinx: {dst}')
    if atmosphere:
        export_atmosphere(out, os.path.abspath(atmosphere), name)
    return out


def export_atmosphere(out, dest, name='SilkNX'):
    """Lay out an SD card for Atmosphere: module + romfs under contents/<TID>, IPS under exefs_patches."""
    import platform_paths
    sd = os.path.join(dest, 'atmosphere')
    contents = os.path.join(sd, 'contents', platform_paths.TITLE_ID.upper())
    patches = os.path.join(sd, 'exefs_patches', name)
    shutil.rmtree(contents, ignore_errors=True)
    shutil.rmtree(patches, ignore_errors=True)
    os.makedirs(os.path.join(contents, 'exefs'))
    os.makedirs(patches)
    shutil.copy(os.path.join(out, 'exefs', 'subsdk9'), os.path.join(contents, 'exefs', 'subsdk9'))
    for f in os.listdir(os.path.join(out, 'exefs')):
        if f.endswith('.ips'):
            shutil.copy(os.path.join(out, 'exefs', f), os.path.join(patches, f))
    if os.path.isdir(os.path.join(out, 'romfs')):
        shutil.copytree(os.path.join(out, 'romfs'), os.path.join(contents, 'romfs'))
    zip_path = shutil.make_archive(os.path.join(dest, 'SilkNX-atmosphere'), 'zip', dest, 'atmosphere')
    print(f'Atmosphere SD files: {sd}')
    print(f'  (also zipped: {zip_path}) - copy/extract the "atmosphere" folder to the root of your SD card')


def _trace_native_calls(lines, db):
    """Debug (SILKNX_TRACE=2): log every direct game call by name, right before the statement making it."""
    names = {}
    for t in db.types.values():
        for m in t['methods']:
            if m.get('rva'):
                names.setdefault(m['rva'], f"{t['name']}::{m['name']}")
    out, in_fn = [], False
    pat = re.compile(r'silk_main_base \+ 0x([0-9a-f]+)\)')
    for ln in lines:
        for sub in ln.split('\n'):
            if sub.startswith(('void F_', 'obj F_', 'bool F_', 'int', 'float F_', 'double F_', 'V_', 'uint')) and \
                    sub.endswith('{'):
                in_fn = True
            elif sub == '}':
                in_fn = False
            m = pat.search(sub)
            if in_fn and m and sub.startswith('    ') and not sub.lstrip().startswith(('case ', 'default')):
                nm = names.get(int(m.group(1), 16), m.group(1)).replace('"', "'")
                out.append(f'    RT_NTRACE("{nm}");')
            out.append(sub)
    return out
