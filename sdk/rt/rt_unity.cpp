// SilkNX runtime: Unity integration (virtual MonoBehaviours, hosts, coroutines, input, on-screen console).
#include "rt.h"
#include "unity.h"

using namespace rva;
using unity::Rect;
using unity::Color;

void rt_boot_plugins(obj manager_go);
void rt_autotest_frame();
void rt_input_frame();
int rt_cursor_gui_begin();
void rt_cursor_gui_end(int restore);
void rt_cursor_draw();

namespace {

// ---------------------------------------------------------------- host registry
// A "host" is a real SlideSurface component on a GameObject; all virtual components of that
// GameObject hang off it. Records live in rooted memory so the GC sees the object references.
constexpr int kMaxComps = 12;
struct HostRec {
    obj host;
    obj go;
    obj comps[kMaxComps];
    int32_t ncomps;
    int32_t active;  // OnEnable seen and not disabled
};

constexpr int kMaxHosts = 2048;
HostRec *g_hosts;
int g_nhosts;
bool g_creating_host;  // AddComponent(SlideSurface) in progress: its messages are ours
obj g_pending_host;

obj g_manager_go;      // SilkNX manager GameObject (plugins live here)
obj g_ub, g_cp;        // UpdateBatcher / CodeProfiler hosts for global messages
bool g_creating_global;

int32_t g_type_go = -1;

obj type_by_name(const char *aqn) { return rt_call<obj>(Type_GetType, rt_str(aqn)); }

HostRec *find_host(obj host) {
    if (!host || !g_hosts) return nullptr;
    for (int i = 0; i < g_nhosts; i++)
        if (g_hosts[i].host == host) return &g_hosts[i];
    return nullptr;
}

HostRec *find_host_go(obj go) {
    if (!go || !g_hosts) return nullptr;
    for (int i = 0; i < g_nhosts; i++)
        if (g_hosts[i].go == go && g_hosts[i].host) return &g_hosts[i];
    return nullptr;
}

HostRec *new_host_rec() {
    for (int i = 0; i < g_nhosts; i++)
        if (!g_hosts[i].host) return &g_hosts[i];
    if (g_nhosts >= kMaxHosts) rt_unsupported("too many GameObjects with mod components");
    return &g_hosts[g_nhosts++];
}

bool go_active(obj go) { return go && rt_call<bool>(GameObject_get_activeInHierarchy, go); }

const char *const kMsgNames[] = {"Awake", "Start", "OnEnable", "OnDisable", "OnDestroy", "Update", "LateUpdate",
                                 "FixedUpdate", "OnGUI", "OnApplicationQuit"};

void call_msg(obj comp, rt_message m) {
    auto *c = (ModComponent *)comp;
    void *fn = c->desc->messages[m];
    if (!fn) return;
    char ctx[160];
    silk::snfmt(ctx, sizeof(ctx), "%s.%s", c->desc->name, kMsgNames[m]);
    const char *saved = g_rt_context;
    g_rt_context = ctx;
    RT_GUARD_BEGIN
    ((void (*)(obj, const void *))fn)(comp, nullptr);
    RT_GUARD_END
    g_rt_context = saved;
}

// ---------------------------------------------------------------- coroutines
struct Coroutine {
    obj stack[8];     // nested IEnumerators (top = last)
    obj handle;       // handle object returned by StartCoroutine
    obj owner;        // ModComponent
    float wait_until; // Time.time to resume at
    obj wait_on;      // yield instruction / handle being waited on
    int32_t depth;
    int32_t alive;
};
constexpr int kMaxCoroutines = 512;
Coroutine *g_co;

rt_vsite s_movenext = {0, -1, -1, true, "MoveNext", "System.Collections.IEnumerator, mscorlib|MoveNext|||i", nullptr,
                       nullptr, nullptr};
rt_vsite s_current = {0, -1, -1, true, "get_Current", "System.Collections.IEnumerator, mscorlib|get_Current|||i",
                      nullptr, nullptr, nullptr};
rt_vsite s_keepwaiting = {0, -1, -1, false, "get_keepWaiting",
                          "UnityEngine.CustomYieldInstruction, UnityEngine.CoreModule|get_keepWaiting|||i", nullptr,
                          nullptr, nullptr};
rt_vsite s_isdone = {0, -1, -1, false, "get_isDone", "UnityEngine.AsyncOperation, UnityEngine.CoreModule|get_isDone|||i",
                     nullptr, nullptr, nullptr};

uint32_t g_mid_movenext, g_mid_current;

bool enum_movenext(obj e) {
    if (rt_is_mod(e)) {
        void *fn = rt_mod_vfn(e, g_mid_movenext, nullptr);
        if (!fn) rt_unsupported("mod IEnumerator without MoveNext");
        return ((bool (*)(obj, const void *))fn)(e, nullptr);
    }
    return ((bool (*)(obj, const void *))rt_vfn(e, &s_movenext))(e, s_movenext.mi);
}

obj enum_current(obj e) {
    if (rt_is_mod(e)) {
        void *fn = rt_mod_vfn(e, g_mid_current, nullptr);
        return fn ? ((obj(*)(obj, const void *))fn)(e, nullptr) : nullptr;
    }
    return ((obj(*)(obj, const void *))rt_vfn(e, &s_current))(e, s_current.mi);
}

const char *klass_name(obj o) { return *(const char **)((uint8_t *)o->klass + 0x10); }

}  // namespace

// Method ids for IEnumerator.MoveNext/get_Current are emitted by the translator.
extern "C" __attribute__((weak)) const uint32_t silk_mid_movenext = 0;
extern "C" __attribute__((weak)) const uint32_t silk_mid_current = 0;

// ======================================================================== components

obj rt_component_go(obj comp) {
    if (rt_is_mod(comp)) return ((ModComponent *)comp)->go;
    return rt_call<obj>(Component_get_gameObject, rt_nc(comp));
}

static obj ensure_host(obj go) {
    HostRec *h = find_host_go(go);
    if (h) return h->host;
    static obj ss_type;
    if (!ss_type) {
        ss_type = type_by_name("SlideSurface, Assembly-CSharp");
        obj *keep = (obj *)rt_root_alloc(8);
        *keep = ss_type;
    }
    h = new_host_rec();
    memset(h, 0, sizeof(*h));
    h->go = go;
    h->host = (obj)1;  // reserve
    g_creating_host = true;
    g_pending_host = nullptr;
    obj host = rt_call<obj>(GameObject_AddComponent, go, ss_type);
    g_creating_host = false;
    h->host = host;
    h->active = go_active(go);
    return host;
}

obj rt_add_component(obj go, const rt_modtype *d) {
    rt_nc(go);
    obj host = ensure_host(go);
    HostRec *h = find_host(host);
    if (!h || h->ncomps >= kMaxComps) rt_unsupported("too many mod components on one GameObject");
    obj comp = rt_new_mod(d);
    auto *c = (ModComponent *)comp;
    c->go = go;
    c->host = host;
    c->enabled = 1;
    h->comps[h->ncomps++] = comp;
    if (d->ctor) {
        RT_GUARD_BEGIN
        ((void (*)(obj, const void *))d->ctor)(comp, nullptr);
        RT_GUARD_END
    }
    if (go_active(go)) {
        c->awoken = 1;
        call_msg(comp, MSG_Awake);
#ifdef SILKNX_TRACE
        silk::log("trace: %s.Awake returned", d->name);
        ((void (*)(int32_t, const void *))(silk_main_base + 0x4fc4d10))(1, nullptr);  // GC.InternalCollect(1)
        silk::log("trace: GC survived");
#endif
        if (c->enabled) call_msg(comp, MSG_OnEnable);
    }
    return comp;
}

obj rt_get_component(obj go, const rt_modtype *d) {
    HostRec *h = find_host_go(go);
    if (!h) return nullptr;
    for (int i = 0; i < h->ncomps; i++)
        if (rt_is_mod_type(h->comps[i], d) && !((ModComponent *)h->comps[i])->destroyed) return h->comps[i];
    return nullptr;
}

static void destroy_comp(HostRec *h, int i) {
    obj comp = h->comps[i];
    auto *c = (ModComponent *)comp;
    if (c->destroyed) return;
    if (c->enabled && c->awoken && h->active) call_msg(comp, MSG_OnDisable);
    if (c->awoken) call_msg(comp, MSG_OnDestroy);
    c->destroyed = 1;
    for (int k = 0; k < kMaxCoroutines; k++)
        if (g_co[k].alive && g_co[k].owner == comp) g_co[k].alive = 0;
    h->comps[i] = h->comps[--h->ncomps];
    h->comps[h->ncomps] = nullptr;
}

void rt_destroy_component(obj comp) {
    if (!rt_is_mod(comp)) {
        rt_call<void>(Object_Destroy, comp);
        return;
    }
    HostRec *h = find_host(((ModComponent *)comp)->host);
    if (!h) return;
    for (int i = 0; i < h->ncomps; i++)
        if (h->comps[i] == comp) {
            destroy_comp(h, i);
            break;
        }
}

void rt_dispatch_message(obj comp, rt_message m) { call_msg(comp, m); }

static void set_enabled(obj comp, bool on) {
    auto *c = (ModComponent *)comp;
    if (c->enabled == on) return;
    c->enabled = on;
    HostRec *h = find_host(c->host);
    if (!h || !h->active || !c->awoken) return;
    call_msg(comp, on ? MSG_OnEnable : MSG_OnDisable);
}

// ======================================================================== host hooks

#define OURS(self) (g_creating_host || find_host(self) != nullptr)

SILK_HOOK(SS_Awake, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_Awake");
    if (OURS(self)) return;
    orig_SS_Awake(self, mi);
}

SILK_HOOK(SS_Start, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_Start");
    if (OURS(self)) return;
    orig_SS_Start(self, mi);
}

SILK_HOOK(SS_OnEnable, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_OnEnable");
    if (g_creating_host) return;
    HostRec *h = find_host(self);
    if (!h) return orig_SS_OnEnable(self, mi);
    h->active = 1;
    for (int i = 0; i < h->ncomps; i++) {
        auto *c = (ModComponent *)h->comps[i];
        if (!c->awoken) {
            c->awoken = 1;
            call_msg(h->comps[i], MSG_Awake);
        }
        if (c->enabled) call_msg(h->comps[i], MSG_OnEnable);
    }
}

SILK_HOOK(SS_OnDisable, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_OnDisable");
    HostRec *h = find_host(self);
    if (!h) return orig_SS_OnDisable(self, mi);
    h->active = 0;
    for (int i = 0; i < h->ncomps; i++) {
        auto *c = (ModComponent *)h->comps[i];
        if (c->enabled && c->awoken) call_msg(h->comps[i], MSG_OnDisable);
    }
}

SILK_HOOK(SS_OnDestroy, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_OnDestroy");
    HostRec *h = find_host(self);
    if (!h) return orig_SS_OnDestroy(self, mi);
    while (h->ncomps) destroy_comp(h, h->ncomps - 1);
    memset(h, 0, sizeof(*h));
}

SILK_HOOK(SS_Update, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_Update");
    HostRec *h = find_host(self);
    if (!h) return orig_SS_Update(self, mi);
    for (int i = 0; i < h->ncomps; i++) {
        obj comp = h->comps[i];
        auto *c = (ModComponent *)comp;
        if (!c->enabled || c->destroyed) continue;
        if (!c->started) {
            c->started = 1;
            call_msg(comp, MSG_Start);
        }
        call_msg(comp, MSG_Update);
    }
}

SILK_HOOK(SS_FixedUpdate, void, obj self, const void *mi) {
    RT_HOOK_TRACE("SS_FixedUpdate");
    HostRec *h = find_host(self);
    if (!h) return orig_SS_FixedUpdate(self, mi);
    for (int i = 0; i < h->ncomps; i++) {
        auto *c = (ModComponent *)h->comps[i];
        if (c->enabled && c->started && !c->destroyed) call_msg(h->comps[i], MSG_FixedUpdate);
    }
}

SILK_HOOK(SS_OnTriggerEnter2D, void, obj self, obj col, const void *mi) {
    RT_HOOK_TRACE("SS_OnTriggerEnter2D");
    if (OURS(self)) return;
    orig_SS_OnTriggerEnter2D(self, col, mi);
}
SILK_HOOK(SS_OnTriggerStay2D, void, obj self, obj col, const void *mi) {
    RT_HOOK_TRACE("SS_OnTriggerStay2D");
    if (OURS(self)) return;
    orig_SS_OnTriggerStay2D(self, col, mi);
}
SILK_HOOK(SS_OnTriggerExit2D, void, obj self, obj col, const void *mi) {
    RT_HOOK_TRACE("SS_OnTriggerExit2D");
    if (OURS(self)) return;
    orig_SS_OnTriggerExit2D(self, col, mi);
}

// ---------------------------------------------------------------- global hosts

static void step_coroutines();

SILK_HOOK(UB_Update, void, obj self, const void *mi) {
    RT_HOOK_TRACE("UB_Update");
    if (g_creating_global) return;
    if (self != g_ub) return orig_UB_Update(self, mi);
    rt_input_frame();
    step_coroutines();
    rt_autotest_frame();
}

SILK_HOOK(UB_LateUpdate, void, obj self, const void *mi) {
    RT_HOOK_TRACE("UB_LateUpdate");
    if (g_creating_global) return;
    if (self != g_ub) return orig_UB_LateUpdate(self, mi);
    for (int i = 0; i < g_nhosts; i++) {
        HostRec &h = g_hosts[i];
        if (!h.host || !h.active) continue;
        for (int k = 0; k < h.ncomps; k++) {
            auto *c = (ModComponent *)h.comps[k];
            if (c->enabled && c->started && !c->destroyed) call_msg(h.comps[k], MSG_LateUpdate);
        }
    }
}

SILK_HOOK(UB_FixedUpdate, void, obj self, const void *mi) {
    RT_HOOK_TRACE("UB_FixedUpdate");
    if (g_creating_global || self == g_ub) return;
    orig_UB_FixedUpdate(self, mi);
}

static void draw_console();

SILK_HOOK(CP_Awake, void, obj self, const void *mi) {
    RT_HOOK_TRACE("CP_Awake");
    if (g_creating_global || self == g_cp) return;
    orig_CP_Awake(self, mi);
}

SILK_HOOK(CP_Update, void, obj self, const void *mi) {
    RT_HOOK_TRACE("CP_Update");
    if (g_creating_global || self == g_cp) return;
    orig_CP_Update(self, mi);
}

SILK_HOOK(CP_OnGUI, void, obj self, const void *mi) {
    RT_HOOK_TRACE("CP_OnGUI");
    if (self != g_cp) return orig_CP_OnGUI(self, mi);
    int restore = rt_cursor_gui_begin();
    for (int i = 0; i < g_nhosts; i++) {
        HostRec &h = g_hosts[i];
        if (!h.host || !h.active) continue;
        for (int k = 0; k < h.ncomps; k++) {
            auto *c = (ModComponent *)h.comps[k];
#ifdef SILKNX_SKIP_ONGUI  // debug bisect: skip OnGUI of components whose type name starts with this
            if (startswith_(c->desc->name, SILKNX_SKIP_ONGUI)) continue;
#endif
            if (c->enabled && c->awoken && !c->destroyed) call_msg(h.comps[k], MSG_OnGUI);
        }
    }
#ifdef SILKNX_TRACE_ALL
    if (g_rt_trace_budget-- > 0) silk::log("trace: OnGUI mods done");
#endif
    rt_cursor_gui_end(restore);
    draw_console();
    rt_cursor_draw();
#ifdef SILKNX_TRACE_ALL
    if (g_rt_trace_budget-- > 0) silk::log("trace: console done");
#endif
}

// ======================================================================== coroutines

static obj new_handle() {
    static rt_modtype desc = {"UnityEngine.Coroutine(SilkNX)", nullptr, sizeof(ModObj) + 8, 0, false, nullptr, 0,
                              nullptr, false, nullptr, 0, nullptr, {}, nullptr};
    return rt_new_mod(&desc);
}

obj rt_start_coroutine(obj owner, obj enumerator) {
    if (!enumerator) return nullptr;
    for (int i = 0; i < kMaxCoroutines; i++) {
        Coroutine &c = g_co[i];
        if (c.alive) continue;
        memset(&c, 0, sizeof(c));
        c.alive = 1;
        c.owner = owner;
        c.stack[0] = enumerator;
        c.depth = 1;
        c.handle = new_handle();
#ifdef SILKNX_TRACE
        silk::log("trace: coroutine %d started", i);
#endif
        return c.handle;
    }
    rt_unsupported("too many coroutines");
}

void rt_stop_coroutine(obj owner, obj handle_or_enum) {
    for (int i = 0; i < kMaxCoroutines; i++) {
        Coroutine &c = g_co[i];
        if (c.alive && c.owner == owner && (c.handle == handle_or_enum || c.stack[0] == handle_or_enum)) c.alive = 0;
    }
}

void rt_stop_all_coroutines(obj owner) {
    for (int i = 0; i < kMaxCoroutines; i++)
        if (g_co[i].alive && g_co[i].owner == owner) g_co[i].alive = 0;
}

static bool handle_running(obj handle) {
    for (int i = 0; i < kMaxCoroutines; i++)
        if (g_co[i].alive && g_co[i].handle == handle) return true;
    return false;
}

static bool still_waiting(Coroutine &c, float now) {
    if (c.wait_until > now) return true;
    obj w = c.wait_on;
    if (!w) return false;
    if (rt_is_mod(w)) return handle_running(w);
    const char *n = klass_name(w);
    if (!strcmp_(n, "WaitUntil") || !strcmp_(n, "WaitWhile") || !strcmp_(n, "WaitForSecondsRealtime")) {
        return ((bool (*)(obj, const void *))rt_vfn(w, &s_keepwaiting))(w, s_keepwaiting.mi);
    }
    if (!strcmp_(n, "AsyncOperation") || !strcmp_(n, "ResourceRequest") || !strcmp_(n, "AssetBundleRequest") ||
        !strcmp_(n, "AssetBundleCreateRequest")) {
        return !((bool (*)(obj, const void *))rt_vfn(w, &s_isdone))(w, s_isdone.mi);
    }
    return false;
}

static void step_one(Coroutine &c, float now) {
    if (still_waiting(c, now)) return;
    c.wait_on = nullptr;
    while (c.alive && c.depth > 0) {
        obj e = c.stack[c.depth - 1];
        bool more = false;
        bool ok = false;
        RT_GUARD_BEGIN
        more = enum_movenext(e);
        ok = true;
        RT_GUARD_END
        if (!ok) {
            c.alive = 0;
            return;
        }
        if (!more) {
            c.stack[--c.depth] = nullptr;
            if (!c.depth) c.alive = 0;
            continue;  // resume the parent immediately
        }
        obj y = enum_current(e);
        if (!y) return;  // next frame
        if (rt_is_mod(y)) {
            if (rt_mod_vfn(y, g_mid_movenext, nullptr)) {  // nested mod IEnumerator
                if (c.depth < 8) c.stack[c.depth++] = y;
                continue;
            }
            c.wait_on = y;  // coroutine handle
            return;
        }
        const char *n = klass_name(y);
        if (!strcmp_(n, "WaitForSeconds")) {
            float secs = *(float *)((uint8_t *)y + 0x10);
            c.wait_until = now + secs;
            return;
        }
        if (!strcmp_(n, "WaitForEndOfFrame") || !strcmp_(n, "WaitForFixedUpdate")) return;
        // nested game IEnumerator?
        static rt_vsite s_is = {0, -1, -1, true, "", "", nullptr, nullptr, nullptr};
        (void)s_is;
        c.wait_on = y;
        return;
    }
}

static void step_coroutines() {
    float now = rt_call<float>(Time_get_time);
    for (int i = 0; i < kMaxCoroutines; i++) {
        Coroutine &c = g_co[i];
        if (!c.alive) continue;
        auto *owner = (ModComponent *)c.owner;
        if (owner && (owner->destroyed || !owner->enabled)) continue;
        step_one(c, now);
    }
}

// ======================================================================== on-screen console

namespace {
constexpr int kLines = 10;
char g_lines[kLines][160];
float g_line_time[kLines];
int g_line_head;
float g_now;
}  // namespace

void rt_console(const char *fmt, ...) {
    char buf[160];
    __builtin_va_list ap;
    __builtin_va_start(ap, fmt);
    silk::vsnfmt(buf, sizeof(buf), fmt, ap);
    __builtin_va_end(ap);
    // Repeats of the most recent message just refresh it with a count.
    int last = (g_line_head + kLines - 1) % kLines;
    static int repeat = 1;
    static char last_raw[160];
    if (g_lines[last][0] && !strcmp_(last_raw, buf)) {
        repeat++;
        silk::snfmt(g_lines[last], sizeof(g_lines[last]), "%s  (x%d)", buf, repeat);
        g_line_time[last] = g_now + 8.0f;
        return;
    }
    repeat = 1;
    memcpy(last_raw, buf, sizeof(buf));
    memcpy(g_lines[g_line_head], buf, sizeof(buf));
    g_line_time[g_line_head] = g_now + 8.0f;
    g_line_head = (g_line_head + 1) % kLines;
}

static void draw_console() {
    void *ev = rt_call<void *>(Event_get_current);
    if (!ev || rt_call<int>(Event_get_type, ev) != unity::EventType_Repaint) return;
    g_now = rt_call<float>(Time_get_unscaledTime);
    float sh = (float)rt_call<int>(Screen_get_height);
    float k = sh / 1080.0f;
    obj skin = rt_call<obj>(GUI_get_skin);
    obj style = skin ? rt_call<obj>(GUISkin_get_label, skin) : nullptr;
    if (!style) return;
    rt_call<void>(GUIStyle_set_fontSize, style, (int32_t)(18 * k));
    rt_call<void>(GUIStyle_set_alignment, style, (int32_t)unity::TextAnchor_MiddleLeft);
    float y = 12 * k;
    for (int i = 0; i < kLines; i++) {
        int idx = (g_line_head + i) % kLines;
        if (!g_lines[idx][0] || g_line_time[idx] < g_now) continue;
        float a = g_line_time[idx] - g_now < 1 ? g_line_time[idx] - g_now : 1;
        obj s = rt_str(g_lines[idx]);
        rt_call<void>(GUI_set_color, Color{0, 0, 0, a});
        rt_call<void>(GUI_Label, Rect{14 * k, y + 2 * k, 1800 * k, 26 * k}, s);
        rt_call<void>(GUI_set_color, Color{1, 0.92f, 0.7f, a});
        rt_call<void>(GUI_Label, Rect{12 * k, y, 1800 * k, 26 * k}, s);
        y += 24 * k;
    }
    rt_call<void>(GUI_set_color, Color{1, 1, 1, 1});
}

// ======================================================================== boot

static obj new_gameobject(const char *name) {
    obj t = type_by_name("UnityEngine.GameObject, UnityEngine.CoreModule");
    obj go = rt_call<obj>(FormatterServices_GetUninitializedObject, t);
    rt_call<void>(GameObject_ctor, go, rt_str(name));
    return go;
}

#ifdef SILKNX_SELFTEST
void rt_selftest_gc();
#endif
SILK_HOOK(RT_Boot, void, const void *mi) {
    RT_HOOK_TRACE("RT_Boot");
    orig_RT_Boot(mi);
    if (rt_ready()) return;
    rt_boot_core();
#ifdef SILKNX_SELFTEST
    try {
        rt_call<int32_t>(Int32_Parse, rt_str("not a number"));
        silk::log("selftest: no exception?");
    } catch (...) {
        silk::log("selftest: caught a game exception natively - exception bridge OK");
    }
    RT_GUARD_BEGIN
    rt_call<int32_t>(Int32_Parse, rt_str("still not a number"));
    RT_GUARD_END
    rt_selftest_gc();
#endif
    g_hosts = (HostRec *)rt_root_alloc(sizeof(HostRec) * kMaxHosts);
    g_co = (Coroutine *)rt_root_alloc(sizeof(Coroutine) * kMaxCoroutines);
    g_mid_movenext = silk_mid_movenext;
    g_mid_current = silk_mid_current;
    obj *roots = (obj *)rt_root_alloc(8 * 4);
    g_manager_go = roots[0] = new_gameobject("SilkNX");
    rt_call<void>(Object_DontDestroyOnLoad, g_manager_go);
    g_creating_global = true;
    g_ub = roots[1] = rt_call<obj>(GameObject_AddComponent, g_manager_go, type_by_name("UpdateBatcher, Assembly-CSharp"));
    g_cp = roots[2] = rt_call<obj>(GameObject_AddComponent, g_manager_go, type_by_name("CodeProfiler, Assembly-CSharp"));
    g_creating_global = false;
    silk::log("SilkNX runtime ready (manager=%p ub=%p cp=%p)", g_manager_go, g_ub, g_cp);
    rt_console("SilkNX: %u mod(s) loaded", silk_plugin_count);
    rt_boot_plugins(g_manager_go);
#ifdef SILKNX_TRACE
    silk::log("trace: boot done");
#endif
}

void silk_main() { silk::log("SilkNX module loaded; waiting for the game to boot"); }

// set_enabled is used by the Behaviour.enabled shim
void rt_set_enabled(obj comp, bool on) { set_enabled(comp, on); }

// All live virtual components of a mod type (FindObjectsByType<T> / FindObjectsOfType<T>).
obj rt_find_components(const rt_modtype *d) {
    int n = 0;
    for (int i = 0; i < g_nhosts; i++)
        for (int k = 0; k < g_hosts[i].ncomps; k++)
            if (rt_is_mod_type(g_hosts[i].comps[k], d) && !((ModComponent *)g_hosts[i].comps[k])->destroyed) n++;
    obj arr = rt_call<obj>(Array_CreateInstance, rt_call<obj>(Type_GetType, rt_str("System.Object, mscorlib")), (int32_t)n);
    int j = 0;
    for (int i = 0; i < g_nhosts; i++)
        for (int k = 0; k < g_hosts[i].ncomps && j < n; k++)
            if (rt_is_mod_type(g_hosts[i].comps[k], d) && !((ModComponent *)g_hosts[i].comps[k])->destroyed)
                RT_ELEM(arr, j++, obj) = g_hosts[i].comps[k];
    return arr;
}

// GUIContent(string text, string tooltip) was stripped from the Switch build; the 3-argument one remains.
obj shim_UnityEngine_GUIContent___ctor__string_string(obj text, obj tooltip) {
    static obj type;
    if (!type) {
        type = type_by_name("UnityEngine.GUIContent, UnityEngine.IMGUIModule");
        *(obj *)rt_root_alloc(8) = type;
    }
    obj c = rt_call<obj>(FormatterServices_GetUninitializedObject, type);
    rt_call<void>(GUIContent_ctor_text_image_tooltip, c, text, (obj) nullptr, tooltip);
    return c;
}

// ======================================================================== textures
// Mod PNGs are converted at build time (tools/silkport/textures.py): the PNG becomes a header-only stub with a
// private 'snXa' chunk naming an ASTC file, which is uploaded directly (Switch memory budget, no decode).
static bool astc_stub_path(obj bytes, char *out, size_t cap) {
    if (!bytes) return false;
    auto *a = (Il2CppArray *)bytes;
    const uint8_t *b = (const uint8_t *)bytes + 0x20;
    intptr_t n = (intptr_t)a->max_length;
    static const uint8_t sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
    if (n < 8 + 25 + 12 || memcmp(b, sig, 8) != 0) return false;
    const uint8_t *c = b + 8 + 25;  // chunk after IHDR
    uint32_t len = ((uint32_t)c[0] << 24) | ((uint32_t)c[1] << 16) | ((uint32_t)c[2] << 8) | c[3];
    if (memcmp(c + 4, "snXa", 4) != 0 || len == 0 || len >= cap || 8 + 25 + 12 + (intptr_t)len > n) return false;
    memcpy(out, c + 8, len);
    out[len] = 0;
    return true;
}

static bool load_astc(obj tex, const char *path, bool non_readable) {
    obj data = rt_call<obj>(File_ReadAllBytes, rt_str(path));
    auto *a = (Il2CppArray *)rt_nc(data);
    const uint8_t *d = (const uint8_t *)data + 0x20;
    if ((intptr_t)a->max_length < 12 || memcmp(d, "SNXA", 4) != 0) return false;
    int32_t w = d[4] | (d[5] << 8), h = d[6] | (d[7] << 8), fmt = d[10];
    rt_call<void>(Texture2D_Reinitialize_format, rt_nc(tex), w, h, fmt, false);
    rt_call<void>(Texture2D_LoadRawTextureData_ptr, tex, (intptr_t)(d + 12), (int32_t)(a->max_length - 12));
    rt_call<void>(Texture2D_Apply2, tex, false, non_readable);
    return true;
}

bool shim_UnityEngine_ImageConversion__LoadImage__UnityEngine_Texture2D_byteArr(obj tex, obj bytes) {
    char path[512];
    if (astc_stub_path(bytes, path, sizeof(path))) return load_astc(tex, path, false);
    return rt_call<bool>(ImageConversion_LoadImage, tex, bytes);
}

bool shim_UnityEngine_ImageConversion__LoadImage__UnityEngine_Texture2D_byteArr_bool(obj tex, obj bytes,
                                                                                     bool non_readable) {
    char path[512];
    if (astc_stub_path(bytes, path, sizeof(path))) return load_astc(tex, path, non_readable);
    bool ok = rt_call<bool>(ImageConversion_LoadImage, tex, bytes);
    if (ok && non_readable) rt_call<void>(Texture2D_Apply2, tex, false, true);
    return ok;
}

// ---------------------------------------------------------------- graphics wrappers stripped from the Switch build
// Same struct name the converter emits for UnityEngine.Rect (name = V_ + sanitized type + hash of the type name).
struct V_UnityEngine_Rect_79cb36 {
    float x, y, width, height;
};

// Graphics.CopyTexture is gone entirely: report no CopyTexture support so mods take their Blit/DrawTexture path.
int32_t shim_UnityEngine_SystemInfo__get_copyTextureSupport() { return 0; }

bool shim_UnityEngine_RenderTexture__Create(obj self) {
    return rt_call<bool>(RenderTexture_Internal_Create, rt_nc(self));
}

void shim_UnityEngine_Texture__IncrementUpdateCount(obj) {}  // change counter for editor tooling only

// Graphics.DrawTexture(Rect, Texture): what Unity's wrapper does - full source rect, neutral GUI colour (0.5),
// no borders, default material.
void shim_UnityEngine_Graphics__DrawTexture__UnityEngine_Rect_UnityEngine_Texture(V_UnityEngine_Rect_79cb36 r,
                                                                                    obj tex) {
    struct Args {  // UnityEngine.Internal_DrawTextureArguments (unboxed layout from the dump)
        float screen[4], source[4];
        int32_t borders[4];
        float border_colors[4][4], color[4], border_widths[4], corner_radiuses[4];
        bool smooth_corners;
        int32_t pass;
        obj texture, mat;
    };
    static_assert(sizeof(Args) == 0xb8, "Internal_DrawTextureArguments layout");
    Args a{};
    a.screen[0] = r.x, a.screen[1] = r.y, a.screen[2] = r.width, a.screen[3] = r.height;
    a.source[2] = a.source[3] = 1.0f;
    for (auto &c : a.border_colors) c[3] = 1.0f;  // Color.black
    for (float &c : a.color) c = 0.5f;
    a.smooth_corners = true;
    a.pass = -1;
    a.texture = tex;
    rt_call<void>(Graphics_Internal_DrawTexture, &a);
}

// ---------------------------------------------------------------- render texture budget
// Mods written for PC build atlases at PC resolution in RenderTextures (4096^2 RGBA = 64 MB each), which the
// Switch's memory budget can't hold. Large ones are created at a reduced scale (about the Switch build's own
// asset density) while width/height keep reporting the size the mod asked for, so every coordinate it
// derives (GL.LoadPixelMatrix, DrawTexture rects, normalized UVs) stays consistent - drawing just lands
// downsampled.
namespace {
constexpr int32_t kMaxRtSide = 2048;
struct ScaledRt {
    obj rt;  // not GC-traced: validated with the instance id
    int32_t id, w, h;
};
ScaledRt g_scaled_rt[256];
obj g_rt_type;

const ScaledRt *scaled_rt(obj o) {
    if (!o) return nullptr;
    for (const ScaledRt &s : g_scaled_rt)
        if (s.rt == o && rt_call<int32_t>(Object_GetInstanceID, o) == s.id) return &s;
    return nullptr;
}

obj new_render_texture(int32_t w, int32_t h, int32_t depth, int32_t fmt, int32_t rw, int nargs) {
    if (!g_rt_type) {
        g_rt_type = type_by_name("UnityEngine.RenderTexture, UnityEngine.CoreModule");
        *(obj *)rt_root_alloc(8) = g_rt_type;
    }
    int32_t side = w > h ? w : h;
    int32_t aw = w, ah = h;
    if (side > kMaxRtSide) {
        aw = (int32_t)((int64_t)w * kMaxRtSide / side);
        ah = (int32_t)((int64_t)h * kMaxRtSide / side);
        if (aw < 1) aw = 1;
        if (ah < 1) ah = 1;
    }
    obj rt = rt_call<obj>(FormatterServices_GetUninitializedObject, g_rt_type);
    if (nargs == 5) rt_call<void>(RenderTexture_ctor5, rt, aw, ah, depth, fmt, rw);
    else if (nargs == 4) rt_call<void>(RenderTexture_ctor4, rt, aw, ah, depth, fmt);
    else rt_call<void>(RenderTexture_ctor3, rt, aw, ah, depth);
    if (aw != w || ah != h) {
        static int slot;
        g_scaled_rt[slot] = {rt, rt_call<int32_t>(Object_GetInstanceID, rt), w, h};
        slot = (slot + 1) % 256;
        static int logged;
        if (logged++ < 8)
            silk::log("mod RenderTexture %dx%d created as %dx%d (Switch memory budget)", w, h, aw, ah);
    }
    return rt;
}

rt_vsite s_tex_width = {0, -1, -1, false, "get_width", "UnityEngine.Texture, UnityEngine.CoreModule|get_width|||i",
                        nullptr, nullptr, nullptr};
rt_vsite s_tex_height = {0, -1, -1, false, "get_height",
                         "UnityEngine.Texture, UnityEngine.CoreModule|get_height|||i", nullptr, nullptr, nullptr};
int32_t tex_dim(obj self, bool width) {
    rt_nc(self);
    if (const ScaledRt *s = scaled_rt(self)) return width ? s->w : s->h;
    rt_vsite *site = width ? &s_tex_width : &s_tex_height;
    return ((int32_t(*)(obj, const void *))rt_vfn(self, site))(self, site->mi);
}
}  // namespace

obj shim_UnityEngine_RenderTexture___ctor__int_int_int_UnityEngine_RenderTextureFormat_UnityEngine_RenderTextureReadWrite(
    int32_t w, int32_t h, int32_t depth, int32_t fmt, int32_t rw) {
    return new_render_texture(w, h, depth, fmt, rw, 5);
}
obj shim_UnityEngine_RenderTexture___ctor__int_int_int_UnityEngine_RenderTextureFormat(int32_t w, int32_t h,
                                                                                       int32_t depth, int32_t fmt) {
    return new_render_texture(w, h, depth, fmt, 0, 4);
}
obj shim_UnityEngine_RenderTexture___ctor__int_int_int(int32_t w, int32_t h, int32_t depth) {
    return new_render_texture(w, h, depth, 0, 0, 3);
}
int32_t shim_UnityEngine_Texture__get_width(obj self) { return tex_dim(self, true); }
int32_t shim_UnityEngine_Texture__get_height(obj self) { return tex_dim(self, false); }
int32_t shim_UnityEngine_RenderTexture__get_width(obj self) { return tex_dim(self, true); }
int32_t shim_UnityEngine_RenderTexture__get_height(obj self) { return tex_dim(self, false); }
