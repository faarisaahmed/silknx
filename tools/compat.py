#!/usr/bin/env python3
"""Report how much of a BepInEx mod's external API surface exists in the Switch IL2CPP build.

Usage: compat.py <mod.dll>...
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clrmeta import Assembly  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEPINEX = ('BepInEx', 'HarmonyLib', 'MonoMod', 'Mono.Cecil')
RISKY = ('System.Reflection', 'System.IO', 'System.Net', 'System.Threading', 'UnityEngine.AssetBundle')


def load_dump():
    d = json.load(open(os.path.join(ROOT, 'game', 'dump.json')))
    types = {}
    for t in d['types']:
        types.setdefault(t['name'].replace('.', '/') if False else t['name'], t)
    return types


def main():
    types = load_dump()
    for path in sys.argv[1:]:
        a = Assembly(path)
        ok, missing, loader, risky = [], [], set(), set()
        for i in range(1, len(a.tables.get(0x0A, [])) + 1):
            owner, name, ret, ps = a.memberref(i)
            base = owner.split('<')[0].replace('/', '.')
            if base.startswith(BEPINEX):
                loader.add(f'{owner}::{name}')
                continue
            if base.startswith(RISKY):
                risky.add(f'{owner}::{name}')
            t = types.get(base) or types.get(base.split('`')[0])
            if not t:
                # generic instantiation names in the dump drop the arity suffix
                cands = [n for n in types if n.split('`')[0] == base.split('`')[0]]
                t = types[cands[0]] if cands else None
            if not t:
                missing.append(f'{owner}::{name}  (type missing)')
                continue
            if ret == 'field':
                (ok if any(f['name'] == name for f in t['fields']) else missing).append(f'{owner}::{name}')
                continue
            ms = [m for m in t['methods'] if m['name'] == name and len(m['params']) == len(ps)]
            if any(m['rva'] for m in ms):
                ok.append(f'{owner}::{name}')
            elif ms:
                missing.append(f'{owner}::{name}({len(ps)})  (stripped/generic-only)')
            else:
                missing.append(f'{owner}::{name}({len(ps)})  (method missing)')
        total = len(ok) + len(missing)
        print(f'\n=== {os.path.basename(path)}: {len(ok)}/{total} game/Unity/BCL refs exist on Switch, '
              f'{len(loader)} BepInEx/Harmony refs, {len(risky)} risky')
        for m in sorted(set(missing))[:25]:
            print('   MISSING', m)
        if len(set(missing)) > 25:
            print(f'   ... {len(set(missing)) - 25} more')
        for r in sorted(risky)[:8]:
            print('   RISKY  ', r)


if __name__ == '__main__':
    main()
