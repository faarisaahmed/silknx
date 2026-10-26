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
    u, mods = load_universe(dlls, REF_DIRS)
    db = SwitchDB()
    tr = Translator(u, db, mods, implemented_shims())
    hp = HarmonyPass(tr)

    plugins = []
    for asm in mods:
        tr.report.cur_asm = asm.name
        tr.report.items.setdefault(asm.name, [])
        hp.scan(asm)
        for td in asm.typedefs:
            for key, args, named in td.attrs:
                if key == 'BepInEx.BepInPlugin' and args:
                    plugins.append((asm, td, args))
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
        folder = os.path.basename(os.path.dirname(asm.path))
        lines.append(f'    {{"{cstr(guid)}", "{cstr(pname)}", "{cstr(ver)}", "{cstr(folder)}", &D_{info.cname}, '
                     f'{"(void*)&" + tr.fn(ctor) if ctor else "nullptr"}, {tr.asm_index(asm)}}},')
    lines.append('    {nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, -1}};')
    lines.append(f'extern "C" const uint32_t silk_plugin_count = {len(plugins)};')
    lines.append(f'extern "C" const uint32_t silk_mid_movenext = '
                 f'{tr.method_id(("virt", "System.Collections.IEnumerator", "MoveNext", "()"))};')
    lines.append(f'extern "C" const uint32_t silk_mid_current = '
                 f'{tr.method_id(("virt", "System.Collections.IEnumerator", "get_Current", "()"))};')
    lines.append('extern "C" const char* const silk_asm_names[] = {' +
                 ', '.join(f'"{cstr(a.name)}"' for a in mods) + ', nullptr};')
    build = os.path.join(ROOT, 'build', name)
    os.makedirs(build, exist_ok=True)
    gen_cpp = os.path.join(build, 'silkport_gen.cpp')
    open(gen_cpp, 'w').write('\n'.join(lines))

    # assets + configs -> romfs
    romfs = os.path.join(build, 'romfs')
    shutil.rmtree(romfs, ignore_errors=True)
    base = os.path.join(romfs, 'Data', 'StreamingAssets', 'SilkNX')
    for asm, td, args in plugins:
        src = os.path.dirname(asm.path)
        if os.path.abspath(src) == os.path.abspath(plugins_dir):
            continue
        dst = os.path.join(base, 'plugins', os.path.basename(src))
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('*.dll', '*.pdb', '*.xml', '.DS_Store', '._*'),
                        dirs_exist_ok=True)
    if config_dir and os.path.isdir(config_dir):
        os.makedirs(os.path.join(base, 'config'), exist_ok=True)
        for asm, td, args in plugins:
            cfg = os.path.join(config_dir, f'{args[0]}.cfg')
            if os.path.exists(cfg):
                shutil.copy(cfg, os.path.join(base, 'config'))
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
                                     '-Wno-self-assign'],
                       romfs_dir=romfs if os.path.isdir(romfs) else None, install=False, verbose=False,
                       sdk_sources=('crt0.S', 'svc.S', 'runtime.cpp'))
    if install:
        dst = platform_paths.ryujinx_mod_dir(name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(out, dst)
        print(f'Installed into Ryujinx: {dst}')
    if atmosphere:
        dst = os.path.join(os.path.abspath(atmosphere), 'atmosphere', 'contents', platform_paths.TITLE_ID.upper())
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(out, dst)
        print(f'Atmosphere files written to: {dst}  (copy the "atmosphere" folder to your SD card)')
    return out
