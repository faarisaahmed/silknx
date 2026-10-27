#!/usr/bin/env python3
"""Minimal .NET (ECMA-335) metadata reader + IL disassembler.

Usage:
  clrmeta.py <mod.dll> refs            # external APIs the assembly uses (MemberRefs grouped by type)
  clrmeta.py <mod.dll> types           # types/methods defined in the assembly
  clrmeta.py <mod.dll> il [filter]     # IL of every method (optionally only methods whose full name contains filter)
  clrmeta.py <mod.dll> strings         # user string literals
"""
import struct
import sys

# ---------------------------------------------------------------- table schema

S, G, B = 'S', 'G', 'B'
CODED = {
    'TypeDefOrRef': (2, [0x02, 0x01, 0x1B]),
    'HasConstant': (2, [0x04, 0x08, 0x17]),
    'HasCustomAttribute': (5, [0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00, 0x0E, 0x17, 0x14, 0x11, 0x1A,
                               0x1B, 0x20, 0x23, 0x26, 0x27, 0x28, 0x2A, 0x2C, 0x2B]),
    'HasFieldMarshal': (1, [0x04, 0x08]),
    'HasDeclSecurity': (2, [0x02, 0x06, 0x20]),
    'MemberRefParent': (3, [0x02, 0x01, 0x1A, 0x06, 0x1B]),
    'HasSemantics': (1, [0x14, 0x17]),
    'MethodDefOrRef': (1, [0x06, 0x0A]),
    'MemberForwarded': (1, [0x04, 0x06]),
    'Implementation': (2, [0x26, 0x23, 0x27]),
    'CustomAttributeType': (3, [None, None, 0x06, 0x0A, None]),
    'ResolutionScope': (2, [0x00, 0x1A, 0x23, 0x01]),
    'TypeOrMethodDef': (1, [0x02, 0x06]),
}
SCHEMA = {
    0x00: ('Module', [2, S, G, G, G]),
    0x01: ('TypeRef', ['ResolutionScope', S, S]),
    0x02: ('TypeDef', [4, S, S, 'TypeDefOrRef', ('T', 0x04), ('T', 0x06)]),
    0x03: ('FieldPtr', [('T', 0x04)]),
    0x04: ('Field', [2, S, B]),
    0x05: ('MethodPtr', [('T', 0x06)]),
    0x06: ('MethodDef', [4, 2, 2, S, B, ('T', 0x08)]),
    0x07: ('ParamPtr', [('T', 0x08)]),
    0x08: ('Param', [2, 2, S]),
    0x09: ('InterfaceImpl', [('T', 0x02), 'TypeDefOrRef']),
    0x0A: ('MemberRef', ['MemberRefParent', S, B]),
    0x0B: ('Constant', [2, 'HasConstant', B]),
    0x0C: ('CustomAttribute', ['HasCustomAttribute', 'CustomAttributeType', B]),
    0x0D: ('FieldMarshal', ['HasFieldMarshal', B]),
    0x0E: ('DeclSecurity', [2, 'HasDeclSecurity', B]),
    0x0F: ('ClassLayout', [2, 4, ('T', 0x02)]),
    0x10: ('FieldLayout', [4, ('T', 0x04)]),
    0x11: ('StandAloneSig', [B]),
    0x12: ('EventMap', [('T', 0x02), ('T', 0x14)]),
    0x13: ('EventPtr', [('T', 0x14)]),
    0x14: ('Event', [2, S, 'TypeDefOrRef']),
    0x15: ('PropertyMap', [('T', 0x02), ('T', 0x17)]),
    0x16: ('PropertyPtr', [('T', 0x17)]),
    0x17: ('Property', [2, S, B]),
    0x18: ('MethodSemantics', [2, ('T', 0x06), 'HasSemantics']),
    0x19: ('MethodImpl', [('T', 0x02), 'MethodDefOrRef', 'MethodDefOrRef']),
    0x1A: ('ModuleRef', [S]),
    0x1B: ('TypeSpec', [B]),
    0x1C: ('ImplMap', [2, 'MemberForwarded', S, ('T', 0x1A)]),
    0x1D: ('FieldRVA', [4, ('T', 0x04)]),
    0x1E: ('EncLog', [4, 4]),
    0x1F: ('EncMap', [4]),
    0x20: ('Assembly', [4, 2, 2, 2, 2, 4, B, S, S]),
    0x21: ('AssemblyProcessor', [4]),
    0x22: ('AssemblyOS', [4, 4, 4]),
    0x23: ('AssemblyRef', [2, 2, 2, 2, 4, B, S, S, B]),
    0x24: ('AssemblyRefProcessor', [4, ('T', 0x23)]),
    0x25: ('AssemblyRefOS', [4, 4, 4, ('T', 0x23)]),
    0x26: ('File', [4, S, B]),
    0x27: ('ExportedType', [4, 4, S, S, 'Implementation']),
    0x28: ('ManifestResource', [4, 4, S, 'Implementation']),
    0x29: ('NestedClass', [('T', 0x02), ('T', 0x02)]),
    0x2A: ('GenericParam', [2, 2, 'TypeOrMethodDef', S]),
    0x2B: ('MethodSpec', ['MethodDefOrRef', B]),
    0x2C: ('GenericParamConstraint', [('T', 0x2A), 'TypeDefOrRef']),
}

PRIM = {0x01: 'void', 0x02: 'bool', 0x03: 'char', 0x04: 'sbyte', 0x05: 'byte', 0x06: 'short', 0x07: 'ushort',
        0x08: 'int', 0x09: 'uint', 0x0A: 'long', 0x0B: 'ulong', 0x0C: 'float', 0x0D: 'double', 0x0E: 'string',
        0x16: 'TypedReference', 0x18: 'IntPtr', 0x19: 'UIntPtr', 0x1C: 'object'}

# ---------------------------------------------------------------- IL opcodes
# operand kinds: n=none, i1/i4/i8/r4/r8, t=token, s=string token, br1/br4, sw=switch, u1/u2 var index
_OPS1 = """
00 nop n|01 break n|02 ldarg.0 n|03 ldarg.1 n|04 ldarg.2 n|05 ldarg.3 n|06 ldloc.0 n|07 ldloc.1 n|08 ldloc.2 n
09 ldloc.3 n|0a stloc.0 n|0b stloc.1 n|0c stloc.2 n|0d stloc.3 n|0e ldarg.s u1|0f ldarga.s u1|10 starg.s u1
11 ldloc.s u1|12 ldloca.s u1|13 stloc.s u1|14 ldnull n|15 ldc.i4.m1 n|16 ldc.i4.0 n|17 ldc.i4.1 n|18 ldc.i4.2 n
19 ldc.i4.3 n|1a ldc.i4.4 n|1b ldc.i4.5 n|1c ldc.i4.6 n|1d ldc.i4.7 n|1e ldc.i4.8 n|1f ldc.i4.s i1|20 ldc.i4 i4
21 ldc.i8 i8|22 ldc.r4 r4|23 ldc.r8 r8|25 dup n|26 pop n|27 jmp t|28 call t|29 calli t|2a ret n|2b br.s br1
2c brfalse.s br1|2d brtrue.s br1|2e beq.s br1|2f bge.s br1|30 bgt.s br1|31 ble.s br1|32 blt.s br1|33 bne.un.s br1
34 bge.un.s br1|35 bgt.un.s br1|36 ble.un.s br1|37 blt.un.s br1|38 br br4|39 brfalse br4|3a brtrue br4|3b beq br4
3c bge br4|3d bgt br4|3e ble br4|3f blt br4|40 bne.un br4|41 bge.un br4|42 bgt.un br4|43 ble.un br4|44 blt.un br4
45 switch sw|46 ldind.i1 n|47 ldind.u1 n|48 ldind.i2 n|49 ldind.u2 n|4a ldind.i4 n|4b ldind.u4 n|4c ldind.i8 n
4d ldind.i n|4e ldind.r4 n|4f ldind.r8 n|50 ldind.ref n|51 stind.ref n|52 stind.i1 n|53 stind.i2 n|54 stind.i4 n
55 stind.i8 n|56 stind.r4 n|57 stind.r8 n|58 add n|59 sub n|5a mul n|5b div n|5c div.un n|5d rem n|5e rem.un n
5f and n|60 or n|61 xor n|62 shl n|63 shr n|64 shr.un n|65 neg n|66 not n|67 conv.i1 n|68 conv.i2 n|69 conv.i4 n
6a conv.i8 n|6b conv.r4 n|6c conv.r8 n|6d conv.u4 n|6e conv.u8 n|6f callvirt t|70 cpobj t|71 ldobj t|72 ldstr s
73 newobj t|74 castclass t|75 isinst t|76 conv.r.un n|79 unbox t|7a throw n|7b ldfld t|7c ldflda t|7d stfld t
7e ldsfld t|7f ldsflda t|80 stsfld t|81 stobj t|82 conv.ovf.i1.un n|83 conv.ovf.i2.un n|84 conv.ovf.i4.un n
85 conv.ovf.i8.un n|86 conv.ovf.u1.un n|87 conv.ovf.u2.un n|88 conv.ovf.u4.un n|89 conv.ovf.u8.un n
8a conv.ovf.i.un n|8b conv.ovf.u.un n|8c box t|8d newarr t|8e ldlen n|8f ldelema t|90 ldelem.i1 n|91 ldelem.u1 n
92 ldelem.i2 n|93 ldelem.u2 n|94 ldelem.i4 n|95 ldelem.u4 n|96 ldelem.i8 n|97 ldelem.i n|98 ldelem.r4 n
99 ldelem.r8 n|9a ldelem.ref n|9b stelem.i n|9c stelem.i1 n|9d stelem.i2 n|9e stelem.i4 n|9f stelem.i8 n
a0 stelem.r4 n|a1 stelem.r8 n|a2 stelem.ref n|a3 ldelem t|a4 stelem t|a5 unbox.any t|b3 conv.ovf.i1 n
b4 conv.ovf.u1 n|b5 conv.ovf.i2 n|b6 conv.ovf.u2 n|b7 conv.ovf.i4 n|b8 conv.ovf.u4 n|b9 conv.ovf.i8 n
ba conv.ovf.u8 n|c2 refanyval t|c3 ckfinite n|c6 mkrefany t|d0 ldtoken t|d1 conv.u2 n|d2 conv.u1 n|d3 conv.i n
d4 conv.ovf.i n|d5 conv.ovf.u n|d6 add.ovf n|d7 add.ovf.un n|d8 mul.ovf n|d9 mul.ovf.un n|da sub.ovf n
db sub.ovf.un n|dc endfinally n|dd leave br4|de leave.s br1|df stind.i n|e0 conv.u n
"""
_OPS2 = """
00 arglist n|01 ceq n|02 cgt n|03 cgt.un n|04 clt n|05 clt.un n|06 ldftn t|07 ldvirtftn t|09 ldarg u2|0a ldarga u2
0b starg u2|0c ldloc u2|0d ldloca u2|0e stloc u2|0f localloc n|11 endfilter n|12 unaligned. u1|13 volatile. n
14 tail. n|15 initobj t|16 constrained. t|17 cpblk n|18 initblk n|19 no. u1|1a rethrow n|1c sizeof t
1d refanytype n|1e readonly. n
"""


def _parse_ops(text):
    out = {}
    for item in text.replace('\n', '|').split('|'):
        item = item.strip()
        if item:
            code, name, kind = item.split()
            out[int(code, 16)] = (name, kind)
    return out


OPS1, OPS2 = _parse_ops(_OPS1), _parse_ops(_OPS2)


# ---------------------------------------------------------------- reader

class Assembly:
    def __init__(self, path):
        self.d = d = open(path, 'rb').read()
        pe = struct.unpack_from('<I', d, 0x3C)[0]
        assert d[pe:pe + 4] == b'PE\0\0'
        nsec = struct.unpack_from('<H', d, pe + 6)[0]
        opt_size = struct.unpack_from('<H', d, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from('<H', d, opt)[0]
        dd = opt + (96 if magic == 0x10B else 112)
        self.sections = []
        for i in range(nsec):
            o = opt + opt_size + 40 * i
            vsize, va, rsize, raw = struct.unpack_from('<IIII', d, o + 8)
            self.sections.append((va, max(vsize, rsize), raw))
        cli_rva = struct.unpack_from('<I', d, dd + 14 * 8)[0]
        cli = self.off(cli_rva)
        self.resources_rva, self.resources_size = struct.unpack_from('<II', d, cli + 0x18)
        meta_rva = struct.unpack_from('<I', d, cli + 8)[0]
        m = self.off(meta_rva)
        assert d[m:m + 4] == b'BSJB'
        vlen = struct.unpack_from('<I', d, m + 12)[0]
        o = m + 16 + vlen
        nstreams = struct.unpack_from('<H', d, o + 2)[0]
        o += 4
        self.streams = {}
        for _ in range(nstreams):
            soff, ssize = struct.unpack_from('<II', d, o)
            e = d.index(b'\0', o + 8)
            name = d[o + 8:e].decode()
            o = (e + 4) & ~3
            self.streams[name] = (m + soff, ssize)
        self._tables()

    def off(self, rva):
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(hex(rva))

    def _tables(self):
        d = self.d
        base = self.streams.get('#~', self.streams.get('#-'))[0]
        heap_sizes = d[base + 6]
        valid = struct.unpack_from('<Q', d, base + 8)[0]
        self.sidx = 4 if heap_sizes & 1 else 2
        self.gidx = 4 if heap_sizes & 2 else 2
        self.bidx = 4 if heap_sizes & 4 else 2
        o = base + 24
        self.rows = {}
        for t in range(64):
            if valid >> t & 1:
                self.rows[t] = struct.unpack_from('<I', d, o)[0]
                o += 4
        if heap_sizes & 0x40:  # extra data
            o += 4

        def col_size(c):
            if c == 2 or c == 4:
                return c
            if c == S:
                return self.sidx
            if c == G:
                return self.gidx
            if c == B:
                return self.bidx
            if isinstance(c, tuple):
                return 2 if self.rows.get(c[1], 0) < 0x10000 else 4
            bits, tabs = CODED[c]
            mx = max(self.rows.get(t, 0) for t in tabs if t is not None)
            return 2 if mx < (1 << (16 - bits)) else 4

        self.tables = {}
        for t in sorted(self.rows):
            name, cols = SCHEMA[t]
            sizes = [col_size(c) for c in cols]
            rs = sum(sizes)
            rows = []
            for r in range(self.rows[t]):
                vals, p = [], o + r * rs
                for c, sz in zip(cols, sizes):
                    v = struct.unpack_from('<H' if sz == 2 else '<I', d, p)[0]
                    p += sz
                    if isinstance(c, str) and c in CODED:
                        bits, tabs = CODED[c]
                        v = (tabs[v & ((1 << bits) - 1)], v >> bits)
                    vals.append(v)
                rows.append(vals)
            self.tables[t] = rows
            o += rs * self.rows[t]

    # heaps
    def manifest_resources(self):
        """[(name, bytes)] for resources embedded in this assembly."""
        out = []
        if not self.resources_rva:
            return out
        base = self.off(self.resources_rva)
        for offset, flags, name, impl in self.tables.get(0x28, []):
            if impl[1]:  # stored in another file/assembly
                continue
            n = struct.unpack_from('<I', self.d, base + offset)[0]
            out.append((self.string(name), self.d[base + offset + 4:base + offset + 4 + n]))
        return out

    def string(self, i):
        b = self.streams['#Strings'][0] + i
        return self.d[b:self.d.index(b'\0', b)].decode('utf-8', 'replace')

    def blob(self, i):
        o = self.streams['#Blob'][0] + i
        n, o = self._cu(self.d, o)
        return self.d[o:o + n]

    def us(self, i):
        o = self.streams['#US'][0] + i
        n, o = self._cu(self.d, o)
        return self.d[o:o + n - 1 if n else o].decode('utf-16-le', 'replace')

    @staticmethod
    def _cu(d, o):
        b = d[o]
        if b & 0x80 == 0:
            return b, o + 1
        if b & 0xC0 == 0x80:
            return ((b & 0x3F) << 8) | d[o + 1], o + 2
        return ((b & 0x1F) << 24) | (d[o + 1] << 16) | (d[o + 2] << 8) | d[o + 3], o + 4

    def row(self, t, i):  # 1-based
        return self.tables[t][i - 1]

    # names
    def typeref_name(self, i):
        scope, name, ns = self.row(0x01, i)
        n = self.string(name)
        if scope[0] == 0x01 and scope[1]:
            return self.typeref_name(scope[1]) + '/' + n
        ns = self.string(ns)
        return f'{ns}.{n}' if ns else n

    def typedef_name(self, i):
        r = self.row(0x02, i)
        n = self.string(r[1])
        for nested, enclosing in self.tables.get(0x29, []):
            if nested == i:
                return self.typedef_name(enclosing) + '/' + n
        ns = self.string(r[2])
        return f'{ns}.{n}' if ns else n

    def tdor_name(self, coded):
        t, i = coded
        if not i:
            return None
        if t == 0x02:
            return self.typedef_name(i)
        if t == 0x01:
            return self.typeref_name(i)
        return self.sig_type(self.blob(self.row(0x1B, i)[0]), 0)[0]

    def sig_type(self, b, o):
        e = b[o]
        o += 1
        if e in PRIM:
            return PRIM[e], o
        if e in (0x11, 0x12):  # VALUETYPE, CLASS
            v, o = self._cu(b, o)
            return self.tdor_name(([0x02, 0x01, 0x1B][v & 3], v >> 2)), o
        if e == 0x1D:
            t, o = self.sig_type(b, o)
            return t + '[]', o
        if e == 0x0F:
            t, o = self.sig_type(b, o)
            return t + '*', o
        if e == 0x10:
            t, o = self.sig_type(b, o)
            return 'ref ' + t, o
        if e in (0x13, 0x1E):
            v, o = self._cu(b, o)
            return ('!' if e == 0x13 else '!!') + str(v), o
        if e == 0x15:
            t, o = self.sig_type(b, o)
            n, o = self._cu(b, o)
            args = []
            for _ in range(n):
                a, o = self.sig_type(b, o)
                args.append(a)
            return f'{t.split("`")[0]}<{", ".join(args)}>', o
        if e == 0x14:  # ARRAY
            t, o = self.sig_type(b, o)
            rank, o = self._cu(b, o)
            nsz, o = self._cu(b, o)
            for _ in range(nsz):
                _, o = self._cu(b, o)
            nlb, o = self._cu(b, o)
            for _ in range(nlb):
                _, o = self._cu(b, o)
            return t + '[' + ',' * (rank - 1) + ']', o
        if e in (0x1F, 0x20):  # modreq/modopt
            _, o = self._cu(b, o)
            return self.sig_type(b, o)
        if e == 0x45:  # pinned
            return self.sig_type(b, o)
        if e == 0x1B:
            return 'fnptr', len(b)
        return f'?{e:#x}', o

    def method_sig(self, b):
        cc = b[0]
        o = 1
        if cc & 0x10:
            _, o = self._cu(b, o)
        n, o = self._cu(b, o)
        ret, o = self.sig_type(b, o)
        ps = []
        for _ in range(n):
            if o < len(b) and b[o] == 0x41:  # sentinel
                o += 1
            p, o = self.sig_type(b, o)
            ps.append(p)
        return ret, ps, bool(cc & 0x20)

    def memberref(self, i):
        parent, name, sig = self.row(0x0A, i)
        t, pi = parent
        if t == 0x01:
            owner = self.typeref_name(pi)
        elif t == 0x02:
            owner = self.typedef_name(pi)
        elif t == 0x1B:
            owner = self.sig_type(self.blob(self.row(0x1B, pi)[0]), 0)[0]
        else:
            owner = f'?{t:#x}'
        b = self.blob(sig)
        n = self.string(name)
        if b and b[0] == 0x06:
            return owner, n, 'field', self.sig_type(b, 1)[0]
        ret, ps, _ = self.method_sig(b)
        return owner, n, ret, ps

    def method_owner(self, mi):
        tds = self.tables[0x02]
        owner = None
        for k, r in enumerate(tds):
            if r[5] <= mi:
                owner = k + 1
        return owner

    def field_owner(self, fi):
        owner = None
        for k, r in enumerate(self.tables[0x02]):
            if r[4] <= fi:
                owner = k + 1
        return owner

    def token(self, tok):
        t, i = tok >> 24, tok & 0xFFFFFF
        if t == 0x0A:
            owner, n, ret, ps = self.memberref(i)
            if ret == 'field':
                return f'{ps} {owner}::{n}'
            return f'{ret} {owner}::{n}({", ".join(ps)})'
        if t == 0x06:
            r = self.row(0x06, i)
            ret, ps, _ = self.method_sig(self.blob(r[4]))
            return f'{ret} {self.typedef_name(self.method_owner(i))}::{self.string(r[3])}({", ".join(ps)})'
        if t == 0x04:
            r = self.row(0x04, i)
            return f'{self.sig_type(self.blob(r[2]), 1)[0]} {self.typedef_name(self.field_owner(i))}::{self.string(r[1])}'
        if t in (0x01, 0x02, 0x1B):
            return self.tdor_name((t, i))
        if t == 0x2B:
            m, inst = self.row(0x2B, i)
            b = self.blob(inst)
            n, o = self._cu(b, 1)
            args = []
            for _ in range(n):
                a, o = self.sig_type(b, o)
                args.append(a)
            base = self.token((0x06 if m[0] == 0x06 else 0x0A) << 24 | m[1])
            return f'{base} <{", ".join(args)}>'
        if t == 0x70:
            return repr(self.us(i))
        if t == 0x11:
            return 'sig'
        return f'tok {tok:#x}'

    def methods(self):
        for i, r in enumerate(self.tables.get(0x06, []), 1):
            owner = self.typedef_name(self.method_owner(i))
            yield i, owner, self.string(r[3]), r

    def il(self, rva):
        o = self.off(rva)
        d = self.d
        h = d[o]
        if h & 3 == 2:
            size, code = h >> 2, o + 1
        else:
            size = struct.unpack_from('<I', d, o + 4)[0]
            code = o + (struct.unpack_from('<H', d, o)[0] >> 12) * 4
        p, end, out = code, code + size, []
        while p < end:
            at = p - code
            op = d[p]
            p += 1
            if op == 0xFE:
                name, kind = OPS2.get(d[p], (f'fe{d[p]:02x}', 'n'))
                p += 1
            else:
                name, kind = OPS1.get(op, (f'op{op:02x}', 'n'))
            arg = ''
            if kind == 'i1':
                arg = str(struct.unpack_from('<b', d, p)[0]); p += 1
            elif kind == 'u1':
                arg = str(d[p]); p += 1
            elif kind == 'u2':
                arg = str(struct.unpack_from('<H', d, p)[0]); p += 2
            elif kind == 'i4':
                arg = str(struct.unpack_from('<i', d, p)[0]); p += 4
            elif kind == 'i8':
                arg = str(struct.unpack_from('<q', d, p)[0]); p += 8
            elif kind == 'r4':
                arg = repr(struct.unpack_from('<f', d, p)[0]); p += 4
            elif kind == 'r8':
                arg = repr(struct.unpack_from('<d', d, p)[0]); p += 8
            elif kind in ('t', 's'):
                arg = self.token(struct.unpack_from('<I', d, p)[0]); p += 4
            elif kind == 'br1':
                arg = f'IL_{p + 1 - code + struct.unpack_from("<b", d, p)[0]:04x}'; p += 1
            elif kind == 'br4':
                arg = f'IL_{p + 4 - code + struct.unpack_from("<i", d, p)[0]:04x}'; p += 4
            elif kind == 'sw':
                n = struct.unpack_from('<I', d, p)[0]
                tg = struct.unpack_from(f'<{n}i', d, p + 4)
                p += 4 + 4 * n
                arg = ', '.join(f'IL_{p - code + t:04x}' for t in tg)
            out.append(f'IL_{at:04x}: {name} {arg}'.rstrip())
        return out


def main():
    a = Assembly(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else 'refs'
    if mode == 'refs':
        by = {}
        for i in range(1, len(a.tables.get(0x0A, [])) + 1):
            owner, n, ret, ps = a.memberref(i)
            sig = f'{n} : {ps}' if ret == 'field' else f'{n}({", ".join(ps)}) -> {ret}'
            by.setdefault(owner, set()).add(sig)
        for owner in sorted(by):
            print(owner)
            for s in sorted(by[owner]):
                print('   ', s)
    elif mode == 'types':
        for i, r in enumerate(a.tables.get(0x02, []), 1):
            print(a.typedef_name(i), ':', a.tdor_name(r[3]))
        for i, owner, n, r in a.methods():
            ret, ps, _ = a.method_sig(a.blob(r[4]))
            print(f'  {owner}::{n}({", ".join(ps)}) -> {ret}')
    elif mode == 'strings':
        o, (base, size) = 1, a.streams['#US']
        while o < size:
            n, p = a._cu(a.d, base + o)
            if n:
                print(repr(a.d[p:p + n - 1].decode('utf-16-le', 'replace')))
            o = p - base + n
    elif mode == 'il':
        flt = sys.argv[3] if len(sys.argv) > 3 else ''
        for i, owner, n, r in a.methods():
            full = f'{owner}::{n}'
            if flt not in full or not r[0]:
                continue
            ret, ps, _ = a.method_sig(a.blob(r[4]))
            print(f'\n.method {ret} {full}({", ".join(ps)})')
            for line in a.il(r[0]):
                print('   ', line)


if __name__ == '__main__':
    main()
