"""Harmony patch discovery (attributes + TargetMethod evaluation) and hook wrapper generation."""
from .model import MethodDef, MemberRef, sig_str, subst
from .translate import decode, cstr
from .types import cid, norm

PATCH_KINDS = {'Prefix': 'HarmonyLib.HarmonyPrefix', 'Postfix': 'HarmonyLib.HarmonyPostfix',
               'Transpiler': 'HarmonyLib.HarmonyTranspiler', 'Finalizer': 'HarmonyLib.HarmonyFinalizer'}
METHOD_TYPES = {0: 'Normal', 1: 'Getter', 2: 'Setter', 3: 'Constructor', 4: 'StaticConstructor', 5: 'Enumerator'}


def attr_type_key(v):
    """('type', 'Name+Nested, Assembly, ...') -> 'Name/Nested'."""
    if not v or v[0] != 'type' or not v[1]:
        return None
    return v[1].split(',')[0].strip().replace('+', '/')


class Target:
    def __init__(self):
        self.type = None
        self.method = None
        self.kind = 'Normal'
        self.argtypes = None

    def merge(self, args):
        for a in args:
            if isinstance(a, tuple) and a and a[0] == 'type':
                self.type = attr_type_key(a) or self.type
            elif isinstance(a, str):
                self.method = a
            elif isinstance(a, int) and not isinstance(a, bool):
                self.kind = METHOD_TYPES.get(a, 'Normal')
            elif isinstance(a, list):
                self.argtypes = [attr_type_key(x) for x in a if isinstance(x, tuple)]

    def copy(self):
        t = Target()
        t.type, t.method, t.kind, t.argtypes = self.type, self.method, self.kind, self.argtypes
        return t


class Patch:
    def __init__(self, asm, cls, target_md, kind, patch_md, index):
        self.asm, self.cls, self.target, self.kind, self.patch_md, self.index = asm, cls, target_md, kind, patch_md, index


class HarmonyPass:
    def __init__(self, tr):
        self.tr = tr
        self.u = tr.u
        self.patches = []
        self.classes = []  # (asm_index, TypeDef) patch classes, index == patch-class id
        self.manual_sites = {}  # (id(MethodDef), il offset of Harmony.Patch call) -> [patch-class ids]
        self._unresolved = set()

    # ---------------------------------------------------------- discovery
    def scan(self, asm):
        for td in asm.typedefs:
            cls_attrs = [a for a in td.attrs if a[0] == 'HarmonyLib.HarmonyPatch']
            method_targets = {}
            for md in td.methods:
                margs = [a for a in md.attrs if a[0] == 'HarmonyLib.HarmonyPatch']
                if margs:
                    method_targets[md] = margs
            if not cls_attrs and not method_targets and not self.has_patch_methods(td):
                continue
            if not cls_attrs and not method_targets:
                continue
            base = Target()
            for _, args, named in cls_attrs:
                base.merge(args or [])
            class_id = len(self.classes)
            self.classes.append((self.tr.asm_index(asm), td))
            targets = []
            tm = td.method('TargetMethod') or td.method('TargetMethods')
            if tm is not None:
                try:
                    resolved = self.eval_target_method(tm)
                except Exception as e:
                    self.tr.report.add('error', f'{td.key}.{tm.name}: cannot evaluate at build time ({e})', asm.name)
                    resolved = []
                targets = [(r, None) for r in resolved]
            elif base.method or (base.type and not method_targets) or (base.type and base.kind != 'Normal'):
                targets = [(self.resolve(base, td, asm), None)]
            for md, margs in method_targets.items():
                t = base.copy()
                for _, args, named in margs:
                    t.merge(args or [])
                targets.append((self.resolve(t, td, asm), md))
            for target_md, only in targets:
                if target_md is None:
                    continue
                for md in td.methods:
                    kind = self.patch_kind(md)
                    if kind is None or (only is not None and md is not only):
                        continue
                    if kind in ('Transpiler', 'Finalizer'):
                        self.tr.report.add('error', f'{td.key}.{md.name}: Harmony {kind}s cannot run on Switch '
                                                    f'(target {target_md})', asm.name)
                        continue
                    self.patches.append(Patch(asm, td, target_md, kind, md, class_id))

    # ---------------------------------------------------------- harmony.Patch(...) in code
    def scan_manual(self, asm):
        """Find `harmony.Patch(original, prefix, postfix, ...)` calls whose arguments are static."""
        for td in asm.typedefs:
            for md in td.methods:
                body = md.body()
                if body is None or b'\x28' not in body.code and b'\x6f' not in body.code:
                    continue
                try:
                    self._scan_method(asm, td, md, body)
                except Exception as e:
                    self.tr.report.add('warn', f'{md}: could not analyse Harmony.Patch calls ({e})', asm.name)

    def _calls_patch(self, md):
        body = md.body()
        if body is None:
            return False
        for at, name, kind, arg, nxt in decode(body.code):
            if name in ('call', 'callvirt'):
                try:
                    t = md.asm.resolve_token(arg)
                except Exception:
                    continue
                if getattr(t, 'name', None) == 'Patch' and getattr(t, 'parent', (None, None))[1] == 'HarmonyLib.Harmony':
                    return True
        return False

    def _scan_method(self, asm, td, md, body, argvals=None, depth=0):
        ins = decode(body.code)
        stack, locs = [], {}
        simple_push = ('ldarg', 'ldarga', 'ldsfld', 'ldsflda', 'ldftn', 'ldnull', 'ldloca', 'ldc.i8', 'ldc.r4',
                       'ldc.r8', 'sizeof', 'arglist')
        un = ('ldfld', 'ldflda', 'ldlen', 'neg', 'not', 'isinst', 'castclass', 'box', 'unbox', 'unbox.any',
              'ldobj', 'ldvirtftn', 'localloc', 'ckfinite', 'refanyval', 'mkrefany')
        for at, name, kind, arg, nxt in ins:
            def pop(n):
                vals = []
                for _ in range(n):
                    vals.append(stack.pop() if stack else None)
                return vals[::-1]
            base = name.split('.')[0]
            if name in ('nop', 'volatile.', 'tail.', 'readonly.', 'constrained.', 'unaligned.', 'no.', 'break'):
                continue
            if name == 'ldtoken':
                t = asm.resolve_token(arg)
                stack.append(('type', t[1]) if isinstance(t, tuple) and t[0] in ('class', 'vt', 'gi') else
                             ('type', __import__('silkport.model', fromlist=['PRIM_KEYS']).PRIM_KEYS.get(t[1]))
                             if isinstance(t, tuple) and t[0] == 'prim' else None)
            elif name == 'ldstr':
                stack.append(('str', asm.us(arg & 0xFFFFFF)))
            elif name.startswith('ldc.i4'):
                stack.append(('int', arg if kind != 'n' else (-1 if name.endswith('m1') else int(name[7:]))))
            elif name.startswith('ldloc') and not name.startswith('ldloca'):
                i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
                stack.append(locs.get(i))
            elif name.startswith('stloc'):
                i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
                locs[i] = pop(1)[0]
            elif name.startswith('ldarg') and not name.startswith('ldarga'):
                i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
                stack.append(argvals[i] if argvals and i < len(argvals) else None)
            elif name.startswith(simple_push) or name in simple_push:
                stack.append(None)
            elif name == 'dup':
                stack.append(stack[-1] if stack else None)
            elif name == 'newarr':
                n = pop(1)[0]
                stack.append(['arr', [None] * (n[1] if n and n[0] == 'int' and 0 <= n[1] < 64 else 0)])
            elif name.startswith('stelem'):
                a, i, v = pop(3)
                if a and a[0] == 'arr' and i and i[0] == 'int' and i[1] < len(a[1]):
                    a[1][i[1]] = v
            elif name.startswith('ldelem'):
                pop(2)
                stack.append(None)
            elif name in ('call', 'callvirt', 'newobj'):
                target = asm.resolve_token(arg)
                if not hasattr(target, 'params'):
                    stack.clear()
                    continue
                n = len(target.params) + (1 if target.has_this and name != 'newobj' else 0)
                argv = pop(n)
                owner = target.parent[1] if hasattr(target, 'parent') else target.owner.key
                if owner == 'HarmonyLib.Harmony' and target.name == 'Patch':
                    self._register_manual(asm, td, md, at, argv[1:] if name != 'newobj' else argv)
                    stack.append(None)
                    continue
                # helper in the mod that itself calls Harmony.Patch: analyse it with these arguments
                if isinstance(target, MethodDef) and target.asm is asm and depth < 3 and target is not md and \
                        self._calls_patch(target):
                    self._scan_method(asm, target.owner, target, target.body(), argv, depth + 1)
                    if target.ret != ('prim', 'void'):
                        stack.append(None)
                    continue
                val = None
                try:
                    if owner == 'HarmonyLib.HarmonyMethod' and name == 'newobj':
                        val = self._harmony_method(argv)
                    else:
                        val = self.eval_call(target, argv)
                except Exception:
                    val = None
                if target.ret != ('prim', 'void') or name == 'newobj':
                    stack.append(val)
            elif name.startswith(('stfld', 'stind', 'stobj', 'cpobj')) or base in ('beq', 'bne', 'bge', 'bgt',
                                                                                    'ble', 'blt'):
                pop(2)
            elif name.startswith(('starg', 'stsfld', 'pop', 'brtrue', 'brfalse', 'switch', 'initobj')):
                pop(1)
            elif name.startswith(un) or name.startswith(('ldind', 'conv')):
                pop(1)
                stack.append(None)
            elif base in ('add', 'sub', 'mul', 'div', 'rem', 'and', 'or', 'xor', 'shl', 'shr', 'ceq', 'cgt',
                          'clt'):
                pop(2)
                stack.append(None)
            elif name in ('br', 'br.s'):
                continue
            else:  # ret, leave, throw, endfinally, ... : the stack is not carried across
                stack.clear()

    def report_unresolved(self):
        for key, at, mdname, asm_name in sorted(self._unresolved, key=lambda x: x[2]):
            if (key, at) not in self.manual_sites:
                self.tr.report.add('error', f'{mdname}: Harmony.Patch target could not be determined at build time',
                                   asm_name)

    def _harmony_method(self, argv):
        if len(argv) >= 1 and argv[0] and argv[0][0] == 'method':
            return ('hm', argv[0][1])
        if len(argv) >= 2 and argv[0] and argv[0][0] == 'type' and argv[1] and argv[1][0] == 'str':
            t = self.u.get(argv[0][1])
            types = None
            if len(argv) >= 3 and argv[2] and argv[2][0] == 'arr':
                types = [x[1] if x else None for x in argv[2][1]]
            m = self.find_quiet(t, argv[1][1], types) if t else None
            return ('hm', m) if m else None
        return None

    def _register_manual(self, asm, td, md, at, args):
        original = args[0] if args else None
        if not original or original[0] != 'method':
            if (id(md), at) not in self.manual_sites:  # may still be resolved through another caller
                self._unresolved.add((id(md), at, str(md), asm.name))
            return
        kinds = ['Prefix', 'Postfix', 'Transpiler', 'Finalizer', 'ILManipulator']
        site = len(self.classes)
        self.classes.append((self.tr.asm_index(asm), td))
        any_patch = False
        for kind, v in zip(kinds, args[1:]):
            if not v or v[0] != 'hm':
                continue
            if kind not in ('Prefix', 'Postfix'):
                self.tr.report.add('error', f'{md}: Harmony {kind} on {original[1]} cannot run on Switch', asm.name)
                continue
            self.patches.append(Patch(asm, td, original[1], kind, v[1], site))
            any_patch = True
        if any_patch:
            self.manual_sites.setdefault((id(md), at), []).append(site)

    def has_patch_methods(self, td):
        return any(self.patch_kind(m) for m in td.methods)

    def patch_kind(self, md):
        for kind, attr in PATCH_KINDS.items():
            if any(a[0] == attr for a in md.attrs):
                return kind
        if md.name in PATCH_KINDS and md.static:
            return md.name
        return None

    def resolve(self, t, td, asm):
        if not t.type:
            self.tr.report.add('error', f'{td.key}: HarmonyPatch without target type', asm.name)
            return None
        ttd = self.u.get(t.type)
        if ttd is None:
            self.tr.report.add('error', f'{td.key}: patch target type {t.type} not found', asm.name)
            return None
        name = t.method
        if t.kind == 'Getter':
            name = 'get_' + name
        elif t.kind == 'Setter':
            name = 'set_' + name
        elif t.kind == 'Constructor':
            name = '.ctor'
        elif t.kind == 'StaticConstructor':
            name = '.cctor'
        elif t.kind == 'Enumerator':
            # compiler-generated iterator class <Name>d__N.MoveNext
            for n in ttd.nested:
                if n.name.startswith(f'<{t.method}>d__'):
                    m = n.method('MoveNext')
                    if m:
                        return m
            self.tr.report.add('error', f'{td.key}: iterator for {t.type}.{t.method} not found', asm.name)
            return None
        return self.find_method(ttd, name, t.argtypes, td, asm)

    def find_method(self, ttd, name, argtypes, td, asm):
        cands = [m for m in ttd.methods if m.name == name]
        if argtypes is not None:
            cands = [m for m in cands if len(m.params) == len(argtypes) and
                     all(self.param_matches(p, a) for p, a in zip(m.params, argtypes))]
        if not cands:
            self.tr.report.add('error', f'{td.key}: patch target {ttd.key}.{name} not found', asm.name)
            return None
        if len(cands) > 1:
            self.tr.report.add('warn', f'{td.key}: {ttd.key}.{name} is overloaded; patching the first overload')
        return cands[0]

    def param_matches(self, p, key):
        p = norm(p)
        if p[0] == 'byref':
            p = p[1]
        s = sig_str(p)
        from .model import PRIM_KEYS
        if p[0] == 'prim':
            s = PRIM_KEYS.get(p[1], s)
        return s == key or s.split('<')[0] == key.split('`')[0]

    # ---------------------------------------------------------- TargetMethod() evaluation
    def eval_target_method(self, md):
        body = md.body()
        asm = md.asm
        ins = decode(body.code)
        by_off = {i[0]: k for k, i in enumerate(ins)}
        stack, locs = [], {}
        k, steps = 0, 0
        results = []
        while k < len(ins) and steps < 2000:
            steps += 1
            at, name, kind, arg, nxt = ins[k]
            jump = None
            if name in ('nop',):
                pass
            elif name == 'ldtoken':
                t = asm.resolve_token(arg)
                stack.append(('type', t[1]) if isinstance(t, tuple) else None)
            elif name == 'ldstr':
                stack.append(('str', asm.us(arg & 0xFFFFFF)))
            elif name == 'ldnull':
                stack.append(None)
            elif name.startswith('ldc.i4'):
                stack.append(('int', arg if kind != 'n' else (-1 if name.endswith('m1') else int(name[7:]))))
            elif name.startswith('stloc'):
                i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
                locs[i] = stack.pop()
            elif name.startswith('ldloc'):
                i = int(name[6]) if name[5:6] == '.' and name[6:].isdigit() else arg
                stack.append(locs.get(i))
            elif name == 'newarr':
                n = stack.pop()
                stack.append(['arr', [None] * (n[1] if n else 0)])
            elif name == 'dup':
                stack.append(stack[-1])
            elif name.startswith('stelem'):
                v, i, a = stack.pop(), stack.pop(), stack.pop()
                if a and a[0] == 'arr' and i:
                    a[1][i[1]] = v
            elif name in ('call', 'callvirt', 'newobj'):
                target = asm.resolve_token(arg)
                nparams = len(target.params) + (1 if target.has_this and name != 'newobj' else 0)
                argv = [stack.pop() for _ in range(nparams)][::-1]
                stack_val = self.eval_call(target, argv)
                if target.ret != ('prim', 'void') or name == 'newobj':
                    stack.append(stack_val)
            elif name in ('brfalse', 'brfalse.s', 'brtrue', 'brtrue.s'):
                v = stack.pop()
                truthy = bool(v) and v != ('int', 0) and v != ('bool', False)
                if truthy == ('true' in name):
                    jump = arg
            elif name in ('br', 'br.s', 'leave', 'leave.s'):
                jump = arg
            elif name == 'ret':
                v = stack.pop() if stack else None
                if v and v[0] == 'method':
                    results.append(v[1])
                elif v and v[0] == 'list':
                    results += [x[1] for x in v[1] if x and x[0] == 'method']
                break
            elif name == 'pop':
                stack.pop()
            elif name in ('ceq',):
                b, a = stack.pop(), stack.pop()
                stack.append(('int', int(a == b)))
            else:
                raise ValueError(f'unsupported opcode {name} in TargetMethod')
            k = by_off[jump] if jump is not None else k + 1
        return [r for r in results if r is not None]

    def eval_call(self, target, argv):
        owner = target.parent[1] if isinstance(target, MemberRef) else target.owner.key
        name = target.name
        u = self.u

        def ty(v):
            return v[1] if v and v[0] == 'type' else None

        def s(v):
            return v[1] if v and v[0] == 'str' else None

        def types_arr(v):
            if v and v[0] == 'arr':
                return [ty(x) for x in v[1]]
            return None
        if owner == 'System.Type' and name == 'GetTypeFromHandle':
            return argv[0]
        if owner == 'System.Type' and name in ('op_Equality', 'op_Inequality'):
            eq = argv[0] == argv[1]
            return ('int', int(eq if name == 'op_Equality' else not eq))
        if owner in ('System.Type', 'System.Reflection.TypeInfo') and name == 'GetNestedType':
            t = u.get(ty(argv[0]))
            for n in (t.nested if t else []):
                if n.name == s(argv[1]):
                    return ('type', n.key)
            return None
        if owner == 'HarmonyLib.AccessTools' and name in ('TypeByName',):
            k = s(argv[0])
            return ('type', k.replace('+', '/')) if k and u.get(k.replace('+', '/')) else None
        if owner == 'HarmonyLib.AccessTools' and name == 'Inner':
            t = u.get(ty(argv[0]))
            for n in (t.nested if t else []):
                if n.name == s(argv[1]):
                    return ('type', n.key)
            return None
        if (owner == 'HarmonyLib.AccessTools' and name in ('Method', 'DeclaredMethod')) or \
                (owner == 'System.Type' and name == 'GetMethod'):
            t = u.get(ty(argv[0]))
            mname = s(argv[1])
            argtypes = types_arr(argv[2]) if len(argv) > 2 and argv[2] and argv[2][0] == 'arr' else None
            if t is None or mname is None:
                return None
            if '.' in mname and owner == 'HarmonyLib.AccessTools' and ':' in mname:
                return None
            while t is not None:
                m = self.find_quiet(t, mname, argtypes)
                if m:
                    return ('method', m)
                if name == 'DeclaredMethod' or t.extends is None:
                    break
                t = u.get(t.extends[1])
            return None
        if owner == 'HarmonyLib.AccessTools' and name in ('PropertyGetter', 'DeclaredPropertyGetter',
                                                           'PropertySetter', 'DeclaredPropertySetter'):
            t = u.get(ty(argv[0]))
            pre = 'get_' if 'Getter' in name else 'set_'
            while t is not None:
                m = t.method(pre + (s(argv[1]) or ''))
                if m:
                    return ('method', m)
                if t.extends is None:
                    break
                t = u.get(t.extends[1])
            return None
        if owner == 'HarmonyLib.AccessTools' and name in ('Constructor', 'DeclaredConstructor'):
            t = u.get(ty(argv[0]))
            argtypes = types_arr(argv[1]) if len(argv) > 1 else None
            m = self.find_quiet(t, '.ctor', argtypes) if t else None
            return ('method', m) if m else None
        if owner == 'System.Type' and name == 'GetProperty':
            return ('prop', ty(argv[0]), s(argv[1]))
        if owner == 'System.Reflection.PropertyInfo' and name in ('GetGetMethod', 'get_GetMethod',
                                                                  'GetSetMethod', 'get_SetMethod'):
            p = argv[0]
            if p and p[0] == 'prop':
                t = u.get(p[1])
                m = t.method(('get_' if 'Get' in name else 'set_') + p[2]) if t else None
                return ('method', m) if m else None
            return None
        raise ValueError(f'call {owner}::{name}')

    def find_quiet(self, t, name, argtypes):
        for m in t.methods:
            if m.name != name:
                continue
            if argtypes is None or (len(m.params) == len(argtypes) and
                                    all(self.param_matches(p, a) for p, a in zip(m.params, argtypes))):
                return m
        return None

    # ---------------------------------------------------------- wrapper generation
    def generate(self):
        """Emit hook wrappers; returns list of (hook_name, rva)."""
        tr = self.tr
        ts = tr.ts
        by_target = {}
        for p in self.patches:
            by_target.setdefault(id(p.target), []).append(p)
        hooks = []
        lines = []
        lines.append(f'extern "C" rt_patch_class silk_patch_classes[] = {{')
        for asm_i, td in self.classes:
            info = tr.mod_type(tr.owner_sig(td, ()))
            lines.append(f'    {{{asm_i}, &D_{info.cname}, false}},')
        lines.append('    {-1, nullptr, false}};')
        lines.append(f'extern "C" const uint32_t silk_patch_class_count = {len(self.classes)};')
        for _, plist in by_target.items():
            target = plist[0].target
            key = target.owner.key
            if tr.ts.origin(key) != 'game':
                tr.report.add('error', f'patch target {target} is not part of the Switch game code')
                continue
            dm = tr.db.method(key, target.name, target.params, static=target.static)
            if dm is None or not dm['rva']:
                tr.report.add('error', f'patch target {target} has no native code on Switch (stripped, generic or '
                                       f'inlined); the patch cannot be applied')
                continue
            hname = f'P{len(hooks)}_{cid(key.split("/")[-1].split(".")[-1], 20)}_{cid(target.name, 20)}'
            try:
                lines += self.wrapper(hname, target, plist)
            except Exception as e:
                tr.report.add('error', f'cannot build hook for {target}: {e}')
                continue
            hooks.append((hname, dm['rva'], str(target)))
        return hooks, lines

    def wrapper(self, hname, target, plist):
        tr = self.tr
        ts = tr.ts
        own_sig = tr.owner_sig(target.owner, ())
        this_ct = (ts.cty(own_sig) + '*') if ts.is_vt(own_sig) else 'obj'
        params = []
        names = []
        if target.has_this:
            params.append(f'{this_ct} self')
            names.append('self')
        for i, p in enumerate(target.params):
            params.append(f'{ts.cty(norm(p))} a{i}')
            names.append(f'a{i}')
        params.append('const void* mi')
        ret = norm(target.ret)
        rct = ts.cty(ret)
        void = ret == ('prim', 'void')
        out = [f'SILK_HOOK_OPT({hname}, {rct}, {", ".join(params)}) {{']
        cond = ' || '.join(f'silk_patch_classes[{p.index}].applied' for p in plist)
        call_orig = f'orig_{hname}({", ".join(names + ["mi"])})'
        out.append(f'    if (!rt_ready() || !({cond})) {{ {"" if void else "return "}{call_orig};{" return;" if void else ""} }}')
        if not void:
            out.append(f'    {rct} __result{{}};')
        out.append('    bool __run = true;')
        out.append(f'    static bool __seen; if (!__seen) {{ __seen = true; silk::log("patch active: {cstr(str(target))}"); }}')
        states = {}
        for p in plist:
            for i, pn in enumerate(p.patch_md.param_names):
                if pn == '__state':
                    st = norm(p.patch_md.params[i])
                    st = st[1] if st[0] == 'byref' else st
                    states[p.index] = ts.cty(st)
        for idx, ct in states.items():
            out.append(f'    {ct} __state{idx}{{}};')
        out.append(f'    const char* __saved_ctx = g_rt_context; g_rt_context = "Harmony patch on {cstr(str(target))}";')
        out.append('    RT_GUARD_BEGIN')
        for p in sorted([p for p in plist if p.kind == 'Prefix'], key=lambda p: p.index):
            fn = tr.fn(p.patch_md)
            args = self.patch_args(p, target, ret)
            pret = norm(p.patch_md.ret)
            call = f'{fn}({", ".join(args + ["nullptr"])})'
            out.append(f'    if (silk_patch_classes[{p.index}].applied) {{')
            if pret == ('prim', 'bool'):
                out.append(f'        if (!{call}) __run = false;')
            else:
                out.append(f'        {call};')
            out.append('    }')
        out.append('    RT_GUARD_END')
        if void:
            out.append(f'    if (__run) {call_orig};')
        else:
            out.append(f'    if (__run) __result = {call_orig};')
        out.append('    RT_GUARD_BEGIN')
        for p in sorted([p for p in plist if p.kind == 'Postfix'], key=lambda p: p.index):
            fn = tr.fn(p.patch_md)
            args = self.patch_args(p, target, ret)
            pret = norm(p.patch_md.ret)
            call = f'{fn}({", ".join(args + ["nullptr"])})'
            out.append(f'    if (silk_patch_classes[{p.index}].applied) {{')
            if not void and pret == ret:
                out.append(f'        __result = {call};')
            else:
                out.append(f'        {call};')
            out.append('    }')
        out.append('    RT_GUARD_END')
        out.append('    g_rt_context = __saved_ctx;')
        if not void:
            out.append('    return __result;')
        out.append('}')
        return out

    def patch_args(self, p, target, ret):
        tr = self.tr
        ts = tr.ts
        out = []
        tnames = target.param_names
        for i, (pn, psig) in enumerate(zip(p.patch_md.param_names, p.patch_md.params)):
            psig = norm(psig)
            byref = psig[0] == 'byref'
            if pn == '__instance':
                out.append('self' if target.has_this else 'nullptr')
            elif pn == '__result':
                out.append('&__result' if byref else '__result')
            elif pn == '__state':
                out.append(f'&__state{p.index}' if byref else f'__state{p.index}')
            elif pn == '__runOriginal':
                out.append('__run')
            elif pn in ('__originalMethod', '__args'):
                tr.report.add('warn', f'{p.patch_md}: {pn} is not available on Switch (passed null)')
                out.append('nullptr')
            elif pn.startswith('___'):
                fname = pn[3:]
                df = tr.db.field(target.owner.key, fname)
                if df is None:
                    raise ValueError(f'field {fname} not on Switch')
                inner = psig[1] if byref else psig
                ct = ts.cty(inner)
                ptr = f'(({ct}*)((uint8_t*)self + {df["offset"]:#x}))'
                out.append(ptr if byref else f'*{ptr}')
            elif pn.startswith('__') and pn[2:].isdigit():
                k = int(pn[2:])
                out.append(f'&a{k}' if byref else f'a{k}')
            elif pn in tnames:
                k = tnames.index(pn)
                tgt_byref = norm(target.params[k])[0] == 'byref'
                if byref and not tgt_byref:
                    out.append(f'&a{k}')
                elif not byref and tgt_byref:
                    out.append(f'*a{k}')
                else:
                    out.append(f'a{k}')
            else:
                raise ValueError(f'patch parameter {pn} does not match the target')
        return out
