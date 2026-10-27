// SilkNX runtime: support code for rt_collections.h.
#include "rt_collections.h"

using namespace rva;

namespace silk_coll {

rt_modtype d_list = {"System.Collections.Generic.List(SilkNX)", nullptr, sizeof(ListObj), 5, false, nullptr, 0,
                     nullptr, false, nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_dict = {"System.Collections.Generic.Dictionary(SilkNX)", nullptr, sizeof(DictObj), 5, false, nullptr, 0,
                     nullptr, false, nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_set = {"System.Collections.Generic.HashSet(SilkNX)", nullptr, sizeof(DictObj), 5, false, nullptr, 0,
                    nullptr, false, nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_keys = {"System.Collections.Generic.KeyCollection(SilkNX)", nullptr, sizeof(CollView), 5, false,
                     nullptr, 0, nullptr, false, nullptr, 0, nullptr, {}, nullptr};
rt_modtype d_values = {"System.Collections.Generic.ValueCollection(SilkNX)", nullptr, sizeof(CollView), 5, false,
                       nullptr, 0, nullptr, false, nullptr, 0, nullptr, {}, nullptr};

static void *g_string_klass;

bool is_string(obj o) {
    if (!g_string_klass) g_string_klass = rt_str16(u"", 0)->klass;
    return o && o->klass == g_string_klass;
}

obj buffer(size_t bytes) {
    // (bytes/8 + 1) zero-filled, GC-traced slots (see rt_new_raw_storage)
    return rt_new_raw_storage((intptr_t)((bytes + 7) / 8 + 1));
}

static obj make(const char *aqn) {
    obj t = rt_call<obj>(Type_GetType, rt_str(aqn));
    return t ? rt_call<obj>(FormatterServices_GetUninitializedObject, t) : nullptr;
}
void key_not_found() { rt_throw(make("System.Collections.Generic.KeyNotFoundException, mscorlib")); }
void duplicate_key() { rt_throw(make("System.ArgumentException, mscorlib")); }
void modified() { rt_throw(make("System.InvalidOperationException, mscorlib")); }

}  // namespace silk_coll

obj new_object_array(intptr_t n);

// A converted (emulated) collection is an object[] to the game. Game code taking IEnumerable would walk
// its raw slots (descriptor pointer first) and crash, so hand it a real object[] snapshot instead.
obj rt_native_enum(obj o, const char *where) {
    if (!o || !rt_is_mod(o)) return o;
    const rt_modtype *d = ((ModObj *)o)->desc;
    using silk_coll::d_list;
    using silk_coll::d_set;
    if (d == &d_list && ((silk_coll::ListObj *)o)->ref_elems) {
        auto *l = (silk_coll::ListObj *)o;
        obj a = new_object_array(l->size);
        for (int32_t i = 0; i < l->size; i++) RT_ELEM(a, i, obj) = silk_coll::items<obj>(o)[i];
        return a;
    }
    if (d == &d_set && ((silk_coll::DictObj *)o)->ref_keys) {
        int32_t n = silk_coll::Dict_get_Count<obj, silk_coll::Unit>(o), k = 0;
        obj a = new_object_array(n);
        for (int32_t i = 0; silk_coll::Dict_next<obj, silk_coll::Unit>(o, i) && k < n;)
            RT_ELEM(a, k++, obj) = silk_coll::entries<obj, silk_coll::Unit>(o)[i - 1].key;
        return a;
    }
    char msg[200];
    silk::snfmt(msg, sizeof(msg), "passing a converted %s to game code (%s)", d->name, where);
    rt_unsupported(msg);
}
