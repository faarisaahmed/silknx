"""Whole-program translation: drives MethodTranslator, resolves calls/fields, emits the C++ translation unit."""
import hashlib
import re

from .model import MethodDef, MemberRef, FieldDef, sig_str, subst, PRIM_KEYS
from .translate import MethodTranslator, Unsupported, cstr, SV, OBJ, I4, I8, NI, R4, R8
from .types import TypeSystem, cid, norm, EMU_BASES, UNITY_EMU_BASES
from .switchdb import dkey, dump_type_name

DELEGATE_BASES = ('System.MulticastDelegate', 'System.Delegate')
G_KVP = 'System.Collections.Generic.KeyValuePair`2'
# Generic APIs where a mod-defined reference type argument can safely be treated as System.Object.
ERASABLE = ('System.Linq.', 'System.Collections.', 'System.Array', 'System.Tuple', 'System.ValueTuple',
            'System.Func', 'System.Action', 'System.Predicate', 'System.Comparison', 'System.Lazy')
# APIs stripped from the Switch build that only matter for developer tools (mouse GUIs, file watching,
# diagnostics). Calls become harmless defaults instead of conversion errors.
STUBBABLE = ('UnityEngine.GUILayout', 'UnityEngine.GUILayoutUtility', 'UnityEngine.GUIStyle', 'UnityEngine.GUI',
             'UnityEngine.GUIContent', 'UnityEngine.TextEditor', 'UnityEngine.GUIUtility', 'UnityEngine.Cursor',
             'System.IO.FileSystemWatcher', 'System.IO.FileSystemEventArgs', 'System.IO.RenamedEventArgs',
             'System.IO.ErrorEventArgs', 'System.IO.FileSystemEventHandler', 'System.IO.RenamedEventHandler',
             'System.IO.ErrorEventHandler', 'System.GC', 'System.Diagnostics.Process', 'System.Diagnostics.Debugger',
             'UnityEngine.TextCore.LowLevel.FontEngine', 'System.Console', 'System.Diagnostics.Stopwatch',
             'UnityEngine.AssetBundle', 'UnityEngine.MaterialPropertyBlock', 'UnityEngine.Video.',
             'UnityEngine.Networking.', 'System.Reflection.AssemblyMetadataAttribute', 'UnityEngine.GL',
             'UnityEngine.Vector4')


class Report:
    def __init__(self):
        self.items = {}  # mod asm -> list of (severity, text)
        self.cur_asm = None

    def add(self, sev, text, asm=None):
        lst = self.items.setdefault(asm or self.cur_asm or '?', [])
        if (sev, text) not in lst:
            lst.append((sev, text))

    def unsupported(self, md, text):
        self.add('error', f'{md}: {text}', md.asm.name if md.asm.name in self.items or True else None)

    def missing(self, what, where):
        self.add('error', f'missing on Switch: {what} (used by {where})')

    def warn(self, text):
        self.add('warn', text)


class Translator:
    def __init__(self, universe, db, mod_asms, shims):
        self.u = universe
        self.db = db
        self.mod_asms = mod_asms
        self.ts = TypeSystem(universe, db, [a.name for a in mod_asms])
        self.report = Report()
        self.shims = shims  # set of shim function names implemented by the runtime
        self.calls = CallEmitter(self)
        from .collections_emu import CollectionEmu
        self.coll = CollectionEmu(self)
        self.funcs = {}  # cname -> (head, lines)
        self.fn_names = {}  # (md id, targs, margs) -> cname
        self.queue = []
        self.literals = {}
        self.typerefs = {}  # key -> (index, sig)
        self.fin_targets = {}
        self.mod_types = {}  # (key, targs) -> ModTypeInfo
        self.method_ids = {}
        self.sites = []  # reflect/virtual site descriptors
        self.field_blobs = {}
        self.thunks = {}
        self.extra = []  # extra generated code (thunks etc.)
        self.shim_protos = {}  # shim name -> prototype

    # ---------------------------------------------------------- functions
    def fn(self, md, targs=(), margs=()):
        if getattr(md.asm, 'kind', None) == 'il2cpp':
            md = self.u.pc_method(md) or md  # translate from the PC copy's IL when available
        key = (id(md), tuple(targs), tuple(margs))
        name = self.fn_names.get(key)
        if name is None:
            base = f'{md.owner.key}::{md.name}'
            sig = f'{base}({",".join(sig_str(p) for p in md.params)})<{",".join(sig_str(t) for t in targs)}|' \
                  f'{",".join(sig_str(t) for t in margs)}>'
            h = hashlib.sha1(sig.encode()).hexdigest()[:8]
            name = f'F_{cid(md.owner.key.split(".")[-1], 30)}_{cid(md.name, 30)}_{h}'
            self.fn_names[key] = name
            self.queue.append((md, tuple(targs), tuple(margs), name))
        return name

    def run(self):
        while self.queue:
            md, targs, margs, name = self.queue.pop()
            if name in self.funcs:
                continue
            self.report.cur_asm = md.asm.name if md.asm in self.mod_asms else self.report.cur_asm
            if md.body() is None:
                self.funcs[name] = self.stub(md, targs, margs, name, 'needs the PC copy')
                self.report.add('error', self.needs_pc(str(md)))
                continue
            try:
                mt = MethodTranslator(self, md, targs, margs, name)
                head, lines = mt.translate()
            except Unsupported as e:
                self.report.add('error', f'{md}: {e}')
                head, lines = self.stub(md, targs, margs, name, str(e))
            except Exception as e:
                self.report.add('error', f'{md}: internal translator error: {type(e).__name__}: {e}')
                head, lines = self.stub(md, targs, margs, name, f'translator error: {e}')
            self.funcs[name] = (head, lines)

    def stub(self, md, targs, margs, name, why):
        ps = []
        n = len(md.params) + (1 if md.has_this else 0)
        mt = MethodTranslator.__new__(MethodTranslator)
        mt.tr, mt.md, mt.targs, mt.margs, mt.ts = self, md, targs, margs, self.ts
        for i in range(n):
            try:
                ps.append(f'{self.ts.cty(mt.arg_sig(i))} a{i}')
            except Exception:
                ps.append(f'intptr_t a{i}')
        ps.append('const void* mi')
        try:
            ret = self.ts.cty(norm(subst(md.ret, targs, margs)))
        except Exception:
            ret = 'void'
        head = f'{ret} {name}({", ".join(ps)})'
        body = [head + ' {', f'    rt_unsupported("{cstr(str(md))}: {cstr(why)}");']
        if ret != 'void':
            body.append(f'    return {ret}{{}};')
        return head, body + ['}']

    def stubbable(self, key):
        return key.startswith(STUBBABLE)

    def needs_pc(self, what):
        if self.u.has_pc:
            return f'{what} does not exist in either the Switch or the PC version of the game'
        return (f'{what} was removed from the Switch version of the game; converting this needs a PC copy '
                f'(run: python3 silknx.py setup --pc "/path/to/Hollow Knight Silksong")')

    def owner_sig(self, td, targs):
        vt = self.u.is_valuetype_def(td)
        if td.gparams:
            return ('gi', td.key, tuple(targs), vt)
        return ('vt' if vt else 'class', td.key)

    # ---------------------------------------------------------- literals & type refs
    def literal(self, s):
        if s not in self.literals:
            self.literals[s] = len(self.literals)
        return f'RT_LIT({self.literals[s]})'

    def type_ref(self, sig):
        """Index of a runtime type reference (System.Type for game types, descriptor for mod types)."""
        sig = norm(sig)
        k = sig_str(sig)
        if k not in self.typerefs:
            self.typerefs[k] = (len(self.typerefs), sig)
        return self.typerefs[k][0]

    def aqn(self, sig):
        """Assembly-qualified name usable with Type.GetType on Switch (None if not a game type)."""
        sig = norm(sig)
        k = sig[0]
        if k == 'prim':
            key = PRIM_KEYS[sig[1]]
            return f'{key}, mscorlib'
        if k in ('class', 'vt'):
            if self.ts.origin(sig[1]) != 'game':
                return None
            t = self.db.type(sig[1])
            asm = t['image'][:-4] if t['image'].endswith('.dll') else t['image']
            return f'{sig[1].replace("/", "+")}, {asm}'
        if k == 'gi':
            base = self.aqn(('class', sig[1]))
            if base is None:
                return None
            args = []
            for a in sig[2]:
                if self.ts.is_mod_ref_type(a) or (a[0] in ('class', 'gi') and self.ts.origin(a[1]) in ('emu', 'unknown')
                                                  and not self.ts.is_vt(a)):
                    a = ('prim', 'object')  # mod / emulated / stripped reference types share object's code
                aa = self.aqn(a)
                if aa is None:
                    return None
                args.append(f'[{aa}]')
            name, asm = base.split(', ', 1)
            return f'{name}[{",".join(args)}], {asm}'
        if k == 'sz':
            inner = sig[1]
            if self.ts.is_mod_ref_type(inner):
                inner = ('prim', 'object')
            a = self.aqn(inner)
            if a is None:
                return None
            name, asm = a.rsplit(', ', 1) if '[' not in a else (a[:a.rindex('], ') + 1] if a.endswith(']') else a.rsplit(', ', 1)[0], a.rsplit(', ', 1)[1])
            return f'{name}[], {asm}'
        return None

    def array_elem_type_ref(self, t):
        t = norm(t)
        if self.ts.is_mod_ref_type(t) or (t[0] in ('class', 'gi') and self.ts.origin(t[1]) == 'emu'):
            t = ('prim', 'object')
        if self.ts.is_vt(t) and not self.ts.is_enum_sig(t) and self.aqn(t) is None:
            # mod value type: store raw bytes in an object[] (conservatively scanned)
            return f'RT_TYPE_RAW({self.ts.sizeof(t)})'
        return self.type_ref(t)

    def field_data(self, f):
        """Static field with RVA initial data (array initializers)."""
        if f.rva is None:
            raise Unsupported(f'ldtoken field without data {f}')
        key = (f.asm.name, f.idx)
        if key not in self.field_blobs:
            size = self.ts.sizeof(f.sig) if f.sig[0] != 'vt' or self.u.get(f.sig[1]) is None else \
                (self.u.get(f.sig[1]).class_size or self.ts.sizeof(f.sig))
            data = f.asm.d[f.asm.off(f.rva):f.asm.off(f.rva) + size]
            self.field_blobs[key] = (len(self.field_blobs), data)
        return f'&FB_{self.field_blobs[key][0]}'

    # ---------------------------------------------------------- mod types
    def mod_type(self, sig):
        """Layout/descriptor info for a mod (or PC-only) reference/value type instantiation."""
        sig = norm(sig)
        key = sig[1]
        targs = sig[2] if sig[0] == 'gi' else ()
        mk = (key, targs)
        if mk in self.mod_types:
            return self.mod_types[mk]
        td = self.u.get(key)
        if td is None or self.ts.origin(key) not in ('mod', 'pconly'):
            raise Unsupported(f'{sig_str(sig)} is not available on Switch')
        info = ModTypeInfo(self, td, targs)
        for other in self.mod_types.values():  # same C name = same type spelled differently
            if other.cname == info.cname:
                self.mod_types[mk] = other
                return other
        self.mod_types[mk] = info
        info.build()
        return info

    def is_mod_obj_sig(self, sig):
        sig = norm(sig)
        return sig[0] in ('class', 'gi') and self.ts.origin(sig[1]) in ('mod', 'pconly') and not self.ts.is_vt(sig)

    def method_id(self, key):
        if key not in self.method_ids:
            self.method_ids[key] = len(self.method_ids) + 1
        return self.method_ids[key]

    # ---------------------------------------------------------- fields
    def field_access(self, f, mt):
        """Returns (static, field_sig, lvalue_fn(obj_or_None) -> C lvalue string)."""
        if isinstance(f, FieldDef):
            owner_sig = self.owner_sig(f.owner, ())
            fsig, name, static = f.sig, f.name, f.static
            fdef = f
        else:
            owner_sig = mt.s(f.parent)
            fsig, name = f.type, f.name
            td = self.u.get(owner_sig[1] if owner_sig[0] != 'prim' else PRIM_KEYS[owner_sig[1]])
            fdef = td.field(name) if td else None
            static = fdef.static if fdef else False
        owner_sig = mt.s(owner_sig)
        if owner_sig[0] == 'prim' and owner_sig[1] in PRIM_KEYS:
            owner_sig = ('class', PRIM_KEYS[owner_sig[1]])
        targs = owner_sig[2] if owner_sig[0] == 'gi' else ()
        fsig = norm(subst(fsig, targs))
        key = owner_sig[1]
        origin = self.ts.origin(key)
        if fdef is not None and fdef.literal:
            raise Unsupported(f'access to literal field {key}::{name}')
        if origin in ('mod', 'pconly'):
            info = self.mod_type(owner_sig)
            fname = cid(name)
            if static:
                return True, fsig, lambda o: f'RT_MSTATICS({info.cname})->{fname}'
            if info.is_vt:
                def lv(o):
                    v, sv = o
                    if sv.ct.endswith('*'):
                        return f'(({info.struct}*){v})->{fname}'
                    return f'{v}.{fname}'
                return False, fsig, lv
            return False, fsig, lambda o: f'((T_{info.cname}*)rt_mc({o[0]}))->{fname}'
        if origin == 'game':
            df = self.db.field(key, name)
            if df is None:
                raise Unsupported(f'field {key}::{name} does not exist on Switch')
            ct = self.ts.cty(fsig)
            off = df['offset']
            if static:
                tr = self.type_ref(owner_sig)
                return True, fsig, lambda o: f'(*({ct}*)((uint8_t*)rt_game_statics({tr}) + {off:#x}))'
            if self.ts.is_vt(owner_sig):
                s = self.ts.cty(owner_sig)

                def lv(o):
                    v, sv = o
                    if sv.ct.endswith('*') or sv.ct == 'intptr_t':
                        return f'(*({ct}*)((uint8_t*){v} + {off - 0x10:#x}))'
                    if sv.ct == OBJ:  # boxed
                        return f'(*({ct}*)((uint8_t*)rt_nc({v}) + {off:#x}))'
                    return f'(*({ct}*)((uint8_t*)&{v} + {off - 0x10:#x}))'
                return False, fsig, lv
            return False, fsig, lambda o: f'(*({ct}*)((uint8_t*)rt_nc({o[0]}) + {off:#x}))'
        if origin == 'emu':
            raise Unsupported(f'direct field access on emulated type {key}::{name}')
        raise Unsupported(f'field on unknown type {key}::{name}')

    # ---------------------------------------------------------- boxing / casting
    def box_expr(self, v, sv, t, mt):
        t = norm(t)
        ts = self.ts
        if ts.is_vt(t) and ts.unknown_vt(t):
            t = ('prim', 'i4')
        if not ts.is_vt(t):
            return v
        if t[0] == 'gi' and t[1] == 'System.Nullable`1':
            inner = t[2][0]
            ct = ts.cty(t)
            return f'({v}.hasValue ? {self.box_expr(f"{v}.value", SV(ts.cty(inner), inner), inner, mt)} : nullptr)'
        ct = ts.cty(t)
        val = mt.conv_to(v, sv, t)
        if self.aqn(t) is None and not ts.is_enum_sig(t):
            info = self.mod_type(t)
            return f'rt_box_mod(&D_{info.cname}, ({ct}[]){{{val}}}, sizeof({ct}))'
        return f'rt_box({self.type_ref(t)}, ({ct}[]){{{val}}}, sizeof({ct}))'

    def unbox_ptr(self, v, t, mt):
        t = norm(t)
        ct = self.ts.cty(t)
        if t[0] == 'gi' and t[1] == 'System.Nullable`1':
            raise Unsupported('unbox to Nullable')
        if self.aqn(t) is None and not self.ts.is_enum_sig(t):
            return f'(({ct}*)rt_unbox_mod({v}))'
        return f'(({ct}*)rt_unbox({v}))'

    def cast_expr(self, v, t, mt, throw):
        cond = self.type_check_expr(t, v, mt)
        fn = 'rt_castclass' if throw else 'rt_isinst'
        return f'{fn}({v}, {cond})'

    def type_check_expr(self, t, v, mt):
        """C boolean expression: is object `v` (non-null) an instance of t?"""
        t = norm(t)
        k = t[0]
        if k == 'prim' and t[1] == 'object':
            return 'true'
        if k in ('class', 'gi'):
            origin = self.ts.origin(t[1])
            if origin in ('mod', 'pconly'):
                info = self.mod_type(t)
                if info.td.is_interface:
                    return f'rt_is_mod_iface({v}, {self.iface_id(t)})'
                return f'rt_is_mod_type({v}, &D_{info.cname})'
            if origin == 'emu':
                return f'rt_is_emu({v}, "{t[1]}")'
            td = self.u.get(t[1])
            game = f'rt_is_game({v}, {self.type_ref(t)})'
            if td is not None and td.is_interface:
                return f'({game} || rt_is_mod_iface({v}, {self.iface_id(t)}))'
            if t[1] in UNITY_EMU_BASES or t[1] == 'System.Exception':
                return f'({game} || rt_mod_is({v}, {self.type_ref(t)}))'
            return game
        return f'rt_is_game({v}, {self.type_ref(t)})'

    def iface_id(self, t):
        return self.method_id(('iface', sig_str(norm(t))))

    # ---------------------------------------------------------- output
    def emit(self):
        ts = self.ts
        out = ['// Generated by silkport. Do not edit.', '#include "rt.h"', '#include "rt_collections.h"', '']
        # mod type structs need value-type structs first
        mod_struct_lines = []
        for info in list(self.mod_types.values()):
            info.finalize()
        for info in self.ordered_mod_types():
            mod_struct_lines += info.struct_lines()
        out += ts.struct_defs()
        out += mod_struct_lines
        out += ['']
        # runtime shims used by this translation
        out += sorted(self.shim_protos.values())
        # forward declarations
        for name, (head, lines) in self.funcs.items():
            if head:
                out.append(f'{head};')
        out += ['']
        # descriptors
        for info in self.ordered_mod_types():
            out += info.desc_lines()
        # literals
        out.append(f'extern "C" const rt_literal silk_literals[] = {{')
        for s, i in sorted(self.literals.items(), key=lambda x: x[1]):
            out.append(f'    {{u"{cstr16(s)}", {len(s.encode("utf-16-le")) // 2}}},')
        out.append('    {nullptr, 0}};')
        out.append(f'extern "C" const uint32_t silk_literal_count = {len(self.literals)};')
        out.append('extern "C" const rt_typeref silk_typerefs[] = {')
        for k, (i, sig) in sorted(self.typerefs.items(), key=lambda x: x[1][0]):
            a = self.aqn(sig)
            if a is not None:
                out.append(f'    {{"{cstr(a)}", nullptr, 0, "{cstr(k)}"}},  // {i}')
            elif self.is_mod_obj_sig(sig) or (sig[0] in ('vt', 'class', 'gi') and self.ts.origin(sig[1]) in ('mod', 'pconly')):
                try:
                    info = self.mod_type(sig)
                    out.append(f'    {{nullptr, &D_{info.cname}, 0, "{cstr(k)}"}},  // {i}')
                except Unsupported:
                    out.append(f'    {{nullptr, nullptr, 0, "{cstr(k)} (not on Switch)"}},  // {i}')
            else:
                out.append(f'    {{nullptr, nullptr, 0, "{cstr(k)} (not available on Switch)"}},  // {i}')
        out.append('    {nullptr, nullptr, 0, nullptr}};')
        out.append(f'extern "C" const uint32_t silk_typeref_count = {len(self.typerefs)};')
        for key, (i, data) in self.field_blobs.items():
            out.append(f'alignas(8) static const uint8_t FB_{i}_data[] = {{{", ".join(str(b) for b in data) or "0"}}};')
            out.append(f'static const rt_blob FB_{i} = {{FB_{i}_data, {len(data)}}};')
        out += self.calls.site_lines()
        out += self.extra
        for name, (head, lines) in self.funcs.items():
            if lines:
                out += lines
                out.append('')
        return '\n'.join(out) + '\n'

    def touch_typerefs(self):
        """Make sure every mod type referenced by typeof/generic args has a descriptor."""
        for k, (i, sig) in list(self.typerefs.items()):
            if self.aqn(sig) is None and sig[0] in ('vt', 'class', 'gi') and \
                    self.ts.origin(sig[1]) in ('mod', 'pconly'):
                self.mod_type(sig)

    def ordered_mod_types(self):
        done, order = set(), []

        def visit(info):
            if id(info) in done:
                return
            done.add(id(info))
            if info.base_info:
                visit(info.base_info)
            for dep in info.deps:
                visit(dep)
            order.append(info)
        for info in list(self.mod_types.values()):
            visit(info)
        return order


ENUM_IFACES = {'System.Collections.IEnumerable', 'System.Collections.ICollection', 'System.Collections.IList',
               'System.Collections.Generic.IEnumerable`1', 'System.Collections.Generic.ICollection`1',
               'System.Collections.Generic.IList`1', 'System.Collections.Generic.IReadOnlyCollection`1',
               'System.Collections.Generic.IReadOnlyList`1', 'System.Collections.Generic.ISet`1'}


def plain(s):
    return re.sub(r'[^A-Za-z0-9]', '_', s)


def cstr16(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif 32 <= o < 127:
            out.append(ch)
        elif o <= 0xFFFF:
            out.append(f'\\u{o:04x}')
        else:
            out.append(f'\\U{o:08x}')
    return ''.join(out)


# ======================================================================== mod types

class ModTypeInfo:
    def __init__(self, tr, td, targs):
        self.tr, self.td, self.targs = tr, td, tuple(targs)
        self.key = td.key
        self.sig = tr.owner_sig(td, targs)
        self.cname = cid(sig_str(self.sig), 50)
        self.is_vt = tr.u.is_valuetype_def(td)
        self.is_iface = td.is_interface
        self.base_info = None
        self.emu_base = None
        self.deps = []
        self.vmap = {}  # method id -> fn name
        self.messages = {}
        self.finalized = False

    @property
    def struct(self):
        return self.tr.ts.cty(self.sig) if self.is_vt else f'T_{self.cname}'

    def build(self):
        tr, td = self.tr, self.td
        if self.is_vt or self.is_iface:
            return
        base = td.extends
        if base is not None:
            bsig = subst(base, self.targs)
            if bsig[0] == 'prim':
                bsig = ('class', PRIM_KEYS[bsig[1]])
            origin = tr.ts.origin(bsig[1])
            if origin in ('mod', 'pconly'):
                self.base_info = tr.mod_type(bsig)
                self.emu_base = self.base_info.emu_base
            elif bsig[1] in EMU_BASES:
                self.emu_base = EMU_BASES[bsig[1]] or 'ModObj'
            elif bsig[1] in DELEGATE_BASES:
                self.emu_base = 'ModDelegate'
            else:
                tr.report.add('error', f'{td.key} derives from game type {bsig[1]}, which is not supported yet')
                self.emu_base = 'ModObj'
        else:
            self.emu_base = 'ModObj'

    def own_fields(self):
        out = []
        for f in self.td.fields:
            if f.literal:
                continue
            out.append((f, norm(subst(f.sig, self.targs))))
        return out

    def finalize(self):
        if self.finalized or self.is_iface:
            return
        self.finalized = True
        tr = self.tr
        # make sure all field types are generated before us
        for f, fsig in self.own_fields():
            if tr.ts.is_vt(fsig) and not tr.ts.is_enum_sig(fsig):
                tr.ts.cty(fsig)
        cctor = self.td.method('.cctor')
        if cctor is not None and cctor.body():
            tr.fn(cctor, self.targs)
        if self.is_vt:
            return
        ctor = self.td.method('.ctor', 0)
        if ctor is not None and ctor.body():
            tr.fn(ctor, self.targs)
        # virtual map: inherited + own
        if self.base_info:
            self.base_info.finalize()
            self.vmap.update(self.base_info.vmap)
            self.messages.update(self.base_info.messages)
        for md in self.td.methods:
            if md.static or md.body() is None:
                if md.virtual and not md.static and md.abstract:
                    continue
                if md.static or md.body() is None:
                    continue
            if md.gparam_count:
                continue
            fn = None
            if md.virtual:
                for slot_key in self.slot_keys(md):
                    fn = fn or tr.fn(md, self.targs)
                    self.vmap[tr.method_id(slot_key)] = fn
            if md.name in UNITY_MESSAGES and not md.params and self.emu_base in ('ModComponent', 'ModPlugin'):
                self.messages[md.name] = tr.fn(md, self.targs)
            elif md.name in UNITY_MESSAGES_ARGS and self.emu_base in ('ModComponent', 'ModPlugin'):
                pass

    def slot_keys(self, md):
        """Method-identity keys this virtual method fills (its own slot, overridden base slots, interfaces)."""
        tr = self.tr
        keys = []
        sig = method_key_sig(md, self.targs)
        if not md.newslot:
            # override: find the base declaration
            b = self.td
            while b is not None:
                bsig = b.extends
                if bsig is None:
                    break
                bk = norm(subst(bsig, self.targs))[1]
                b = tr.u.get(bk)
                if b is None:
                    break
                for bm in b.methods:
                    if bm.name == md.name and bm.virtual and len(bm.params) == len(md.params):
                        keys.append(('virt', b.key, md.name, sig))
                        break
        keys.append(('virt', self.td.key, md.name, sig))
        # interfaces (implicit by name/sig, explicit via MethodImpl)
        for iface in self.all_interfaces():
            itd = tr.u.get(iface[1])
            if itd is None:
                continue
            for im in itd.methods:
                if (im.name == md.name or md.name.endswith('.' + im.name)) and len(im.params) == len(md.params):
                    keys.append(('virt', iface[1], im.name, method_key_sig(im, iface[2] if iface[0] == 'gi' else ())))
        return keys

    def all_interfaces(self):
        out = []
        td = self.td
        targs = self.targs
        while td is not None:
            for i in td.interfaces:
                out.append(norm(subst(i, targs)))
            if td.extends is None:
                break
            b = norm(subst(td.extends, targs))
            td = self.tr.u.get(b[1])
            targs = b[2] if b[0] == 'gi' else ()
        # interfaces of interfaces
        k = 0
        while k < len(out):
            itd = self.tr.u.get(out[k][1])
            if itd:
                for i in itd.interfaces:
                    s = norm(subst(i, out[k][2] if out[k][0] == 'gi' else ()))
                    if s not in out:
                        out.append(s)
            k += 1
        return out

    def struct_lines(self):
        tr = self.tr
        if self.is_iface:
            return []
        if self.is_vt:
            # generated through TypeSystem.struct_for (computed layout)
            tr.ts.cty(self.sig)
            return []
        base = f'T_{self.base_info.cname}' if self.base_info else self.emu_base
        lines = [f'struct T_{self.cname} : {base} {{  // {sig_str(self.sig)}']
        for f, fsig in self.own_fields():
            if f.static:
                continue
            lines.append(f'    {tr.ts.cty(fsig)} {cid(f.name)};')
        lines.append('};')
        statics = [(f, fsig) for f, fsig in self.own_fields() if f.static]
        lines.append(f'struct S_{self.cname} {{')
        for f, fsig in statics:
            lines.append(f'    {tr.ts.cty(fsig)} {cid(f.name)};')
        if not statics:
            lines.append('    uint8_t _unused;')
        lines.append('};')
        return lines

    def desc_lines(self):
        tr = self.tr
        if self.is_iface:
            return []
        c = self.cname
        out = []
        cctor = self.td.method('.cctor')
        cctor_fn = tr.fn(cctor, self.targs) if cctor and cctor.body() else None
        if self.is_vt:
            out.append(f'struct S_{c} {{ uint8_t _unused; }};')
        vm = sorted(self.vmap.items())
        out.append(f'static const rt_vmap_entry VM_{c}[] = {{')
        for mid, fn in vm:
            out.append(f'    {{{mid}, (void*)&{fn}}},')
        out.append('    {0, nullptr}};')
        ifaces = sorted({tr.iface_id(i) for i in (self.all_interfaces() if not self.is_vt else [])})
        out.append(f'static const uint32_t IF_{c}[] = {{{", ".join(map(str, ifaces + [0]))}}};')
        msgs = self.messages
        mlist = ', '.join(f'(void*)&{msgs[m]}' if m in msgs else 'nullptr' for m in UNITY_MESSAGES)
        size = f'sizeof(T_{c})' if not self.is_vt else f'sizeof({self.struct})'
        base = f'&D_{self.base_info.cname}' if self.base_info else 'nullptr'
        emu = {'ModObj': 0, 'ModComponent': 1, 'ModPlugin': 2, 'ModException': 3, 'ModDelegate': 4}.get(
            self.emu_base or 'ModObj', 0)
        ctor = self.td.method('.ctor', 0)
        ctor_fn = tr.fn(ctor, self.targs) if ctor is not None and ctor.body() and not self.is_vt else None
        out.append(f'extern "C" rt_modtype D_{c} = {{"{cstr(sig_str(self.sig))}", {base}, (uint32_t){size}, '
                   f'{emu}, {"true" if self.is_vt else "false"}, '
                   f'{"(void*)&" + cctor_fn if cctor_fn else "nullptr"}, sizeof(S_{c}), nullptr, false, '
                   f'VM_{c}, {len(vm)}, IF_{c}, {{{mlist}}}, {"(void*)&" + ctor_fn if ctor_fn else "nullptr"}, '
                   f'{tr.asm_index(self.td.asm)}}};')
        return out


UNITY_MESSAGES = ('Awake', 'Start', 'OnEnable', 'OnDisable', 'OnDestroy', 'Update', 'LateUpdate', 'FixedUpdate',
                  'OnGUI', 'OnApplicationQuit')
UNITY_MESSAGES_ARGS = ('OnApplicationFocus', 'OnApplicationPause')


def method_key_sig(md, targs=()):
    return '(' + ','.join(sig_str(norm(subst(p, targs))) for p in md.params) + ')'


# ======================================================================== calls

class CallEmitter:
    def __init__(self, tr):
        self.tr = tr
        self.site_defs = []

    def site_lines(self):
        return self.site_defs

    # ---------------------------------------------------------- resolution
    def resolve(self, target, mt):
        """Return a dict describing the call target with concrete signature."""
        tr = self.tr
        if isinstance(target, MethodDef):
            md = target
            owner = tr.owner_sig(md.owner, mt.targs if md.owner.gparams and md.owner is mt.md.owner else ())
            margs = ()
            params, ret = md.params, md.ret
            has_this, name = md.has_this, md.name
            definition = md
        else:
            owner = mt.s(target.parent)
            margs = tuple(mt.s(a) for a in target.margs)
            params, ret = target.params, target.ret
            has_this, name = target.has_this, target.name
            definition = getattr(target, 'definition', None) or self.find_def(owner, name, params, target.gparam_count)
        otargs = owner[2] if owner[0] == 'gi' else ()
        cparams = [norm(subst(p, otargs, margs)) for p in params]
        cret = norm(subst(ret, otargs, margs))
        if owner[0] == 'prim':
            key = PRIM_KEYS[owner[1]]
        else:
            key = owner[1] if owner[0] in ('class', 'vt', 'gi') else sig_str(owner)
        return {'owner': owner, 'key': key,
                'name': name, 'params': cparams, 'ret': cret, 'has_this': has_this, 'margs': margs,
                'def': definition, 'raw_params': params, 'otargs': otargs}

    def find_def(self, owner, name, params, gpc):
        if owner[0] == 'prim' and owner[1] in PRIM_KEYS:
            owner = ('class', PRIM_KEYS[owner[1]])
        if owner[0] not in ('class', 'vt', 'gi'):
            return None
        m = self._find_in(self.tr.u.types, owner[1], name, params, gpc)
        if m is None and self.tr.u.pc_types:
            m = self._find_in(self.tr.u.pc_types, owner[1], name, params, gpc)
        return m

    def _find_in(self, types, key, name, params, gpc):
        td = types.get(key)
        while td is not None:
            for m in td.methods:
                if m.name == name and len(m.params) == len(params) and m.gparam_count == gpc:
                    if [sig_str(p) for p in m.params] == [sig_str(p) for p in params]:
                        return m
            for m in td.methods:
                if m.name == name and len(m.params) == len(params) and m.gparam_count == gpc:
                    return m
            if td.extends is None:
                break
            td = types.get(td.extends[1])
        return None

    def this_sig(self, c):
        o = c['owner']
        return ('byref', o) if self.tr.ts.is_vt(o) else o

    def fn_type(self, c, with_this=True, hidden=True):
        ts = self.tr.ts
        ps = []
        if c['has_this'] and with_this:
            ps.append(ts.cty(self.this_sig(c)) if c['owner'][0] != 'prim' or True else 'obj')
        ps += [ts.cty(p) for p in c['params']]
        if hidden:
            ps.append('const void*')
        return f'{ts.cty(c["ret"])} (*)({", ".join(ps)})'

    # ---------------------------------------------------------- emission
    def emit(self, mt, op, tok, nxt, stack):
        tr = self.tr
        target = mt.md.asm.resolve_token(tok)
        c = self.resolve(target, mt)
        n = len(c['params'])
        args = []
        for i in range(n):
            args.append(mt.pop(stack))
        args.reverse()
        this = None
        if op != 'newobj' and c['has_this']:
            this = mt.pop(stack)
        constrained, mt.constrained = mt.constrained, None
        argx = [mt.conv_to(v, sv, p) for (v, sv), p in zip(args, c['params'])]
        expr, push_sig = self.call_expr(mt, op, c, this, args, argx, constrained)
        ret = c['ret']
        if op == 'newobj':
            # Build the object in its own temporary: the result's stack slot still holds the first ctor argument.
            ct = mt.stack_ct(push_sig)
            mt.newobj_n = getattr(mt, 'newobj_n', 0) + 1
            tmp = f'nw{mt.newobj_n}'
            mt.vars[tmp] = ct
            mt.lines.extend(expr(tmp))
            mt.push(stack, mt.stack_ct(push_sig), push_sig, mt.cast_to_stack(tmp, push_sig))
        elif ret == ('prim', 'void'):
            mt.lines.append(f'{expr};')
        else:
            mt.push(stack, mt.stack_ct(ret), ret, mt.cast_to_stack(expr, ret))
        return [nxt]

    def call_expr(self, mt, op, c, this, args, argx, constrained):
        tr = self.tr
        ts = tr.ts
        key, name = c['key'], c['name']
        origin = ts.origin(key) if c['owner'][0] in ('class', 'vt', 'gi', 'prim') else 'game'
        where = str(mt.md)

        # ----- special cases
        special = self.special(mt, op, c, this, args, argx)
        if special is not None:
            return special
        # ----- object creation
        if op == 'newobj':
            return self.newobj(mt, c, args, argx, origin)
        thisx = None
        if this is not None:
            tv, tsv = this
            if constrained is not None:
                thisx = self.constrained_this(mt, constrained, tv, tsv, c)
            else:
                thisx = tv if not ts.is_vt(c['owner']) or tsv.ct.endswith('*') else f'&{tv}'
                if ts.is_vt(c['owner']) and tsv.ct == OBJ:
                    thisx = f'({ts.cty(c["owner"])}*)rt_unbox({tv})'
        allx = ([thisx] if thisx is not None else []) + argx
        ft = self.fn_type(c)
        # ----- emulated / shim
        shim = self.shim_name(c)
        if origin == 'emu' or (key in UNITY_EMU_BASES and shim in tr.shims) or shim in tr.shims:
            if shim not in tr.shims:
                tr.report.add('error', f'unsupported API {self.describe(c)} (used by {where}) [{shim}]')
                return self.unsupported_call(c, where), None
            extra = self.shim_context(mt, c)
            self.proto(shim, c, len(extra), with_this=True)
            return f'{shim}({", ".join(allx + extra)})', None
        # ----- mod / pc-only methods
        if origin in ('mod', 'pconly') or (c['def'] is not None and origin == 'game' and self.needs_fallback(c)):
            md = c['def']
            if md is None:
                tr.report.add('error', f'cannot resolve {self.describe(c)} (used by {where})')
                return self.unsupported_call(c, where), None
            if op == 'callvirt' and md.virtual and not md.is_ctor and self.may_be_overridden(md):
                mid = tr.method_id(('virt', self.decl_key(md), md.name, method_key_sig(md, c['otargs'])))
                fn = tr.fn(md, c['otargs'], c['margs']) if md.body() else 'nullptr'
                return f'(({ft})rt_mod_vfn({thisx}, {mid}, (void*){"&" + fn if fn != "nullptr" else fn}))' \
                       f'({", ".join(allx + ["nullptr"])})', None
            if md.abstract or md.body() is None:
                if md.owner.is_interface or md.abstract:
                    return self.virtual_call(mt, c, thisx, allx, ft, md), None
                tr.report.add('error', f'no body for {md} (used by {where})')
                return self.unsupported_call(c, where), None
            fn = tr.fn(md, c['otargs'], c['margs'])
            if this is not None and not ts.is_vt(c['owner']):
                allx[0] = f'rt_nc({allx[0]})'
            return f'{fn}({", ".join(allx + ["nullptr"])})', None
        # ----- game methods
        if origin == 'game' and c['owner'][0] == 'gi':
            emu = tr.coll.call(c, thisx, argx)
            if emu is not None:
                return emu, None
            if key.startswith(('System.Nullable`', G_KVP, 'System.ValueTuple`')) and not tr.coll.native(c['owner']) \
                    and thisx is not None:
                fields = [f for f in tr.u.get(key).fields if not f.static and not f.literal]
                fx = {cid(f.name) for f in fields}
                p = f'({ts.cty(c["owner"])}*)({thisx})'
                simple = {'get_HasValue': 'hasValue', 'get_Value': 'value', 'get_Key': 'key',
                          'GetValueOrDefault': 'value'}
                fname = simple.get(c['name'])
                if fname and fname in fx and not c['params']:
                    return f'({p})->{fname}', None
        if origin == 'game' and not key.startswith(ERASABLE) and self.shim_name(c) not in tr.shims and any(self.tr.is_mod_obj_sig(m) or
                                    (m[0] in ('class', 'vt', 'gi') and ts.origin(m[1]) in ('mod', 'pconly'))
                                    for m in c['margs']):
            tr.report.add('error', f'unsupported API {self.describe(c)} with a mod-defined type argument '
                                   f'(used by {where}) [{self.shim_name(c)}]')
            return self.unsupported_call(c, where), None
        if origin == 'game':
            allx = self.guard_enum_args(c, allx, where)
            dm = tr.db.method(key, name, c['raw_params'] if not c['otargs'] else c['params'], static=not c['has_this'])
            if dm is None:
                dm = tr.db.method(key, name, c['params'], static=not c['has_this'])
            if dm is None:
                md = c['def']
                if md is not None and md.body() is not None:
                    tr.report.add('info', f'translated from PC IL (stripped on Switch): {self.describe(c)}')
                    fn = tr.fn(md, c['otargs'], c['margs'])
                    return f'{fn}({", ".join(allx + ["nullptr"])})', None
                if tr.stubbable(key):
                    tr.report.add('warn', f'{self.describe(c)} is not in the Switch build; it does nothing there')
                    return self.default_value(c), None
                tr.report.add('error', tr.needs_pc(f'{self.describe(c)} (used by {where})'))
                return self.unsupported_call(c, where), None
            if this is not None and not ts.is_vt(c['owner']):
                allx[0] = f'rt_nc({allx[0]})'
            virt = op == 'callvirt' and dm['virtual'] and not self.sealed_game(key)
            receiver_may_be_mod = this is not None and self.receiver_may_be_mod(this[1], c)
            if virt or (receiver_may_be_mod and dm['virtual']):
                return self.virtual_call(mt, c, thisx, allx, ft, c['def'], dm), None
            if c['otargs'] or c['margs'] or not dm['rva']:
                site = self.reflect_site(c)
                return f'(({ft})RT_SITE_FP({site}))({", ".join(allx + [f"RT_SITE_MI({site})"])})', None
            return f'(({ft})(silk_main_base + {dm["rva"]:#x}))({", ".join(allx + ["nullptr"])})', None
        if tr.stubbable(key):
            tr.report.add('warn', f'{self.describe(c)} is not in the Switch build; it does nothing there')
            return self.default_value(c), None
        tr.report.add('error', tr.needs_pc(f'{self.describe(c)} (used by {where})') if origin == 'unknown'
                      else f'unresolved call {self.describe(c)} (used by {where})')
        return self.unsupported_call(c, where), None

    def needs_fallback(self, c):
        return False

    def guard_enum_args(self, c, allx, where):
        """Game code walks IEnumerable arguments itself; a converted collection must reach it as a real
        object[] (see rt_native_enum), never as the emulated object."""
        off = len(allx) - len(c['params'])
        out = list(allx)
        for i, p in enumerate(c['params']):
            p = norm(p)
            if p[0] in ('class', 'gi') and p[1] in ENUM_IFACES:
                out[off + i] = f'rt_native_enum({out[off + i]}, "{cstr(self.describe(c))[:120]}")'
        return out

    def proto(self, shim, c, nextra, with_this):
        ts = self.tr.ts
        ps = []
        if with_this and c['has_this']:
            ps.append(ts.cty(self.this_sig(c)))
        ps += [ts.cty(p) for p in c['params']]
        ps += ['int32_t'] * nextra
        self.tr.shim_protos[shim] = f'{ts.cty(c["ret"])} {shim}({", ".join(ps)});'

    def default_value(self, c):
        ret = self.tr.ts.cty(c['ret'])
        return '(void)0' if ret == 'void' else f'{ret}{{}}'

    def last_literal(self, mt, var):
        """If `var` was just assigned a string literal in this method, return the literal."""
        for line in reversed(mt.lines):
            if line.startswith(f'{var} = RT_LIT('):
                idx = int(line.split('RT_LIT(')[1].split(')')[0])
                for s_, i in self.tr.literals.items():
                    if i == idx:
                        return s_
            if line.startswith(f'{var} ='):
                return None
        return None

    def describe(self, c):
        m = f'<{", ".join(sig_str(a) for a in c["margs"])}>' if c['margs'] else ''
        return f'{sig_str(c["owner"])}::{c["name"]}{m}({", ".join(sig_str(p) for p in c["params"])})'

    def unsupported_call(self, c, where):
        ret = self.tr.ts.cty(c['ret'])
        msg = cstr(f'{self.describe(c)} in {where}')
        if ret == 'void':
            return f'rt_unsupported("{msg}")'
        return f'(rt_unsupported("{msg}"), {ret}{{}})'

    def decl_key(self, md):
        """Key of the type that introduced md's vtable slot."""
        tr = self.tr
        if md.newslot or not md.virtual:
            return md.owner.key
        td = md.owner
        while td is not None and td.extends is not None:
            b = tr.u.get(td.extends[1])
            if b is None:
                break
            for bm in b.methods:
                if bm.name == md.name and bm.virtual and len(bm.params) == len(md.params):
                    if bm.newslot or b.extends is None:
                        return b.key
            td = b
        return md.owner.key

    def may_be_overridden(self, md):
        if md.owner.is_sealed or not md.virtual:
            return md.owner.is_interface
        return True

    def sealed_game(self, key):
        td = self.tr.u.get(key)
        return td is not None and td.is_sealed and not td.is_interface

    def receiver_may_be_mod(self, sv, c):
        sig = sv.sig
        if sig is None or not isinstance(sig, tuple):
            return True
        if sig[0] in ('class', 'gi') and self.tr.ts.origin(sig[1]) in ('mod', 'pconly'):
            return True
        if sig[0] == 'prim' and sig[1] == 'object':
            return True
        td = self.tr.u.get(sig[1]) if sig[0] in ('class', 'gi') else None
        if td is not None and (td.is_interface or sig[1] in UNITY_EMU_BASES or sig[1] == 'System.Object'):
            return True
        return False

    def virtual_call(self, mt, c, thisx, allx, ft, md, dm=None):
        tr = self.tr
        if dm is None:
            dm = tr.db.method(c['key'], c['name'], c['params'], static=False)
        decl = md if md is not None else None
        mid = tr.method_id(('virt', self.decl_key(decl) if decl else c['key'], c['name'],
                            method_key_sig(decl, c['otargs']) if decl else
                            '(' + ','.join(sig_str(p) for p in c['params']) + ')'))
        owner_t = tr.db.type(c['key'])
        is_iface = bool(owner_t and owner_t.get('interface'))
        slot = dm['slot'] if dm else -1
        tref = tr.type_ref(c['owner']) if tr.aqn(c['owner']) else -1
        idx = len(self.site_defs)
        desc = self.reflect_desc(c)
        self.site_defs.append(f'static rt_vsite VS_{idx} = {{{mid}, {slot}, {tref}, {"true" if is_iface else "false"}, '
                              f'"{cstr(c["name"])}", "{cstr(desc)}"}};')
        return f'(({ft})rt_vfn({allx[0]}, &VS_{idx}))({", ".join(allx + [f"VS_{idx}.mi"])})'

    def reflect_desc(self, c):
        tr = self.tr
        owner = tr.aqn(c['owner']) or ''
        ps = ';'.join(tr.aqn(p) or '?' for p in c['raw_params_conc']) if 'raw_params_conc' in c else \
            ';'.join(self.param_aqn(p) for p in c['params'])
        ms = ';'.join(tr.aqn(self.erase(m)) or '?' for m in c['margs'])
        return f'{owner}|{c["name"]}|{ps}|{ms}|{"i" if c["has_this"] else "s"}'

    def erase(self, t):
        if self.tr.ts.is_mod_ref_type(t):
            return ('prim', 'object')
        return t

    def param_aqn(self, p):
        tr = self.tr
        byref = p[0] == 'byref'
        if byref:
            p = p[1]
        a = tr.aqn(self.erase(p)) or '?'
        if byref and a != '?':
            name, asm = a.rsplit(', ', 1) if not a.endswith(']') else (a, '')
            a = f'{name}&, {asm}' if asm else a + '&'
        return a

    def reflect_site(self, c):
        # Parameters must be the *definition's* types for GetMethod with generic classes: use generic def params
        tr = self.tr
        idx = len(self.site_defs)
        desc = self.reflect_desc(c)
        self.site_defs.append(f'static rt_rsite RS_{idx} = {{"{cstr(desc)}", nullptr, nullptr}};')
        return f'RS_{idx}'

    def constrained_this(self, mt, t, tv, tsv, c):
        ts = self.tr.ts
        if ts.is_vt(t):
            if c['key'] in ('System.Object', 'System.ValueType', 'System.Enum') or ts.td(c['owner']) is None or \
                    ts.td(c['owner']).is_interface:
                # box the value (simple + correct for ToString/GetHashCode/Equals on structs)
                inner = f'*({ts.cty(t)}*){tv}'
                return self.tr.box_expr(inner, SV(ts.cty(t), t), t, mt)
            return tv
        return f'*(obj*){tv}'

    def shim_name(self, c):
        """Runtime shim implementing this API, e.g. shim_UnityEngine_Input__GetKeyDown__UnityEngine_KeyCode."""
        ts = self.tr.ts

        def pname(raw, conc):
            # generic parameters are named by their concrete C type (one shim per ABI shape)
            if raw is not None and self.mentions_generic(raw):
                return plain(ts.cty(conc))
            return plain(dump_type_name(conc).replace('[]', 'Arr'))
        raws = c['raw_params'] if len(c['raw_params']) == len(c['params']) else [None] * len(c['params'])
        ps = '_'.join(pname(r, p) for r, p in zip(raws, c['params']))
        ms = ('_G' + str(len(c['margs']))) if c['margs'] else ''
        owner = plain(c['key'])
        if c['owner'][0] == 'gi':
            owner += '_' + '_'.join(plain(ts.cty(a)) for a in c['owner'][2])
        name = f'shim_{owner}__{plain(c["name"])}'
        if ps:
            return f'{name}__{ps}{ms}'
        return name + ms

    def mentions_generic(self, t):
        if t[0] in ('var', 'mvar'):
            return True
        if t[0] == 'gi':
            return any(self.mentions_generic(a) for a in t[2])
        if t[0] in ('sz', 'byref', 'ptr', 'md'):
            return self.mentions_generic(t[1])
        return False

    def shim_context(self, mt, c):
        """Implicit context arguments some shims receive (calling assembly id, generic arg type refs)."""
        extra = []
        if c['key'].startswith(('HarmonyLib.', 'BepInEx.')) or c['key'] == 'System.Reflection.Assembly':
            extra.append(f'{self.tr.asm_index(mt.md.asm)}')
        for m in c['margs']:
            extra.append(f'{self.tr.type_ref(m)}')
        return extra

    def newobj(self, mt, c, args, argx, origin):
        tr = self.tr
        ts = tr.ts
        owner = c['owner']
        key = c['key']
        where = str(mt.md)
        ct = ts.cty(owner)
        shim = self.shim_name(c)
        if origin == 'emu' or shim in tr.shims:
            if shim not in tr.shims:
                tr.report.add('error', f'unsupported API {self.describe(c)} (used by {where}) [{shim}]')
                return (lambda v: [f'{v} = {ct}{{}};', self.unsupported_call(c, where) + ';']), owner
            extra = self.shim_context(mt, c)
            self.proto(shim, dict(c, ret=owner if not ts.is_vt(owner) else owner), len(extra), with_this=False)
            return (lambda v: [f'{v} = {shim}({", ".join(argx + extra)});']), owner
        td = tr.u.get(key)
        if key.startswith('System.ValueTuple`') and ts.is_vt(owner) and (origin != 'game' or not tr.coll.native(owner)):
            fields = [f for f in td.fields if not f.static and not f.literal]
            assigns = [f'{{v}}.{cid(f.name)} = {a};' for f, a in zip(fields, argx)]
            return (lambda v: [f'{v} = {ct}{{}};'] + [a.replace('{v}', v) for a in assigns]), owner
        if origin == 'game' and owner[0] == 'gi':
            emu = tr.coll.construct(c, argx)
            if emu is not None:
                return (lambda v: [f'{v} = {emu};']), owner
            if ts.is_vt(owner) and owner[1].startswith(('System.ValueTuple`', 'System.Nullable`', G_KVP)) and \
                    not tr.coll.native(owner):
                # simple generic structs the game never compiled: the constructor just stores its arguments
                info_fields = [f for f in tr.u.get(key).fields if not f.static and not f.literal]
                assigns = [f'{{v}}.{cid(f.name)} = {a};' for f, a in zip(info_fields, argx)]
                if key.startswith('System.Nullable`'):
                    assigns = [f'{{v}}.{cid(info_fields[0].name)} = true;', f'{{v}}.{cid(info_fields[1].name)} = {argx[0]};']
                return (lambda v: [f'{v} = {ct}{{}};'] + [a.replace('{v}', v) for a in assigns]), owner
        # delegates
        if td is not None and td.extends is not None and td.extends[1] in DELEGATE_BASES:
            (tv, tsv), (fv, fsv) = args
            info = fsv.extra
            if info is None:
                raise Unsupported('delegate from non-ldftn function pointer')
            fn_expr, is_static, target_c = info
            thunk = self.delegate_thunk(c, info) if is_static else fn_expr
            if origin in ('mod', 'pconly') or tr.aqn(owner) is None:
                # mod delegate types, or game delegate types over mod structs (only mod code invokes these)
                return (lambda v: [f'{v} = rt_new_mod_delegate((void*){thunk}, {tv});']), owner
            guarded = self.guard_thunk(c, thunk, is_static)
            # A guarded thunk receives the delegate itself as `this` (like a static delegate) and fetches the
            # real target from it, so game code never sees a converted object as the invocation receiver.
            self_is_delegate = is_static or guarded != thunk
            return (lambda v: [f'{v} = rt_new_delegate({tr.type_ref(owner)}, (void*){guarded}, {tv}, '
                               f'{"true" if self_is_delegate else "false"});']), owner
        if origin in ('mod', 'pconly'):
            md = c['def']
            info = tr.mod_type(owner)
            fn = tr.fn(md, c['otargs']) if md is not None and md.body() is not None else None
            if info.is_vt:
                call = f'{fn}({", ".join([f"&{{v}}"] + argx + ["nullptr"])});' if fn else ''
                return (lambda v: [f'{v} = {ct}{{}};'] + ([call.replace('{v}', v)] if fn else [])), owner
            return (lambda v: [f'{v} = rt_new_mod(&D_{info.cname});'] +
                    ([f'{fn}({", ".join([v] + argx + ["nullptr"])});'] if fn else [])), owner
        if origin == 'game':
            if key == 'System.String':
                # IL2CPP implements string constructors as String.CreateString(null, ...)
                dm = tr.db.method(key, 'CreateString', c['params'], static=False)
                if dm is None or not dm['rva']:
                    raise Unsupported(f'string constructor {self.describe(c)}')
                ft = f'obj (*)(obj, {", ".join(ts.cty(p) for p in c["params"])}, const void*)'
                return (lambda v: [f'{v} = (({ft})(silk_main_base + {dm["rva"]:#x}))(nullptr, '
                                   f'{", ".join(argx + ["nullptr"])});']), owner
            dm = tr.db.method(key, '.ctor', c['raw_params'] if not c['otargs'] else c['params'], static=False) or \
                tr.db.method(key, '.ctor', c['params'], static=False)
            ft = self.fn_type(c)
            argx = self.guard_enum_args(c, argx, where)
            if ts.is_vt(owner):
                if dm is None:
                    md = c['def']
                    if md is not None and md.body() is not None:
                        fn = tr.fn(md, c['otargs'])
                        return (lambda v: [f'{v} = {ct}{{}};', f'{fn}({", ".join([f"&{v}"] + argx + ["nullptr"])});']), owner
                    raise Unsupported(f'missing struct ctor {self.describe(c)}')
                if c['otargs'] or not dm['rva']:
                    site = self.reflect_site(dict(c, name='.ctor'))
                    return (lambda v: [f'{v} = {ct}{{}};', f'(({ft})RT_SITE_FP({site}))({", ".join([f"&{v}"] + argx + [f"RT_SITE_MI({site})"])});']), owner
                return (lambda v: [f'{v} = {ct}{{}};',
                                   f'(({ft})(silk_main_base + {dm["rva"]:#x}))({", ".join([f"&{v}"] + argx + ["nullptr"])});']), owner
            tref = tr.type_ref(owner)
            if dm is None:
                md = c['def']
                if md is not None and md.body() is not None:
                    fn = tr.fn(md, c['otargs'])
                    return (lambda v: [f'{v} = rt_new_obj({tref});', f'{fn}({", ".join([v] + argx + ["nullptr"])});']), owner
                tr.report.add('error', tr.needs_pc(f'{self.describe(c)} (used by {where})'))
                return (lambda v: [f'{v} = {{}};', self.unsupported_call(c, where) + ';']), owner
            if c['otargs'] or not dm['rva']:
                site = self.reflect_site(c)
                return (lambda v: [f'{v} = rt_new_obj({tref});',
                                   f'(({ft})RT_SITE_FP({site}))({", ".join([v] + argx + [f"RT_SITE_MI({site})"])});']), owner
            return (lambda v: [f'{v} = rt_new_obj({tref});',
                               f'(({ft})(silk_main_base + {dm["rva"]:#x}))({", ".join([v] + argx + ["nullptr"])});']), owner
        if tr.stubbable(key):
            tr.report.add('warn', f'{sig_str(owner)} is not in the Switch build; a placeholder is used')
            return (lambda v: [f'{v} = rt_placeholder("{cstr(key)}");']), owner
        tr.report.add('error', f'cannot construct {sig_str(owner)} (used by {where})')
        return (lambda v: [f'{v} = {{}};', self.unsupported_call(c, where) + ';']), owner

    def delegate_thunk(self, c, info):
        """Static target: game delegate invoke passes (method_code, args..., method); drop the first."""
        fn_expr, is_static, target_c = info
        tr = self.tr
        ts = tr.ts
        key = (fn_expr, sig_str(c['owner']))
        if key in tr.thunks:
            return tr.thunks[key]
        inv = self.find_def(c['owner'], 'Invoke', [None] * 0, 0)
        td = tr.u.get(c['key'])
        invoke = td.method('Invoke') if td else None
        if invoke is None:
            raise Unsupported('delegate without Invoke')
        ps = [norm(subst(p, c['otargs'])) for p in invoke.params]
        rt = norm(subst(invoke.ret, c['otargs']))
        name = f'TH_{len(tr.thunks)}'
        params = ', '.join(['obj self'] + [f'{ts.cty(p)} p{i}' for i, p in enumerate(ps)] + ['const void* mi'])
        ftype = f'{ts.cty(rt)} (*)({", ".join([ts.cty(p) for p in ps] + ["const void*"])})'
        call = f'(({ftype})({fn_expr}))({", ".join([f"p{i}" for i in range(len(ps))] + ["nullptr"])})'
        tr.extra.append(f'static {ts.cty(rt)} {name}({params}) {{ (void)self; (void)mi; '
                        f'{"return " if rt != ("prim", "void") else ""}{call}; }}')
        tr.thunks[key] = f'&{name}'
        return f'&{name}'

    def guard_thunk(self, c, fn_expr, is_static):
        """Game code invokes this delegate (an event, a callback): give it a SilkNX boundary so an exception
        in mod code is logged (as Unity would) instead of unwinding into game code with nowhere to land."""
        if not re.fullmatch(r'&\w+', fn_expr):
            return fn_expr  # computed at runtime (virtual mod method): no static wrapper possible
        tr = self.tr
        ts = tr.ts
        key = ('guard', fn_expr, sig_str(c['owner']), is_static)
        if key in tr.thunks:
            return tr.thunks[key]
        td = tr.u.get(c['key'])
        invoke = td.method('Invoke') if td else None
        if invoke is None:
            return fn_expr
        ps = [norm(subst(p, c['otargs'])) for p in invoke.params]
        rt = norm(subst(invoke.ret, c['otargs']))
        rct = ts.cty(rt)
        name = f'GT_{len(tr.thunks)}'
        params = ', '.join(['obj self'] + [f'{ts.cty(p)} p{i}' for i, p in enumerate(ps)] + ['const void* mi'])
        ftype = f'{rct} (*)({", ".join(["obj"] + [ts.cty(p) for p in ps] + ["const void*"])})'
        recv = 'self' if is_static else 'rt_delegate_bound_target(self)'
        call = f'(({ftype})({fn_expr[1:]}))({", ".join([recv] + [f"p{i}" for i in range(len(ps))] + ["mi"])})'
        if rt == ('prim', 'void'):
            body = f'RT_GUARD_BEGIN {call}; RT_GUARD_END'
            tail = ''
        else:
            body = f'{rct} r{{}}; RT_GUARD_BEGIN r = {call}; RT_GUARD_END'
            tail = ' return r;'
        tr.extra.append(f'static {rct} {name}({params}) {{ {body}{tail} }}')
        tr.thunks[key] = f'&{name}'
        return f'&{name}'

    def function_pointer(self, mt, target, obj_v):
        """ldftn/ldvirtftn: returns (C expr, is_static, call info)."""
        tr = self.tr
        c = self.resolve(target, mt)
        origin = tr.ts.origin(c['key']) if c['owner'][0] in ('class', 'vt', 'gi') else 'game'
        md = c['def']
        if origin in ('mod', 'pconly') and md is not None and md.body() is not None:
            fn = tr.fn(md, c['otargs'], c['margs'])
            if obj_v is not None and md.virtual:
                mid = tr.method_id(('virt', self.decl_key(md), md.name, method_key_sig(md, c['otargs'])))
                return (f'rt_mod_vfn({obj_v}, {mid}, (void*)&{fn})', not c['has_this'], c)
            return (f'&{fn}', not c['has_this'], c)
        if origin == 'game':
            dm = tr.db.method(c['key'], c['name'], c['params'], static=not c['has_this'])
            if dm and dm['rva'] and not c['otargs'] and not c['margs']:
                return (f'(void*)(silk_main_base + {dm["rva"]:#x})', not c['has_this'], c)
        raise Unsupported(f'function pointer to {self.describe(c)}')

    # ---------------------------------------------------------- special cases
    def special(self, mt, op, c, this, args, argx):
        tr = self.tr
        key, name = c['key'], c['name']
        if key == 'System.Type' and name == 'GetTypeFromHandle' and args and args[0][1].sig and \
                args[0][1].sig[0] == 'typehandle':
            return f'rt_type({tr.type_ref(args[0][1].sig[1])})', None
        if key == 'System.Runtime.CompilerServices.RuntimeHelpers' and name == 'InitializeArray':
            return f'rt_init_array({args[0][0]}, (const rt_blob*){args[1][0]})', None
        if key == 'System.Object' and name == '.ctor' and op != 'newobj':
            return '(void)0', None
        if key == 'HarmonyLib.AccessTools' and name == 'FieldRefAccess' and len(c['margs']) == 2 and args and \
                args[-1][1].sig == ('prim', 'string'):
            ex = args[-1][1].extra
            lit = ex[1] if isinstance(ex, tuple) and ex and ex[0] == 'lit' else None
            owner_t = c['margs'][0]
            df = tr.db.field(owner_t[1], lit) if lit and owner_t[0] in ('class', 'vt') else None
            if df is not None:
                return f'rt_fieldref({df["offset"]})', None
            tr.report.add('error', f'FieldRefAccess<{sig_str(owner_t)}>("{lit}"): field not found on Switch')
            return 'rt_fieldref(-1)', None
        if key == 'HarmonyLib.AccessTools/FieldRef`2' and name == 'Invoke':
            fct = tr.ts.cty(c['ret'])
            return f'(({fct})rt_fieldref_ptr({this[0]}, {argx[0]}))', None
        if key == 'HarmonyLib.Harmony' and name == 'Patch' and getattr(tr, 'harmony', None) is not None:
            sites = tr.harmony.manual_sites.get((id(mt.md), mt.cur))
            if sites:
                return '(' + ', '.join(f'rt_harmony_activate({x})' for x in sites) + ')', None
        if op == 'call' and name == '.ctor' and this is not None and key in UNITY_EMU_BASES:
            return '(void)0', None
        if name == 'Invoke' and c['has_this'] and tr.u.get(key) is not None and \
                tr.u.get(key).extends is not None and tr.u.get(key).extends[1] in DELEGATE_BASES:
            ts = tr.ts
            ft = f'{ts.cty(c["ret"])} (*)({", ".join(["obj"] + [ts.cty(p) for p in c["params"]] + ["const void*"])})'
            d = this[0]
            return f'(({ft})rt_delegate_fn({d}))({", ".join([f"rt_delegate_target({d})"] + argx + [f"rt_delegate_mi({d})"])})', None
        return None


def _asm_index(self, asm):
    for i, a in enumerate(self.mod_asms):
        if a is asm:
            return i
    return -1


Translator.asm_index = _asm_index
