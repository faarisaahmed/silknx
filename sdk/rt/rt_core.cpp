// SilkNX runtime core: memory, strings, types, objects, dispatch, exceptions.
#include "rt.h"

extern "C" uint64_t rt_thread_id();
extern "C" char __bss_end[];

using namespace rva;

namespace {

bool g_ready;
obj g_objarr_type;     // typeof(object)
void *g_objarr_klass;  // klass of object[]
void *g_string_klass;
obj g_type_type;       // typeof(System.Type)

// Il2CppClass / MethodInfo layout, calibrated at boot.
uint32_t MI_KLASS = 0x20;
uint32_t MI_NAME = 0x18;
uint32_t KLASS_STATIC_FIELDS = 0xB8;
uint32_t KLASS_NAME = 0x10;

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

void add_root_chunk(size_t slots) {
    obj arr = new_object_array((intptr_t)slots);
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

void *g_rt_objarr_klass;

bool rt_ready() { return g_ready; }

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
    if (!t) rt_unsupported(silk_typerefs[tref].aqn ? silk_typerefs[tref].aqn : "new of unknown type");
    return rt_call<obj>(FormatterServices_GetUninitializedObject, t);
}

obj rt_new_mod(const rt_modtype *d) {
    size_t total = d->is_vt ? sizeof(ModObj) + d->size : d->size;
    intptr_t slots = (intptr_t)((total - 0x20 + 7) / 8);
    obj o = new_object_array(slots);
    ((ModObj *)o)->desc = d;
    return o;
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
    if (!o || o->klass != g_objarr_klass) return false;
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
        if (d->cctor) ((void (*)(const void *))d->cctor)(nullptr);
    }
    return d->statics;
}

// ---------------------------------------------------------------- arrays

obj rt_new_array(int32_t tref, intptr_t n, size_t elem_size) {
    if (n < 0) rt_unsupported("negative array size");
    if (tref & 0x40000000) rt_unsupported("arrays of mod-defined structs");
    obj t = rt_type(tref);
    if (!t) rt_unsupported("array of unknown element type");
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
    obj m;
    if (ctor) {
        m = rt_call<obj>(Type_GetConstructor4, owner, (int32_t)(4 | 16 | 32), (obj) nullptr, ptypes, (obj) nullptr);
        if (!m) return nullptr;
        return *(void **)((uint8_t *)m + off::RuntimeConstructorInfo_mhandle);
    }
    m = rt_call<obj>(Type_GetMethod5, owner, rt_str(name), (int32_t)(4 | 8 | 16 | 32), (obj) nullptr, ptypes,
                     (obj) nullptr);
    if (!m && *margs) {
        // generic method definitions are not found by parameter types; retry by name only
        m = rt_call<obj>(Type_GetMethod5, owner, rt_str(name), (int32_t)(4 | 8 | 16 | 32), (obj) nullptr,
                         (obj) nullptr, (obj) nullptr);
    }
    if (!m) return nullptr;
    if (*margs) {
        int nm = 0;
        obj gtypes = type_array(margs, &nm);
        m = rt_call<obj>(RuntimeMethodInfo_MakeGenericMethod, m, gtypes);
        if (!m) return nullptr;
    }
    return method_handle(m);
}

rt_rsite *rt_site_resolve(rt_rsite *s) {
    if (s->fp) return s;
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
    return s;
}

static rt_vsite g_tostring_site = {0, 3, -1, false, "ToString", "System.Object, mscorlib|ToString|||i",
                                   nullptr, nullptr, nullptr};

void *rt_vfn(obj o, rt_vsite *site) {
    rt_nc(o);
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

obj rt_new_delegate(int32_t tref, void *fn, obj target, bool is_static) {
    obj d = rt_new_obj(tref);
    uint8_t *p = (uint8_t *)d;
    *(void **)(p + off::Delegate_method_ptr) = fn;
    *(void **)(p + off::Delegate_invoke_impl) = fn;
    *(obj *)(p + off::Delegate_m_target) = target;
    *(obj *)(p + off::Delegate_method_code) = is_static ? d : target;
    *(void **)(p + off::Delegate_method) = nullptr;
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

obj rt_delegate_target(obj d) {
    if (rt_is_mod(d)) return ((ModDelegate *)d)->target;
    return *(obj *)((uint8_t *)d + off::Delegate_method_code);
}

const void *rt_delegate_mi(obj d) {
    if (rt_is_mod(d)) return nullptr;
    return *(void **)((uint8_t *)d + off::Delegate_method);
}

// ---------------------------------------------------------------- exceptions

void rt_push(rt_handler *h) {
    ThreadEH *t = eh();
    h->prev = t->top;
    t->top = h;
}

void rt_pop(rt_handler *h) {
    ThreadEH *t = eh();
    if (t->top == h) {
        t->top = h->prev;
        return;
    }
    for (rt_handler *p = t->top; p; p = p->prev)
        if (p->prev == h) {
            p->prev = h->prev;
            return;
        }
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
    silk::snfmt(buf, cap, "%s.%s", ns, name);
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

void rt_throw_nre() { rt_throw(make_exception("System.NullReferenceException, mscorlib")); }

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

void rt_report_exception(const char *where) {
    char d[300];
    describe_exception(rt_exception(), d, sizeof(d));
    silk::log("exception in %s: %s", where, d);
    rt_console("exception in %s: %s", where, d);
}

// ---------------------------------------------------------------- logging helpers

void rt_log_obj(const char *prefix, obj msg) {
    char buf[600];
    if (!msg) {
        silk::log("%s(null)", prefix);
        rt_console("%s(null)", prefix);
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
    rt_console("%s%s", prefix, buf);
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
    return true;
}

void rt_boot_core() {
    obj s = rt_str16(u"x", 1);
    g_string_klass = s->klass;
    g_objarr_type = rt_call<obj>(Type_GetType, rt_str("System.Object, mscorlib"));
    g_type_type = rt_call<obj>(Type_GetType, rt_str("System.Type, mscorlib"));
    obj probe = new_object_array(1);
    g_objarr_klass = probe->klass;
    g_rt_objarr_klass = g_objarr_klass;
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
