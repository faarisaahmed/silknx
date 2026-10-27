// SilkNX runtime core: memory, strings, types, objects, dispatch, exceptions.
#include "rt.h"

extern "C" uint64_t rt_thread_id();
extern "C" char __bss_end[];

using namespace rva;

namespace {

bool g_ready;
obj g_objarr_type;     // typeof(object)
void *g_objarr_klass;  // klass of object[]
// Converted objects (and other raw storage) are allocated as object[] so the GC traces every slot, then
// re-tagged with long[]'s class: Unity's asset liveness scan (Resources.UnloadUnusedAssets) walks object[]
// elements as objects and would dereference our raw data, but it skips arrays without references. The GC
// keeps tracing by allocation kind, not by the current class (verified by rt_selftest_gc).
void *g_mod_klass;
void *g_string_klass;
obj g_type_type;       // typeof(System.Type)

// Il2CppClass / MethodInfo layout, calibrated at boot.
uint32_t MI_KLASS = 0x20;
uint32_t MI_NAME = 0x18;
uint32_t KLASS_STATIC_FIELDS = 0xB8;
uint32_t KLASS_NAME = 0x10;
uint32_t KLASS_IFACE_OFFSETS = 0xB0;  // Il2CppRuntimeInterfaceOffsetPair*
uint32_t KLASS_VTABLE = 0;            // calibrated: start of VirtualInvokeData vtable[]
uint32_t KLASS_IOC = 0;               // calibrated: uint16 interface_offsets_count
bool g_vtable_ok;

struct VirtualInvokeData {
    void *fp;
    const void *mi;
};
struct IfacePair {
    void *iface;
    int32_t offset;
    int32_t _pad;
};

// ---------------------------------------------------------------- rooted heap
struct RootChunk {
    uint8_t *base;
    size_t used, cap;
};
RootChunk g_roots[64];
int g_nroots;

obj new_object_array(intptr_t n) {
    return rt_call<obj>(Array_CreateInstance, g_objarr_type, (int32_t)n);
}

obj raw_storage(intptr_t slots) {
    obj arr = new_object_array(slots);
    arr->klass = (decltype(arr->klass))g_mod_klass;
    return arr;
}

void add_root_chunk(size_t slots) {
    obj arr = raw_storage((intptr_t)slots);
    rt_call<intptr_t>(GCHandle_Alloc, arr);  // pinned forever
    g_roots[g_nroots++] = {(uint8_t *)arr + 0x20, 0, slots * 8};
}

// ---------------------------------------------------------------- caches
obj *g_lit_cache;
obj *g_type_cache;
void *g_klass_cache[4096];
bool g_cctor_ran[4096];

struct IsInstEntry {
    void *klass;
    int32_t tref;
    int8_t result;
};
IsInstEntry g_isinst[1024];

struct VEntry {
    void *klass;
    const void *site;
    void *fp;
    const void *mi;
};
VEntry g_vcache[4096];

// ---------------------------------------------------------------- exceptions (per thread)
struct ThreadEH {
    uint64_t tid;
    rt_handler *top;
};
ThreadEH g_eh[32];
obj *g_exc_slot;  // rooted current exception (shared; exceptions are rare)

ThreadEH *eh() {
    uint64_t t = rt_thread_id();
    for (auto &e : g_eh)
        if (e.tid == t) return &e;
    for (auto &e : g_eh)
        if (!e.tid) {
            e.tid = t;
            return &e;
        }
    return &g_eh[0];
}

bool in_module(const void *p) {
    return (const char *)p >= __module_start && (const char *)p < __bss_end;
}

}  // namespace

// ======================================================================== core

void *g_rt_objarr_klass;  // == g_mod_klass (inline checks in rt.h)

bool rt_ready() { return g_ready; }

obj rt_new_raw_storage(intptr_t slots) { return raw_storage(slots); }
void *rt_mod_klass() { return g_mod_klass; }

void *rt_root_alloc(size_t bytes) {
    bytes = (bytes + 7) & ~(size_t)7;
    if (!bytes) bytes = 8;
    for (int i = 0; i < g_nroots; i++) {
        RootChunk &c = g_roots[i];
        if (c.cap - c.used >= bytes) {
            void *p = c.base + c.used;
            c.used += bytes;
            return p;
        }
    }
    size_t slots = bytes / 8 > 65536 ? bytes / 8 + 1024 : 65536;
    if (g_nroots >= 64) rt_unsupported("rooted memory exhausted");
    add_root_chunk(slots);
    return rt_root_alloc(bytes);
}

obj rt_str16(const char16_t *s, int32_t len) {
    return rt_call<obj>(String_CreateString, (obj) nullptr, s, (int32_t)0, len);
}

obj rt_str(const char *u8) {
    char16_t buf[512];
    int n = 0;
    while (*u8 && n < 511) {
        uint32_t c = (uint8_t)*u8++;
        if (c >= 0xC0) {
            int extra = c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : 1;
            c &= 0x3F >> extra;
            while (extra-- && (*u8 & 0xC0) == 0x80) c = (c << 6) | (*u8++ & 0x3F);
        }
        buf[n++] = c > 0xFFFF ? u'?' : (char16_t)c;
    }
    return rt_str16(buf, n);
}

int rt_str_to_utf8(obj s, char *buf, int cap) {
    if (!s || cap <= 0) {
        if (cap) buf[0] = 0;
        return 0;
    }
    auto *str = (Il2CppString *)s;
    int n = 0;
    for (int i = 0; i < str->length && n < cap - 4; i++) {
        uint32_t c = str->chars[i];
        if (c < 0x80) {
            buf[n++] = (char)c;
        } else if (c < 0x800) {
            buf[n++] = (char)(0xC0 | (c >> 6));
            buf[n++] = (char)(0x80 | (c & 0x3F));
        } else {
            buf[n++] = (char)(0xE0 | (c >> 12));
            buf[n++] = (char)(0x80 | ((c >> 6) & 0x3F));
            buf[n++] = (char)(0x80 | (c & 0x3F));
        }
    }
    buf[n] = 0;
    return n;
}

obj rt_lit(uint32_t idx) {
    obj &slot = g_lit_cache[idx];
    if (!slot) slot = rt_str16(silk_literals[idx].s, (int32_t)silk_literals[idx].len);
    return slot;
}

// ---------------------------------------------------------------- types

static obj get_type(const char *aqn) {
    obj t = rt_call<obj>(Type_GetType, rt_str(aqn));
    if (!t) silk::log("type not found on Switch: %s", aqn);
    return t;
}

obj rt_type(int32_t tref) {
    obj &slot = g_type_cache[tref];
    if (slot) return slot;
    const rt_typeref &r = silk_typerefs[tref];
    if (r.aqn) {
        slot = get_type(r.aqn);
    } else if (r.desc) {
        // Mod types have no System.Type on Switch; hand out a token object carrying the descriptor.
        static rt_modtype typeobj = {"SilkNX.ModType", nullptr, sizeof(ModObj) + 8, 0, false, nullptr, 0,
                                     nullptr, false, nullptr, 0, nullptr, {}};
        obj o = rt_new_mod(&typeobj);
        *(rt_modtype **)((uint8_t *)o + sizeof(ModObj)) = r.desc;
        slot = o;
    }
    return slot;
}

rt_modtype *rt_modtype_of_typeobj(obj t) {
    if (!rt_is_mod(t) || strcmp_(((ModObj *)t)->desc->name, "SilkNX.ModType")) return nullptr;
    return *(rt_modtype **)((uint8_t *)t + sizeof(ModObj));
}

static void *method_handle(obj mi_obj) {
    return mi_obj ? *(void **)((uint8_t *)mi_obj + off::RuntimeMethodInfo_mhandle) : nullptr;
}

void *rt_klass_of_type(obj type) {
    if (!type) return nullptr;
    // DeclaredOnly | Instance | Static | Public | NonPublic
    obj methods = rt_call<obj>(RuntimeType_GetMethods, type, (int32_t)(2 | 4 | 8 | 16 | 32));
    if (methods && ((Il2CppArray *)methods)->max_length) {
        void *mi = method_handle(RT_ELEM(methods, 0, obj));
        if (mi) return *(void **)((uint8_t *)mi + MI_KLASS);
    }
    obj o = rt_call<obj>(FormatterServices_GetUninitializedObject, type);
    return o ? o->klass : nullptr;
}

void *rt_klass(int32_t tref) {
    if (tref < 4096 && g_klass_cache[tref]) return g_klass_cache[tref];
    void *k = rt_klass_of_type(rt_type(tref));
    if (tref < 4096) g_klass_cache[tref] = k;
    return k;
}

void *rt_game_statics(int32_t tref) {
    void *k = rt_klass(tref);
    if (!k) rt_unsupported(silk_typerefs[tref].aqn ? silk_typerefs[tref].aqn : "statics of unknown type");
    if (tref < 4096 && !g_cctor_ran[tref]) {
        g_cctor_ran[tref] = true;
        obj t = rt_type(tref);
        rt_call<void>(RuntimeHelpers_RunClassConstructor, *(intptr_t *)((uint8_t *)t + off::Type_impl));
    }
    return *(void **)((uint8_t *)k + KLASS_STATIC_FIELDS);
}

// ---------------------------------------------------------------- objects

obj rt_new_obj(int32_t tref) {
    obj t = rt_type(tref);
    if (!t) rt_unsupported(silk_typerefs[tref].aqn ? silk_typerefs[tref].aqn : silk_typerefs[tref].name);
    return rt_call<obj>(FormatterServices_GetUninitializedObject, t);
}

obj rt_new_mod(const rt_modtype *d) {
    size_t total = d->is_vt ? sizeof(ModObj) + d->size : d->size;
    intptr_t slots = (intptr_t)((total - 0x20 + 7) / 8);
    obj o = rt_new_raw_storage(slots);
    ((ModObj *)o)->desc = d;
    return o;
}

// Converted objects must be cloned as GC-traced raw storage; the game's own clone would copy them as a real
// long[] (untraced memory) and lose every reference inside.
obj shim_System_Object__MemberwiseClone(obj self) {
    rt_nc(self);
    if (self->klass != g_mod_klass) return rt_call<obj>(Object_MemberwiseClone, self);
    intptr_t n = (intptr_t)((Il2CppArray *)self)->max_length;
    obj c = rt_new_raw_storage(n);
    ((Il2CppArray *)c)->bounds = ((Il2CppArray *)self)->bounds;
    memcpy((uint8_t *)c + 0x20, (uint8_t *)self + 0x20, (size_t)n * 8);
    return c;
}

obj rt_box(int32_t tref, const void *data, size_t size) {
    obj o = rt_new_obj(tref);
    memcpy((uint8_t *)o + 0x10, data, size);
    return o;
}

obj rt_box_mod(const rt_modtype *d, const void *data, size_t size) {
    obj o = rt_new_mod(d);
    memcpy((uint8_t *)o + sizeof(ModObj), data, size);
    return o;
}

void *rt_unbox(obj o) { return (uint8_t *)rt_nc(o) + 0x10; }

void *rt_unbox_mod(obj o) { return (uint8_t *)rt_nc(o) + sizeof(ModObj); }

bool rt_is_mod(obj o) {
    if (!o || o->klass != g_mod_klass) return false;
    auto *m = (ModObj *)o;
    return m->max_length >= 1 && in_module(m->desc);
}

bool rt_is_mod_type(obj o, const rt_modtype *d) {
    if (!rt_is_mod(o)) return false;
    for (const rt_modtype *t = ((ModObj *)o)->desc; t; t = t->base)
        if (t == d) return true;
    return false;
}

bool rt_is_mod_iface(obj o, uint32_t id) {
    if (!rt_is_mod(o)) return false;
    for (const uint32_t *p = ((ModObj *)o)->desc->ifaces; p && *p; p++)
        if (*p == id) return true;
    return false;
}

bool rt_is_emu(obj o, const char *key) {
    if (!rt_is_mod(o)) return false;
    int emu = ((ModObj *)o)->desc->emu;
    if (!strcmp_(key, "BepInEx.BaseUnityPlugin")) return emu == 2;
    return false;
}

bool rt_mod_is(obj o, int32_t tref) {
    if (!rt_is_mod(o)) return false;
    const char *aqn = silk_typerefs[tref].aqn;
    int emu = ((ModObj *)o)->desc->emu;
    if (!aqn) return false;
    if (emu == 1 || emu == 2) {
        return startswith_(aqn, "UnityEngine.MonoBehaviour,") || startswith_(aqn, "UnityEngine.Behaviour,") ||
               startswith_(aqn, "UnityEngine.Component,") || startswith_(aqn, "UnityEngine.Object,");
    }
    if (emu == 3) return startswith_(aqn, "System.Exception,");
    return false;
}

bool rt_is_game(obj o, int32_t tref) {
    if (!o || rt_is_mod(o)) return false;
    unsigned h = (unsigned)(((uintptr_t)o->klass >> 4) * 31 + (unsigned)tref) % 1024;
    IsInstEntry &e = g_isinst[h];
    if (e.klass == o->klass && e.tref == tref) return e.result;
    obj t = rt_type(tref);
    bool r = t && rt_call<bool>(RuntimeType_IsInstanceOfType, t, o);
    e = {o->klass, tref, (int8_t)r};
    return r;
}

void *rt_mstatics(rt_modtype *d) {
    if (!d->statics) d->statics = rt_root_alloc(d->statics_size ? d->statics_size : 8);
    if (!d->cctor_done) {
        d->cctor_done = true;
        if (d->cctor) {
            const char *saved = g_rt_context;
            char ctx[200];
            silk::snfmt(ctx, sizeof(ctx), "static constructor of %s", d->name);
            g_rt_context = ctx;
            RT_GUARD_BEGIN
            ((void (*)(const void *))d->cctor)(nullptr);
            RT_GUARD_END
            g_rt_context = saved;
        }
    }
    return d->statics;
}

// ---------------------------------------------------------------- arrays

obj rt_new_array(int32_t tref, intptr_t n, size_t elem_size) {
    if (n < 0) rt_unsupported("negative array size");
    if (tref & 0x40000000) {
        // Arrays of mod-defined structs: an object[] big enough for the raw elements (scanned by the GC),
        // with the element count tagged into the unused `bounds` field.
        size_t size = tref & 0xFFFFFF;
        obj a = rt_new_raw_storage((intptr_t)((n * size + 7) / 8 + 1));
        ((Il2CppArray *)a)->bounds = (void *)(((uintptr_t)n << 16) | 1);
        return a;
    }
    obj t = rt_type(tref);
    if (!t) rt_unsupported(silk_typerefs[tref].name ? silk_typerefs[tref].name : "array of unknown element type");
    return rt_call<obj>(Array_CreateInstance, t, (int32_t)n);
}

void rt_init_array(obj arr, const rt_blob *blob) { memcpy((uint8_t *)rt_nc(arr) + 0x20, blob->data, blob->size); }

// ---------------------------------------------------------------- reflection call sites

static obj type_array(const char *list, int *count) {
    // list: "aqn;aqn;..." (may be empty)
    obj types[16];
    int n = 0;
    char buf[512];
    const char *p = list;
    while (*p && n < 16) {
        int k = 0;
        while (*p && *p != ';' && k < 511) buf[k++] = *p++;
        buf[k] = 0;
        if (*p == ';') p++;
        types[n++] = get_type(buf);
    }
    obj arr = rt_call<obj>(Array_CreateInstance, g_type_type, (int32_t)n);
    for (int i = 0; i < n; i++) RT_ELEM(arr, i, obj) = types[i];
    *count = n;
    return arr;
}

static void split_desc(const char *desc, char parts[5][1024]) {
    int k = 0, j = 0;
    for (const char *p = desc; *p; p++) {
        if (*p == '|' && k < 4) {
            parts[k][j] = 0;
            k++;
            j = 0;
        } else if (j < 1023) {
            parts[k][j++] = *p;
        }
    }
    parts[k][j] = 0;
    for (int i = k + 1; i < 5; i++) parts[i][0] = 0;
}

static void *resolve_method(obj owner, const char *name, const char *params, const char *margs, bool ctor) {
    int np = 0;
    obj ptypes = type_array(params, &np);
    bool all_known = true;
    for (int i = 0; i < np; i++)
        if (!RT_ELEM(ptypes, i, obj)) all_known = false;
    const int32_t flags = 4 | 8 | 16 | 32;
    if (ctor) {
        const int32_t cflags = 4 | 16 | 32;  // instance only: Static would also match .cctor
        obj m = all_known ? rt_call<obj>(Type_GetConstructor4, owner, cflags, (obj) nullptr, ptypes, (obj) nullptr) : nullptr;
        if (!m) {  // a parameter type has no name on Switch: pick by parameter count (never guess blindly)
            obj all = rt_call<obj>(RuntimeType_GetConstructors, owner, cflags);
            intptr_t n = all ? (intptr_t)((Il2CppArray *)all)->max_length : 0;
            int hits = 0;
            for (intptr_t i = 0; i < n; i++) {
                obj c = RT_ELEM(all, i, obj);
                if (rt_call<int32_t>(RuntimeConstructorInfo_GetParametersCount, c) == np) {
                    if (!hits++) m = c;
                }
            }
            if (hits > 1) silk::log("note: %d constructors with %d parameters; using the first", hits, np);
        }
        if (!m || rt_call<int32_t>(RuntimeConstructorInfo_GetParametersCount, m) != np) return nullptr;
        return *(void **)((uint8_t *)m + off::RuntimeConstructorInfo_mhandle);
    }
    obj m = nullptr;
    if (all_known && !*margs)
        m = rt_call<obj>(Type_GetMethod5, owner, rt_str(name), flags, (obj) nullptr, ptypes, (obj) nullptr);
    int nm = 0;
    obj gtypes = *margs ? type_array(margs, &nm) : nullptr;
    if (!m) {
        // Scan by name / parameter count / generic arity (generic definitions don't match concrete types,
        // and GetMethod would throw on ambiguity).
        obj all = rt_call<obj>(RuntimeType_GetMethods, owner, flags);
        intptr_t n = all ? (intptr_t)((Il2CppArray *)all)->max_length : 0;
        for (intptr_t i = 0; i < n && !m; i++) {
            obj cand = RT_ELEM(all, i, obj);
            void *mi = method_handle(cand);
            if (!mi || strcmp_(*(const char **)((uint8_t *)mi + MI_NAME), name)) continue;
            if (rt_call<int32_t>(RuntimeMethodInfo_GetParametersCount, cand) != np) continue;
            bool gdef = rt_call<bool>(RuntimeMethodInfo_get_IsGenericMethodDefinition, cand);
            if (gdef != (nm > 0)) continue;
            if (nm && ((Il2CppArray *)rt_call<obj>(RuntimeMethodInfo_GetGenericArguments, cand))->max_length != (uintptr_t)nm)
                continue;
            m = cand;
        }
    }
    if (!m) return nullptr;
    if (rt_call<int32_t>(RuntimeMethodInfo_GetParametersCount, m) != np) return nullptr;
    if (nm) {
        m = rt_call<obj>(RuntimeMethodInfo_MakeGenericMethod, m, gtypes);
        if (!m) return nullptr;
    }
    return method_handle(m);
}

const char *g_rt_last_site;  // most recently resolved reflection site, printed with null-reference reports

rt_rsite *rt_site_resolve(rt_rsite *s) {
    g_rt_last_site = s->desc;
    if (s->fp) return s;
#ifdef SILKNX_TRACE
    silk::log("trace-site: %s", s->desc);
#endif
    char parts[5][1024];
    split_desc(s->desc, parts);
    obj owner = get_type(parts[0]);
    void *mi = owner ? resolve_method(owner, parts[1], parts[2], parts[3], !strcmp_(parts[1], ".ctor")) : nullptr;
    if (!mi || !*(void **)mi) {
        silk::log("cannot resolve %s", s->desc);
        rt_unsupported(s->desc);
    }
    s->mi = mi;
    s->fp = *(void **)mi;
#ifdef SILKNX_TRACE
    silk::log("trace-site: -> fp=main+%lx", (uint64_t)((uintptr_t)s->fp - silk_main_base));
#endif
    return s;
}

static rt_vsite g_tostring_site = {0, 3, -1, false, "ToString", "System.Object, mscorlib|ToString|||i",
                                   nullptr, nullptr, nullptr};

// Native IL2CPP dispatch through the vtable / interface offset table (exact, handles explicit implementations).
static const VirtualInvokeData *vtable_entry(void *klass, rt_vsite *site) {
    if (!g_vtable_ok || site->slot < 0) return nullptr;
    int32_t slot = site->slot;
    if (site->iface) {
        void *ik = site->tref >= 0 ? rt_klass(site->tref) : nullptr;
        if (!ik) return nullptr;
        auto *pairs = *(IfacePair **)((uint8_t *)klass + KLASS_IFACE_OFFSETS);
        uint16_t n = *(uint16_t *)((uint8_t *)klass + KLASS_IOC);
        int32_t base = -1;
        for (uint16_t i = 0; i < n && pairs; i++)
            if (pairs[i].iface == ik) {
                base = pairs[i].offset;
                break;
            }
        if (base < 0) return nullptr;
        slot += base;
    }
    auto *vt = (const VirtualInvokeData *)((uint8_t *)klass + KLASS_VTABLE);
    return vt[slot].fp ? &vt[slot] : nullptr;
}

void *rt_vfn(obj o, rt_vsite *site) {
    rt_nc(o);
    g_rt_last_site = site->desc;
    if (rt_is_mod(o)) {
        const rt_modtype *d = ((ModObj *)o)->desc;
        for (uint32_t i = 0; i < d->nvmap; i++)
            if (d->vmap[i].id == site->mid) {
                site->mi = nullptr;
                return d->vmap[i].fn;
            }
        // not overridden by the mod type: fall through to the object[] implementation
    }
    if (site->klass == o->klass) return site->fp;
    unsigned h = (unsigned)((((uintptr_t)o->klass) >> 4) ^ (((uintptr_t)site) >> 3)) % 4096;
    VEntry &e = g_vcache[h];
    if (e.klass == o->klass && e.site == site) {
        site->mi = e.mi;
        return e.fp;
    }
    char parts[5][1024];
    split_desc(site->desc, parts);
    if (!parts[3][0]) {  // not a generic method: use the class's own dispatch tables
        if (const VirtualInvokeData *v = vtable_entry(o->klass, site)) {
#ifdef SILKNX_TRACE_ALL
            silk::log("vfn: %s on %s -> main+%lx", site->desc, *(const char **)((uint8_t *)o->klass + KLASS_NAME),
                      (uint64_t)((uintptr_t)v->fp - silk_main_base));
#endif
            e = {o->klass, site, v->fp, v->mi};
            site->klass = o->klass;
            site->fp = v->fp;
            site->mi = v->mi;
            return v->fp;
        }
    }
    obj rtype = rt_call<obj>(Object_GetType, o);
    void *mi = resolve_method(rtype, site->name, parts[2], parts[3], false);
    if (!mi && site->iface && parts[0][0]) {
        // explicit interface implementation: "Namespace.IFace.Method"
        char full[1100];
        int k = 0;
        for (const char *p = parts[0]; *p && *p != ',' && k < 1000; p++) full[k++] = *p == '+' ? '.' : *p;
        full[k++] = '.';
        for (const char *p = site->name; *p && k < 1090; p++) full[k++] = *p;
        full[k] = 0;
        mi = resolve_method(rtype, full, parts[2], parts[3], false);
    }
    if (!mi) {
        silk::log("virtual dispatch failed: %s on %s", site->desc, (const char *)*(void **)((uint8_t *)o->klass + KLASS_NAME));
        rt_unsupported(site->desc);
    }
    void *fp = ((void **)mi)[1] ? ((void **)mi)[1] : ((void **)mi)[0];  // virtualMethodPointer handles boxed structs
    e = {o->klass, site, fp, mi};
    site->klass = o->klass;
    site->fp = fp;
    site->mi = mi;
    return fp;
}

void *rt_mod_vfn(obj o, uint32_t mid, void *fallback) {
    rt_nc(o);
    if (rt_is_mod(o)) {
        const rt_modtype *d = ((ModObj *)o)->desc;
        for (uint32_t i = 0; i < d->nvmap; i++)
            if (d->vmap[i].id == mid) return d->vmap[i].fn;
    }
    return fallback;
}

// ---------------------------------------------------------------- delegates

// IL2CPP delegates keep a signature-specific multicast invoker in extra_arg, and Delegate.Combine installs
// it as the combined delegate's invoke_impl (it walks the `delegates` array). Left at 0, firing an event
// with two or more listeners jumps to address 0. Each delegate type's generated constructor stores it as a
// constant (`adrp x8, F; add x8, x8, :lo12:F; str/stp x8, .., [x0, #0x38]`), so read it from that code.
static void *ctor_extra_arg_constant(const uint32_t *code) {
    uint64_t x8 = 0;
    bool have = false;
    for (int i = 0; i < 48; i++) {
        uint32_t in = code[i];
        uint64_t pc = (uint64_t)(code + i);
        if ((in & 0x9F00001F) == 0x90000008) {  // adrp x8, page
            int64_t imm = (int64_t)((((in >> 5) & 0x7FFFF) << 2) | ((in >> 29) & 3));
            imm = (imm << 43) >> 43;  // sign-extend 21 bits
            x8 = (pc & ~0xFFFull) + (uint64_t)(imm << 12);
            have = true;
        } else if ((in & 0xFFC003FF) == 0x91000108 && have) {  // add x8, x8, #imm12
            x8 += (in >> 10) & 0xFFF;
        } else if (have && (in == 0xF9001C08 ||                // str x8, [x0, #0x38]
                            (in & 0xFFFF83FF) == 0xA9038008)) {  // stp x8, xN, [x0, #0x38]
            return (void *)x8;
        } else if (in == 0xD65F03C0) {  // ret
            have = false;
        }
    }
    return nullptr;
}

static void *delegate_multicast_invoker(int32_t tref) {
    static void **cache;
    if (!cache) cache = (void **)rt_root_alloc(8 * (silk_typeref_count + 1));
    if (cache[tref]) return cache[tref];
    void *result = nullptr;
    obj type = rt_type(tref);
    obj ctors = type ? rt_call<obj>(RuntimeType_GetConstructors, type, (int32_t)(4 | 16 | 32)) : nullptr;
    intptr_t n = ctors ? (intptr_t)((Il2CppArray *)ctors)->max_length : 0;
    for (intptr_t i = 0; i < n && !result; i++) {
        obj c = RT_ELEM(ctors, i, obj);
        if (rt_call<int32_t>(RuntimeConstructorInfo_GetParametersCount, c) != 2) continue;
        void *mi = *(void **)((uint8_t *)c + off::RuntimeConstructorInfo_mhandle);
        const uint32_t *code = mi ? *(const uint32_t **)mi : nullptr;
        if (code) result = ctor_extra_arg_constant(code);
    }
    if (!result) silk::log("warning: no multicast invoker for delegate type %s", silk_typerefs[tref].name);
#ifdef SILKNX_TRACE
    silk::log("trace: multicast invoker for %s = main+%lx", silk_typerefs[tref].name,
              (uint64_t)((uintptr_t)result - silk_main_base));
#endif
    cache[tref] = result;
    return result;
}

// A MethodInfo for a converted function, so `Delegate.method` is never null: Combine/Remove/Equals and
// Delegate.Method all read it, and a combined delegate is a clone of its first element. Cloned from the
// delegate type's Invoke (same signature, so reflection and DynamicInvoke see a consistent method), then
// pointed at our function and marked as a plain instance method. One per function, so `-=` still matches.
static void *delegate_method_info(int32_t tref, void *fn) {
    constexpr int kSlots = 4096, kSize = 0x58;
    static void *keys[kSlots];
    static uint8_t *pool;
    if (!pool) pool = (uint8_t *)rt_root_alloc(kSlots * kSize);  // never freed; holds no GC references
    uint32_t h = (uint32_t)(((uintptr_t)fn >> 2) * 2654435761u) % kSlots;
    for (int n = 0; n < kSlots; n++, h = (h + 1) % kSlots) {
        if (keys[h] == fn) return pool + h * kSize;
        if (keys[h]) continue;
        obj inv = rt_find_method(rt_type(tref), rt_str("Invoke"), nullptr);
        void *src = inv ? method_handle(inv) : nullptr;
        if (!src) return nullptr;
        uint8_t *mi = pool + h * kSize;
        memcpy(mi, src, kSize);
        *(void **)(mi + 0x00) = fn;                // methodPointer
        *(void **)(mi + 0x08) = fn;                // virtualMethodPointer
        *(uint16_t *)(mi + 0x4c) = 0x0086;         // flags: public hidebysig (instance, not virtual)
        *(uint16_t *)(mi + 0x50) = 0xFFFF;         // slot: none
        keys[h] = fn;
        return mi;
    }
    return nullptr;
}

obj rt_new_delegate(int32_t tref, void *fn, obj target, bool is_static) {
    obj d = rt_new_obj(tref);
    uint8_t *p = (uint8_t *)d;
    *(void **)(p + off::Delegate_extra_arg) = delegate_multicast_invoker(tref);
    *(void **)(p + off::Delegate_method_ptr) = fn;
    *(void **)(p + off::Delegate_invoke_impl) = fn;
    *(obj *)(p + off::Delegate_m_target) = target;
    *(obj *)(p + off::Delegate_method_code) = is_static ? d : target;
    *(void **)(p + off::Delegate_method) = delegate_method_info(tref, fn);
    return d;
}

obj rt_new_mod_delegate(void *fn, obj target) {
    static rt_modtype desc = {"SilkNX.Delegate", nullptr, sizeof(ModDelegate), 4, false, nullptr, 0, nullptr,
                              false, nullptr, 0, nullptr, {}};
    obj d = rt_new_mod(&desc);
    ((ModDelegate *)d)->fn = fn;
    ((ModDelegate *)d)->target = target;
    return d;
}

void *rt_delegate_fn(obj d) {
    rt_nc(d);
    if (rt_is_mod(d)) return ((ModDelegate *)d)->fn;
    return *(void **)((uint8_t *)d + off::Delegate_invoke_impl);
}

obj rt_delegate_bound_target(obj d) { return *(obj *)((uint8_t *)d + off::Delegate_m_target); }

obj rt_delegate_target(obj d) {
    if (rt_is_mod(d)) return ((ModDelegate *)d)->target;
    return *(obj *)((uint8_t *)d + off::Delegate_method_code);
}

const void *rt_delegate_mi(obj d) {
    if (rt_is_mod(d)) return nullptr;
    return *(void **)((uint8_t *)d + off::Delegate_method);
}

// ---------------------------------------------------------------- exceptions

rt_handler *rt_eh_top() { return eh()->top; }

void rt_push(rt_handler *h) {
    ThreadEH *t = eh();
#ifdef SILKNX_TRACE
    h->pushed_by = (uintptr_t)__builtin_return_address(0) - (uintptr_t)__module_start;
#endif
    h->prev = t->top;
    t->top = h;
}

void rt_pop(rt_handler *h) {
    // Handlers nest with the stack, so everything above h belongs to frames that already returned
    // (a leaked handler). Never walk those: their memory is dead stack. Just drop them.
    ThreadEH *t = eh();
    if (t->top != h) {
        static int warned;
        if (warned++ < 8) {
#ifdef SILKNX_TRACE
            silk::log("internal: stale exception handler dropped (%s), leaked by module+%lx",
                      g_rt_context ? g_rt_context : "?", (uint64_t)t->top->pushed_by);
#else
            silk::log("internal: stale exception handler dropped (%s)", g_rt_context ? g_rt_context : "?");
#endif
        }
    }
    t->top = h->prev;
}

void rt_unwind_to(rt_handler *h) {
    // A C++ exception unwound past setjmp handlers: drop everything above (and including) h.
    ThreadEH *t = eh();
    t->top = h->prev;
}

extern "C" void *__cxa_current_primary_exception();
extern "C" void __cxa_decrement_exception_refcount(void *);

void rt_caught_native(const char *where) {
    // IL2CPP throws Il2CppExceptionWrapper { Il2CppException* ex; }
    void *p = __cxa_current_primary_exception();
    obj ex = p ? *(obj *)p : nullptr;
    if (p) __cxa_decrement_exception_refcount(p);
    if (g_exc_slot) *g_exc_slot = ex;
    if (g_rt_last_site) silk::log("  (last resolved call: %s)", g_rt_last_site);
    rt_report_exception(where);
}

obj rt_exception() { return g_exc_slot ? *g_exc_slot : nullptr; }

obj rt_take_exception() { return rt_exception(); }

static void describe_exception(obj ex, char *buf, int cap) {
    if (!ex) {
        silk::snfmt(buf, cap, "(null)");
        return;
    }
    if (rt_is_mod(ex)) {
        char msg[256] = "";
        if (((ModObj *)ex)->desc->emu == 3) rt_str_to_utf8(((ModException *)ex)->message, msg, sizeof(msg));
        silk::snfmt(buf, cap, "%s: %s", ((ModObj *)ex)->desc->name, msg);
        return;
    }
    const char *ns = *(const char **)((uint8_t *)ex->klass + KLASS_NAME + 8);
    const char *name = *(const char **)((uint8_t *)ex->klass + KLASS_NAME);
    char msg[200] = "";
    obj m = *(obj *)((uint8_t *)ex + off::Exception_message);
    if (m) rt_str_to_utf8(m, msg, sizeof(msg));
    silk::snfmt(buf, cap, "%s.%s%s%s", ns, name, msg[0] ? ": " : "", msg);
}

void rt_throw(obj ex) {
    ThreadEH *t = eh();
    if (g_exc_slot) *g_exc_slot = ex;
    if (!t->top) {
        char d[300];
        describe_exception(ex, d, sizeof(d));
        silk::log("FATAL: unhandled exception outside any SilkNX boundary: %s", d);
        svcBreak(0, 0, 0);
    }
    rt_handler *h = t->top;
    t->top = h->prev;
    rt_longjmp(&h->jb, 1);
}

void rt_rethrow() { rt_throw(rt_exception()); }

static obj make_exception(const char *aqn) {
    obj t = get_type(aqn);
    return t ? rt_call<obj>(FormatterServices_GetUninitializedObject, t) : nullptr;
}

void rt_throw_nre() {
    // Record where in the converted code the null was hit (symbolize with: silknx.py where <offset>).
    uintptr_t at = (uintptr_t)__builtin_return_address(0) - 4 - (uintptr_t)__module_start;
    static uintptr_t last;
    if (at != last) {
        last = at;
        silk::log("null reference at module+%lx (%s)", (uint64_t)at, g_rt_context ? g_rt_context : "?");
    }
    rt_throw(make_exception("System.NullReferenceException, mscorlib"));
}

void rt_throw_cast(obj) { rt_throw(make_exception("System.InvalidCastException, mscorlib")); }

void rt_throw_index() { rt_throw(make_exception("System.IndexOutOfRangeException, mscorlib")); }

void rt_unsupported(const char *what) {
    static const char *seen[64];
    static int nseen;
    bool known = false;
    for (int i = 0; i < nseen; i++)
        if (seen[i] == what) known = true;
    if (!known) {
        if (nseen < 64) seen[nseen++] = what;
        silk::log("unsupported on Switch: %s", what);
        rt_console("unsupported: %s", what);
    }
    rt_throw(make_exception("System.NotSupportedException, mscorlib"));
}

const char *g_rt_context;  // what mod code is running (for error messages)
#ifdef SILKNX_TRACE_ALL
int g_rt_trace_budget = 3000000;
#endif

// true the first time a message is seen (on-screen errors are shown once; the log gets every one)
static bool first_time(const char *a, const char *b) {
    static uint32_t seen[512];
    uint32_t h = 2166136261u;
    const char *parts[] = {a, b};
    for (const char *s : parts)
        for (; *s; s++) h = (h ^ (uint8_t)*s) * 16777619u;
    if (!h) h = 1;
    for (uint32_t i = 0, k = h % 512; i < 512; i++, k = (k + 1) % 512) {
        if (seen[k] == h) return false;
        if (!seen[k]) {
            seen[k] = h;
            return true;
        }
    }
    return false;
}

void rt_report_exception(const char *where) {
    char d[400];
    describe_exception(rt_exception(), d, sizeof(d));
    const char *ctx = g_rt_context ? g_rt_context : where;
    silk::log("exception in %s: %s", ctx, d);
    if (first_time(ctx, d)) rt_console("error in %s: %s", ctx, d);
}

// ---------------------------------------------------------------- logging helpers

void *rt_string_klass() { return g_string_klass; }

obj rt_object_to_string(obj o) {
    rt_nc(o);
    return ((obj(*)(obj, const void *))rt_vfn(o, &g_tostring_site))(o, g_tostring_site.mi);
}

void rt_log_obj(const char *prefix, obj msg, bool on_screen, bool once) {
    char buf[600];
    if (!msg) {
        silk::log("%s(null)", prefix);
        if (on_screen) rt_console("%s(null)", prefix);
        return;
    }
    obj s = msg;
    if (msg->klass != g_string_klass) {
        if (rt_is_mod(msg)) {
            silk::log("%s%s", prefix, ((ModObj *)msg)->desc->name);
            return;
        }
        s = ((obj(*)(obj, const void *))rt_vfn(msg, &g_tostring_site))(msg, g_tostring_site.mi);
    }
    rt_str_to_utf8(s, buf, sizeof(buf));
    silk::log("%s%s", prefix, buf);
    if (on_screen && (!once || first_time(prefix, buf))) rt_console("%s%s", prefix, buf);
}

// ---------------------------------------------------------------- boot

static bool calibrate() {
    obj str_t = get_type("System.String, mscorlib");
    if (!str_t) return false;
    obj m = rt_call<obj>(Type_GetMethod5, str_t, rt_str("get_Length"), (int32_t)(4 | 16), (obj) nullptr,
                         rt_call<obj>(Array_CreateInstance, g_type_type, (int32_t)0), (obj) nullptr);
    void *mi = method_handle(m);
    if (!mi) return false;
    // MethodInfo: methodPointer, virtualMethodPointer, invoker, name, klass
    const char *name = *(const char **)((uint8_t *)mi + MI_NAME);
    void *klass = *(void **)((uint8_t *)mi + MI_KLASS);
    silk::log("calibrate: MethodInfo name='%s' klass=%p string_klass=%p", name, klass, g_string_klass);
    if (strcmp_(name, "get_Length") || klass != g_string_klass) return false;
    const char *kname = *(const char **)((uint8_t *)klass + KLASS_NAME);
    if (strcmp_(kname, "String")) return false;
    // static_fields: String.Empty is the first static field
    void *sf = *(void **)((uint8_t *)klass + KLASS_STATIC_FIELDS);
    obj empty = sf ? *(obj *)sf : nullptr;
    silk::log("calibrate: static_fields=%p String.Empty=%p", sf, empty);
    if (!empty || empty->klass != g_string_klass || ((Il2CppString *)empty)->length != 0) return false;
    // vtable: String overrides ToString (slot 3) and Equals(object) (slot 0)
    uintptr_t to_string = silk_main_base + String_ToString, equals = silk_main_base + String_Equals_object;
    for (uint32_t off = 0x100; off < 0x200; off += 8) {
        if (*(uintptr_t *)((uint8_t *)klass + off) == to_string &&
            *(uintptr_t *)((uint8_t *)klass + off - 3 * 16) == equals) {
            KLASS_VTABLE = off - 3 * 16;
            break;
        }
    }
    // counts: method_count, property_count, field_count, event_count, nested, vtable, interfaces, interface_offsets
    for (uint32_t off = KLASS_VTABLE - 0x30; KLASS_VTABLE && off < KLASS_VTABLE; off += 2)
        if (*(uint16_t *)((uint8_t *)klass + off) == String_method_count) {
            KLASS_IOC = off + 14;
            break;
        }
    uint16_t n = KLASS_IOC ? *(uint16_t *)((uint8_t *)klass + KLASS_IOC) : 0;
    auto *pairs = *(IfacePair **)((uint8_t *)klass + KLASS_IFACE_OFFSETS);
    bool sane = KLASS_VTABLE && n > 0 && n < 64 && pairs;
    for (uint16_t i = 0; sane && i < n; i++) {
        const char *nm = pairs[i].iface ? *(const char **)((uint8_t *)pairs[i].iface + KLASS_NAME) : nullptr;
        if (!nm || nm[0] != 'I') sane = false;  // interface names all start with I
    }
    g_vtable_ok = sane;
    silk::log("calibrate: vtable=+%x iface_count=+%x (%u interfaces) %s", KLASS_VTABLE, KLASS_IOC, n,
              sane ? "ok" : "FAILED (falling back to reflection)");
    return true;
}

void rt_boot_core() {
    obj s = rt_str16(u"x", 1);
    g_string_klass = s->klass;
    g_objarr_type = rt_call<obj>(Type_GetType, rt_str("System.Object, mscorlib"));
    g_type_type = rt_call<obj>(Type_GetType, rt_str("System.Type, mscorlib"));
    obj probe = new_object_array(1);
    g_objarr_klass = probe->klass;
    g_mod_klass = rt_call<obj>(Array_CreateInstance, rt_call<obj>(Type_GetType, rt_str("System.Int64, mscorlib")),
                               (int32_t)1)->klass;
    g_rt_objarr_klass = g_mod_klass;
    add_root_chunk(65536);
    g_exc_slot = (obj *)rt_root_alloc(8);
    g_lit_cache = (obj *)rt_root_alloc(8 * (silk_literal_count + 1));
    g_type_cache = (obj *)rt_root_alloc(8 * (silk_typeref_count + 1));
    if (!calibrate()) silk::log("WARNING: IL2CPP layout calibration failed; static fields may misbehave");
    g_ready = true;
}

// small libc-ish helpers used across the runtime
int strcmp_(const char *a, const char *b) {
    while (*a && *a == *b) a++, b++;
    return (uint8_t)*a - (uint8_t)*b;
}

bool startswith_(const char *s, const char *pre) {
    while (*pre)
        if (*s++ != *pre++) return false;
    return true;
}

// Managed MethodInfo for (type, name[, parameter types]) using real reflection; never throws on ambiguity.
obj rt_find_method(obj type, obj name, obj param_types) {
    if (!type || rt_is_mod(type)) return nullptr;
    char n[256];
    rt_str_to_utf8(name, n, sizeof(n));
    bool all_known = param_types != nullptr;
    intptr_t np = param_types ? (intptr_t)((Il2CppArray *)param_types)->max_length : -1;
    for (intptr_t i = 0; i < np; i++)
        if (!RT_ELEM(param_types, i, obj)) all_known = false;
    if (all_known)
        return rt_call<obj>(Type_GetMethod5, type, name, (int32_t)(4 | 8 | 16 | 32), (obj) nullptr, param_types,
                            (obj) nullptr);
    obj all = rt_call<obj>(RuntimeType_GetMethods, type, (int32_t)(4 | 8 | 16 | 32));
    intptr_t cnt = all ? (intptr_t)((Il2CppArray *)all)->max_length : 0;
    for (intptr_t i = 0; i < cnt; i++) {
        obj cand = RT_ELEM(all, i, obj);
        void *mi = method_handle(cand);
        if (!mi || strcmp_(*(const char **)((uint8_t *)mi + MI_NAME), n)) continue;
        if (np >= 0 && rt_call<int32_t>(RuntimeMethodInfo_GetParametersCount, cand) != np) continue;
        return cand;
    }
    return nullptr;
}

#ifdef SILKNX_STACKCHK
// -fstack-protector-all (debug builds): report which converted function overran its frame.
extern "C" uintptr_t __stack_chk_guard = 0x5e1f2e3d4c5b6a79ull;
extern "C" [[noreturn]] void __stack_chk_fail() {
    uintptr_t at = (uintptr_t)__builtin_return_address(0) - (uintptr_t)__module_start;
    silk::log("STACK SMASH detected in function returning at module+%lx (%s)", (uint64_t)at,
              g_rt_context ? g_rt_context : "?");
    svcBreak(0, 0, 0);
    __builtin_unreachable();
}
#endif

#ifdef SILKNX_SELFTEST
// Does the GC still trace an object[]'s contents after its class pointer is swapped to long[]? (Unity's
// asset liveness scan dereferences every object[] slot; mod objects keep raw data there.)
namespace {
constexpr uintptr_t kHide = 0x5a5a5a5a5a5a5a5aull;
uintptr_t g_canary[8];
__attribute__((noinline)) void fill_canaries(obj holder, int n) {
    for (int i = 0; i < n; i++) {
        obj s = rt_str("silknx-canary-0123456789abcdef");
        if (holder) RT_ELEM(holder, i, obj) = s;
        g_canary[i] = (uintptr_t)s ^ kHide;
    }
}
__attribute__((noinline)) int churn_and_count(int n) {
    int reused = 0;
    for (int k = 0; k < 300000; k++) {
        obj s = rt_str("silknx-canary-0123456789abcdef");
        for (int i = 0; i < n; i++)
            if (((uintptr_t)s ^ kHide) == g_canary[i]) reused++;
    }
    return reused;
}
__attribute__((noinline)) void collect() {
    for (int i = 0; i < 3; i++) ((void (*)(int32_t, const void *))(silk_main_base + 0x4fc4d10))(2, nullptr);
}
}  // namespace

void rt_selftest_gc() {
    obj probe = rt_call<obj>(Array_CreateInstance, get_type("System.Int64, mscorlib"), (int32_t)1);
    void *long_klass = probe->klass;
    // control: unreferenced canaries must get reused, or this test proves nothing
    fill_canaries(nullptr, 8);
    collect();
    int control = churn_and_count(8);
    // object[] holder (kept alive by a GC handle) whose class pointer we swap to long[]
    obj holder = new_object_array(8);
    rt_call<intptr_t>(GCHandle_Alloc, holder);
    fill_canaries(holder, 8);
    holder->klass = (decltype(holder->klass))long_klass;
    collect();
    int swapped = churn_and_count(8);
    holder->klass = (decltype(holder->klass))g_objarr_klass;
    silk::log("selftest gc: unreferenced canaries reused=%d (want >0); referenced from a long[]-tagged object[] "
              "reused=%d (want 0)", control, swapped);

    // Block-compressed textures with dimensions that are not multiples of the block size (most mod sprites).
    struct Case { int32_t w, h, fmt, bw, bh; const char *name; };
    static const Case cases[] = {{76, 332, 12, 4, 4, "DXT5 76x332"}, {75, 331, 48, 4, 4, "ASTC4x4 75x331"},
                                 {75, 331, 50, 6, 6, "ASTC6x6 75x331"}, {75, 331, 51, 8, 8, "ASTC8x8 75x331"}};
    obj t2d = get_type("UnityEngine.Texture2D, UnityEngine.CoreModule");
    for (const Case &cs : cases) {
        obj tex = rt_call<obj>(FormatterServices_GetUninitializedObject, t2d);
        bool ok = false;
        RT_GUARD_BEGIN
        ((void (*)(obj, int32_t, int32_t, int32_t, bool, const void *))(silk_main_base + 0x5b3ef30))(
            tex, cs.w, cs.h, cs.fmt, false, nullptr);
        int32_t blocks = ((cs.w + cs.bw - 1) / cs.bw) * ((cs.h + cs.bh - 1) / cs.bh);
        obj bytes = rt_call<obj>(Array_CreateInstance, get_type("System.Byte, mscorlib"), blocks * 16);
        ((void (*)(obj, obj, const void *))(silk_main_base + 0x5b3ff20))(tex, bytes, nullptr);
        ((void (*)(obj, bool, bool, const void *))(silk_main_base + 0x5b40060))(tex, false, true, nullptr);
        ok = true;
        RT_GUARD_END
        silk::log("selftest %s: %s", cs.name, ok ? "accepted" : "rejected");
    }
}
#endif
