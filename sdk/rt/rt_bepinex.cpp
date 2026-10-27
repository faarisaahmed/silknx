// SilkNX runtime: BepInEx / HarmonyX / Unity API shims used by translated plugins.
//
// Shim names are derived by the translator from the API they replace:
//   shim_<Type>__<Method>[__<param types>][_G<generic arg count>]
// BepInEx/Harmony shims receive the calling assembly index as a trailing argument; generic shims receive the
// type-reference index of each generic argument.
#include "rt.h"

using namespace rva;

obj rt_start_coroutine(obj owner, obj enumerator);
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_float_string_G1(obj, obj, obj, float, obj, int32_t, int32_t);
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_obj_string_G1(obj, obj, obj, obj, obj, int32_t, int32_t);
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_bool_string_G1(obj, obj, obj, bool, obj, int32_t, int32_t);
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_int32_t_string_G1(obj, obj, obj, int32_t, obj, int32_t, int32_t);
float shim_BepInEx_Configuration_ConfigEntry_1_float__get_Value(obj, int32_t);
void rt_stop_coroutine(obj owner, obj handle_or_enum);
void rt_stop_all_coroutines(obj owner);
void rt_set_enabled(obj comp, bool on);

namespace {

struct EmuLogger : ModObj {
    obj name;
};
struct EmuHarmony : ModObj {
    obj id;
    int32_t asm_index;
};

rt_modtype d_logger = {"BepInEx.Logging.ManualLogSource", nullptr, sizeof(EmuLogger), 0, false, nullptr, 0,
                       nullptr, false, nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_harmony = {"HarmonyLib.Harmony", nullptr, sizeof(EmuHarmony), 0, false, nullptr, 0, nullptr, false,
                        nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_asmtoken = {"System.Reflection.Assembly(SilkNX)", nullptr, sizeof(ModObj) + 8, 0, false, nullptr, 0,
                         nullptr, false, nullptr, 0, nullptr, {}, nullptr};

const rt_plugin *plugin_for(obj self) {
    for (uint32_t i = 0; i < silk_plugin_count; i++)
        if (rt_is_mod_type(self, silk_plugins[i].desc)) return &silk_plugins[i];
    return nullptr;
}

void apply_patches_for_asm(int32_t asm_index) {
    int n = 0;
    for (uint32_t i = 0; i < silk_patch_class_count; i++)
        if (silk_patch_classes[i].asm_index == asm_index && !silk_patch_classes[i].applied) {
            silk_patch_classes[i].applied = true;
            n++;
        }
    silk::log("Harmony: %s applied %d patch class(es)", silk_asm_names[asm_index], n);
}

void apply_patches_for_type(rt_modtype *d) {
    for (uint32_t i = 0; i < silk_patch_class_count; i++)
        if (silk_patch_classes[i].desc == d) silk_patch_classes[i].applied = true;
}

bool unity_null(obj o) {
    if (!o) return true;
    if (rt_is_mod(o)) {
        auto *m = (ModObj *)o;
        if (m->desc->emu == 1 || m->desc->emu == 2) return ((ModComponent *)o)->destroyed;
        return false;
    }
    return *(void **)((uint8_t *)o + off::Object_m_CachedPtr) == nullptr;
}

}  // namespace

// ======================================================================== plugin boot

void rt_boot_plugins(obj manager_go) {
    for (uint32_t i = 0; i < silk_plugin_count; i++) {
        const rt_plugin &p = silk_plugins[i];
        silk::log("loading plugin %s %s (%s)", p.name, p.version, p.guid);
        rt_add_component(manager_go, p.desc);
    }
}

// ======================================================================== BepInEx

void shim_BepInEx_BaseUnityPlugin___ctor(obj self, int32_t asm_index) {
    auto *pl = (ModPlugin *)self;
    const rt_plugin *p = plugin_for(self);
    obj lg = rt_new_mod(&d_logger);
    ((EmuLogger *)lg)->name = rt_str(p ? p->name : silk_asm_names[asm_index]);
    pl->logger = lg;
    pl->plugin_index = p ? (int32_t)(p - silk_plugins) : -1;
}

obj shim_BepInEx_BaseUnityPlugin__get_Logger(obj self, int32_t) { return ((ModPlugin *)rt_nc(self))->logger; }

static void log_with(obj self, const char *level, obj msg) {
    char name[64] = "mod", prefix[96];
    if (rt_is_mod(self)) rt_str_to_utf8(((EmuLogger *)self)->name, name, sizeof(name));
    silk::snfmt(prefix, sizeof(prefix), "[%s%s] ", strcmp_(level, "msg ") ? level : "", name);
    // Everything goes to the log; the screen only gets messages meant for the player (LogMessage) and errors,
    // each distinct error once (mods often repeat theirs on every room change).
    bool err = level[0] == 'E' || level[0] == 'F';
    rt_log_obj(prefix, msg, err || !strcmp_(level, "msg "), err);
}

void shim_BepInEx_Logging_ManualLogSource__LogInfo__object(obj self, obj msg, int32_t) { log_with(self, "", msg); }
void shim_BepInEx_Logging_ManualLogSource__LogMessage__object(obj self, obj msg, int32_t) { log_with(self, "msg ", msg); }
void shim_BepInEx_Logging_ManualLogSource__LogDebug__object(obj self, obj msg, int32_t) { log_with(self, "debug ", msg); }
void shim_BepInEx_Logging_ManualLogSource__LogWarning__object(obj self, obj msg, int32_t) { log_with(self, "warn ", msg); }
void shim_BepInEx_Logging_ManualLogSource__LogError__object(obj self, obj msg, int32_t) { log_with(self, "ERROR ", msg); }
void shim_BepInEx_Logging_ManualLogSource__LogFatal__object(obj self, obj msg, int32_t) { log_with(self, "FATAL ", msg); }
void shim_BepInEx_Logging_ManualLogSource__Log__BepInEx_Logging_LogLevel_object(obj self, int32_t level, obj msg,
                                                                               int32_t) {
    // BepInEx.Logging.LogLevel flags: Fatal 1, Error 2, Warning 4, Message 8, Info 16, Debug 32
    const char *tag = (level & 1) ? "FATAL " : (level & 2) ? "ERROR " : (level & 4) ? "warn " : (level & 8) ? "msg " : (level & 16) ? "" : "debug ";
    log_with(self, tag, msg);
}

// ======================================================================== Harmony

obj shim_HarmonyLib_Harmony___ctor__string(obj id, int32_t asm_index) {
    obj h = rt_new_mod(&d_harmony);
    ((EmuHarmony *)h)->id = id;
    ((EmuHarmony *)h)->asm_index = asm_index;
    return h;
}

void shim_HarmonyLib_Harmony__PatchAll(obj self, int32_t asm_index) { apply_patches_for_asm(asm_index); }

void shim_HarmonyLib_Harmony__PatchAll__System_Reflection_Assembly(obj self, obj asm_obj, int32_t asm_index) {
    int32_t a = asm_obj && rt_is_mod(asm_obj) ? *(int32_t *)((uint8_t *)asm_obj + sizeof(ModObj)) : asm_index;
    apply_patches_for_asm(a);
}

obj shim_System_Reflection_Assembly__GetExecutingAssembly(int32_t asm_index) {
    obj t = rt_new_mod(&d_asmtoken);
    *(int32_t *)((uint8_t *)t + sizeof(ModObj)) = asm_index;
    return t;
}

obj shim_HarmonyLib_Harmony__CreateAndPatchAll__System_Type_string(obj type, obj id, int32_t asm_index) {
    obj h = shim_HarmonyLib_Harmony___ctor__string(id, asm_index);
    rt_modtype *d = rt_modtype_of_typeobj(type);
    if (d) apply_patches_for_type(d);
    return h;
}

obj shim_HarmonyLib_Harmony__CreateAndPatchAll__System_Reflection_Assembly_string(obj asm_obj, obj id,
                                                                                  int32_t asm_index) {
    obj h = shim_HarmonyLib_Harmony___ctor__string(id, asm_index);
    shim_HarmonyLib_Harmony__PatchAll__System_Reflection_Assembly(h, asm_obj, asm_index);
    return h;
}

void shim_HarmonyLib_Harmony__UnpatchSelf(obj self, int32_t asm_index) {
    for (uint32_t i = 0; i < silk_patch_class_count; i++)
        if (silk_patch_classes[i].asm_index == asm_index) silk_patch_classes[i].applied = false;
}

// ======================================================================== Unity: components

obj shim_UnityEngine_Component__get_gameObject(obj self) {
    if (rt_is_mod(self)) return ((ModComponent *)self)->go;
    return rt_call<obj>(Component_get_gameObject, rt_nc(self));
}

obj shim_UnityEngine_Component__get_transform(obj self) {
    if (rt_is_mod(self)) return rt_call<obj>(GameObject_get_transform, ((ModComponent *)self)->go);
    return rt_call<obj>(Component_get_transform, rt_nc(self));
}

bool shim_UnityEngine_Behaviour__get_enabled(obj self) {
    if (rt_is_mod(self)) return ((ModComponent *)self)->enabled;
    return rt_call<bool>(Behaviour_get_enabled, rt_nc(self));
}

void shim_UnityEngine_Behaviour__set_enabled__bool(obj self, bool on) {
    if (rt_is_mod(self)) return rt_set_enabled(self, on);
    rt_call<void>(Behaviour_set_enabled, rt_nc(self), on);
}

bool shim_UnityEngine_Behaviour__get_isActiveAndEnabled(obj self) {
    if (rt_is_mod(self)) {
        auto *c = (ModComponent *)self;
        return c->enabled && !c->destroyed && rt_call<bool>(GameObject_get_activeInHierarchy, c->go);
    }
    return rt_call<bool>(Behaviour_get_isActiveAndEnabled, rt_nc(self));
}

// Sprite atlas textures are named "sactx-<page>-<w>x<h>-<format>-<atlas>-<hash>". PC mods recognise them by the
// PC formats ("-BC7-", "DXT5|BC3-"); the Switch's are "-ASTC 4x4-", "-ASTC 6x6-"... Report those as "-BC7-".
static obj pc_atlas_name(obj name) {
    auto *s = (Il2CppString *)name;
    static const char16_t kPre[] = u"sactx-", kFmt[] = u"-ASTC ";
    if (!s || s->length < 16) return name;
    for (int i = 0; i < 6; i++)
        if (s->chars[i] != kPre[i]) return name;
    for (int i = 6; i + 6 <= s->length; i++) {
        int k = 0;
        while (k < 6 && s->chars[i + k] == kFmt[k]) k++;
        if (k < 6) continue;
        int end = i + 6;  // past "-ASTC ", skip the block size up to the next '-'
        while (end < s->length && s->chars[end] != u'-') end++;
        if (end == s->length) return name;
        char16_t buf[256];
        int n = 0;
        for (int j = 0; j < i && n < 250; j++) buf[n++] = s->chars[j];
        for (char16_t c : u"-BC7") if (c) buf[n++] = c;
        for (int j = end; j < s->length && n < 255; j++) buf[n++] = s->chars[j];
        return rt_str16(buf, n);
    }
    return name;
}

obj shim_UnityEngine_Object__get_name(obj self) {
    if (rt_is_mod(self)) return rt_call<obj>(Object_get_name, ((ModComponent *)self)->go);
    return pc_atlas_name(rt_call<obj>(Object_get_name, rt_nc(self)));
}

bool shim_UnityEngine_Object__op_Implicit__UnityEngine_Object(obj o) { return !unity_null(o); }

bool shim_UnityEngine_Object__op_Equality__UnityEngine_Object_UnityEngine_Object(obj a, obj b) {
    bool na = unity_null(a), nb = unity_null(b);
    if (na || nb) return na && nb;
    return a == b;
}

bool shim_UnityEngine_Object__op_Inequality__UnityEngine_Object_UnityEngine_Object(obj a, obj b) {
    return !shim_UnityEngine_Object__op_Equality__UnityEngine_Object_UnityEngine_Object(a, b);
}

void shim_UnityEngine_Object__Destroy__UnityEngine_Object(obj o) {
    if (!o) return;
    if (rt_is_mod(o)) return rt_destroy_component(o);
    rt_call<void>(Object_Destroy, o);
}

void shim_UnityEngine_Object__DontDestroyOnLoad__UnityEngine_Object(obj o) {
    if (rt_is_mod(o)) o = ((ModComponent *)o)->go;
    rt_call<void>(Object_DontDestroyOnLoad, o);
}

// generic GetComponent<T>/AddComponent<T>: mod types are virtual components, game types use the Type overloads
static obj get_component(obj go, int32_t tref) {
    rt_modtype *d = silk_typerefs[tref].desc;
    if (d) return rt_get_component(go, d);
    return rt_call<obj>(rva::GameObject_GetComponent_Type, go, rt_type(tref));
}

obj shim_UnityEngine_GameObject__GetComponent_G1(obj go, int32_t tref) { return get_component(rt_nc(go), tref); }

obj shim_UnityEngine_Component__GetComponent_G1(obj self, int32_t tref) {
    return get_component(shim_UnityEngine_Component__get_gameObject(self), tref);
}

obj shim_UnityEngine_GameObject__AddComponent_G1(obj go, int32_t tref) {
    rt_modtype *d = silk_typerefs[tref].desc;
    if (d) return rt_add_component(rt_nc(go), d);
    return rt_call<obj>(GameObject_AddComponent, rt_nc(go), rt_type(tref));
}

// ======================================================================== Unity: coroutines

obj shim_UnityEngine_MonoBehaviour__StartCoroutine__System_Collections_IEnumerator(obj self, obj e) {
    return rt_start_coroutine(rt_is_mod(self) ? self : nullptr, e);
}

void shim_UnityEngine_MonoBehaviour__StopCoroutine__UnityEngine_Coroutine(obj self, obj h) {
    rt_stop_coroutine(rt_is_mod(self) ? self : nullptr, h);
}

void shim_UnityEngine_MonoBehaviour__StopCoroutine__System_Collections_IEnumerator(obj self, obj e) {
    rt_stop_coroutine(rt_is_mod(self) ? self : nullptr, e);
}

void shim_UnityEngine_MonoBehaviour__StopAllCoroutines(obj self) {
    rt_stop_all_coroutines(rt_is_mod(self) ? self : nullptr);
}

// ======================================================================== input: keyboard -> controller

namespace {
constexpr int kModifier = 5;  // InControl LeftStickButton (L3)
// (L3 + R3 is taken: it toggles the mouse cursor, rt_cursor.cpp)
const int kButtons[] = {18, 17, 16, 15, 11, 12, 13, 14, 22, 21, 20, 19};
const char *const kButtonNames[] = {"R", "L", "ZR", "ZL", "D-pad Up", "D-pad Down", "D-pad Left", "D-pad Right",
                                    "X", "Y", "A", "B"};
constexpr int kCombos = sizeof(kButtons) / sizeof(kButtons[0]);
int32_t g_keys[kCombos];  // KeyCode assigned to each combo (0 = free)

const char *key_name(int32_t k, char *buf) {
    if (k >= 282 && k <= 296) {
        silk::snfmt(buf, 16, "F%d", k - 281);
    } else if (k >= 97 && k <= 122) {
        silk::snfmt(buf, 16, "%c", (char)(k - 32));
    } else if (k >= 48 && k <= 57) {
        silk::snfmt(buf, 16, "%c", (char)k);
    } else {
        silk::snfmt(buf, 16, "key %d", k);
    }
    return buf;
}

bool is_modifier_key(int32_t k) { return k >= 303 && k <= 310; }  // shift/ctrl/alt/cmd

obj control(int type) {
    obj dev = rt_call<obj>(InputManager_get_ActiveDevice);
    return dev ? rt_call<obj>(InputDevice_GetControl, dev, (int32_t)type) : nullptr;
}

bool pressed(int type) {
    obj c = control(type);
    return c && rt_call<bool>(OneAxisInputControl_get_IsPressed, c);
}

int combo_for(int32_t key) {
    for (int i = 0; i < kCombos; i++)
        if (g_keys[i] == key) return i;
    for (int i = 0; i < kCombos; i++)
        if (!g_keys[i]) {
            g_keys[i] = key;
            char kb[16];
            silk::log("input: keyboard %s -> hold L3 + press %s", key_name(key, kb), kButtonNames[i]);
            return i;
        }
    return -1;
}

bool key_query(int32_t key, int kind) {  // 0 held, 1 down, 2 up
    if (is_modifier_key(key)) return kind == 0 && pressed(kModifier);
    int i = combo_for(key);
    if (i < 0 || !pressed(kModifier)) return false;
    obj c = control(kButtons[i]);
    if (!c) return false;
    if (kind == 0) return rt_call<bool>(OneAxisInputControl_get_IsPressed, c);
    if (kind == 1) return rt_call<bool>(OneAxisInputControl_get_WasPressed, c);
    return rt_call<bool>(OneAxisInputControl_get_WasReleased, c);
}
}  // namespace

// "1 = L3+R, F11 = L3+ZR" for every keyboard key a mod has asked about so far
void rt_key_combos(char *out, int cap) {
    int n = 0;
    out[0] = 0;
    for (int i = 0; i < kCombos && n < cap - 40; i++) {
        if (!g_keys[i]) continue;
        char kb[16];
        n += silk::snfmt(out + n, cap - n, "%s%s = L3+%s", n ? ", " : "", key_name(g_keys[i], kb), kButtonNames[i]);
    }
}

bool shim_UnityEngine_Input__GetKey__UnityEngine_KeyCode(int32_t key) { return key_query(key, 0); }
bool shim_UnityEngine_Input__GetKeyDown__UnityEngine_KeyCode(int32_t key) { return key_query(key, 1); }
bool shim_UnityEngine_Input__GetKeyUp__UnityEngine_KeyCode(int32_t key) { return key_query(key, 2); }

// ======================================================================== configuration (BepInEx ConfigFile)
//
// Values come from SilkNX/config/<GUID>.cfg in the romfs (copied from the PC BepInEx/config folder by
// silknx), falling back to the defaults the plugin passes to Bind. Changes made at runtime are kept in
// memory (romfs is read-only).

namespace {

struct EmuConfig : ModObj {
    obj text;   // whole .cfg file (string) or null
    int32_t plugin_index;
};

enum { CK_BOOL, CK_INT, CK_FLOAT, CK_STRING, CK_ENUM };

struct EmuEntry : ModObj {
    obj section, key, svalue, enum_type, def_boxed;
    int64_t ivalue;
    double fvalue;
    int32_t kind;
};

rt_modtype d_config = {"BepInEx.Configuration.ConfigFile", nullptr, sizeof(EmuConfig), 0, false, nullptr, 0,
                       nullptr, false, nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_entry = {"BepInEx.Configuration.ConfigEntry", nullptr, sizeof(EmuEntry), 0, false, nullptr, 0,
                      nullptr, false, nullptr, 0, nullptr, {}, nullptr};

obj read_config_text(const rt_plugin *p) {
    if (!p) return nullptr;
    char path[512];
    char sa[300];
    rt_str_to_utf8(rt_call<obj>(Application_get_streamingAssetsPath), sa, sizeof(sa));
    silk::snfmt(path, sizeof(path), "%s/SilkNX/config/%s.cfg", sa, p->guid);
    obj spath = rt_str(path);
    if (!rt_call<bool>(File_Exists, spath)) {
        silk::log("config: %s not found, using defaults", path);
        return nullptr;
    }
    silk::log("config: loaded %s", path);
    return rt_call<obj>(File_ReadAllText, spath);
}

// Find "key = value" in [section] of the cfg text; writes the trimmed value into out.
bool lookup(obj text, obj section, obj key, char *out, int cap) {
    if (!text) return false;
    auto *t = (Il2CppString *)text;
    char sec[128], k[128];
    rt_str_to_utf8(section, sec, sizeof(sec));
    rt_str_to_utf8(key, k, sizeof(k));
    bool in_sec = false;
    int i = 0, n = t->length;
    char line[512];
    while (i < n) {
        int len = 0;
        while (i < n && t->chars[i] != '\n') {
            char16_t c = t->chars[i++];
            if (len < 511 && c != '\r') line[len++] = c < 128 ? (char)c : '?';
        }
        i++;
        line[len] = 0;
        char *s = line;
        while (*s == ' ' || *s == '\t') s++;
        if (*s == '#' || !*s) continue;
        if (*s == '[') {
            char *e = s + 1;
            while (*e && *e != ']') e++;
            *e = 0;
            in_sec = !strcmp_(s + 1, sec);
            continue;
        }
        if (!in_sec) continue;
        char *eq = s;
        while (*eq && *eq != '=') eq++;
        if (!*eq) continue;
        char *kend = eq;
        while (kend > s && (kend[-1] == ' ' || kend[-1] == '\t')) kend--;
        *kend = 0;
        if (strcmp_(s, k)) continue;
        char *v = eq + 1;
        while (*v == ' ' || *v == '\t') v++;
        int vl = 0;
        while (v[vl]) vl++;
        while (vl && (v[vl - 1] == ' ' || v[vl - 1] == '\t')) vl--;
        if (vl >= cap) vl = cap - 1;
        memcpy(out, v, vl);
        out[vl] = 0;
        return true;
    }
    return false;
}

bool parse_float(const char *s, double *out) {
    double v = 0, frac = 0.1;
    bool neg = false, dot = false, any = false;
    if (*s == '-') neg = true, s++;
    for (; *s; s++) {
        if (*s >= '0' && *s <= '9') {
            any = true;
            if (dot) {
                v += (*s - '0') * frac;
                frac /= 10;
            } else {
                v = v * 10 + (*s - '0');
            }
        } else if ((*s == '.' || *s == ',') && !dot) {
            dot = true;
        } else {
            break;
        }
    }
    *out = neg ? -v : v;
    return any;
}

obj new_entry(obj self, obj section, obj key, int kind) {
    obj e = rt_new_mod(&d_entry);
    auto *en = (EmuEntry *)e;
    en->section = section;
    en->key = key;
    en->kind = kind;
    return e;
}

bool entry_text(obj self, obj e, char *buf, int cap) {
    auto *en = (EmuEntry *)e;
    return lookup(self ? ((EmuConfig *)self)->text : nullptr, en->section, en->key, buf, cap);
}

}  // namespace

obj shim_BepInEx_BaseUnityPlugin__get_Config(obj self, int32_t) {
    auto *pl = (ModPlugin *)rt_nc(self);
    if (!pl->config) {
        obj c = rt_new_mod(&d_config);
        const rt_plugin *p = plugin_for(self);
        ((EmuConfig *)c)->plugin_index = pl->plugin_index;
        ((EmuConfig *)c)->text = read_config_text(p);
        pl->config = c;
    }
    return pl->config;
}

obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_bool_string_G1(obj self, obj sec, obj key, bool def,
                                                                             obj, int32_t, int32_t) {
    obj e = new_entry(self, sec, key, CK_BOOL);
    char buf[64];
    ((EmuEntry *)e)->ivalue = def;
    if (entry_text(self, e, buf, sizeof(buf)))
        ((EmuEntry *)e)->ivalue = buf[0] == 't' || buf[0] == 'T' || buf[0] == '1';
    return e;
}

obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_int32_t_string_G1(obj self, obj sec, obj key,
                                                                                int32_t def, obj, int32_t,
                                                                                int32_t tref) {
    const char *aqn = silk_typerefs[tref].aqn;
    bool is_enum = aqn && !startswith_(aqn, "System.Int32,");
    obj e = new_entry(self, sec, key, is_enum ? CK_ENUM : CK_INT);
    auto *en = (EmuEntry *)e;
    en->ivalue = def;
    char buf[128];
    if (!entry_text(self, e, buf, sizeof(buf))) return e;
    if (!is_enum) {
        double v;
        if (parse_float(buf, &v)) en->ivalue = (int64_t)v;
        return e;
    }
    // enum: look the name up through System.Enum.Parse
    obj boxed = rt_call<obj>(rva::Enum_Parse, rt_type(tref), rt_str(buf));
    if (boxed) en->ivalue = *(int32_t *)((uint8_t *)boxed + 0x10);
    return e;
}

obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_float_string_G1(obj self, obj sec, obj key, float def,
                                                                              obj, int32_t, int32_t) {
    obj e = new_entry(self, sec, key, CK_FLOAT);
    ((EmuEntry *)e)->fvalue = def;
    char buf[64];
    double v;
    if (entry_text(self, e, buf, sizeof(buf)) && parse_float(buf, &v)) ((EmuEntry *)e)->fvalue = v;
    return e;
}

obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_obj_string_G1(obj self, obj sec, obj key, obj def,
                                                                            obj, int32_t, int32_t) {
    obj e = new_entry(self, sec, key, CK_STRING);
    ((EmuEntry *)e)->svalue = def;
    char buf[512];
    if (entry_text(self, e, buf, sizeof(buf))) ((EmuEntry *)e)->svalue = rt_str(buf);
    return e;
}

bool shim_BepInEx_Configuration_ConfigEntry_1_bool__get_Value(obj e, int32_t) { return ((EmuEntry *)rt_nc(e))->ivalue; }
int32_t shim_BepInEx_Configuration_ConfigEntry_1_int32_t__get_Value(obj e, int32_t) {
    return (int32_t)((EmuEntry *)rt_nc(e))->ivalue;
}
float shim_BepInEx_Configuration_ConfigEntry_1_float__get_Value(obj e, int32_t) {
    return (float)((EmuEntry *)rt_nc(e))->fvalue;
}
obj shim_BepInEx_Configuration_ConfigEntry_1_obj__get_Value(obj e, int32_t) { return ((EmuEntry *)rt_nc(e))->svalue; }

void shim_BepInEx_Configuration_ConfigEntry_1_bool__set_Value__bool(obj e, bool v, int32_t) {
    ((EmuEntry *)rt_nc(e))->ivalue = v;
}
void shim_BepInEx_Configuration_ConfigEntry_1_int32_t__set_Value__int32_t(obj e, int32_t v, int32_t) {
    ((EmuEntry *)rt_nc(e))->ivalue = v;
}
void shim_BepInEx_Configuration_ConfigEntry_1_float__set_Value__float(obj e, float v, int32_t) {
    ((EmuEntry *)rt_nc(e))->fvalue = v;
}
void shim_BepInEx_Configuration_ConfigEntry_1_obj__set_Value__obj(obj e, obj v, int32_t) {
    ((EmuEntry *)rt_nc(e))->svalue = v;
}

void shim_BepInEx_Configuration_ConfigFile__Save(obj, int32_t) {}
void shim_BepInEx_Configuration_ConfigFile__Reload(obj, int32_t) {}

// ======================================================================== paths
// Plugin folders are packaged under <StreamingAssets>/SilkNX/plugins/<folder>/ in the romfs.

static void plugin_dir(int32_t asm_index, char *out, int cap) {
    char sa[300];
    rt_str_to_utf8(rt_call<obj>(Application_get_streamingAssetsPath), sa, sizeof(sa));
    const char *folder = "";
    for (uint32_t i = 0; i < silk_plugin_count; i++)
        if (silk_plugins[i].asm_index == asm_index) folder = silk_plugins[i].folder;
    silk::snfmt(out, cap, "%s/SilkNX/plugins/%s", sa, folder);
}

obj shim_System_Reflection_Assembly__get_Location(obj self, int32_t asm_index) {
    int32_t a = self && rt_is_mod(self) ? *(int32_t *)((uint8_t *)self + sizeof(ModObj)) : asm_index;
    char dir[400], path[500];
    plugin_dir(a, dir, sizeof(dir));
    silk::snfmt(path, sizeof(path), "%s/%s.dll", dir, silk_asm_names[a]);
    return rt_str(path);
}

obj shim_BepInEx_Paths__get_PluginPath(int32_t) {
    char sa[300], path[400];
    rt_str_to_utf8(rt_call<obj>(Application_get_streamingAssetsPath), sa, sizeof(sa));
    silk::snfmt(path, sizeof(path), "%s/SilkNX/plugins", sa);
    return rt_str(path);
}

obj shim_BepInEx_Paths__get_ConfigPath(int32_t) {
    char sa[300], path[400];
    rt_str_to_utf8(rt_call<obj>(Application_get_streamingAssetsPath), sa, sizeof(sa));
    silk::snfmt(path, sizeof(path), "%s/SilkNX/config", sa);
    return rt_str(path);
}

obj rt_find_components(const rt_modtype *d);

obj shim_UnityEngine_Object__FindObjectsByType__UnityEngine_FindObjectsSortMode_G1(int32_t mode, int32_t tref) {
    if (silk_typerefs[tref].desc) return rt_find_components(silk_typerefs[tref].desc);
    return rt_call<obj>(Object_FindObjectsByType_Type, rt_type(tref), mode);
}

obj shim_UnityEngine_Object__FindObjectsOfType_G1(int32_t tref) {
    if (silk_typerefs[tref].desc) return rt_find_components(silk_typerefs[tref].desc);
    return rt_call<obj>(Object_FindObjectsOfType_Type, rt_type(tref));
}

obj rt_harmony_activate(uint32_t site) {
    if (site < silk_patch_class_count) silk_patch_classes[site].applied = true;
    static rt_modtype tok = {"System.Reflection.MethodInfo(SilkNX)", nullptr, sizeof(ModObj) + 8, 0, false,
                             nullptr, 0, nullptr, false, nullptr, 0, nullptr, {}, nullptr};
    return rt_new_mod(&tok);
}

// ======================================================================== HarmonyLib.AccessTools (real reflection)

obj shim_HarmonyLib_AccessTools__Method__System_Type_string_System_TypeArr_System_TypeArr(obj t, obj name,
                                                                                        obj params, obj generics,
                                                                                        int32_t) {
    obj m = rt_find_method(t, name, params);
    if (m && generics) m = rt_call<obj>(rva::RuntimeMethodInfo_MakeGenericMethod, m, generics);
    return m;
}

obj shim_HarmonyLib_AccessTools__Method__System_Type_string_System_TypeArr(obj t, obj name, obj params, int32_t) {
    return rt_find_method(t, name, params);
}

obj shim_HarmonyLib_AccessTools__Method__System_Type_string(obj t, obj name, int32_t) {
    return rt_find_method(t, name, nullptr);
}

obj shim_HarmonyLib_AccessTools__DeclaredMethod__System_Type_string_System_TypeArr_System_TypeArr(obj t, obj n, obj p,
                                                                                                obj, int32_t) {
    return rt_find_method(t, n, p);
}

static obj prefixed(const char *pre, obj name) {
    char n[256], full[300];
    rt_str_to_utf8(name, n, sizeof(n));
    silk::snfmt(full, sizeof(full), "%s%s", pre, n);
    return rt_str(full);
}

obj shim_HarmonyLib_AccessTools__PropertyGetter__System_Type_string(obj t, obj name, int32_t) {
    return rt_find_method(t, prefixed("get_", name), nullptr);
}

obj shim_HarmonyLib_AccessTools__PropertySetter__System_Type_string(obj t, obj name, int32_t) {
    return rt_find_method(t, prefixed("set_", name), nullptr);
}

obj shim_HarmonyLib_AccessTools__Field__System_Type_string(obj t, obj name, int32_t) {
    if (!t || rt_is_mod(t)) return nullptr;
    return rt_call<obj>(rva::RuntimeType_GetField, t, name, (int32_t)(4 | 8 | 16 | 32));
}

obj shim_HarmonyLib_AccessTools__Constructor__System_Type_System_TypeArr_bool(obj t, obj params, bool, int32_t) {
    if (!t || rt_is_mod(t)) return nullptr;
    if (!params) params = rt_call<obj>(rva::Array_CreateInstance,
                                       rt_call<obj>(rva::Type_GetType, rt_str("System.Type, mscorlib")), (int32_t)0);
    return rt_call<obj>(rva::Type_GetConstructor4, t, (int32_t)(4 | 8 | 16 | 32), (obj) nullptr, params, (obj) nullptr);
}

obj shim_HarmonyLib_AccessTools__TypeByName__string(obj name, int32_t) {
    char n[256], aqn[400];
    rt_str_to_utf8(name, n, sizeof(n));
    for (int i = -1; i < 0 || silk_image_names[i]; i++) {
        if (i < 0) silk::snfmt(aqn, sizeof(aqn), "%s", n);
        else silk::snfmt(aqn, sizeof(aqn), "%s, %s", n, silk_image_names[i]);
        obj t = rt_call<obj>(rva::Type_GetType, rt_str(aqn));
        if (t) return t;
    }
    return nullptr;  // mod-defined or unknown types: Harmony also returns null
}

// HarmonyMethod / Patch objects only matter for patches resolved at build time; keep them inert.
static obj harmony_token() {
    static rt_modtype d = {"HarmonyLib.HarmonyMethod", nullptr, sizeof(ModObj) + 8, 0, false, nullptr, 0, nullptr,
                           false, nullptr, 0, nullptr, {}, nullptr};
    return rt_new_mod(&d);
}
obj shim_HarmonyLib_HarmonyMethod___ctor__System_Type_string_System_TypeArr(obj, obj, obj, int32_t) {
    return harmony_token();
}
obj shim_HarmonyLib_HarmonyMethod___ctor__System_Reflection_MethodInfo(obj, int32_t) { return harmony_token(); }
obj shim_HarmonyLib_HarmonyMethod___ctor__System_Type_string(obj, obj, int32_t) { return harmony_token(); }

obj shim_HarmonyLib_Harmony__Patch__System_Reflection_MethodBase_HarmonyLib_HarmonyMethod_HarmonyLib_HarmonyMethod_HarmonyLib_HarmonyMethod_HarmonyLib_HarmonyMethod_HarmonyLib_HarmonyMethod(
    obj, obj, obj, obj, obj, obj, obj, int32_t) {
    static int warned;
    if (!warned++) silk::log("Harmony.Patch with a target only known at runtime cannot be applied on Switch");
    return nullptr;
}

obj shim_HarmonyLib_Harmony__GetPatchedMethods(obj, int32_t) { return rt_new_string_array(0); }

// ======================================================================== BepInEx extras

obj shim_BepInEx_Logging_Logger__get_Listeners(int32_t) {
    return rt_new_generic_instance("System.Collections.Generic.List`1[[System.Object, mscorlib]], mscorlib");
}

obj shim_BepInEx_Bootstrap_Chainloader__get_PluginInfos(int32_t) {
    return rt_new_generic_instance(
        "System.Collections.Generic.Dictionary`2[[System.String, mscorlib],[System.Object, mscorlib]], mscorlib");
}

obj shim_BepInEx_Paths__get_ProcessName(int32_t) { return rt_str("Hollow Knight Silksong"); }
obj shim_BepInEx_Paths__get_GameRootPath(int32_t) {
    char sa[300];
    rt_str_to_utf8(rt_call<obj>(rva::Application_get_streamingAssetsPath), sa, sizeof(sa));
    char out[400];
    silk::snfmt(out, sizeof(out), "%s/SilkNX", sa);
    return rt_str(out);
}
obj shim_BepInEx_Paths__get_BepInExRootPath(int32_t a) { return shim_BepInEx_Paths__get_GameRootPath(a); }

obj shim_BepInEx_PluginInfo__get_Metadata(obj, int32_t) { return nullptr; }
obj shim_BepInEx_BepInPlugin__get_Name(obj, int32_t) { return rt_str(""); }

obj shim_BepInEx_Configuration_ConfigDescription___ctor__string_BepInEx_Configuration_AcceptableValueBase_objectArr(
    obj, obj, obj, int32_t) {
    return harmony_token();
}
obj shim_BepInEx_Configuration_ConfigDescription___ctor__string(obj, int32_t) { return harmony_token(); }
obj shim_BepInEx_Configuration_AcceptableValueList_1_obj___ctor__obj(obj, int32_t) { return harmony_token(); }

void shim_BepInEx_Configuration_ConfigEntry_1_obj__add_SettingChanged__System_EventHandler(obj, obj, int32_t) {}
void shim_BepInEx_Configuration_ConfigEntry_1_float__add_SettingChanged__System_EventHandler(obj, obj, int32_t) {}
void shim_BepInEx_Configuration_ConfigEntry_1_bool__add_SettingChanged__System_EventHandler(obj, obj, int32_t) {}
void shim_BepInEx_Configuration_ConfigEntry_1_int32_t__add_SettingChanged__System_EventHandler(obj, obj, int32_t) {}
void shim_BepInEx_Configuration_ConfigFile__add_SettingChanged__System_EventHandler_BepInEx_Configuration_SettingChangedEventArgs_(obj, obj, int32_t) {}

obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_double_string_G1(obj self, obj sec, obj key, double def,
                                                                               obj d, int32_t a, int32_t t) {
    return shim_BepInEx_Configuration_ConfigFile__Bind__string_string_float_string_G1(self, sec, key, (float)def, d, a, t);
}
double shim_BepInEx_Configuration_ConfigEntry_1_double__get_Value(obj e, int32_t a) {
    return shim_BepInEx_Configuration_ConfigEntry_1_float__get_Value(e, a);
}
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_obj_BepInEx_Configuration_ConfigDescription_G1(
    obj self, obj sec, obj key, obj def, obj desc, int32_t a, int32_t t) {
    return shim_BepInEx_Configuration_ConfigFile__Bind__string_string_obj_string_G1(self, sec, key, def, desc, a, t);
}
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_bool_BepInEx_Configuration_ConfigDescription_G1(
    obj self, obj sec, obj key, bool def, obj desc, int32_t a, int32_t t) {
    return shim_BepInEx_Configuration_ConfigFile__Bind__string_string_bool_string_G1(self, sec, key, def, desc, a, t);
}
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_float_BepInEx_Configuration_ConfigDescription_G1(
    obj self, obj sec, obj key, float def, obj desc, int32_t a, int32_t t) {
    return shim_BepInEx_Configuration_ConfigFile__Bind__string_string_float_string_G1(self, sec, key, def, desc, a, t);
}
obj shim_BepInEx_Configuration_ConfigFile__Bind__string_string_int32_t_BepInEx_Configuration_ConfigDescription_G1(
    obj self, obj sec, obj key, int32_t def, obj desc, int32_t a, int32_t t) {
    return shim_BepInEx_Configuration_ConfigFile__Bind__string_string_int32_t_string_G1(self, sec, key, def, desc, a, t);
}

obj shim_System_Type__get_Assembly(obj self) {
    rt_modtype *d = rt_modtype_of_typeobj(self);
    if (d) return shim_System_Reflection_Assembly__GetExecutingAssembly(d->asm_index);
    return rt_call<obj>(rva::Type_get_Assembly, rt_nc(self));
}
