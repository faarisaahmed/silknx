#!/usr/bin/env python3
"""Minimal IL2CPP (metadata v31) dumper for Switch NSO binaries.

Usage: il2cpp_dump.py <main.nso> <global-metadata.dat> <out.json> [--cs out.cs]

Produces, per type: namespace, name, parent, fields (name, type, offset, static),
methods (name, signature, rva). RVAs are offsets from the start of `main`.
"""
import bisect
import json
import struct
import sys

from nso import Nso

META_SECTIONS = ('stringLiteral stringLiteralData string events properties methods paramDefVals fieldDefVals '
                 'fieldParamDefData fieldMarshaledSizes parameters fields genericParameters genericParamConstraints '
                 'genericContainers nestedTypes interfaces vtableMethods interfaceOffsets typeDefinitions images '
                 'assemblies fieldRefs referencedAssemblies attributeData attributeDataRange unresolvedICallParamTypes '
                 'unresolvedICallParamRanges winrtTypeNames winrtStrings exportedTypeDefinitions').split()

TYPE_NAMES = {1: 'void', 2: 'bool', 3: 'char', 4: 'sbyte', 5: 'byte', 6: 'short', 7: 'ushort', 8: 'int',
              9: 'uint', 0xA: 'long', 0xB: 'ulong', 0xC: 'float', 0xD: 'double', 0xE: 'string',
              0x18: 'IntPtr', 0x19: 'UIntPtr', 0x1C: 'object', 0x16: 'TypedReference'}


class Metadata:
    def __init__(self, path):
        d = self.d = open(path, 'rb').read()
        magic, self.version = struct.unpack_from('<Ii', d, 0)
        assert magic == 0xFAB11BAF and self.version == 31, (hex(magic), self.version)
        self.h = {n: struct.unpack_from('<ii', d, 8 + 8 * i) for i, n in enumerate(META_SECTIONS)}

    def table(self, name, fmt):
        off, size = self.h[name]
        sz = struct.calcsize(fmt)
        return [struct.unpack_from(fmt, self.d, off + sz * i) for i in range(size // sz)]

    def string(self, idx):
        o = self.h['string'][0] + idx
        return self.d[o:self.d.index(b'\0', o)].decode()


class Image:
    """main NSO with R_AARCH64_RELATIVE relocations applied (base 0)."""

    def __init__(self, path):
        self.nso = n = Nso(path)
        img = bytearray(n.image)
        img += bytes(n.data[0] + n.data[1] + n.bss_size - len(img))
        dy = n.dynamic()
        if 7 in dy:
            rela, relasz = dy[7][0], dy[8][0]
            for o in range(rela, rela + relasz, 24):
                off, info, add = struct.unpack_from('<QQq', img, o)
                if info & 0xFFFFFFFF == 1027:
                    struct.pack_into('<Q', img, off, add & 0xFFFFFFFFFFFFFFFF)
        # DT_RELR places already hold their (base-0) values, nothing to apply.
        self.img = bytes(img)

    def u64(self, o):
        return struct.unpack_from('<Q', self.img, o)[0]

    def i32(self, o):
        return struct.unpack_from('<i', self.img, o)[0]

    def cstr(self, o):
        return self.img[o:self.img.index(b'\0', o)].decode(errors='replace')

    def find_ptrs(self, target, lo, hi):
        pat = struct.pack('<Q', target)
        o = self.img.find(pat, lo, hi)
        while o != -1:
            if o % 8 == 0:
                yield o
            o = self.img.find(pat, o + 1, hi)


def locate(img, meta, n_images, n_types):
    nso = img.nso
    data_lo = nso.ro[0]
    data_hi = nso.data[0] + nso.data[1]
    name_off = img.img.find(b'Assembly-CSharp.dll\0', nso.ro[0], nso.ro[0] + nso.ro[1])
    assert name_off > 0
    # Il2CppCodeGenModule.moduleName -> Assembly-CSharp.dll
    for mod in img.find_ptrs(name_off, data_lo, data_hi):
        for arr_slot in img.find_ptrs(mod, data_lo, data_hi):
            # codeGenModules array; walk back to its start: CodeRegistration holds (count, ptr)
            for k in range(n_images):
                start = arr_slot - 8 * k
                for cr_slot in img.find_ptrs(start, data_lo, data_hi):
                    if img.u64(cr_slot - 8) == n_images:
                        code_reg = cr_slot - 8  # &codeGenModulesCount
                        break
                else:
                    continue
                break
            else:
                continue
            modules = [img.u64(start + 8 * i) for i in range(n_images)]
            break
        else:
            continue
        break
    else:
        raise SystemExit('CodeRegistration not found')
    # MetadataRegistration: (fieldOffsetsCount, fieldOffsets, typeDefinitionsSizesCount, typeDefinitionsSizes)
    pat = struct.pack('<Q', n_types)
    o = img.img.find(pat, data_lo, data_hi)
    meta_reg = None
    while o != -1:
        if o % 8 == 0 and img.u64(o + 16) == n_types:
            meta_reg = o - 8 * 10
            break
        o = img.img.find(pat, o + 1, data_hi)
    assert meta_reg, 'MetadataRegistration not found'
    return code_reg, modules, meta_reg


def main():
    nso_path, meta_path, out = sys.argv[1:4]
    meta = Metadata(meta_path)
    img = Image(nso_path)
    images = meta.table('images', '<10i')
    typedefs = meta.table('typeDefinitions', '<16i8H2I')
    methods = meta.table('methods', '<7i4H')
    fields = meta.table('fields', '<3i')
    params = meta.table('parameters', '<3i')
    code_reg, modules, meta_reg = locate(img, meta, len(images), len(typedefs))
    types_count, types_ptr = img.u64(meta_reg + 48), img.u64(meta_reg + 56)
    field_offsets = img.u64(meta_reg + 88)
    type_sizes = img.u64(meta_reg + 104)
    print(f'CodeRegistration.codeGenModulesCount @ {code_reg:#x}, MetadataRegistration @ {meta_reg:#x}, types={types_count}', file=sys.stderr)

    mod_by_name = {}
    for m in modules:
        mod_by_name[img.cstr(img.u64(m))] = m

    def tdef_name(ti):
        t = typedefs[ti]
        name = meta.string(t[0])
        if t[3] >= 0:
            return tdef_name(img.u64(img.u64(types_ptr + 8 * t[3])) & 0xFFFFFFFF) + '.' + name
        ns = meta.string(t[1])
        return f'{ns}.{name}' if ns else name

    def type_name(tidx, depth=0):
        if tidx < 0 or tidx >= types_count or depth > 6:
            return '?'
        p = img.u64(types_ptr + 8 * tidx)
        return il2type_name(p, depth)

    def il2type_name(p, depth=0):
        data = img.u64(p)
        bits = struct.unpack_from('<I', img.img, p + 8)[0]
        t = (bits >> 16) & 0xFF
        if t in TYPE_NAMES:
            return TYPE_NAMES[t]
        if t in (0x11, 0x12):  # VALUETYPE, CLASS
            return tdef_name(data & 0xFFFFFFFF)
        if t == 0x1D:  # SZARRAY
            return il2type_name(data, depth + 1) + '[]'
        if t == 0x0F:
            return il2type_name(data, depth + 1) + '*'
        if t == 0x14:  # ARRAY: Il2CppArrayType* {etype, rank}
            return il2type_name(img.u64(data), depth + 1) + '[,]'
        if t == 0x15:  # GENERICINST: Il2CppGenericClass {typeDefinitionIndex/type*, context{class_inst, method_inst}}
            base = il2type_name(img.u64(data), depth + 1) if depth < 6 else '?'
            inst = img.u64(data + 8)
            args = []
            if inst:
                argc, argv = img.u64(inst), img.u64(inst + 8)
                for k in range(min(argc, 8)):
                    args.append(il2type_name(img.u64(argv + 8 * k), depth + 1) if depth < 6 else '?')
            return base.split('`')[0] + '<' + ', '.join(args) + '>'
        if t in (0x13, 0x1E):
            return f'T{data & 0xFFFF}'
        return f'type{t:#x}'

    out_types = []
    for ii, im in enumerate(images):
        iname = meta.string(im[0])
        mod = mod_by_name.get(iname)
        mp_count = img.u64(mod + 8) if mod else 0
        mp_arr = img.u64(mod + 16) if mod else 0
        for ti in range(im[2], im[2] + im[3]):
            t = typedefs[ti]
            (name_i, ns_i, byval, decl, parent, elem, gen_cont, flags, field_start, method_start, ev_start,
             prop_start, nested_start, iface_start, vt_start, io_start,
             method_count, prop_count, field_count, ev_count, nested_count, vt_count, if_count, io_count,
             bitfield, token) = t
            fo_ptr = img.u64(field_offsets + 8 * ti) if field_offsets else 0
            fl = []
            for k in range(field_count):
                fname_i, ftype_i, ftok = fields[field_start + k]
                ftp = img.u64(types_ptr + 8 * ftype_i)
                attrs = struct.unpack_from('<I', img.img, ftp + 8)[0] & 0xFFFF
                off = img.i32(fo_ptr + 4 * k) if fo_ptr else None
                fl.append({'name': meta.string(fname_i), 'type': type_name(ftype_i),
                           'offset': off, 'static': bool(attrs & 0x10), 'const': bool(attrs & 0x40)})
            ml = []
            for k in range(method_count):
                m = methods[method_start + k]
                (mname_i, mdecl, mret, _rpt, pstart, mgc, mtok, mflags, miflags, slot, pcount) = m
                rid = mtok & 0xFFFFFF
                rva = img.u64(mp_arr + 8 * (rid - 1)) if mp_arr and 0 < rid <= mp_count else 0
                ps = []
                for j in range(pcount):
                    pn, ptok, ptype = params[pstart + j]
                    ps.append(f'{type_name(ptype)} {meta.string(pn)}')
                ml.append({'name': meta.string(mname_i), 'ret': type_name(mret), 'params': ps,
                           'static': bool(mflags & 0x10), 'virtual': bool(mflags & 0x40), 'slot': slot,
                           'abstract': bool(mflags & 0x400), 'generic': mgc >= 0,
                           'rva': rva or None})
            sz_ptr = img.u64(type_sizes + 8 * ti)
            inst_size, _native, static_size, _ts = struct.unpack_from('<IiII', img.img, sz_ptr) if sz_ptr else (0, 0, 0, 0)
            out_types.append({'index': ti, 'image': iname, 'name': tdef_name(ti), 'instance_size': inst_size,
                              'static_size': static_size, 'generic': gen_cont >= 0, 'interface': bool(flags & 0x20),
                              'parent': type_name(parent) if parent >= 0 else None,
                              'valuetype': bool(bitfield & 1), 'fields': fl, 'methods': ml})
    json.dump({'code_registration': code_reg, 'metadata_registration': meta_reg, 'types': out_types},
              open(out, 'w'))
    if '--cs' in sys.argv:
        with open(sys.argv[sys.argv.index('--cs') + 1], 'w') as cs:
            for t in out_types:
                cs.write(f'// {t["image"]} idx={t["index"]}\n{"struct" if t["valuetype"] else "class"} {t["name"]}'
                         f'{" : " + t["parent"] if t["parent"] else ""}\n{{\n')
                for f in t['fields']:
                    off = f'0x{f["offset"]:X}' if isinstance(f['offset'], int) else '?'
                    cs.write(f'    {"static " if f["static"] else ""}{f["type"]} {f["name"]}; // {off}\n')
                for m in t['methods']:
                    rva = f'0x{m["rva"]:X}' if m['rva'] else '-'
                    cs.write(f'    {"static " if m["static"] else ""}{"virtual " if m["virtual"] else ""}'
                             f'{m["ret"]} {m["name"]}({", ".join(m["params"])}); // {rva}\n')
                cs.write('}\n\n')


if __name__ == '__main__':
    main()
