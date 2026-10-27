"""Type classification, C++ type mapping and struct layouts for translated code."""
import hashlib
import re

from .model import PRIM_KEYS, KEY_PRIMS, sig_str, subst
from .switchdb import dkey

# Types whose behaviour the runtime provides (never translated, never called natively).
EMULATED_PREFIXES = ('BepInEx.', 'HarmonyLib.', 'MonoMod.', 'Mono.Cecil.')
# Unity base classes a mod component can derive from; their members are routed through runtime shims.
UNITY_EMU_BASES = ('UnityEngine.Object', 'UnityEngine.Component', 'UnityEngine.Behaviour',
                   'UnityEngine.MonoBehaviour')
# Emulated base classes for mod-defined classes.
EMU_BASES = {
    'UnityEngine.MonoBehaviour': 'ModComponent',
    'UnityEngine.Behaviour': 'ModComponent',
    'UnityEngine.Component': 'ModComponent',
    'UnityEngine.ScriptableObject': 'ModObj',
    'UnityEngine.Object': 'ModObj',
    'BepInEx.BaseUnityPlugin': 'ModPlugin',
    'System.Object': 'ModObj',
    'System.Attribute': 'ModObj',
    'System.Exception': 'ModException',
    'System.ValueType': None,
    'System.Enum': None,
    'System.MulticastDelegate': 'Delegate',
}

CPRIM = {'void': 'void', 'bool': 'bool', 'char': 'char16_t', 'i1': 'int8_t', 'u1': 'uint8_t', 'i2': 'int16_t',
         'u2': 'uint16_t', 'i4': 'int32_t', 'u4': 'uint32_t', 'i8': 'int64_t', 'u8': 'uint64_t', 'r4': 'float',
         'r8': 'double', 'i': 'intptr_t', 'u': 'uintptr_t', 'string': 'obj', 'object': 'obj',
         'typedref': 'intptr_t'}
PRIM_SIZE = {'bool': 1, 'char': 2, 'i1': 1, 'u1': 1, 'i2': 2, 'u2': 2, 'i4': 4, 'u4': 4, 'i8': 8, 'u8': 8,
             'r4': 4, 'r8': 8, 'i': 8, 'u': 8, 'string': 8, 'object': 8}


def cid(s, maxlen=60):
    """Sanitize an arbitrary name into a C identifier (with a hash when shortened/ambiguous)."""
    clean = re.sub(r'[^A-Za-z0-9_]', '_', s)
    if len(clean) > maxlen or clean != s:
        h = hashlib.sha1(s.encode()).hexdigest()[:6]
        clean = clean[:maxlen] + '_' + h
    return clean


def norm(sig):
    """Map primitive-named class keys (System.Int32) to prim sigs."""
    if sig[0] in ('class', 'vt') and sig[1] in KEY_PRIMS:
        return ('prim', KEY_PRIMS[sig[1]])
    return sig


class TypeSystem:
    def __init__(self, universe, db, mod_asm_names):
        self.u = universe
        self.db = db
        self.mod_asms = set(mod_asm_names)
        self.structs = {}  # cname -> (definition lines, deps)
        self.struct_order = []
        self.layout_cache = {}

    # ---------------------------------------------------------- classification
    def td(self, sig):
        sig = norm(sig)
        if sig[0] in ('class', 'vt', 'gi'):
            return self.u.get(sig[1])
        if sig[0] == 'prim' and sig[1] in PRIM_KEYS:
            return self.u.get(PRIM_KEYS[sig[1]])
        return None

    def origin(self, key):
        """'mod' | 'emu' | 'game' | 'pconly' | 'unknown' for a type key."""
        if key.startswith(EMULATED_PREFIXES):
            return 'emu'
        td = self.u.get(key)
        if td is not None and td.asm.name in self.mod_asms:
            return 'mod'
        if self.db.has_type(key):
            return 'game'
        if td is not None:
            return 'pconly'
        return 'unknown'

    def is_vt(self, sig):
        sig = norm(sig)
        if sig[0] == 'prim':
            return sig[1] not in ('string', 'object', 'void')
        if sig[0] == 'vt':
            return True
        if sig[0] == 'gi':
            return sig[3]
        if sig[0] == 'class':
            td = self.u.get(sig[1])
            return td is not None and self.u.is_valuetype_def(td)
        return False

    def is_enum_sig(self, sig):
        td = self.td(sig)
        return td is not None and self.u.is_enum(td)

    def is_ref(self, sig):
        sig = norm(sig)
        if sig[0] == 'prim':
            return sig[1] in ('string', 'object')
        if sig[0] in ('sz', 'md'):
            return True
        if sig[0] in ('class', 'gi'):
            return not self.is_vt(sig)
        return False

    def is_mod_ref_type(self, sig):
        sig = norm(sig)
        return sig[0] in ('class', 'gi') and self.origin(sig[1]) in ('mod', 'pconly') and not self.is_vt(sig)

    # ---------------------------------------------------------- C++ types
    def cty(self, sig):
        sig = norm(sig)
        k = sig[0]
        if k == 'prim':
            return CPRIM[sig[1]]
        if k in ('sz', 'md'):
            return 'obj'
        if k in ('byref', 'ptr'):
            inner = sig[1]
            if inner[0] == 'prim' and inner[1] == 'void':
                return 'void*'
            return self.cty(inner) + '*'
        if k in ('var', 'mvar'):
            raise ValueError(f'unsubstituted generic parameter {sig}')
        if not self.is_vt(sig):
            return 'obj'
        if self.is_enum_sig(sig):
            return self.cty(self.u.enum_underlying(self.td(sig)))
        if self.unknown_vt(sig):
            return 'int32_t'
        return self.struct_for(sig)

    def unknown_vt(self, sig):
        """A value type with no definition anywhere (stripped from the Switch build): almost always an enum."""
        return sig[0] == 'vt' and self.u.get(sig[1]) is None  # never for generic structs

    # ---------------------------------------------------------- value type layouts
    def struct_for(self, sig):
        """Return the C++ struct name for a value type, generating it if needed."""
        sig = norm(sig)
        name = 'V_' + cid(sig_str(sig))
        if name in self.structs:
            return name
        self.structs[name] = None  # break recursion
        key = sig[1]
        targs = sig[2] if sig[0] == 'gi' else ()
        origin = self.origin(key)
        lines, size = [], 0
        dump_fields = self.db.instance_fields(key) if origin == 'game' and not targs else None
        td = self.u.get(key)
        if dump_fields:
            # Authoritative Switch layout.
            size = (self.db.size(key) or 0x10) - 0x10
            pc_fields = {f.name: f for f in td.fields} if td else {}
            members = []
            for f in dump_fields:
                pf = pc_fields.get(f['name'])
                fsig = pf.sig if pf else None
                members.append((f['offset'] - 0x10, f['name'], fsig, f['type']))
            members.sort(key=lambda m: m[0])
            lines, pos = self._emit_members(members, size)
        elif td is not None:
            members, size = self.computed_layout(td, targs)
            lines, pos = self._emit_members(members, size)
        else:
            raise ValueError(f'unknown value type {sig_str(sig)}')
        body = [f'struct {name} {{  // {sig_str(sig)} ({size} bytes)'] + lines + ['};',
                                                                                f'static_assert(sizeof({name}) == {max(size, 1)}, "{name} size");']
        self.structs[name] = body
        self.struct_order.append(name)
        return name

    def _emit_members(self, members, size):
        lines, pos, pad = [], 0, 0
        for off, fname, fsig, dtype in members:
            if off < pos:
                continue  # overlapping (explicit layout) – keep first
            if off > pos:
                lines.append(f'    uint8_t _pad{pad}[{off - pos}];')
                pad += 1
                pos = off
            if fsig is None:
                ct, fsize = self._dump_type_c(dtype)
            else:
                ct = self.cty(fsig)
                fsize = self.sizeof(fsig)
            lines.append(f'    {ct} {cid(fname)};')
            pos += fsize
        if size == 0 and pos == 0:
            lines.append('    uint8_t _empty;')
            pos = 1
        if pos < size:
            lines.append(f'    uint8_t _pad{pad}[{size - pos}];')
        return lines, pos

    def _dump_type_c(self, dtype):
        prim = {'bool': ('bool', 1), 'char': ('char16_t', 2), 'sbyte': ('int8_t', 1), 'byte': ('uint8_t', 1),
                'short': ('int16_t', 2), 'ushort': ('uint16_t', 2), 'int': ('int32_t', 4), 'uint': ('uint32_t', 4),
                'long': ('int64_t', 8), 'ulong': ('uint64_t', 8), 'float': ('float', 4), 'double': ('double', 8),
                'IntPtr': ('intptr_t', 8), 'UIntPtr': ('uintptr_t', 8)}
        if dtype in prim:
            return prim[dtype]
        t = self.db.types.get(dtype)
        if t and t['parent'] in ('System.ValueType',):
            sz = t['instance_size'] - 0x10
            return f'uint8_t /*{dtype}*/', sz  # treated as bytes
        return 'obj', 8

    def alignof(self, sig):
        sig = norm(sig)
        if self.is_vt(sig) and self.unknown_vt(sig):
            return 4
        if sig[0] == 'prim':
            return min(PRIM_SIZE.get(sig[1], 8), 8)
        if self.is_vt(sig) and not self.is_enum_sig(sig):
            members, size = self.layout(sig)
            return max([self.alignof(m[2]) for m in members if m[2] is not None] or [1])
        if self.is_enum_sig(sig):
            return self.alignof(self.u.enum_underlying(self.td(sig)))
        return 8

    def sizeof(self, sig):
        sig = norm(sig)
        if self.is_vt(sig) and self.unknown_vt(sig):
            return 4
        if sig[0] == 'prim':
            return PRIM_SIZE[sig[1]]
        if sig[0] in ('byref', 'ptr', 'sz', 'md'):
            return 8
        if not self.is_vt(sig):
            return 8
        if self.is_enum_sig(sig):
            return self.sizeof(self.u.enum_underlying(self.td(sig)))
        key = sig[1]
        if self.origin(key) == 'game' and sig[0] != 'gi' and self.db.size(key):
            return self.db.size(key) - 0x10
        return self.layout(sig)[1]

    def layout(self, sig):
        sig = norm(sig)
        td = self.td(sig)
        return self.computed_layout(td, sig[2] if sig[0] == 'gi' else ())

    def computed_layout(self, td, targs):
        """Sequential layout with natural alignment (what IL2CPP/Mono use for auto/sequential structs)."""
        ck = (td.key, targs)
        if ck in self.layout_cache:
            return self.layout_cache[ck]
        members, pos, maxal = [], 0, 1
        for f in td.fields:
            if f.static or f.literal:
                continue
            fs = subst(f.sig, targs)
            if td.explicit_layout and f.idx in td.asm.field_offsets:
                off = td.asm.field_offsets[f.idx]
            else:
                al = self.alignof(fs)
                if td.packing:
                    al = min(al, td.packing)
                pos = (pos + al - 1) // al * al
                off = pos
                maxal = max(maxal, al)
            members.append((off, f.name, fs, None))
            pos = max(pos, off + self.sizeof(fs))
        size = (pos + maxal - 1) // maxal * maxal
        if td.class_size:
            size = max(size, td.class_size)
        self.layout_cache[ck] = (members, size)
        return members, size

    def struct_defs(self):
        out = []
        for name in self.struct_order:
            out += self.structs[name]
        return out
