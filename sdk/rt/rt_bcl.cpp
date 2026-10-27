// SilkNX runtime: .NET base-library functions the Switch build stripped, plus small helpers.
//
// File/folder enumeration works from the index of files SilkNX packed into the romfs (the only files mods
// can see on Switch). Writes are accepted and ignored: the romfs is read-only.
#include "rt.h"

using namespace rva;

namespace {

// UTF-16 managed string -> normalized romfs-relative path ("Data/StreamingAssets/..."), no trailing '/'.
int norm_path(obj s, char *out, int cap) {
    char raw[512];
    rt_str_to_utf8(s, raw, sizeof(raw));
    const char *p = raw;
    if (startswith_(p, "rom:/")) p += 5;
    else if (startswith_(p, "rom:")) p += 4;
    int n = 0;
    for (; *p && n < cap - 1; p++) {
        char c = *p == '\\' ? '/' : *p;
        if (c == '/' && (n == 0 || out[n - 1] == '/')) continue;
        out[n++] = c;
    }
    while (n && out[n - 1] == '/') n--;
    out[n] = 0;
    return n;
}

bool ieq(char a, char b) {
    if (a >= 'A' && a <= 'Z') a += 32;
    if (b >= 'A' && b <= 'Z') b += 32;
    return a == b;
}

// Windows-style glob: * and ?, case-insensitive; "*.*" matches everything.
bool glob(const char *pat, const char *s) {
    if (!pat || !*pat || !strcmp_(pat, "*") || !strcmp_(pat, "*.*")) return true;
    if (*pat == '*') {
        for (const char *t = s;; t++) {
            if (glob(pat + 1, t)) return true;
            if (!*t) return false;
        }
    }
    if (!*s) return false;
    if (*pat == '?' || ieq(*pat, *s)) return glob(pat + 1, s + 1);
    return false;
}

// Is `path` inside `dir` (any depth)? On success *rest points at the part after "dir/".
bool under(const char *path, const char *dir, int dlen, const char **rest) {
    if (dlen == 0) {
        *rest = path;
        return true;
    }
    for (int i = 0; i < dlen; i++)
        if (!ieq(path[i], dir[i])) return false;
    if (path[dlen] != '/') return false;
    *rest = path + dlen + 1;
    return true;
}

obj managed_path(const char *rel) {
    char buf[600];
    silk::snfmt(buf, sizeof(buf), "rom:/%s", rel);
    return rt_str(buf);
}

obj list_files(obj dir, obj pattern, int32_t option) {
    char d[512], pat[128] = "*";
    int dlen = norm_path(dir, d, sizeof(d));
    if (pattern) rt_str_to_utf8(pattern, pat, sizeof(pat));
    static const char *hits[8192];
    int n = 0;
    for (uint32_t i = 0; i < silk_romfs_count && n < 8192; i++) {
        const char *rest;
        if (!under(silk_romfs_files[i].path, d, dlen, &rest)) continue;
        const char *slash = rest;
        while (*slash && *slash != '/') slash++;
        if (*slash && option == 0) continue;  // TopDirectoryOnly
        const char *name = rest;
        for (const char *q = rest; *q; q++)
            if (*q == '/') name = q + 1;
        if (glob(pat, name)) hits[n++] = silk_romfs_files[i].path;
    }
    obj arr = rt_new_string_array(n);
    for (int i = 0; i < n; i++) RT_ELEM(arr, i, obj) = managed_path(hits[i]);
    return arr;
}

obj list_dirs(obj dir, obj pattern, int32_t option) {
    char d[512], pat[128] = "*";
    int dlen = norm_path(dir, d, sizeof(d));
    if (pattern) rt_str_to_utf8(pattern, pat, sizeof(pat));
    static char found[1024][256];
    int n = 0;
    for (uint32_t i = 0; i < silk_romfs_count; i++) {
        const char *rest;
        if (!under(silk_romfs_files[i].path, d, dlen, &rest)) continue;
        // every directory prefix of `rest` (only the first level for TopDirectoryOnly)
        for (const char *q = rest; *q; q++) {
            if (*q != '/') continue;
            int len = (int)(q - silk_romfs_files[i].path);
            const char *name = q;
            while (name > rest && name[-1] != '/') name--;
            char nm[128];
            int nl = (int)(q - name) < 127 ? (int)(q - name) : 127;
            memcpy(nm, name, nl);
            nm[nl] = 0;
            if (len < 255 && glob(pat, nm)) {
                bool dup = false;
                for (int k = 0; k < n && !dup; k++)
                    if (!memcmp(found[k], silk_romfs_files[i].path, len) && found[k][len] == 0) dup = true;
                if (!dup && n < 1024) {
                    memcpy(found[n], silk_romfs_files[i].path, len);
                    found[n++][len] = 0;
                }
            }
            if (option == 0) break;
        }
    }
    obj arr = rt_new_string_array(n);
    for (int i = 0; i < n; i++) RT_ELEM(arr, i, obj) = managed_path(found[i]);
    return arr;
}

void ignored_write(obj path) {
    static int warned;
    if (warned++ < 3) {
        char p[300];
        rt_str_to_utf8(path, p, sizeof(p));
        silk::log("file writes are ignored on Switch (read-only game files): %s", p);
    }
}

obj split_lines(obj text) {
    if (!text) return rt_new_string_array(0);
    auto *s = (Il2CppString *)text;
    int count = 1;
    for (int i = 0; i < s->length; i++)
        if (s->chars[i] == '\n') count++;
    if (s->length && s->chars[s->length - 1] == '\n') count--;
    obj arr = rt_new_string_array(count);
    int start = 0, k = 0;
    for (int i = 0; i <= s->length && k < count; i++) {
        if (i == s->length || s->chars[i] == '\n') {
            int end = i;
            if (end > start && s->chars[end - 1] == '\r') end--;
            RT_ELEM(arr, k++, obj) = rt_str16(s->chars + start, end - start);
            start = i + 1;
        }
    }
    return arr;
}

obj join_paths(obj *parts, int n) {
    char16_t buf[1024];
    int len = 0;
    for (int i = 0; i < n; i++) {
        auto *s = (Il2CppString *)parts[i];
        if (!s || !s->length) continue;
        bool rooted = s->chars[0] == '/' || s->chars[0] == '\\' || (s->length > 1 && s->chars[1] == ':') ||
                      (s->length > 3 && s->chars[3] == ':');
        if (rooted) len = 0;
        if (len && buf[len - 1] != '/' && buf[len - 1] != '\\' && len < 1023) buf[len++] = '/';
        for (int k = 0; k < s->length && len < 1023; k++) buf[len++] = s->chars[k];
    }
    return rt_str16(buf, len);
}

}  // namespace

// ======================================================================== helpers

obj rt_new_string_array(int32_t n) {
    static obj string_type;
    if (!string_type) {
        string_type = rt_call<obj>(Type_GetType, rt_str("System.String, mscorlib"));
        *(obj *)rt_root_alloc(8) = string_type;
    }
    return rt_call<obj>(Array_CreateInstance, string_type, n);
}

obj rt_new_generic_instance(const char *aqn) {
    obj t = rt_call<obj>(Type_GetType, rt_str(aqn));
    return t ? rt_call<obj>(Activator_CreateInstance, t) : nullptr;
}

obj rt_placeholder(const char *type_name) {
    static rt_modtype d = {"SilkNX.Placeholder", nullptr, sizeof(ModObj) + 8, 0, false, nullptr, 0, nullptr,
                           false, nullptr, 0, nullptr, {}, nullptr};
    obj o = rt_new_mod(&d);
    *(const char **)((uint8_t *)o + sizeof(ModObj)) = type_name;
    return o;
}

obj rt_fieldref(int32_t offset) {
    static rt_modtype d = {"HarmonyLib.AccessTools.FieldRef", nullptr, sizeof(ModObj) + 8, 0, false, nullptr, 0,
                           nullptr, false, nullptr, 0, nullptr, {}, nullptr};
    obj o = rt_new_mod(&d);
    *(int32_t *)((uint8_t *)o + sizeof(ModObj)) = offset;
    return o;
}

void *rt_fieldref_ptr(obj fieldref, obj instance) {
    int32_t off = *(int32_t *)((uint8_t *)rt_nc(fieldref) + sizeof(ModObj));
    if (off < 0) rt_unsupported("FieldRefAccess to a field that does not exist on Switch");
    return (uint8_t *)rt_nc(instance) + off;
}

// ======================================================================== System.IO

obj shim_System_IO_File__ReadAllLines__string(obj path) {
    return split_lines(rt_call<obj>(File_ReadAllText, path));
}
obj shim_System_IO_File__ReadLines__string(obj path) { return shim_System_IO_File__ReadAllLines__string(path); }

void shim_System_IO_File__WriteAllLines__string_System_Collections_Generic_IEnumerable_string_(obj p, obj) {
    ignored_write(p);
}
void shim_System_IO_File__WriteAllLines__string_stringArr(obj p, obj) { ignored_write(p); }
void shim_System_IO_File__WriteAllText__string_string(obj p, obj) { ignored_write(p); }
void shim_System_IO_File__AppendAllText__string_string(obj p, obj) { ignored_write(p); }
void shim_System_IO_File__WriteAllBytes__string_byteArr(obj p, obj) { ignored_write(p); }
void shim_System_IO_File__Delete__string(obj p) { ignored_write(p); }
obj shim_System_IO_Directory__CreateDirectory__string(obj p) { return nullptr; }

obj shim_System_IO_Directory__GetFiles__string(obj d) { return list_files(d, nullptr, 0); }
obj shim_System_IO_Directory__GetFiles__string_string(obj d, obj pat) { return list_files(d, pat, 0); }
obj shim_System_IO_Directory__GetFiles__string_string_System_IO_SearchOption(obj d, obj pat, int32_t o) {
    return list_files(d, pat, o);
}
obj shim_System_IO_Directory__EnumerateFiles__string(obj d) { return list_files(d, nullptr, 0); }
obj shim_System_IO_Directory__EnumerateFiles__string_string(obj d, obj pat) { return list_files(d, pat, 0); }
obj shim_System_IO_Directory__EnumerateFiles__string_string_System_IO_SearchOption(obj d, obj pat, int32_t o) {
    return list_files(d, pat, o);
}
obj shim_System_IO_Directory__GetDirectories__string(obj d) { return list_dirs(d, nullptr, 0); }
obj shim_System_IO_Directory__GetDirectories__string_string(obj d, obj pat) { return list_dirs(d, pat, 0); }
obj shim_System_IO_Directory__GetDirectories__string_string_System_IO_SearchOption(obj d, obj pat, int32_t o) {
    return list_dirs(d, pat, o);
}
obj shim_System_IO_Directory__EnumerateDirectories__string(obj d) { return list_dirs(d, nullptr, 0); }
obj shim_System_IO_Directory__EnumerateDirectories__string_string_System_IO_SearchOption(obj d, obj pat,
                                                                                       int32_t o) {
    return list_dirs(d, pat, o);
}

obj shim_System_IO_Path__Combine__string_string_string(obj a, obj b, obj c) {
    obj parts[] = {a, b, c};
    return join_paths(parts, 3);
}
obj shim_System_IO_Path__Combine__string_string_string_string(obj a, obj b, obj c, obj d) {
    obj parts[] = {a, b, c, d};
    return join_paths(parts, 4);
}

obj shim_System_IO_Path__GetRelativePath__string_string(obj rel_to, obj path) {
    char a[512], b[512];
    int la = norm_path(rel_to, a, sizeof(a));
    norm_path(path, b, sizeof(b));
    const char *rest;
    if (under(b, a, la, &rest)) return rt_str(rest);
    return path;
}

// ======================================================================== System.String / Math / Text

obj shim_System_String__TrimStart(obj self) {
    auto *s = (Il2CppString *)rt_nc(self);
    int i = 0;
    while (i < s->length && (s->chars[i] == ' ' || s->chars[i] == '\t' || s->chars[i] == '\n' ||
                             s->chars[i] == '\r'))
        i++;
    return i ? rt_str16(s->chars + i, s->length - i) : self;
}

obj shim_System_String__TrimEnd(obj self) {
    auto *s = (Il2CppString *)rt_nc(self);
    int n = s->length;
    while (n && (s->chars[n - 1] == ' ' || s->chars[n - 1] == '\t' || s->chars[n - 1] == '\n' ||
                 s->chars[n - 1] == '\r'))
        n--;
    return n != s->length ? rt_str16(s->chars, n) : self;
}

double shim_System_Math__Clamp__double_double_double(double v, double lo, double hi) {
    return v < lo ? lo : v > hi ? hi : v;
}
float shim_System_Math__Clamp__float_float_float(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
int32_t shim_System_Math__Clamp__int_int_int(int32_t v, int32_t lo, int32_t hi) { return v < lo ? lo : v > hi ? hi : v; }

obj shim_System_Text_StringBuilder__Append__long(obj self, int64_t v) {
    char buf[32];
    silk::snfmt(buf, sizeof(buf), "%ld", v);
    return rt_call<obj>(StringBuilder_Append_string, self, rt_str(buf));
}

// ======================================================================== Unity bits stripped from the Switch build

obj shim_UnityEngine_Application__get_version() { return rt_str(silk_game_version); }
int32_t shim_UnityEngine_AudioSettings__get_outputSampleRate() { return 48000; }

// ======================================================================== embedded resources
// The converter extracts each mod's embedded resources to SilkNX/resources/<assembly>/<name>.

static int32_t asm_of(obj self, int32_t fallback) {
    return self && rt_is_mod(self) ? *(int32_t *)((uint8_t *)self + sizeof(ModObj)) : fallback;
}

obj shim_System_Reflection_Assembly__GetManifestResourceNames(obj self, int32_t asm_index) {
    char dir[300];
    silk::snfmt(dir, sizeof(dir), "Data/StreamingAssets/SilkNX/resources/%s", silk_asm_names[asm_of(self, asm_index)]);
    int dl = (int)strlen(dir), n = 0;
    for (uint32_t i = 0; i < silk_romfs_count; i++)
        if (startswith_(silk_romfs_files[i].path, dir) && silk_romfs_files[i].path[dl] == '/') n++;
    obj arr = rt_new_string_array(n);
    int k = 0;
    for (uint32_t i = 0; i < silk_romfs_count && k < n; i++)
        if (startswith_(silk_romfs_files[i].path, dir) && silk_romfs_files[i].path[dl] == '/')
            RT_ELEM(arr, k++, obj) = rt_str(silk_romfs_files[i].path + dl + 1);
    return arr;
}

obj shim_System_Reflection_Assembly__GetManifestResourceStream__string(obj self, obj name, int32_t asm_index) {
    char n[256], path[400];
    rt_str_to_utf8(name, n, sizeof(n));
    silk::snfmt(path, sizeof(path), "rom:/Data/StreamingAssets/SilkNX/resources/%s/%s",
                silk_asm_names[asm_of(self, asm_index)], n);
    obj p = rt_str(path);
    if (!rt_call<bool>(File_Exists, p)) return nullptr;
    obj bytes = rt_call<obj>(File_ReadAllBytes, p);
    obj ms_type = rt_call<obj>(Type_GetType, rt_str("System.IO.MemoryStream, mscorlib"));
    obj args = rt_call<obj>(Array_CreateInstance, rt_call<obj>(Type_GetType, rt_str("System.Object, mscorlib")), 1);
    RT_ELEM(args, 0, obj) = bytes;
    return rt_call<obj>(Activator_CreateInstance_args, ms_type, args);
}

// More write operations: accepted and ignored (game files are read-only on Switch).
void shim_System_IO_File__Copy__string_string(obj src, obj dst) { ignored_write(dst); }
void shim_System_IO_File__Copy__string_string_bool(obj src, obj dst, bool) { ignored_write(dst); }
void shim_System_IO_File__Move__string_string(obj src, obj dst) { ignored_write(dst); }
void shim_System_IO_File__Move__string_string_bool(obj src, obj dst, bool) { ignored_write(dst); }
void shim_System_IO_File__Replace__string_string_string(obj src, obj dst, obj) { ignored_write(dst); }
void shim_System_IO_File__AppendAllLines__string_System_Collections_Generic_IEnumerable_string_(obj p, obj) {
    ignored_write(p);
}
void shim_System_IO_File__SetLastWriteTimeUtc__string_System_DateTime(obj p, int64_t) { ignored_write(p); }
void shim_System_IO_Directory__Delete__string(obj p) { ignored_write(p); }
void shim_System_IO_Directory__Delete__string_bool(obj p, bool) { ignored_write(p); }
void shim_System_IO_Directory__Move__string_string(obj, obj dst) { ignored_write(dst); }

// Interlocked.CompareExchange<T>(ref T, T, T) for reference types (the compiler's event add/remove pattern).
// The generic instance usually needs runtime generic context the mod doesn't have, so do it directly.
obj shim_System_Threading_Interlocked__CompareExchange__obj__obj_obj_G1(obj *loc, obj value, obj comparand,
                                                                                    int32_t) {
    __atomic_compare_exchange_n((loc ? loc : (rt_throw_nre(), loc)), &comparand, value, false, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST);
    return comparand;
}
obj shim_System_Threading_Interlocked__Exchange__obj__obj_G1(obj *loc, obj value, int32_t) {
    return __atomic_exchange_n((loc ? loc : (rt_throw_nre(), loc)), value, __ATOMIC_SEQ_CST);
}

// String.Concat(object...) overloads were stripped from the Switch build (C# compilers emit them for
// `"text" + value`). Same semantics: null -> "", otherwise ToString() (mod overrides included).
extern "C" __attribute__((weak)) const uint32_t silk_mid_tostring = 0;
static obj concat_part(obj o) {
    if (!o) return rt_str("");
    if (o->klass == rt_string_klass()) return o;
    if (rt_is_mod(o)) {
        void *fn = silk_mid_tostring ? rt_mod_vfn(o, silk_mid_tostring, nullptr) : nullptr;
        obj s = fn ? ((obj(*)(obj, const void *))fn)(o, nullptr) : rt_str(((ModObj *)o)->desc->name);
        return s ? s : rt_str("");
    }
    obj s = rt_object_to_string(o);
    return s ? s : rt_str("");
}
obj shim_System_String__Concat__objectArr(obj args) {
    intptr_t n = args ? (intptr_t)((Il2CppArray *)rt_nc(args))->max_length : 0;
    obj parts = rt_new_string_array((int32_t)n);
    for (intptr_t i = 0; i < n; i++) RT_ELEM(parts, i, obj) = concat_part(RT_ELEM(args, i, obj));
    return rt_call<obj>(rva::String_Concat_array, parts);
}
obj shim_System_String__Concat__object(obj a) { return concat_part(a); }
obj shim_System_String__Concat__object_object(obj a, obj b) {
    return rt_call<obj>(rva::String_Concat2, concat_part(a), concat_part(b));
}
