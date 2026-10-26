"""Typed .NET metadata model spanning several assemblies (mods + PC reference assemblies)."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clrmeta import Assembly  # noqa: E402

# ---------------------------------------------------------------- type signatures
# A TypeSig is a tuple:
#   ('prim', name)            name in PRIMS
#   ('class', key)            reference type (key = full name, '/' for nesting, `N for generic arity)
#   ('vt', key)               value type
#   ('gi', key, (args...), is_vt)   generic instantiation
#   ('sz', elem)              single-dim zero-based array
#   ('md', elem, rank)        multi-dim array
#   ('byref', elem)
#   ('ptr', elem)
#   ('var', n) / ('mvar', n)  generic parameters
PRIMS = {0x01: 'void', 0x02: 'bool', 0x03: 'char', 0x04: 'i1', 0x05: 'u1', 0x06: 'i2', 0x07: 'u2', 0x08: 'i4',
         0x09: 'u4', 0x0A: 'i8', 0x0B: 'u8', 0x0C: 'r4', 0x0D: 'r8', 0x0E: 'string', 0x16: 'typedref',
         0x18: 'i', 0x19: 'u', 0x1C: 'object'}
PRIM_KEYS = {'bool': 'System.Boolean', 'char': 'System.Char', 'i1': 'System.SByte', 'u1': 'System.Byte',
             'i2': 'System.Int16', 'u2': 'System.UInt16', 'i4': 'System.Int32', 'u4': 'System.UInt32',
             'i8': 'System.Int64', 'u8': 'System.UInt64', 'r4': 'System.Single', 'r8': 'System.Double',
             'string': 'System.String', 'object': 'System.Object', 'i': 'System.IntPtr', 'u': 'System.UIntPtr',
             'void': 'System.Void', 'typedref': 'System.TypedReference'}
KEY_PRIMS = {v: k for k, v in PRIM_KEYS.items()}


def sig_str(t):
    k = t[0]
    if k == 'prim':
        return t[1]
    if k in ('class', 'vt'):
        return t[1]
    if k == 'gi':
        return f'{t[1].split("`")[0]}<{", ".join(sig_str(a) for a in t[2])}>'
    if k == 'sz':
        return sig_str(t[1]) + '[]'
    if k == 'md':
        return sig_str(t[1]) + '[' + ',' * (t[2] - 1) + ']'
    if k == 'byref':
        return 'ref ' + sig_str(t[1])
    if k == 'ptr':
        return sig_str(t[1]) + '*'
    if k == 'var':
        return f'!{t[1]}'
    if k == 'mvar':
        return f'!!{t[1]}'
    return str(t)


def subst(t, targs=(), margs=()):
    """Replace generic parameters in a TypeSig."""
    k = t[0]
    if k == 'var':
        return targs[t[1]] if t[1] < len(targs) else t
    if k == 'mvar':
        return margs[t[1]] if t[1] < len(margs) else t
    if k == 'gi':
        return ('gi', t[1], tuple(subst(a, targs, margs) for a in t[2]), t[3])
    if k in ('sz', 'byref', 'ptr'):
        return (k, subst(t[1], targs, margs))
    if k == 'md':
        return ('md', subst(t[1], targs, margs), t[2])
    return t


# ---------------------------------------------------------------- definitions

class FieldDef:
    def __init__(self, asm, idx, flags, name, sig):
        self.asm, self.idx, self.flags, self.name, self.sig = asm, idx, flags, name, sig
        self.owner = None
        self.rva = None  # FieldRVA initial data

    @property
    def static(self):
        return bool(self.flags & 0x10)

    @property
    def literal(self):
        return bool(self.flags & 0x40)

    def __repr__(self):
        return f'{self.owner.key}::{self.name}'


class MethodDef:
    def __init__(self, asm, idx, rva, impl_flags, flags, name, sig_blob):
        self.asm, self.idx, self.rva, self.impl_flags, self.flags, self.name = asm, idx, rva, impl_flags, flags, name
        self.owner = None
        self.has_this, self.explicit_this, self.gparam_count, self.ret, self.params = asm.parse_method_sig(sig_blob)
        self.param_names = []
        self.gparams = []  # names
        self.overrides = []  # MethodRefs this method explicitly implements (MethodImpl)
        self.attrs = []  # (ctor_ref, args, named)
        self._body = None

    @property
    def static(self):
        return bool(self.flags & 0x10)

    @property
    def virtual(self):
        return bool(self.flags & 0x40)

    @property
    def newslot(self):
        return bool(self.flags & 0x100)

    @property
    def abstract(self):
        return bool(self.flags & 0x400)

    @property
    def is_ctor(self):
        return self.name in ('.ctor', '.cctor')

    def body(self):
        if self._body is None and self.rva:
            self._body = self.asm.method_body(self.rva)
        return self._body

    def __repr__(self):
        return f'{self.owner.key}::{self.name}({", ".join(sig_str(p) for p in self.params)})'


class TypeDef:
    def __init__(self, asm, idx, flags, name, ns):
        self.asm, self.idx, self.flags, self.name, self.ns = asm, idx, flags, name, ns
        self.extends = None  # TypeSig
        self.fields, self.methods, self.interfaces, self.nested = [], [], [], []
        self.enclosing = None
        self.gparams = []
        self.attrs = []
        self.key = None
        self.class_size = None
        self.packing = None

    @property
    def is_interface(self):
        return bool(self.flags & 0x20)

    @property
    def is_abstract(self):
        return bool(self.flags & 0x80)

    @property
    def is_sealed(self):
        return bool(self.flags & 0x100)

    @property
    def explicit_layout(self):
        return self.flags & 0x18 == 0x10

    def method(self, name, nparams=None):
        for m in self.methods:
            if m.name == name and (nparams is None or len(m.params) == nparams):
                return m
        return None

    def field(self, name):
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def __repr__(self):
        return f'<TypeDef {self.key} ({self.asm.name})>'


class MemberRef:
    """Unresolved reference to a method/field on a (possibly generic) type."""

    def __init__(self, parent, name, kind, sig):
        self.parent, self.name, self.kind = parent, name, kind  # parent: TypeSig
        if kind == 'field':
            self.type = sig
        else:
            self.has_this, self.explicit_this, self.gparam_count, self.ret, self.params = sig
        self.margs = ()  # set for MethodSpec

    def __repr__(self):
        if self.kind == 'field':
            return f'{sig_str(self.parent)}::{self.name}'
        m = f'<{", ".join(sig_str(a) for a in self.margs)}>' if self.margs else ''
        return f'{sig_str(self.parent)}::{self.name}{m}({", ".join(sig_str(p) for p in self.params)})'


class Body:
    def __init__(self, code, max_stack, locals_, init_locals, clauses):
        self.code, self.max_stack, self.locals, self.init_locals, self.clauses = (code, max_stack, locals_,
                                                                                  init_locals, clauses)


# ---------------------------------------------------------------- assembly wrapper

class Asm(Assembly):
    def __init__(self, path, universe):
        super().__init__(path)
        self.path = path
        self.universe = universe
        a = self.tables.get(0x20)
        self.name = self.string(a[0][7]) if a else os.path.splitext(os.path.basename(path))[0]
        self.typedefs = []
        self._load()

    # signatures --------------------------------------------------------
    def tdor(self, coded_val):
        tag, idx = [0x02, 0x01, 0x1B][coded_val & 3], coded_val >> 2
        return self.type_from_token(tag, idx)

    def type_from_token(self, tag, idx, is_vt=None):
        if tag == 0x02:
            td = self.typedefs[idx - 1]
            return ('vt' if self.universe.is_valuetype_def(td) else 'class', td.key)
        if tag == 0x01:
            key = self.typeref_key(idx)
            vt = self.universe.is_valuetype_key(key) if is_vt is None else is_vt
            return ('vt' if vt else 'class', key)
        if tag == 0x1B:
            return self.parse_type(self.blob(self.row(0x1B, idx)[0]), 0)[0]
        raise ValueError(tag)

    def typeref_key(self, i):
        scope, name, ns = self.row(0x01, i)
        n = self.string(name)
        if scope[0] == 0x01 and scope[1]:
            return self.typeref_key(scope[1]) + '/' + n
        ns = self.string(ns)
        return f'{ns}.{n}' if ns else n

    def parse_type(self, b, o):
        e = b[o]
        o += 1
        if e in PRIMS:
            return ('prim', PRIMS[e]), o
        if e in (0x11, 0x12):
            v, o = self._cu(b, o)
            tag, idx = [0x02, 0x01, 0x1B][v & 3], v >> 2
            t = self.type_from_token(tag, idx, is_vt=(e == 0x11))
            if t[0] in ('class', 'vt'):
                t = ('vt' if e == 0x11 else 'class', t[1])
            return t, o
        if e == 0x1D:
            t, o = self.parse_type(b, o)
            return ('sz', t), o
        if e == 0x0F:
            t, o = self.parse_type(b, o)
            return ('ptr', t), o
        if e == 0x10:
            t, o = self.parse_type(b, o)
            return ('byref', t), o
        if e in (0x13, 0x1E):
            v, o = self._cu(b, o)
            return ('var' if e == 0x13 else 'mvar', v), o
        if e == 0x15:
            kind = b[o]
            base, o = self.parse_type(b, o)
            n, o = self._cu(b, o)
            args = []
            for _ in range(n):
                a, o = self.parse_type(b, o)
                args.append(a)
            return ('gi', base[1], tuple(args), kind == 0x11), o
        if e == 0x14:
            t, o = self.parse_type(b, o)
            rank, o = self._cu(b, o)
            nsz, o = self._cu(b, o)
            for _ in range(nsz):
                _, o = self._cu(b, o)
            nlb, o = self._cu(b, o)
            for _ in range(nlb):
                _, o = self._cu(b, o)
            return ('md', t, rank), o
        if e in (0x1F, 0x20):
            _, o = self._cu(b, o)
            return self.parse_type(b, o)
        if e == 0x45:
            return self.parse_type(b, o)
        if e == 0x1B:  # function pointer
            return ('prim', 'i'), len(b)
        raise ValueError(f'sig elem {e:#x}')

    def parse_method_sig(self, b):
        cc = b[0]
        o = 1
        gpc = 0
        if cc & 0x10:
            gpc, o = self._cu(b, o)
        n, o = self._cu(b, o)
        ret, o = self.parse_type(b, o)
        ps = []
        for _ in range(n):
            if b[o] == 0x41:
                o += 1
            p, o = self.parse_type(b, o)
            ps.append(p)
        return bool(cc & 0x20), bool(cc & 0x40), gpc, ret, ps

    def parse_locals(self, tok):
        if not tok:
            return []
        b = self.blob(self.row(0x11, tok & 0xFFFFFF)[0])
        assert b[0] == 0x07
        n, o = self._cu(b, 1)
        out = []
        for _ in range(n):
            t, o = self.parse_type(b, o)
            out.append(t)
        return out

    # bodies -------------------------------------------------------------
    def method_body(self, rva):
        o = self.off(rva)
        d = self.d
        h = d[o]
        if h & 3 == 2:
            return Body(d[o + 1:o + 1 + (h >> 2)], 8, [], False, [])
        flags_size, max_stack, code_size, ltok = struct.unpack_from('<HHII', d, o)
        hsize = (flags_size >> 12) * 4
        code = d[o + hsize:o + hsize + code_size]
        clauses = []
        if flags_size & 0x8:  # more sections
            p = (o + hsize + code_size + 3) & ~3
            while True:
                kind = d[p]
                if kind & 0x40:  # fat
                    size = struct.unpack_from('<I', d, p)[0] >> 8
                    n = (size - 4) // 24
                    for k in range(n):
                        clauses.append(struct.unpack_from('<6I', d, p + 4 + 24 * k))
                else:
                    size = d[p + 1]
                    n = (size - 4) // 12
                    for k in range(n):
                        f, to, tl, ho, hl, ct = struct.unpack_from('<HHBHBI', d, p + 4 + 12 * k)
                        clauses.append((f, to, tl, ho, hl, ct))
                if not kind & 0x80:
                    break
                p += size
        return Body(code, max_stack, self.parse_locals(ltok), bool(flags_size & 0x10), clauses)

    # custom attributes -------------------------------------------------------
    def decode_attr(self, ctor, blob):
        """Decode a custom attribute blob given its ctor (MethodDef or MemberRef)."""
        b = blob
        if len(b) < 2:
            return [], {}
        o = 2
        args = []
        for p in ctor.params:
            v, o = self._attr_value(b, o, p)
            args.append(v)
        named = {}
        if o + 2 <= len(b):
            nn = struct.unpack_from('<H', b, o)[0]
            o += 2
            for _ in range(nn):
                o += 1  # field/property
                et = b[o]
                o += 1
                if et == 0x55:
                    _, o = self._ser_string(b, o)
                    et = 0x08
                name, o = self._ser_string(b, o)
                v, o = self._attr_value(b, o, ('prim', PRIMS.get(et, 'i4')) if et != 0x50 else ('class', 'System.Type'))
                named[name] = v
        return args, named

    def _ser_string(self, b, o):
        if b[o] == 0xFF:
            return None, o + 1
        n, o = self._cu(b, o)
        return b[o:o + n].decode('utf-8'), o + n

    def _attr_value(self, b, o, t):
        k = t[0]
        if k == 'prim':
            n = t[1]
            if n == 'string':
                return self._ser_string(b, o)
            fmt = {'bool': '<?', 'char': '<H', 'i1': '<b', 'u1': '<B', 'i2': '<h', 'u2': '<H', 'i4': '<i',
                   'u4': '<I', 'i8': '<q', 'u8': '<Q', 'r4': '<f', 'r8': '<d'}[n]
            return struct.unpack_from(fmt, b, o)[0], o + struct.calcsize(fmt)
        if k == 'class' and t[1] == 'System.Type':
            s, o = self._ser_string(b, o)
            return ('type', s), o
        if k == 'vt':  # enum: assume int32 underlying
            return struct.unpack_from('<i', b, o)[0], o + 4
        if k == 'sz':
            n = struct.unpack_from('<i', b, o)[0]
            o += 4
            out = []
            for _ in range(max(n, 0)):
                v, o = self._attr_value(b, o, t[1])
                out.append(v)
            return out, o
        if k == 'class' and t[1] == 'System.Object':
            et = b[o]
            o += 1
            if et == 0x50:
                return self._attr_value(b, o, ('class', 'System.Type'))
            return self._attr_value(b, o, ('prim', PRIMS.get(et, 'i4')))
        raise ValueError(f'attr arg type {t}')

    # loading -------------------------------------------------------------
    def _load(self):
        tds = self.tables.get(0x02, [])
        fields = self.tables.get(0x04, [])
        methods = self.tables.get(0x06, [])
        params = self.tables.get(0x08, [])
        self.fielddefs = [None] * len(fields)
        self.methoddefs = [None] * len(methods)
        for i, r in enumerate(tds, 1):
            self.typedefs.append(TypeDef(self, i, r[0], self.string(r[1]), self.string(r[2])))
        for nested, enclosing in self.tables.get(0x29, []):
            self.typedefs[nested - 1].enclosing = self.typedefs[enclosing - 1]
            self.typedefs[enclosing - 1].nested.append(self.typedefs[nested - 1])
        for td in self.typedefs:
            td.key = self._key(td)
        for num, flags, owner, name in self.tables.get(0x2A, []):
            tag, oi = owner
            nm = self.string(name)
            if tag == 0x02:
                td = self.typedefs[oi - 1]
                while len(td.gparams) <= num:
                    td.gparams.append(None)
                td.gparams[num] = nm
        self._pending = (tds, fields, methods, params)

    def _key(self, td):
        if td.enclosing:
            return self._key(td.enclosing) + '/' + td.name
        return f'{td.ns}.{td.name}' if td.ns else td.name

    def finish(self):
        """Second pass: needs the universe to classify value types."""
        tds, fields, methods, params = self._pending
        for i, (td, r) in enumerate(zip(self.typedefs, tds)):
            f0, f1 = r[4], (tds[i + 1][4] if i + 1 < len(tds) else len(fields) + 1)
            m0, m1 = r[5], (tds[i + 1][5] if i + 1 < len(tds) else len(methods) + 1)
            td.extends = self.tdor_val(r[3])
            for fi in range(f0, f1):
                fr = fields[fi - 1]
                b = self.blob(fr[2])
                fd = FieldDef(self, fi, fr[0], self.string(fr[1]), self.parse_type(b, 1)[0])
                fd.owner = td
                td.fields.append(fd)
                self.fielddefs[fi - 1] = fd
            for mi in range(m0, m1):
                mr = methods[mi - 1]
                md = MethodDef(self, mi, mr[0], mr[1], mr[2], self.string(mr[3]), self.blob(mr[4]))
                md.owner = td
                p0 = mr[5]
                p1 = methods[mi][5] if mi < len(methods) else len(params) + 1
                names = [''] * len(md.params)
                for pi in range(p0, p1):
                    pf, seq, pn = params[pi - 1]
                    if 1 <= seq <= len(names):
                        names[seq - 1] = self.string(pn)
                md.param_names = [n or f'p{k}' for k, n in enumerate(names)]
                td.methods.append(md)
                self.methoddefs[mi - 1] = md
        for cls, iface in self.tables.get(0x09, []):
            self.typedefs[cls - 1].interfaces.append(self.tdor_val_coded(iface))
        for num, flags, owner, name in self.tables.get(0x2A, []):
            tag, oi = owner
            if tag == 0x06:
                md = self.methoddefs[oi - 1]
                while len(md.gparams) <= num:
                    md.gparams.append(None)
                md.gparams[num] = self.string(name)
        for cls, body, decl in self.tables.get(0x19, []):
            if body[0] == 0x06:
                self.methoddefs[body[1] - 1].overrides.append(self.method_ref(decl))
        for pack, size, parent in self.tables.get(0x0F, []):
            self.typedefs[parent - 1].packing, self.typedefs[parent - 1].class_size = pack, size
        self.field_offsets = {fi: off for off, fi in self.tables.get(0x10, [])}
        for rva, fi in self.tables.get(0x1D, []):
            self.fielddefs[fi - 1].rva = rva
        for parent, ctype, val in self.tables.get(0x0C, []):
            tag, pi = parent
            ctor = self.method_ref(ctype)
            target = None
            if tag == 0x02:
                target = self.typedefs[pi - 1]
            elif tag == 0x06:
                target = self.methoddefs[pi - 1]
            elif tag == 0x20:
                target = self
            if target is None:
                continue
            try:
                args, named = self.decode_attr(ctor, self.blob(val))
            except Exception:
                args, named = None, None
            entry = (self.attr_type_key(ctor), args, named)
            if target is self:
                self.universe.asm_attrs.setdefault(self.name, []).append(entry)
            else:
                target.attrs.append(entry)

    def tdor_val(self, coded):
        tag, idx = coded
        if not idx:
            return None
        return self.type_from_token(tag, idx)

    def tdor_val_coded(self, coded):
        return self.tdor_val(coded)

    def attr_type_key(self, ctor):
        if isinstance(ctor, MethodDef):
            return ctor.owner.key
        return ctor.parent[1]

    # member references -------------------------------------------------------
    def method_ref(self, coded):
        tag, idx = coded
        if tag == 0x06:
            return self.methoddefs[idx - 1]
        return self.member_ref(idx)

    def member_ref(self, idx):
        parent, name, sig = self.row(0x0A, idx)
        t, pi = parent
        if t == 0x01:
            ptype = self.type_from_token(0x01, pi)
        elif t == 0x02:
            ptype = self.type_from_token(0x02, pi)
        elif t == 0x1B:
            ptype = self.type_from_token(0x1B, pi)
        elif t == 0x06:
            return self.methoddefs[pi - 1]
        else:
            ptype = ('class', '<module>')
        b = self.blob(sig)
        nm = self.string(name)
        if b[0] == 0x06:
            return MemberRef(ptype, nm, 'field', self.parse_type(b, 1)[0])
        return MemberRef(ptype, nm, 'method', self.parse_method_sig(b))

    def resolve_token(self, tok):
        """Token -> MethodDef | FieldDef | MemberRef | TypeSig | ('string', s)."""
        t, i = tok >> 24, tok & 0xFFFFFF
        if t == 0x06:
            return self.methoddefs[i - 1]
        if t == 0x04:
            return self.fielddefs[i - 1]
        if t == 0x0A:
            return self.member_ref(i)
        if t in (0x01, 0x02, 0x1B):
            return self.type_from_token(t, i)
        if t == 0x2B:
            m, inst = self.row(0x2B, i)
            base = self.method_ref(m)
            b = self.blob(inst)
            n, o = self._cu(b, 1)
            args = []
            for _ in range(n):
                a, o = self.parse_type(b, o)
                args.append(a)
            if isinstance(base, MethodDef):
                ref = MemberRef(('class', base.owner.key) if not base.owner.gparams else
                                ('gi', base.owner.key, tuple(('var', k) for k in range(len(base.owner.gparams))),
                                 self.universe.is_valuetype_def(base.owner)),
                                base.name, 'method',
                                (base.has_this, base.explicit_this, base.gparam_count, base.ret, base.params))
                ref.definition = base
            else:
                ref = base
                ref = MemberRef(base.parent, base.name, 'method',
                                (base.has_this, base.explicit_this, base.gparam_count, base.ret, base.params))
            ref.margs = tuple(args)
            return ref
        if t == 0x70:
            return ('string', self.us(i))
        if t == 0x11:
            return ('sig', i)
        raise ValueError(hex(tok))


# ---------------------------------------------------------------- universe

class Universe:
    """All loaded assemblies, with cross-assembly type lookup."""

    def __init__(self):
        self.asms = []
        self.types = {}  # key -> TypeDef (first definition wins: mods, then the Switch game, then a PC copy)
        self.asm_attrs = {}
        self.pc_types = {}  # key -> TypeDef from an (optional) PC copy, used for IL of stripped methods
        self.has_pc = False
        self._twins = {}

    def load(self, path, pc=False):
        a = Asm(path, self)
        self.asms.append(a)
        for td in a.typedefs:
            self.types.setdefault(td.key, td)
            if pc:
                self.pc_types.setdefault(td.key, td)
        if pc:
            self.has_pc = True
        return a

    def load_switch(self, main_path, metadata_path):
        from .il2cpp_model import load_il2cpp
        for a in load_il2cpp(self, main_path, metadata_path):
            self.asms.append(a)
            for td in a.typedefs:
                if td.extends is not None and td.extends[0] == 'prim':
                    td.extends = ('class', PRIM_KEYS[td.extends[1]])
                self.types.setdefault(td.key, td)

    def finish(self):
        for a in self.asms:
            if getattr(a, 'kind', None) != 'il2cpp':
                a.finish()

    def pc_method(self, md):
        """The PC copy's version (with IL) of a game method, or None."""
        key = id(md)
        if key in self._twins:
            return self._twins[key]
        twin = None
        td = self.pc_types.get(md.owner.key)
        if td is not None:
            want = [sig_str(p) for p in md.params]
            for m in td.methods:
                if m.name == md.name and m.gparam_count == md.gparam_count and len(m.params) == len(want):
                    if [sig_str(p) for p in m.params] == want:
                        twin = m
                        break
                    twin = twin or m
        self._twins[key] = twin
        return twin

    def get(self, key):
        return self.types.get(key)

    def is_valuetype_def(self, td):
        if td is None:
            return False
        if td.key in ('System.Enum',):
            return False
        if hasattr(td, 'il2cpp_vt'):
            return td.il2cpp_vt
        r = self.asm_row_extends(td)
        return r in ('System.ValueType', 'System.Enum') and td.key != 'System.ValueType'

    def asm_row_extends(self, td):
        if getattr(td.asm, 'kind', None) == 'il2cpp':
            return td.extends[1] if td.extends else None
        tds = td.asm.tables[0x02]
        tag, idx = tds[td.idx - 1][3]
        if not idx:
            return None
        if tag == 0x01:
            return td.asm.typeref_key(idx)
        if tag == 0x02:
            return td.asm.typedefs[idx - 1].key
        return None

    def is_valuetype_key(self, key):
        if key in EMU_VALUETYPES:
            return True
        return self.is_valuetype_def(self.types.get(key))

    def is_enum(self, td):
        return td is not None and self.asm_row_extends(td) == 'System.Enum'

    def enum_underlying(self, td):
        for f in td.fields:
            if not f.static:
                return f.sig
        return ('prim', 'i4')

    def base_of(self, td):
        if td.extends is None:
            return None
        return self.types.get(td.extends[1])

    def is_subclass(self, td, key):
        while td is not None:
            if td.key == key:
                return True
            td = self.base_of(td)
        return False


# BepInEx value types (BepInEx itself is emulated, so its DLLs are not needed).
EMU_VALUETYPES = {'BepInEx.Configuration.KeyboardShortcut'}


def load_universe(mod_paths, ref_dirs, switch=None):
    """Mods first, then the Switch game's own types (switch=(main, metadata)), then optional PC references."""
    u = Universe()
    mods = [u.load(p) for p in mod_paths]
    if switch:
        u.load_switch(*switch)
    for d in ref_dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith('.dll'):
                try:
                    u.load(os.path.join(d, f), pc=True)
                except Exception as e:  # native or odd DLLs
                    print(f'  (skipping {f}: {e})', file=sys.stderr)
    u.finish()
    return u, mods
