#!/usr/bin/env python3
"""Disassemble main at an RVA (or Class::Method from the dump). Usage: disasm.py <rva|Class::Method> [count]"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nso import Nso  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLVM = '/opt/homebrew/opt/llvm@18/bin'


def main():
    target, count = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40
    if '::' in target:
        cls, name = target.split('::')
        d = json.load(open(os.path.join(ROOT, 'game', 'dump.json')))
        hits = [(m['rva'], m['params']) for t in d['types'] if t['name'] == cls for m in t['methods']
                if m['name'] == name and m['rva']]
        for rva, ps in hits:
            print(f'# {target}({", ".join(ps)}) @ {rva:#x}')
        rva = hits[0][0]
    else:
        rva = int(target, 16)
    cache = os.path.join(ROOT, 'build', 'main_text.o')
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        n = Nso(os.path.join(ROOT, 'game', 'exefs', 'main'))
        raw = cache + '.bin'
        open(raw, 'wb').write(n.image[:n.text[1]])
        subprocess.check_call([f'{LLVM}/llvm-objcopy', '-I', 'binary', '-O', 'elf64-littleaarch64',
                               '--rename-section', '.data=.text,code', raw, cache])
    out = subprocess.check_output([f'{LLVM}/llvm-objdump', '-d', '--no-show-raw-insn',
                                   f'--start-address={rva:#x}', f'--stop-address={rva + 4 * count:#x}', cache],
                                  text=True)
    print('\n'.join(l for l in out.splitlines() if l.strip().startswith(tuple('0123456789abcdef')) and ':' in l))


if __name__ == '__main__':
    main()
