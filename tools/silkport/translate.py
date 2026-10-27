"""IL -> C++ translation of method bodies."""
import os
import re
import struct

from .model import MethodDef, MemberRef, FieldDef, sig_str, subst, PRIM_KEYS
from .types import cid, norm, EMULATED_PREFIXES, UNITY_EMU_BASES
from .switchdb import dkey

# ---------------------------------------------------------------- opcode table (name, operand kind)
from clrmeta import OPS1, OPS2  # noqa: E402

I4, I8, NI, R4, R8, OBJ = 'int32_t', 'int64_t', 'intptr_t', 'float', 'double', 'obj'
INT_PRIMS = {'bool', 'char', 'i1', 'u1', 'i2', 'u2', 'i4', 'u4'}


class Unsupported(Exception):
    pass


def decode(code):
    """Yield (offset, name, kind, operand, next_offset)."""
    p = 0
    out = []
    while p < len(code):
        at = p
        op = code[p]
        p += 1
        if op == 0xFE:
            name, kind = OPS2.get(code[p], (f'fe{code[p]:02x}', 'n'))
            p += 1
        else:
            name, kind = OPS1.get(op, (f'op{op:02x}', 'n'))
        arg = None
        if kind in ('i1',):
            arg = struct.unpack_from('<b', code, p)[0]; p += 1
        elif kind == 'u1':
            arg = code[p]; p += 1
        elif kind == 'u2':
            arg = struct.unpack_from('<H', code, p)[0]; p += 2
        elif kind == 'i4':
            arg = struct.unpack_from('<i', code, p)[0]; p += 4
        elif kind == 'i8':
            arg = struct.unpack_from('<q', code, p)[0]; p += 8
        elif kind == 'r4':
            arg = struct.unpack_from('<f', code, p)[0]; p += 4
        elif kind == 'r8':
            arg = struct.unpack_from('<d', code, p)[0]; p += 8
        elif kind in ('t', 's'):
            arg = struct.unpack_from('<I', code, p)[0]; p += 4
        elif kind == 'br1':
            arg = p + 1 + struct.unpack_from('<b', code, p)[0]; p += 1
        elif kind == 'br4':
            arg = p + 4 + struct.unpack_from('<i', code, p)[0]; p += 4
        elif kind == 'sw':
            n = struct.unpack_from('<I', code, p)[0]
            tg = struct.unpack_from(f'<{n}i', code, p + 4)
            p += 4 + 4 * n
            arg = [p + t for t in tg]
        out.append((at, name, kind, arg, p))
    return out


def cfloat(v, double=False):
    if v != v:
        return '__builtin_nan("")' if double else '__builtin_nanf("")'
    if v in (float('inf'), float('-inf')):
        s = '__builtin_inf()' if double else '__builtin_inff()'
        return s if v > 0 else '-' + s
    r = repr(float(v))
    if 'e' not in r and '.' not in r:
        r += '.0'
    return r if double else r + 'f'


class SV:
    """A stack value: C++ type + precise TypeSig (or a marker)."""
    __slots__ = ('ct', 'sig', 'extra')

    def __init__(self, ct, sig, extra=None):
        self.ct, self.sig, self.extra = ct, sig, extra

    def __repr__(self):
        return f'SV({self.ct},{self.sig})'


class MethodTranslator:
    def __init__(self, tr, md, targs, margs, cname):
        self.tr, self.md, self.targs, self.margs, self.cname = tr, md, tuple(targs), tuple(margs), cname
        self.ts = tr.ts
        self.body = md.body()
        self.ins = decode(self.body.code)
        self.by_off = {i[0]: k for k, i in enumerate(self.ins)}
        self.vars = {}  # name -> ctype
        self.lines = []
        self.prefix = None
        self.constrained = None
        self.labels_used = set()
        self.uses_eh = bool(self.body.clauses)
        self.fin_counter = 0

    # ---------------------------------------------------------- helpers
    def s(self, sig):
        return norm(subst(sig, self.targs, self.margs))

    def arg_sig(self, i):
        md = self.md
        if md.has_this:
            if i == 0:
                own = self.tr.owner_sig(md.owner, self.targs)
                return ('byref', own) if self.ts.is_vt(own) else own
            i -= 1
        return self.s(md.params[i])

    def stack_ct(self, sig):
        """C++ type a value of `sig` has on the IL evaluation stack."""
        sig = norm(sig)
        if sig[0] == 'prim':
            p = sig[1]
            if p in INT_PRIMS:
                return I4
            return {'i8': I8, 'u8': I8, 'i': NI, 'u': NI, 'r4': R4, 'r8': R8}.get(p, OBJ)
        if self.ts.is_enum_sig(sig):
            return self.stack_ct(self.ts.u.enum_underlying(self.ts.td(sig)))
        return self.ts.cty(sig)

    def var(self, depth, ct):
        name = f's{depth}_{cid(ct.replace("*", "P"), 40)}'
        self.vars[name] = ct
        return name

    def label(self, off):
        self.labels_used.add(off)
        return f'IL_{off:04x}'

    # ---------------------------------------------------------- main
    def translate(self):
        body = self.body
        self.locals = [self.s(t) for t in body.locals]
        states = self.flow()
        md = self.md
        # signature
        params = []
        nargs = len(md.params) + (1 if md.has_this else 0)
        for i in range(nargs):
            params.append(f'{self.ts.cty(self.arg_sig(i))} a{i}')
        params.append('const void* mi')
        ret = self.ts.cty(self.s(md.ret))
        out_lines = []
        self.emit_mode = True
        handler_starts = {c[3]: c for c in body.clauses}
        for at, name, kind, arg, nxt in self.ins:
            st = states.get(at)
            if st is None:
                continue  # unreachable
            self.cur = at
            if at in self.branch_targets or at in handler_starts or at in self.try_starts:
                out_lines.append(f'{self.label(at)}:;')
            for c in self.try_starts.get(at, []):
                out_lines += self.try_enter(c)
            self.lines = []
            stack = list(st)
            try:
                self.step(name, kind, arg, nxt, stack)
            except Unsupported as e:
                self.tr.report.unsupported(self.md, str(e))
                self.lines.append(f'rt_unsupported("{cstr(str(e))}");')
            out_lines += ['    ' + l for l in self.lines]
        decl = [f'    {ct} {n}{{}};' for n, ct in sorted(self.vars.items())]
        for i, t in enumerate(self.locals):
            decl.append(f'    {self.ts.cty(t)} l{i}{{}};')
        decl += self.eh_decls
        attr = '__attribute__((optnone)) ' if self.uses_catch else ''
        head = f'{attr}{ret} {self.cname}({", ".join(params)})'
        trace = [f'    RT_TRACE("{cstr(str(self.md))[:150]}");']
        if os.environ.get('SILKNX_TRACE'):
            trace.append(f'    RT_EH_CHECK("{cstr(str(self.md))[:150]}");')
        stub = os.environ.get('SILKNX_STUB_FUNCS')  # debug bisect: matching functions return immediately
        if stub and re.search(stub, str(self.md)) and not self.md.is_ctor:
            early = '    return;' if ret == 'void' else f'    return {ret}{{}};'
            trace.append(early)
        return head, [head + ' {', '    (void)mi;'] + trace + decl + out_lines + ['}']

    # ---------------------------------------------------------- flow analysis (stack types)
    def flow(self):
        self.emit_mode = False
        self.branch_targets = set()
        self.try_starts = {}
        self.eh_decls = []
        self.uses_catch = False
        for c in self.body.clauses:
            flags, toff, tlen, hoff, hlen, ctok = c
            self.try_starts.setdefault(toff, []).append(c)
            self.branch_targets.add(hoff)
            if flags in (0, 1):
                self.uses_catch = True
            if flags == 1:  # filter: the filter block starts at ctok and receives the exception
                self.branch_targets.add(ctok)
        states = {0: ()}
        for c in self.body.clauses:
            flags, toff, tlen, hoff, hlen, ctok = c
            states[hoff] = (SV(OBJ, ('class', 'System.Exception')),) if flags in (0, 1) else ()
            if flags == 1:
                states[ctok] = (SV(OBJ, ('class', 'System.Exception')),)
        work = [0] + [c[3] for c in self.body.clauses] + [c[5] for c in self.body.clauses if c[0] == 1]
        visited = set()
        while work:
            at = work.pop()
            if at in visited or at not in self.by_off:
                continue
            visited.add(at)
            _, name, kind, arg, nxt = self.ins[self.by_off[at]]
            stack = list(states[at])
            self.lines = []
            self.cur = at
            try:
                succ = self.step(name, kind, arg, nxt, stack)
            except Unsupported:
                succ = []
            for s_off in succ:
                if s_off != nxt or name in ('br', 'br.s', 'leave', 'leave.s'):
                    self.branch_targets.add(s_off)
                if s_off not in states:
                    states[s_off] = tuple(stack)
                work.append(s_off)
        for c in self.body.clauses:
            if c[0] in (2, 4) and f'    int32_t fin_{c[3]:x} = 0;' not in self.eh_decls:
                self.eh_decls.append(f'    int32_t fin_{c[3]:x} = 0;')
        self.emit_mode = True
        return states

    # ---------------------------------------------------------- exception handling
    def enclosing(self, off, kinds=(0, 1, 2, 4)):
        """Clauses whose try block contains `off`, innermost first."""
        cs = [c for c in self.body.clauses if c[1] <= off < c[1] + c[2] and c[0] in kinds]
        cs.sort(key=lambda c: c[2])
        return cs

    def handler_of(self, off):
        cs = [c for c in self.body.clauses if c[3] <= off < c[3] + c[4]]
        cs.sort(key=lambda c: c[4])
        return cs

    def try_enter(self, c):
        flags, toff, tlen, hoff, hlen, ctok = c
        hid = f'h_{toff:x}_{hoff:x}'
        if f'    rt_handler {hid};' not in self.eh_decls:
            self.eh_decls.append(f'    rt_handler {hid};')
        if flags == 1:  # filter
            return [f'    if (RT_TRY({hid})) {{ s0_obj = rt_take_exception(); goto {self.label(ctok)}; }}']
        if flags == 0:  # catch
            ctype = self.tr.type_check_expr(self.body_asm_type(ctok), 'rt_exception()', self)
            return [f'    if (RT_TRY({hid})) {{',
                    f'        if ({ctype}) {{ s0_obj = rt_take_exception(); goto {self.label(hoff)}; }}',
                    f'        rt_rethrow();',
                    f'    }}']
        # finally / fault: run handler then rethrow
        fv = f'fin_{hoff:x}'
        if f'    int32_t {fv} = 0;' not in self.eh_decls:
            self.eh_decls.append(f'    int32_t {fv} = 0;')
        self.vars['s0_obj'] = 'obj'
        return [f'    if (RT_TRY({hid})) {{ {fv} = -1; goto {self.label(hoff)}; }}']

    def body_asm_type(self, tok):
        return self.s(self.md.asm.resolve_token(tok))

    def leave(self, target):
        """Exit protected regions up to `target`, running finally blocks."""
        here = self.cur
        exited = [c for c in self.enclosing(here) if not (c[1] <= target < c[1] + c[2])]
        # every exited try block (catch, filter, finally, fault) pops its handler; leaving a catch
        # handler region needs nothing (the handler was popped when the exception was caught)
        out = []
        chain = []
        for c in exited:
            out.append(f'rt_pop(&h_{c[1]:x}_{c[3]:x});')
            if c[0] == 2:
                chain.append(c)
        # handlers we are inside of (catch) that we leave: nothing to do
        if not chain:
            out.append(f'goto {self.label(target)};')
            return out
        # chain finally blocks: each finally's endfinally jumps to the next continuation
        conts = []
        for i, c in enumerate(chain):
            nxt_label = self.label(chain[i + 1][3]) if i + 1 < len(chain) else self.label(target)
            conts.append((c, nxt_label))
        self.fin_counter += 1
        cid_ = self.fin_counter
        for c, nxt_label in conts:
            fv = f'fin_{c[3]:x}'
            self.tr.fin_targets.setdefault((self.cname, c[3]), {})[cid_] = (nxt_label, conts)
            if f'    int32_t {fv} = 0;' not in self.eh_decls:
                self.eh_decls.append(f'    int32_t {fv} = 0;')
        first = conts[0][0]
        out.append(f'fin_{first[3]:x} = {cid_}; goto {self.label(first[3])};')
        return out

    def endfinally(self):
        hs = self.handler_of(self.cur)
        hs = [c for c in hs if c[0] in (2, 4)]
        if not hs:
            return ['rt_rethrow();']
        c = hs[0]
        fv = f'fin_{c[3]:x}'
        out = [f'switch ({fv}) {{', '    case -1: rt_rethrow();']
        for cid_, (nxt_label, conts) in sorted(self.tr.fin_targets.get((self.cname, c[3]), {}).items()):
            # find this finally's position in the chain and continue to the next one
            idx = [k for k, (cc, _) in enumerate(conts) if cc[3] == c[3]][0]
            if idx + 1 < len(conts):
                nf = conts[idx + 1][0]
                out.append(f'    case {cid_}: fin_{nf[3]:x} = {cid_}; goto {self.label(nf[3])};')
            else:
                out.append(f'    case {cid_}: goto {nxt_label};')
        out.append('    default: break;')
        out.append('}')
        return out

    # ---------------------------------------------------------- the big switch
    def push(self, stack, ct, sig, expr, extra=None):
        v = self.var(len(stack), ct)
        if expr is not None:
            self.lines.append(f'{v} = {expr};')
        stack.append(SV(ct, sig, extra))
        return v

    def pop(self, stack):
        sv = stack.pop()
        return self.var(len(stack), sv.ct), sv

    def step(self, name, kind, arg, nxt, stack):
        """Translate one instruction; returns successor offsets."""
        L = self.lines
        ts = self.ts
        if name in ('nop', 'break', 'volatile.', 'tail.', 'unaligned.', 'readonly.', 'no.'):
            return [nxt]
        if name == 'constrained.':
            self.constrained = self.s(self.md.asm.resolve_token(arg))
            return [nxt]
        # --- args / locals
        if name.startswith('ldarg') and name != 'ldarga' and name != 'ldarga.s':
            i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
            sig = self.arg_sig(i)
            self.push(stack, self.stack_ct(sig), sig, self.cast_to_stack(f'a{i}', sig))
            return [nxt]
        if name in ('ldarga', 'ldarga.s'):
            sig = self.arg_sig(arg)
            self.push(stack, ts.cty(sig) + '*', ('byref', sig), f'&a{arg}')
            return [nxt]
        if name.startswith('starg'):
            v, sv = self.pop(stack)
            L.append(f'a{arg} = {self.conv_to(v, sv, self.arg_sig(arg))};')
            return [nxt]
        if name.startswith('ldloc') and not name.startswith('ldloca'):
            i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
            sig = self.locals[i]
            self.push(stack, self.stack_ct(sig), sig, self.cast_to_stack(f'l{i}', sig))
            return [nxt]
        if name.startswith('ldloca'):
            sig = self.locals[arg]
            self.push(stack, ts.cty(sig) + '*', ('byref', sig), f'&l{arg}')
            return [nxt]
        if name.startswith('stloc'):
            i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
            v, sv = self.pop(stack)
            L.append(f'l{i} = {self.conv_to(v, sv, self.locals[i])};')
            return [nxt]
        # --- constants
        if name == 'ldnull':
            self.push(stack, OBJ, ('null',), 'nullptr')
            return [nxt]
        if name.startswith('ldc.i4'):
            if name == 'ldc.i4.m1':
                val = -1
            elif kind == 'n':
                val = int(name[7:])
            else:
                val = arg
            self.push(stack, I4, ('prim', 'i4'), f'(int32_t){val}' if val >= 0 else f'(int32_t)({val})')
            return [nxt]
        if name == 'ldc.i8':
            self.push(stack, I8, ('prim', 'i8'), f'(int64_t){arg}LL' if arg != -2 ** 63 else 'INT64_MIN')
            return [nxt]
        if name == 'ldc.r4':
            self.push(stack, R4, ('prim', 'r4'), cfloat(arg))
            return [nxt]
        if name == 'ldc.r8':
            self.push(stack, R8, ('prim', 'r8'), cfloat(arg, True))
            return [nxt]
        if name == 'ldstr':
            s = self.md.asm.us(arg & 0xFFFFFF)
            self.push(stack, OBJ, ('prim', 'string'), self.tr.literal(s), extra=('lit', s))
            return [nxt]
        if name == 'dup':
            v, sv = stack[-1:][0], stack[-1]
            src = self.var(len(stack) - 1, sv.ct)
            self.push(stack, sv.ct, sv.sig, src, sv.extra)
            return [nxt]
        if name == 'pop':
            self.pop(stack)
            return [nxt]
        # --- arithmetic
        if name in ('add', 'sub', 'mul', 'div', 'rem', 'and', 'or', 'xor', 'div.un', 'rem.un', 'add.ovf',
                    'add.ovf.un', 'sub.ovf', 'sub.ovf.un', 'mul.ovf', 'mul.ovf.un'):
            b, sb = self.pop(stack)
            a, sa = self.pop(stack)
            op = {'add': '+', 'sub': '-', 'mul': '*', 'div': '/', 'rem': '%', 'and': '&', 'or': '|', 'xor': '^'}[
                name.split('.')[0]]
            ct = self.arith_type(sa.ct, sb.ct)
            if ct.endswith('*'):
                expr = f'({ct})((uint8_t*){a} {op} (intptr_t){b})' if not sa.ct.endswith('*') or op != '-' or \
                    not sb.ct.endswith('*') else f'(intptr_t)((uint8_t*){a} - (uint8_t*){b})'
                if sa.ct.endswith('*') and sb.ct.endswith('*'):
                    ct = NI
                elif sb.ct.endswith('*'):
                    expr = f'({ct})((intptr_t){a} {op} (uint8_t*){b})'
                sig = sa.sig if sa.ct.endswith('*') else sb.sig
            else:
                sig = None
                if name.endswith('.un') and ct in (I4, I8, NI):
                    u = {I4: 'uint32_t', I8: 'uint64_t', NI: 'uintptr_t'}[ct]
                    expr = f'({ct})(({u}){a} {op} ({u}){b})'
                elif op == '%' and ct in (R4, R8):
                    expr = f'{"__builtin_fmodf" if ct == R4 else "__builtin_fmod"}({a}, {b})'
                elif op in ('/', '%') and ct in (I4, I8, NI):
                    expr = f'rt_{"div" if op == "/" else "rem"}(({ct}){a}, ({ct}){b})'
                elif op in ('+', '-', '*') and ct in (I4, I8, NI):
                    u = {I4: 'uint32_t', I8: 'uint64_t', NI: 'uintptr_t'}[ct]
                    expr = f'({ct})(({u}){a} {op} ({u}){b})'  # wrapping arithmetic
                else:
                    expr = f'({ct})({a} {op} {b})'
            self.push(stack, ct, sig or ('stack', ct), expr)
            return [nxt]
        if name in ('shl', 'shr', 'shr.un'):
            b, sb = self.pop(stack)
            a, sa = self.pop(stack)
            ct = sa.ct
            u = {I4: 'uint32_t', I8: 'uint64_t', NI: 'uintptr_t'}.get(ct, 'uint64_t')
            bits = {I4: 31, I8: 63, NI: 63}.get(ct, 63)
            if name == 'shl':
                expr = f'({ct})(({u}){a} << ({b} & {bits}))'
            elif name == 'shr':
                expr = f'({ct})({a} >> ({b} & {bits}))'
            else:
                expr = f'({ct})(({u}){a} >> ({b} & {bits}))'
            self.push(stack, ct, ('stack', ct), expr)
            return [nxt]
        if name in ('neg', 'not'):
            a, sa = self.pop(stack)
            self.push(stack, sa.ct, sa.sig, f'({sa.ct})({"-" if name == "neg" else "~"}{a})')
            return [nxt]
        if name.startswith('conv.'):
            a, sa = self.pop(stack)
            t = name.replace('conv.ovf.', 'conv.').replace('.un', '')
            tgt = {'conv.i1': ('int8_t', I4), 'conv.u1': ('uint8_t', I4), 'conv.i2': ('int16_t', I4),
                   'conv.u2': ('uint16_t', I4), 'conv.i4': ('int32_t', I4), 'conv.u4': ('uint32_t', I4),
                   'conv.i8': ('int64_t', I8), 'conv.u8': ('uint64_t', I8), 'conv.i': ('intptr_t', NI),
                   'conv.u': ('uintptr_t', NI), 'conv.r4': ('float', R4), 'conv.r8': ('double', R8),
                   'conv.r': ('double', R8)}[t]
            src = a
            if name in ('conv.r.un',) or (name.endswith('.un') and name.startswith('conv.r')):
                src = f'({ {I4: "uint32_t", I8: "uint64_t", NI: "uintptr_t"}.get(sa.ct, "uint64_t") }){a}'
                tgt = ('double', R8)
            if sa.ct.endswith('*') or sa.ct == OBJ:
                src = f'(intptr_t){a}'
            if tgt[1] in (I4, I8, NI) and sa.ct in (R4, R8):
                src = f'rt_f2i<{tgt[0]}>({a})'
                self.push(stack, tgt[1], ('stack', tgt[1]), f'({tgt[1]}){src}')
                return [nxt]
            self.push(stack, tgt[1], ('stack', tgt[1]), f'({tgt[1]})({tgt[0]}){src}')
            return [nxt]
        # --- comparisons
        if name in ('ceq', 'cgt', 'clt', 'cgt.un', 'clt.un'):
            b, sb = self.pop(stack)
            a, sa = self.pop(stack)
            self.push(stack, I4, ('prim', 'bool'), f'(int32_t)({self.compare(name, a, sa, b, sb)})')
            return [nxt]
        # --- branches
        if name in ('br', 'br.s'):
            L.append(f'goto {self.label(arg)};')
            return [arg]
        if name in ('brfalse', 'brfalse.s', 'brtrue', 'brtrue.s'):
            a, sa = self.pop(stack)
            neg = '!' if 'false' in name else ''
            L.append(f'if ({neg}({a})) goto {self.label(arg)};')
            return [nxt, arg]
        if name.split('.')[0] in ('beq', 'bne', 'bge', 'bgt', 'ble', 'blt'):
            b, sb = self.pop(stack)
            a, sa = self.pop(stack)
            base = name.split('.')[0]
            un = '.un' in name
            cmpop = {'beq': 'ceq', 'bne': 'ceq', 'bge': 'clt', 'bgt': 'cgt', 'ble': 'cgt', 'blt': 'clt'}[base]
            negate = base in ('bne', 'bge', 'ble')
            # bge = !(a < b) (with .un meaning unordered for floats), etc.
            if base in ('bge', 'ble'):
                c = self.compare(cmpop + ('' if un else '.un') if sa.ct in (R4, R8) else cmpop + ('.un' if un else ''),
                                 a, sa, b, sb)
            else:
                c = self.compare(cmpop + ('.un' if un else ''), a, sa, b, sb)
            L.append(f'if ({"!" if negate else ""}({c})) goto {self.label(arg)};')
            return [nxt, arg]
        if name == 'switch':
            a, sa = self.pop(stack)
            L.append(f'switch ((uint32_t){a}) {{')
            for k, t in enumerate(arg):
                L.append(f'    case {k}: goto {self.label(t)};')
            L.append('    default: break;')
            L.append('}')
            return [nxt] + list(arg)
        if name == 'ret':
            ret = self.s(self.md.ret)
            if ret == ('prim', 'void'):
                L.append('return;')
            else:
                a, sa = self.pop(stack)
                L.append(f'return {self.conv_to(a, sa, ret)};')
            return []
        if name in ('leave', 'leave.s'):
            stack.clear()
            L.extend(self.leave(arg))
            return [arg]
        if name == 'endfinally':
            L.extend(self.endfinally())
            return []
        if name == 'throw':
            a, sa = self.pop(stack)
            L.append(f'rt_throw({a});')
            return []
        if name == 'rethrow':
            L.append('rt_rethrow();')
            return []
        # --- indirect
        if name.startswith('ldind.'):
            a, sa = self.pop(stack)
            t = name[6:]
            cmap = {'i1': ('int8_t', 'i1'), 'u1': ('uint8_t', 'u1'), 'i2': ('int16_t', 'i2'), 'u2': ('uint16_t', 'u2'),
                    'i4': ('int32_t', 'i4'), 'u4': ('uint32_t', 'u4'), 'i8': ('int64_t', 'i8'), 'i': ('intptr_t', 'i'),
                    'r4': ('float', 'r4'), 'r8': ('double', 'r8'), 'ref': ('obj', 'object')}[t]
            sig = sa.sig[1] if sa.sig and sa.sig[0] in ('byref', 'ptr') and t == 'ref' else ('prim', cmap[1])
            self.push(stack, self.stack_ct(sig), sig, f'*({cmap[0]}*){a}')
            return [nxt]
        if name.startswith('stind.'):
            v, sv = self.pop(stack)
            a, sa = self.pop(stack)
            t = name[6:]
            cty = {'i1': 'int8_t', 'i2': 'int16_t', 'i4': 'int32_t', 'i8': 'int64_t', 'i': 'intptr_t', 'r4': 'float',
                   'r8': 'double', 'ref': 'obj'}[t]
            L.append(f'*({cty}*){a} = ({cty}){v};')
            return [nxt]
        if name in ('ldobj',):
            a, sa = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            self.push(stack, self.stack_ct(t), t, self.cast_to_stack(f'*({ts.cty(t)}*){a}', t))
            return [nxt]
        if name in ('stobj',):
            v, sv = self.pop(stack)
            a, sa = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            L.append(f'*({ts.cty(t)}*){a} = {self.conv_to(v, sv, t)};')
            return [nxt]
        if name == 'cpobj':
            s_, _ = self.pop(stack)
            d_, _ = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            L.append(f'*({ts.cty(t)}*){d_} = *({ts.cty(t)}*){s_};')
            return [nxt]
        if name == 'initobj':
            a, sa = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            L.append(f'*({ts.cty(t)}*){a} = {ts.cty(t)}{{}};')
            return [nxt]
        if name == 'sizeof':
            t = self.s(self.md.asm.resolve_token(arg))
            self.push(stack, I4, ('prim', 'i4'), f'(int32_t)sizeof({ts.cty(t)})')
            return [nxt]
        if name in ('cpblk', 'initblk'):
            n, _ = self.pop(stack)
            b, _ = self.pop(stack)
            a, _ = self.pop(stack)
            L.append(f'memcpy((void*){a}, (const void*){b}, (size_t){n});' if name == 'cpblk' else
                     f'memset((void*){a}, (int){b}, (size_t){n});')
            return [nxt]
        if name == 'localloc':
            n, _ = self.pop(stack)
            self.push(stack, 'uint8_t*', ('ptr', ('prim', 'u1')), f'(uint8_t*)__builtin_alloca((size_t){n})')
            return [nxt]
        # --- fields
        if name in ('ldfld', 'ldflda', 'stfld', 'ldsfld', 'ldsflda', 'stsfld'):
            return self.field_op(name, arg, nxt, stack)
        # --- objects
        if name in ('call', 'callvirt', 'newobj'):
            return self.tr.calls.emit(self, name, arg, nxt, stack)
        if name in ('ldftn', 'ldvirtftn'):
            obj_v = None
            if name == 'ldvirtftn':
                obj_v, _ = self.pop(stack)
            target = self.md.asm.resolve_token(arg)
            fn = self.tr.calls.function_pointer(self, target, obj_v)
            self.push(stack, NI, ('fnptr',), f'(intptr_t)(void*)({fn[0]})', extra=fn)
            return [nxt]
        if name == 'ldtoken':
            target = self.md.asm.resolve_token(arg)
            if isinstance(target, tuple):
                t = self.s(target)
                self.push(stack, 'intptr_t', ('typehandle', t), f'(intptr_t){self.tr.type_ref(t)}')
            elif isinstance(target, FieldDef):
                self.push(stack, 'intptr_t', ('fieldhandle', target), f'(intptr_t){self.tr.field_data(target)}')
            else:
                raise Unsupported(f'ldtoken {target}')
            return [nxt]
        if name == 'box':
            v, sv = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            if t[0] == 'class' and ts.u.get(t[1]) is None and sv.ct in (I4, I8):
                t = ('prim', 'i4' if sv.ct == I4 else 'i8')  # box of an enum the Switch build stripped
            self.push(stack, OBJ, t if not ts.is_vt(t) else ('boxed', t), self.tr.box_expr(v, sv, t, self))
            return [nxt]
        if name in ('unbox.any', 'unbox'):
            v, sv = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            if not ts.is_vt(t):
                self.push(stack, OBJ, t, self.tr.cast_expr(v, t, self, throw=True))
            elif name == 'unbox':
                self.push(stack, ts.cty(t) + '*', ('byref', t), self.tr.unbox_ptr(v, t, self))
            else:
                self.push(stack, self.stack_ct(t), t, self.cast_to_stack(f'*{self.tr.unbox_ptr(v, t, self)}', t))
            return [nxt]
        if name in ('isinst', 'castclass'):
            v, sv = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            self.push(stack, OBJ, t, self.tr.cast_expr(v, t, self, throw=(name == 'castclass')))
            return [nxt]
        # --- arrays
        if name == 'newarr':
            n, _ = self.pop(stack)
            t = self.s(self.md.asm.resolve_token(arg))
            self.push(stack, OBJ, ('sz', t), f'rt_new_array({self.tr.array_elem_type_ref(t)}, (intptr_t){n}, '
                                             f'sizeof({ts.cty(t)}))')
            return [nxt]
        if name == 'ldlen':
            a, _ = self.pop(stack)
            self.push(stack, NI, ('prim', 'i'), f'rt_ldlen({a})')
            return [nxt]
        if name.startswith('ldelem') or name.startswith('stelem') or name == 'ldelema':
            return self.array_op(name, arg, nxt, stack)
        if name == 'mkrefany' or name == 'refanyval' or name == 'refanytype' or name == 'arglist':
            raise Unsupported(name)
        if name == 'jmp' or name == 'calli':
            raise Unsupported(name)
        if name == 'ckfinite':
            return [nxt]
        if name == 'endfilter':
            v, sv = self.pop(stack)
            c = [c for c in self.body.clauses if c[0] == 1 and c[5] <= self.cur < c[3]]
            if not c:
                raise Unsupported('endfilter outside a filter')
            self.vars['s0_obj'] = 'obj'
            L.append(f'if ({v}) {{ s0_obj = rt_exception(); goto {self.label(c[0][3])}; }}')
            L.append('rt_rethrow();')
            return []
        raise Unsupported(f'opcode {name}')

    # ---------------------------------------------------------- value conversions
    def cast_to_stack(self, expr, sig):
        """Widen a storage-typed value to its stack type."""
        sig = norm(sig)
        if sig[0] == 'prim' and sig[1] in INT_PRIMS:
            return f'(int32_t){expr}'
        if self.ts.is_enum_sig(sig):
            return f'({self.stack_ct(sig)}){expr}'
        return expr

    def conv_to(self, v, sv, sig):
        """Convert a stack value to a storage location type."""
        sig = norm(sig)
        ct = self.ts.cty(sig)
        if sv.ct == ct:
            return v
        if ct == 'obj' or ct.endswith('*') or sv.ct.endswith('*'):
            return f'({ct}){v}'
        if sv.ct in (I4, I8, NI, R4, R8) and ct in ('bool', 'char16_t', 'int8_t', 'uint8_t', 'int16_t', 'uint16_t',
                                                     'int32_t', 'uint32_t', 'int64_t', 'uint64_t', 'intptr_t',
                                                     'uintptr_t', 'float', 'double'):
            if ct == 'bool':
                return f'(bool)({v} != 0)'
            return f'({ct}){v}'
        return v

    def arith_type(self, a, b):
        if a.endswith('*'):
            return a
        if b.endswith('*'):
            return b
        if R8 in (a, b):
            return R8
        if R4 in (a, b):
            return R4
        if I8 in (a, b):
            return I8
        if NI in (a, b):
            return NI
        return I4

    def compare(self, op, a, sa, b, sb):
        flt = sa.ct in (R4, R8) or sb.ct in (R4, R8)
        if op in ('ceq', 'ceq.un'):  # bne.un arrives as ceq.un: plain (in)equality; !(a == b) is also NaN-correct
            if sa.ct == OBJ or sb.ct == OBJ or sa.ct.endswith('*'):
                return f'(void*){a} == (void*){b}'
            return f'{a} == {b}'
        base, un = op[:3], op.endswith('.un')
        sym = '>' if base == 'cgt' else '<'
        if flt:
            if un:  # unordered: true if either is NaN
                inv = '<=' if sym == '>' else '>='
                return f'!({a} {inv} {b})'
            return f'{a} {sym} {b}'
        if sa.ct == OBJ or sb.ct == OBJ or sa.ct.endswith('*') or sb.ct.endswith('*'):
            return f'(uintptr_t){a} {sym} (uintptr_t){b}'
        if un:
            u = {I4: 'uint32_t', I8: 'uint64_t', NI: 'uintptr_t'}.get(self.arith_type(sa.ct, sb.ct), 'uint64_t')
            return f'({u}){a} {sym} ({u}){b}'
        return f'{a} {sym} {b}'

    # ---------------------------------------------------------- fields
    def field_op(self, name, tok, nxt, stack):
        L = self.lines
        f = self.md.asm.resolve_token(tok)
        acc = self.tr.field_access(f, self)  # (static, owner_sig, field_sig, fn(objexpr)->lvalue)
        static, fsig, lval = acc
        if name == 'ldsfld':
            self.push(stack, self.stack_ct(fsig), fsig, self.cast_to_stack(lval(None), fsig))
        elif name == 'ldsflda':
            self.push(stack, self.ts.cty(fsig) + '*', ('byref', fsig), f'&{lval(None)}')
        elif name == 'stsfld':
            v, sv = self.pop(stack)
            L.append(f'{lval(None)} = {self.conv_to(v, sv, fsig)};')
        elif name in ('ldfld', 'ldflda'):
            o, so = self.pop(stack)
            if static:
                target = lval(None)
            else:
                target = lval((o, so))
            if name == 'ldfld':
                self.push(stack, self.stack_ct(fsig), fsig, self.cast_to_stack(target, fsig))
            else:
                self.push(stack, self.ts.cty(fsig) + '*', ('byref', fsig), f'&{target}')
        elif name == 'stfld':
            v, sv = self.pop(stack)
            o, so = self.pop(stack)
            target = lval(None) if static else lval((o, so))
            L.append(f'{target} = {self.conv_to(v, sv, fsig)};')
        return [nxt]

    # ---------------------------------------------------------- arrays
    def array_op(self, name, tok, nxt, stack):
        L = self.lines
        ts = self.ts
        typed = {'i1': 'i1', 'u1': 'u1', 'i2': 'i2', 'u2': 'u2', 'i4': 'i4', 'u4': 'u4', 'i8': 'i8', 'i': 'i',
                 'r4': 'r4', 'r8': 'r8', 'ref': None}
        if name.startswith('stelem'):
            v, sv = self.pop(stack)
            i, _ = self.pop(stack)
            a, sa = self.pop(stack)
            suffix = name[7:] if '.' in name else None
            if name == 'stelem':
                t = self.s(self.md.asm.resolve_token(tok))
            elif suffix == 'ref':
                t = sa.sig[1] if sa.sig and sa.sig[0] == 'sz' else ('prim', 'object')
                if not ts.is_ref(t):
                    t = ('prim', 'object')
            else:
                t = ('prim', typed[suffix])
            ct = ts.cty(t)
            L.append(f'RT_ELEM({a}, {i}, {ct}) = {self.conv_to(v, sv, t)};')
            return [nxt]
        i, _ = self.pop(stack)
        a, sa = self.pop(stack)
        if name in ('ldelem', 'ldelema'):
            t = self.s(self.md.asm.resolve_token(tok))
        elif name == 'ldelem.ref':
            t = sa.sig[1] if sa.sig and sa.sig[0] == 'sz' else ('prim', 'object')
        else:
            t = ('prim', typed[name[7:]])
        ct = ts.cty(t)
        if name == 'ldelema':
            self.push(stack, ct + '*', ('byref', t), f'&RT_ELEM({a}, {i}, {ct})')
        else:
            self.push(stack, self.stack_ct(t), t, self.cast_to_stack(f'RT_ELEM({a}, {i}, {ct})', t))
        return [nxt]


def cstr(s):
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
