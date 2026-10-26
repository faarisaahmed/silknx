"""Game type model built from the Switch build itself (IL2CPP metadata + binary), no PC copy needed.

Produces the same TypeDef/MethodDef/FieldDef objects as model.py so the converter can use either source.
Method bodies are not available (the Switch build has no bytecode); methods stripped from the Switch build can
still be translated when an optional PC copy provides their IL.
"""
import os
import struct
import sys

from .model import TypeDef, MethodDef, FieldDef, PRIMS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from il2cpp_dump import Metadata, Image, locate  # noqa: E402

IL2CPP_PRIMS = {k: v for k, v in PRIMS.items()}


class Il2CppAsm:
    """Stands in for a .NET assembly: one IL2CPP image."""
    kind = 'il2cpp'

    def __init__(self, name, universe):
        self.name = name
        self.universe = universe
        self.typedefs = []
        self.field_offsets = {}
        self.path = None


class Il2CppMethodDef(MethodDef):
    def __init__(self):  # populated by the loader
        self._body = None
        self.attrs = []
        self.overrides = []
        self.rva = None
        self.twin = None  # PC MethodDef with IL (optional)

    def body(self):
        twin = self.asm.universe.pc_method(self)
        return twin.body() if twin is not None else None


def load_il2cpp(universe, main_path, metadata_path):
    meta = Metadata(metadata_path)
    img = Image(main_path)
    images = meta.table('images', '<10i')
    typedefs = meta.table('typeDefinitions', '<16i8H2I')
    methods = meta.table('methods', '<7i4H')
    fields = meta.table('fields', '<3i')
    params = meta.table('parameters', '<3i')
    gparams = meta.table('genericParameters', '<2i2h2H')
    gconts = meta.table('genericContainers', '<4i')
    nested = meta.table('nestedTypes', '<i')
    ifaces = meta.table('interfaces', '<i')
    _, _, meta_reg = locate(img, meta, len(images), len(typedefs))
    types_ptr = img.u64(meta_reg + 56)
    s = meta.string

    def type_ptr(tidx):
        return img.u64(types_ptr + 8 * tidx)

    keys = [None] * len(typedefs)

    def key_of(ti):
        if keys[ti] is None:
            t = typedefs[ti]
            name = s(t[0])
            if t[3] >= 0:
                keys[ti] = key_of(img.u64(type_ptr(t[3])) & 0xFFFFFFFF) + '/' + name
            else:
                ns = s(t[1])
                keys[ti] = f'{ns}.{name}' if ns else name
        return keys[ti]

    def is_vt_def(ti):
        return bool(typedefs[ti][24] & 1)

    def sig(p, depth=0):
        data = img.u64(p)
        bits = struct.unpack_from('<I', img.img, p + 8)[0]
        t = (bits >> 16) & 0xFF
        byref = (bits >> 29) & 1
        if t in IL2CPP_PRIMS:
            r = ('prim', IL2CPP_PRIMS[t])
        elif t in (0x11, 0x12):
            ti = data & 0xFFFFFFFF
            r = ('vt' if is_vt_def(ti) else 'class', key_of(ti))
        elif t == 0x15 and depth < 12:
            gdef = img.u64(data)
            base = sig(gdef, depth + 1)
            inst = img.u64(data + 8)
            args = []
            if inst:
                argc, argv = img.u64(inst), img.u64(inst + 8)
                args = [sig(img.u64(argv + 8 * k), depth + 1) for k in range(argc)]
            r = ('gi', base[1], tuple(args), base[0] == 'vt')
        elif t == 0x1D and depth < 12:
            r = ('sz', sig(data, depth + 1))
        elif t == 0x14 and depth < 12:
            r = ('md', sig(img.u64(data), depth + 1), struct.unpack_from('<B', img.img, data + 8)[0] or 2)
        elif t == 0x0F and depth < 12:
            r = ('ptr', sig(data, depth + 1))
        elif t in (0x13, 0x1E):
            gp = gparams[data & 0xFFFFFFFF] if (data & 0xFFFFFFFF) < len(gparams) else None
            r = ('var' if t == 0x13 else 'mvar', gp[4] if gp else 0)
        else:
            r = ('prim', 'object')
        return ('byref', r) if byref else r

    def tsig(tidx):
        return sig(type_ptr(tidx))

    def gparam_names(container):
        if container < 0:
            return []
        owner, argc, is_method, start = gconts[container]
        return [s(gparams[start + k][1]) for k in range(argc)]

    asms = []
    for im in images:
        aname = s(im[0])
        aname = aname[:-4] if aname.endswith('.dll') else aname
        asm = Il2CppAsm(aname, universe)
        asms.append(asm)
        for ti in range(im[2], im[2] + im[3]):
            t = typedefs[ti]
            td = TypeDef(asm, ti, t[7], s(t[0]), s(t[1]))
            td.key = key_of(ti)
            td.il2cpp_vt = is_vt_def(ti)
            td.il2cpp_enum = bool(t[24] & 2)
            td.extends = tsig(t[4]) if t[4] >= 0 else None
            td.gparams = gparam_names(t[6])
            asm.typedefs.append(td)
    # second pass: members (needs every TypeDef to exist for nesting links)
    all_tds = {}
    for asm in asms:
        for td in asm.typedefs:
            all_tds[td.idx] = td
    for ti, td in all_tds.items():
        t = typedefs[ti]
        (name_i, ns_i, byval, decl, parent, elem, gen_cont, flags, field_start, method_start, ev_start, prop_start,
         nested_start, iface_start, vt_start, io_start, method_count, prop_count, field_count, ev_count,
         nested_count, vt_count, if_count, io_count, bitfield, token) = t
        if decl >= 0:
            td.enclosing = all_tds.get(img.u64(type_ptr(decl)) & 0xFFFFFFFF)
        td.nested = [all_tds[nested[nested_start + k][0]] for k in range(nested_count)
                     if nested[nested_start + k][0] in all_tds]
        td.interfaces = [tsig(ifaces[iface_start + k][0]) for k in range(if_count)]
        for k in range(field_count):
            fname, ftype, _tok = fields[field_start + k]
            fp = type_ptr(ftype)
            attrs = struct.unpack_from('<I', img.img, fp + 8)[0] & 0xFFFF
            fd = FieldDef(td.asm, field_start + k, attrs, s(fname), sig(fp) if not (attrs & 0x40) else
                          _safe(sig, fp))
            fd.owner = td
            td.fields.append(fd)
        for k in range(method_count):
            m = methods[method_start + k]
            (mname, _mdecl, mret, _rpt, pstart, mgc, _mtok, mflags, miflags, slot, pcount) = m
            md = Il2CppMethodDef()
            md.asm, md.idx, md.flags, md.impl_flags, md.name = td.asm, method_start + k, mflags, miflags, s(mname)
            md.owner = td
            md.has_this, md.explicit_this = not (mflags & 0x10), False
            md.gparams = gparam_names(mgc)
            md.gparam_count = len(md.gparams)
            md.ret = tsig(mret)
            md.params = [tsig(params[pstart + j][2]) for j in range(pcount)]
            md.param_names = [s(params[pstart + j][0]) or f'p{j}' for j in range(pcount)]
            td.methods.append(md)
    return asms


def _safe(fn, p):
    try:
        return fn(p)
    except Exception:
        return ('prim', 'i4')
