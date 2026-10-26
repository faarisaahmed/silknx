#!/usr/bin/env python3
"""Build a SilkNX native mod into a Ryujinx/Atmosphere exefs mod folder.

Usage: silkbuild.py <mod_dir> [--install]

<mod_dir>/mod.json:
  {
    "name": "HealthBars",
    "hooks": [ {"name": "HM_TakeDamage", "method": "HealthManager::TakeDamage", "argc": 1}, ... ],
    "methods": [ {"name": "GUI_Label", "method": "UnityEngine.GUI::Label", "params": ["UnityEngine.Rect", "string"]} ],
    "fields":  [ {"name": "HM_hp", "field": "HealthManager::hp"} ]
  }
Sources: <mod_dir>/src/*.cpp. Output: out/<name>/exefs/{subsdk9, <build-id>.ips} (+ romfs/ if present).
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from nso import Nso  # noqa: E402

import platform_paths  # noqa: E402

LLVM = platform_paths.find_llvm() or '/opt/homebrew/opt/llvm@18/bin'
TITLE_ID = platform_paths.TITLE_ID
RYU_MODS = os.path.dirname(platform_paths.ryujinx_mod_dir())
GAME = os.path.join(ROOT, 'game')
PAGE = 0x1000


def align(v, a=PAGE):
    return (v + a - 1) & ~(a - 1)


# ---------------------------------------------------------------- dump lookup

class Dump:
    def __init__(self):
        d = json.load(open(os.path.join(GAME, 'dump.json')))
        self.types = {}
        for t in d['types']:
            self.types.setdefault(t['name'], t)

    def method(self, spec, argc=None, params=None):
        cls, name = spec.split('::')
        t = self.types.get(cls)
        if not t:
            raise SystemExit(f'class not found: {cls}')
        cands = [m for m in t['methods'] if m['name'] == name]
        if argc is not None:
            cands = [m for m in cands if len(m['params']) == argc]
        if params is not None:
            cands = [m for m in cands if [p.rsplit(' ', 1)[0] for p in m['params']] == params]
        if len(cands) != 1:
            sigs = ['(' + ', '.join(m['params']) + ')' for m in t['methods'] if m['name'] == name]
            raise SystemExit(f'{spec}: {len(cands)} matches (argc={argc} params={params}); overloads: {sigs}')
        if not cands[0]['rva']:
            raise SystemExit(f'{spec}: no code (abstract or stripped)')
        return cands[0]['rva']

    def field(self, spec):
        cls, name = spec.split('::')
        t = self.types.get(cls)
        if not t:
            raise SystemExit(f'class not found: {cls}')
        for f in t['fields']:
            if f['name'] == name:
                return f['offset']
        raise SystemExit(f'field not found: {spec}')


# ---------------------------------------------------------------- ELF parsing

class Elf:
    def __init__(self, path):
        self.d = d = open(path, 'rb').read()
        assert d[:4] == b'\x7fELF' and d[4] == 2
        (self.phoff, self.shoff) = struct.unpack_from('<QQ', d, 0x20)
        phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from('<HHHHH', d, 0x36)
        self.phdrs = [struct.unpack_from('<IIQQQQQQ', d, self.phoff + i * phentsize) for i in range(phnum)]
        self.shdrs = [struct.unpack_from('<IIQQQQIIQQ', d, self.shoff + i * shentsize) for i in range(shnum)]

    def symbols(self):
        out = {}
        for sh in self.shdrs:
            if sh[1] != 2:  # SHT_SYMTAB
                continue
            strtab = self.shdrs[sh[6]]
            for i in range(sh[5] // 24):
                name, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', self.d, sh[4] + i * 24)
                s = self.d[strtab[4] + name:self.d.index(b'\0', strtab[4] + name)].decode()
                if s and shndx:
                    out[s] = value
        return out


def elf2nso(elf, out_path, build_id):
    loads = [p for p in elf.phdrs if p[0] == 1]
    segs = []
    for flags in (5, 4, 6):
        p = next(p for p in loads if p[1] == flags)
        _t, _f, off, vaddr, _pa, filesz, memsz, _al = p
        segs.append((vaddr, elf.d[off:off + filesz], memsz))
    (tv, tdata, _), (rv, rdata, _), (dv, ddata, dmem) = segs
    assert tv == 0 and rv % PAGE == 0 and dv % PAGE == 0
    # Data must cover everything up to bss; NSO bss follows data size directly.
    bss = dmem - len(ddata)
    hdr = bytearray(0x100)
    hdr[0:4] = b'NSO0'
    file_off = 0x100
    for i, (va, data, _m) in enumerate(segs):
        struct.pack_into('<III', hdr, 0x10 + 0x10 * i, file_off, va, len(data))
        struct.pack_into('<I', hdr, 0x60 + 4 * i, len(data))
        hdr[0xA0 + 0x20 * i:0xC0 + 0x20 * i] = hashlib.sha256(data).digest()
        file_off += len(data)
    struct.pack_into('<I', hdr, 0x3C, bss)
    hdr[0x40:0x60] = build_id
    with open(out_path, 'wb') as f:
        f.write(hdr + tdata + rdata + ddata)


# ---------------------------------------------------------------- AArch64 bits

def sx(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def mov_imm(reg, val):
    """movz/movk sequence loading a 48-bit unsigned value."""
    out = [f'movz x{reg}, #{val & 0xFFFF}']
    for sh in (16, 32):
        if (val >> sh) & 0xFFFF:
            out.append(f'movk x{reg}, #{(val >> sh) & 0xFFFF}, lsl #{sh}')
    return out


def load_main_addr(reg, rva):
    """reg = silk_main_base + rva, clobbering only x16/x17 (and reg)."""
    tmp = 17 if reg != 17 else 16
    return [f'adrp x{reg}, silk_main_base', f'ldr x{reg}, [x{reg}, :lo12:silk_main_base]',
            *mov_imm(tmp, rva), f'add x{reg}, x{reg}, x{tmp}']


def relocate(insn, pc):
    """Return assembly lines equivalent to `insn` (originally at main+pc), runnable from anywhere."""
    if (insn & 0x9F000000) == 0x90000000:  # ADRP
        rd = insn & 0x1F
        imm = sx(((insn >> 29) & 3) | (((insn >> 5) & 0x7FFFF) << 2), 21) << 12
        return load_main_addr(rd, (pc & ~0xFFF) + imm)
    if (insn & 0x9F000000) == 0x10000000:  # ADR
        rd = insn & 0x1F
        imm = sx(((insn >> 29) & 3) | (((insn >> 5) & 0x7FFFF) << 2), 21)
        return load_main_addr(rd, pc + imm)
    if (insn & 0x7C000000) == 0x14000000:  # B / BL
        tgt = pc + (sx(insn & 0x3FFFFFF, 26) << 2)
        op = 'blr' if insn & 0x80000000 else 'br'
        return load_main_addr(16, tgt) + [f'{op} x16']
    if ((insn & 0xFF000010) == 0x54000000 or (insn & 0x7E000000) in (0x34000000, 0x36000000)
            or (insn & 0x3B000000) == 0x18000000):
        raise ValueError(f'cannot relocate pc-relative branch/literal {insn:#010x} at {pc:#x}')
    return [f'.inst {insn:#010x}']


def adrp_add_br(pc, target):
    """Encode `adrp x16, target; add x16, x16, :lo12:target; br x16` placed at pc."""
    page_delta = (target >> 12) - (pc >> 12)
    assert -(1 << 20) <= page_delta < (1 << 20)
    immlo, immhi = page_delta & 3, (page_delta >> 2) & 0x7FFFF
    adrp = 0x90000000 | (immlo << 29) | (immhi << 5) | 16
    add = 0x91000000 | ((target & 0xFFF) << 10) | (16 << 5) | 16
    br = 0xD61F0200
    return struct.pack('<III', adrp, add, br)


# ---------------------------------------------------------------- build

def main():
    mod_dir = os.path.abspath(sys.argv[1])
    cfg = json.load(open(os.path.join(mod_dir, 'mod.json')))
    name = cfg['name']
    dump = Dump()
    build = os.path.join(ROOT, 'build', name)
    gen = os.path.join(build, 'gen')
    os.makedirs(gen, exist_ok=True)

    # Generated header: method RVAs and field offsets the mod asked for.
    hdr = ['// Generated by silkbuild.py from the IL2CPP dump. Do not edit.', '#pragma once', '#include <stdint.h>',
           'namespace rva {']
    for m in cfg.get('methods', []):
        rva = dump.method(m['method'], m.get('argc'), m.get('params'))
        hdr.append(f'constexpr uintptr_t {m["name"]} = {rva:#x};  // {m["method"]}')
    hdr += ['}', 'namespace off {']
    for f in cfg.get('fields', []):
        hdr.append(f'constexpr uintptr_t {f["name"]} = {dump.field(f["field"]):#x};  // {f["field"]}')
    hdr += ['}']
    open(os.path.join(gen, 'game.h'), 'w').write('\n'.join(hdr) + '\n')

    hooks = [(h['name'], dump.method(h['method'], h.get('argc'), h.get('params'))) for h in cfg.get('hooks', [])]
    srcs = sorted(os.path.join(mod_dir, 'src', f) for f in os.listdir(os.path.join(mod_dir, 'src'))
                  if f.endswith(('.cpp', '.S', '.c')))
    romfs = os.path.join(mod_dir, 'romfs')
    build_module(name, srcs, hooks, include_dirs=[gen, os.path.join(mod_dir, 'src')],
                 romfs_dir=romfs if os.path.isdir(romfs) else None, install='--install' in sys.argv)


def module_layout():
    main_nso = Nso(os.path.join(GAME, 'exefs', 'main'))
    sub0 = Nso(os.path.join(GAME, 'exefs', 'subsdk0'))

    def total(n):
        return align(n.data[0] + n.data[1] + n.bss_size)

    nnmain = next(v for s, v, _sz, sh, _i in main_nso.symbols() if s == 'nnMain' and sh)
    return main_nso, total(main_nso) + total(sub0), nnmain


def build_module(name, srcs, hooks, include_dirs=(), extra_cflags=(), romfs_dir=None, install=False,
                 verbose=True, sdk_sources=('crt0.S', 'svc.S', 'runtime.cpp')):
    """Compile sources + hook trampolines into subsdk9 and the matching IPS patch for main."""
    main_nso, module_offset, nnmain = module_layout()
    build = os.path.join(ROOT, 'build', name)
    gen = os.path.join(build, 'gen')
    os.makedirs(gen, exist_ok=True)

    # Trampolines. Hook sites get a single `b` when the module is within +-128MB (always true for game
    # code past the first few MB), otherwise adrp/add/br (12 bytes).
    asm = ['// Generated by silkbuild.py. Do not edit.', '.section ".rodata.silk", "a"', '.align 3',
           '.global silk_expected_module_offset', f'silk_expected_module_offset: .quad {module_offset:#x}',
           '.section ".text.silk_tramp", "ax"', '.align 4']
    sizes = {}
    for hname, rva in hooks:
        n = 1 if module_offset - rva + 0x400000 < (1 << 27) else 3
        sizes[hname] = n
        insns = struct.unpack_from(f'<{n}I', main_nso.image, rva)
        asm += [f'.global orig_{hname}', f'.hidden orig_{hname}', f'.type orig_{hname}, %function',
                f'orig_{hname}:']
        for k, insn in enumerate(insns):
            asm += ['    ' + line for line in relocate(insn, rva + 4 * k)]
        asm += ['    ' + line for line in load_main_addr(16, rva + 4 * n)] + ['    br x16', '']
    tramp = os.path.join(gen, 'hooks.S')
    open(tramp, 'w').write('\n'.join(asm) + '\n')

    sdk = os.path.join(ROOT, 'sdk')
    all_srcs = [os.path.join(sdk, f) for f in sdk_sources] + [tramp] + list(srcs)
    cflags = ['--target=aarch64-none-elf', '-march=armv8-a', '-fPIC', '-ffreestanding', '-fno-builtin',
              '-nostdlib', '-nostdlibinc', '-nostdinc++', '-fno-exceptions', '-fno-rtti', '-fno-stack-protector',
              '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-fvisibility=hidden', '-O2', '-Wall',
              '-Wno-unused-function', '-ffunction-sections', '-fdata-sections', f'-DSILK_NNMAIN_RVA={nnmain:#x}',
              f'-I{sdk}', f'-I{gen}', *[f'-I{d}' for d in include_dirs], *extra_cflags]
    procs, objs = [], []
    for src in all_srcs:
        obj = os.path.join(build, os.path.basename(src) + '.o')
        lang = ['-std=c++20'] if src.endswith('.cpp') else []
        cmd = [f'{LLVM}/clang', *cflags, *lang, '-c', src, '-o', obj]
        procs.append((cmd, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
        objs.append(obj)
    failed = False
    for cmd, p in procs:
        out, err = p.communicate()
        if p.returncode:
            failed = True
            sys.stderr.write(' '.join(cmd[-3:]) + '\n' + out + err)
        elif err.strip() and verbose:
            sys.stderr.write(err)
    if failed:
        raise SystemExit(1)
    elf_path = os.path.join(build, f'{name}.elf')
    run([platform_paths.lld_path(LLVM) or f'{LLVM}/ld.lld', '-shared', '--hash-style=sysv', '-z', 'norelro', '--no-undefined-version',
         '-Bsymbolic', '--gc-sections', '-init=silk_module_init', '-T', os.path.join(sdk, 'module.ld'),
         '--allow-shlib-undefined', *[f'--undefined=hook_{h}' for h, _ in hooks], '-o', elf_path, *objs])
    elf = Elf(elf_path)
    syms = elf.symbols()

    out = os.path.join(ROOT, 'out', name)
    shutil.rmtree(out, ignore_errors=True)
    exefs = os.path.join(out, 'exefs')
    os.makedirs(exefs)
    build_id = hashlib.sha256(open(elf_path, 'rb').read()).digest()[:0x20]
    elf2nso(elf, os.path.join(exefs, 'subsdk9'), build_id)

    ips = bytearray(b'IPS32')
    for hname, rva in hooks:
        target = module_offset + syms[f'hook_{hname}']
        if sizes[hname] == 1:
            delta = (target - rva) >> 2
            assert -(1 << 25) <= delta < (1 << 25)
            patch = struct.pack('<I', 0x14000000 | (delta & 0x3FFFFFF))
        else:
            patch = adrp_add_br(rva, target)
        ips += struct.pack('>IH', rva + 0x100, len(patch)) + patch
        if verbose:
            print(f'  hook {hname}: main+{rva:#x} -> subsdk9+{syms[f"hook_{hname}"]:#x}')
    ips += b'EEOF'
    if hooks:
        open(os.path.join(exefs, main_nso.build_id.hex().upper() + '.ips'), 'wb').write(ips)

    if romfs_dir:
        shutil.copytree(romfs_dir, os.path.join(out, 'romfs'))
    print(f'built {out}  (module offset {module_offset:#x}, {len(hooks)} hooks)')

    if install:
        dst = os.path.join(RYU_MODS, name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(out, dst)
        print(f'installed -> {dst}')
    return out


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(' '.join(cmd) + '\n' + r.stdout + r.stderr)
        raise SystemExit(1)
    if r.stderr.strip():
        sys.stderr.write(r.stderr)


if __name__ == '__main__':
    main()
