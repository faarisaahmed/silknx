"""Index over the Switch IL2CPP dump (game/dump.json): what exists natively and where."""
import json
import os
import re

from .model import PRIM_KEYS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DUMP_PRIMS = {'void': 'void', 'bool': 'bool', 'char': 'char', 'i1': 'sbyte', 'u1': 'byte', 'i2': 'short',
              'u2': 'ushort', 'i4': 'int', 'u4': 'uint', 'i8': 'long', 'u8': 'ulong', 'r4': 'float', 'r8': 'double',
              'string': 'string', 'object': 'object', 'i': 'IntPtr', 'u': 'UIntPtr', 'typedref': 'TypedReference'}
KEY_TO_DUMP_PRIM = {v: DUMP_PRIMS[k] for k, v in PRIM_KEYS.items()}
GPARAM = re.compile(r'^T\d+$')


def dkey(key):
    """IL type key ('Outer/Inner') -> dump type name ('Outer.Inner')."""
    return key.replace('/', '.')


def dump_type_name(sig):
    """Render a TypeSig the way il2cpp_dump.py names types (for signature matching)."""
    k = sig[0]
    if k == 'prim':
        return DUMP_PRIMS[sig[1]]
    if k in ('class', 'vt'):
        return KEY_TO_DUMP_PRIM.get(sig[1], dkey(sig[1]))
    if k == 'gi':
        return dkey(sig[1]).split('`')[0] + '<' + ', '.join(dump_type_name(a) for a in sig[2]) + '>'
    if k == 'sz':
        return dump_type_name(sig[1]) + '[]'
    if k == 'md':
        return dump_type_name(sig[1]) + '[,]'
    if k == 'byref':
        return dump_type_name(sig[1])
    if k == 'ptr':
        return dump_type_name(sig[1]) + '*'
    if k in ('var', 'mvar'):
        return '*'
    return '?'


def _norm(s):
    # Generic parameter names in the dump are T<n>; treat them (and our '*') as wildcards.
    return re.sub(r'\bT\d+\b', '*', s)


def types_match(dump_t, want):
    a, b = _norm(dump_t), _norm(want)
    if a == b or a == '*' or b == '*':
        return True
    if '*' in a or '*' in b:
        pa = '^' + re.escape(a).replace(r'\*', '.*') + '$'
        pb = '^' + re.escape(b).replace(r'\*', '.*') + '$'
        return re.match(pa, b) is not None or re.match(pb, a) is not None
    return False


class SwitchDB:
    def __init__(self, path=None):
        d = json.load(open(path or os.path.join(ROOT, 'game', 'dump.json')))
        self.generic_instances = d.get('generic_instances', [])
        self.types = {}
        for t in d['types']:
            self.types.setdefault(t['name'], t)
            for m in t['methods']:
                m['ptypes'] = [p.rsplit(' ', 1)[0] for p in m['params']]
                m['pnames'] = [p.rsplit(' ', 1)[1] if ' ' in p else '' for p in m['params']]

    def type(self, key):
        return self.types.get(dkey(key))

    def has_type(self, key):
        return dkey(key) in self.types

    def methods(self, key, name):
        t = self.type(key)
        return [m for m in t['methods'] if m['name'] == name] if t else []

    def method(self, key, name, params, static=None):
        """Find a method by name + parameter TypeSigs. Returns the dump entry or None."""
        want = [dump_type_name(p) for p in params]
        cands = [m for m in self.methods(key, name) if len(m['ptypes']) == len(want)
                 and (static is None or m['static'] == static)]
        exact = [m for m in cands if all(types_match(a, b) for a, b in zip(m['ptypes'], want))]
        if len(exact) > 1:
            strict = [m for m in exact if [_norm(a) for a in m['ptypes']] == [_norm(b) for b in want]]
            if strict:
                exact = strict
        return exact[0] if exact else None

    def field(self, key, name):
        t = self.type(key)
        if not t:
            return None
        for f in t['fields']:
            if f['name'] == name:
                return f
        return None

    def instance_fields(self, key):
        t = self.type(key)
        return [f for f in t['fields'] if not f['static'] and not f['const']] if t else []

    def size(self, key):
        t = self.type(key)
        return t['instance_size'] if t else None

    def base_chain(self, key):
        out = []
        t = self.type(key)
        while t:
            out.append(t['name'])
            p = t['parent']
            t = self.types.get(p) if p else None
        return out
