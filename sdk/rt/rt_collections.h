// SilkNX runtime: List<T> / Dictionary<K,V> / HashSet<T> for element types the Switch build never compiled
// (e.g. a mod's own structs). Used by translated code instead of the game's generic code for those instances.
//
// Storage lives in object[] buffers so the garbage collector scans it (elements may hold game objects).
// Enumerator structs use the exact field layout of the real BCL enumerators, so translated code that
// declares them as locals works unchanged.
#pragma once

#include "rt.h"

namespace silk_coll {

// ---------------------------------------------------------------- equality / hashing (EqualityComparer<T>.Default)
inline bool str_eq(obj a, obj b) {
    auto *x = (Il2CppString *)a, *y = (Il2CppString *)b;
    return x->length == y->length && !memcmp(x->chars, y->chars, (size_t)x->length * 2);
}
bool is_string(obj o);

template <typename T>
inline bool eq(const T &a, const T &b) {
    if constexpr (__is_same(T, obj)) {
        if (a == b) return true;
        if (!a || !b) return false;
        return is_string(a) && is_string(b) && str_eq(a, b);
    } else if constexpr (__is_arithmetic(T) || __is_enum(T) || __is_pointer(T)) {
        return a == b;
    } else {
        return !memcmp(&a, &b, sizeof(T));
    }
}

template <typename T>
inline int32_t hash(const T &v) {
    uint32_t h = 2166136261u;
    if constexpr (__is_same(T, obj)) {
        if (!v) return 0;
        if (is_string(v)) {
            auto *s = (Il2CppString *)v;
            for (int i = 0; i < s->length; i++) h = (h ^ s->chars[i]) * 16777619u;
            return (int32_t)(h & 0x7FFFFFFF);
        }
        uintptr_t p = (uintptr_t)v;
        return (int32_t)((p >> 4) ^ (p >> 20)) & 0x7FFFFFFF;
    } else {
        const uint8_t *b = (const uint8_t *)&v;
        for (size_t i = 0; i < sizeof(T); i++) h = (h ^ b[i]) * 16777619u;
        return (int32_t)(h & 0x7FFFFFFF);
    }
}

// ---------------------------------------------------------------- raw GC-visible buffers
obj buffer(size_t bytes);  // object[] with at least `bytes` of zeroed storage after its header
inline uint8_t *data(obj buf) { return (uint8_t *)buf + 0x20; }

extern rt_modtype d_list, d_dict, d_set, d_keys, d_values;
[[noreturn]] void key_not_found();
[[noreturn]] void duplicate_key();
[[noreturn]] void modified();

// ================================================================ List<T>
struct ListObj : ModObj {
    obj items;
    int32_t size, version, capacity;
    int32_t ref_elems;  // elements are object references (can be handed to game code as an object[])
};

template <typename T>
struct ListEnum {  // == System.Collections.Generic.List`1/Enumerator
    obj list;
    int32_t index, version;
    T current;
};

template <typename T>
inline T *items(obj l) { return (T *)data(((ListObj *)l)->items); }

template <typename T>
obj List_new(int32_t cap) {
    obj l = rt_new_mod(&d_list);
    auto *o = (ListObj *)l;
    o->capacity = cap > 4 ? cap : 4;
    o->items = buffer(sizeof(T) * o->capacity);
    o->ref_elems = __is_same(T, obj);
    return l;
}

template <typename T>
void List_grow(ListObj *o, int32_t need) {
    if (need <= o->capacity) return;
    int32_t cap = o->capacity * 2 > need ? o->capacity * 2 : need;
    obj nb = buffer(sizeof(T) * cap);
    memcpy(data(nb), data(o->items), sizeof(T) * o->size);
    o->items = nb;
    o->capacity = cap;
}

template <typename T>
int32_t List_get_Count(obj l) { return ((ListObj *)rt_nc(l))->size; }

template <typename T>
void List_Add(obj l, T v) {
    auto *o = (ListObj *)rt_nc(l);
    List_grow<T>(o, o->size + 1);
    items<T>(l)[o->size++] = v;
    o->version++;
}

template <typename T>
T List_get_Item(obj l, int32_t i) {
    auto *o = (ListObj *)rt_nc(l);
    if ((uint32_t)i >= (uint32_t)o->size) rt_throw_index();
    return items<T>(l)[i];
}

template <typename T>
void List_set_Item(obj l, int32_t i, T v) {
    auto *o = (ListObj *)rt_nc(l);
    if ((uint32_t)i >= (uint32_t)o->size) rt_throw_index();
    items<T>(l)[i] = v;
    o->version++;
}

template <typename T>
void List_Insert(obj l, int32_t i, T v) {
    auto *o = (ListObj *)rt_nc(l);
    if ((uint32_t)i > (uint32_t)o->size) rt_throw_index();
    List_grow<T>(o, o->size + 1);
    T *it = items<T>(l);
    memmove(it + i + 1, it + i, sizeof(T) * (o->size - i));
    it[i] = v;
    o->size++;
    o->version++;
}

template <typename T>
void List_RemoveAt(obj l, int32_t i) {
    auto *o = (ListObj *)rt_nc(l);
    if ((uint32_t)i >= (uint32_t)o->size) rt_throw_index();
    T *it = items<T>(l);
    memmove(it + i, it + i + 1, sizeof(T) * (o->size - i - 1));
    o->size--;
    memset(it + o->size, 0, sizeof(T));
    o->version++;
}

template <typename T>
int32_t List_IndexOf(obj l, T v) {
    auto *o = (ListObj *)rt_nc(l);
    T *it = items<T>(l);
    for (int32_t i = 0; i < o->size; i++)
        if (eq(it[i], v)) return i;
    return -1;
}

template <typename T>
bool List_Contains(obj l, T v) { return List_IndexOf<T>(l, v) >= 0; }

template <typename T>
bool List_Remove(obj l, T v) {
    int32_t i = List_IndexOf<T>(l, v);
    if (i < 0) return false;
    List_RemoveAt<T>(l, i);
    return true;
}

template <typename T>
void List_Clear(obj l) {
    auto *o = (ListObj *)rt_nc(l);
    memset(items<T>(l), 0, sizeof(T) * o->size);
    o->size = 0;
    o->version++;
}

// Predicate<T> / Comparison<T> / Action<T> are real delegates (or mod delegates): call them directly.
template <typename R, typename... A>
inline R call_delegate(obj d, A... a) {
    return ((R(*)(obj, A..., const void *))rt_delegate_fn(rt_nc(d)))(rt_delegate_target(d), a..., rt_delegate_mi(d));
}

template <typename T>
int32_t List_RemoveAll(obj l, obj pred) {
    auto *o = (ListObj *)rt_nc(l);
    T *it = items<T>(l);
    int32_t w = 0;
    for (int32_t i = 0; i < o->size; i++)
        if (!call_delegate<bool>(pred, it[i])) it[w++] = it[i];
    int32_t removed = o->size - w;
    memset(it + w, 0, sizeof(T) * removed);
    o->size = w;
    o->version++;
    return removed;
}

template <typename T>
bool List_Exists(obj l, obj pred) {
    auto *o = (ListObj *)rt_nc(l);
    for (int32_t i = 0; i < o->size; i++)
        if (call_delegate<bool>(pred, items<T>(l)[i])) return true;
    return false;
}

template <typename T>
T List_Find(obj l, obj pred) {
    auto *o = (ListObj *)rt_nc(l);
    for (int32_t i = 0; i < o->size; i++)
        if (call_delegate<bool>(pred, items<T>(l)[i])) return items<T>(l)[i];
    return T{};
}

template <typename T>
int32_t List_FindIndex(obj l, obj pred) {
    auto *o = (ListObj *)rt_nc(l);
    for (int32_t i = 0; i < o->size; i++)
        if (call_delegate<bool>(pred, items<T>(l)[i])) return i;
    return -1;
}

template <typename T>
void List_ForEach(obj l, obj action) {
    auto *o = (ListObj *)rt_nc(l);
    for (int32_t i = 0; i < o->size; i++) call_delegate<void>(action, items<T>(l)[i]);
}

template <typename T>
void List_Sort(obj l, obj comparison) {  // insertion sort: stable, fine for mod-sized lists
    auto *o = (ListObj *)rt_nc(l);
    T *it = items<T>(l);
    for (int32_t i = 1; i < o->size; i++) {
        T v = it[i];
        int32_t j = i - 1;
        while (j >= 0 && call_delegate<int32_t>(comparison, it[j], v) > 0) {
            it[j + 1] = it[j];
            j--;
        }
        it[j + 1] = v;
    }
    o->version++;
}

template <typename T>
void List_Reverse(obj l) {
    auto *o = (ListObj *)rt_nc(l);
    T *it = items<T>(l);
    for (int32_t i = 0, j = o->size - 1; i < j; i++, j--) {
        T t = it[i];
        it[i] = it[j];
        it[j] = t;
    }
    o->version++;
}

template <typename T>
obj List_ToArray(obj l, int32_t elem_tref) {
    auto *o = (ListObj *)rt_nc(l);
    obj a = rt_new_array(elem_tref, o->size, sizeof(T));
    for (int32_t i = 0; i < o->size; i++) RT_ELEM(a, i, T) = items<T>(l)[i];
    return a;
}

template <typename T>
void List_AddRangeList(obj l, obj other) {  // other: another emulated List<T> or a T[] array
    if (rt_is_mod(other)) {
        auto *src = (ListObj *)other;
        for (int32_t i = 0; i < src->size; i++) List_Add<T>(l, items<T>(other)[i]);
        return;
    }
    intptr_t n = rt_ldlen(other);
    for (intptr_t i = 0; i < n; i++) List_Add<T>(l, RT_ELEM(other, i, T));
}

template <typename T>
ListEnum<T> List_GetEnumerator(obj l) {
    ListEnum<T> e{};
    e.list = rt_nc(l);
    e.version = ((ListObj *)l)->version;
    return e;
}

template <typename T>
bool ListEnum_MoveNext(ListEnum<T> *e) {
    auto *o = (ListObj *)e->list;
    if (e->version != o->version) modified();
    if (e->index < o->size) {
        e->current = items<T>(e->list)[e->index++];
        return true;
    }
    e->index = o->size + 1;
    e->current = T{};
    return false;
}

template <typename T>
T ListEnum_get_Current(ListEnum<T> *e) { return e->current; }

template <typename T>
void ListEnum_Dispose(ListEnum<T> *) {}

// ================================================================ Dictionary<K,V> / HashSet<T>
// Separate chaining: buckets[i] = 1 + index of the first entry, entries linked by `next` (1-based, 0 = end).
template <typename K, typename V>
struct Entry {
    int32_t hash, next;  // hash < 0: free slot
    K key;
    V value;
};

struct DictObj : ModObj {
    obj buckets, entries;
    int32_t count, capacity, free_list, free_count, version;
    int32_t ref_keys;  // keys are object references
    int32_t entry_size, key_off, val_off;  // Entry<K,V> layout, for code that only knows K or V
};

template <typename K, typename V>
struct KVP {  // == KeyValuePair<K,V>
    K key;
    V value;
};

template <typename K, typename V>
struct DictEnum {  // == Dictionary`2/Enumerator
    obj dict;
    int32_t version, index;
    KVP<K, V> current;
    int32_t ret_type;
};

template <typename K, typename V>
struct KeyEnum {  // == Dictionary`2/KeyCollection/Enumerator
    obj dict;
    int32_t index, version;
    K current;
};

template <typename K, typename V>
struct ValEnum {  // == Dictionary`2/ValueCollection/Enumerator
    obj dict;
    int32_t index, version;
    V current;
};

struct CollView : ModObj {  // Keys / Values views
    obj dict;
};

template <typename K, typename V>
inline Entry<K, V> *entries(obj d) { return (Entry<K, V> *)data(((DictObj *)d)->entries); }
inline int32_t *buckets(obj d) { return (int32_t *)data(((DictObj *)d)->buckets); }

template <typename K, typename V>
obj Dict_new_d(const rt_modtype *desc, int32_t cap) {
    obj d = rt_new_mod(desc);
    auto *o = (DictObj *)d;
    o->capacity = cap > 8 ? cap : 8;
    o->buckets = buffer(sizeof(int32_t) * o->capacity);
    o->entries = buffer(sizeof(Entry<K, V>) * o->capacity);
    o->free_list = -1;
    o->ref_keys = __is_same(K, obj);
    o->entry_size = (int32_t)sizeof(Entry<K, V>);
    o->key_off = (int32_t)__builtin_offsetof(Entry<K, V>, key);
    o->val_off = (int32_t)__builtin_offsetof(Entry<K, V>, value);
    return d;
}

template <typename K, typename V>
obj Dict_new(int32_t cap) { return Dict_new_d<K, V>(&d_dict, cap); }

template <typename K, typename V>
void Dict_resize(obj d) {
    auto *o = (DictObj *)d;
    int32_t cap = o->capacity * 2;
    obj nb = buffer(sizeof(int32_t) * cap);
    obj ne = buffer(sizeof(Entry<K, V>) * cap);
    memcpy(data(ne), data(o->entries), sizeof(Entry<K, V>) * o->count);
    auto *e = (Entry<K, V> *)data(ne);
    auto *b = (int32_t *)data(nb);
    for (int32_t i = 0; i < o->count; i++) {
        if (e[i].hash < 0) continue;
        int32_t bi = e[i].hash % cap;
        e[i].next = b[bi];
        b[bi] = i + 1;
    }
    o->buckets = nb;
    o->entries = ne;
    o->capacity = cap;
}

template <typename K, typename V>
int32_t Dict_find(obj d, const K &key) {
    auto *o = (DictObj *)rt_nc(d);
    int32_t h = hash(key);
    for (int32_t i = buckets(d)[h % o->capacity]; i;) {
        Entry<K, V> &e = entries<K, V>(d)[i - 1];
        if (e.hash == h && eq(e.key, key)) return i - 1;
        i = e.next;
    }
    return -1;
}

template <typename K, typename V>
bool Dict_insert(obj d, const K &key, const V &value, int mode) {  // 0 add (throw dup), 1 set, 2 try-add
    auto *o = (DictObj *)rt_nc(d);
    int32_t at = Dict_find<K, V>(d, key);
    if (at >= 0) {
        if (mode == 0) duplicate_key();
        if (mode == 2) return false;
        entries<K, V>(d)[at].value = value;
        o->version++;
        return true;
    }
    int32_t idx;
    if (o->free_count > 0) {
        idx = o->free_list;
        o->free_list = entries<K, V>(d)[idx].next - 1;
        o->free_count--;
    } else {
        if (o->count == o->capacity) Dict_resize<K, V>(d);
        idx = o->count++;
    }
    int32_t h = hash(key);
    Entry<K, V> &e = entries<K, V>(d)[idx];
    e.hash = h;
    e.key = key;
    e.value = value;
    int32_t bi = h % o->capacity;
    e.next = buckets(d)[bi];
    buckets(d)[bi] = idx + 1;
    o->version++;
    return true;
}

template <typename K, typename V>
void Dict_Add(obj d, K k, V v) { Dict_insert<K, V>(d, k, v, 0); }
template <typename K, typename V>
bool Dict_TryAdd(obj d, K k, V v) { return Dict_insert<K, V>(d, k, v, 2); }
template <typename K, typename V>
void Dict_set_Item(obj d, K k, V v) { Dict_insert<K, V>(d, k, v, 1); }

template <typename K, typename V>
V Dict_get_Item(obj d, K k) {
    int32_t at = Dict_find<K, V>(d, k);
    if (at < 0) key_not_found();
    return entries<K, V>(d)[at].value;
}

template <typename K, typename V>
bool Dict_TryGetValue(obj d, K k, V *out) {
    int32_t at = Dict_find<K, V>(d, k);
    if (at < 0) {
        *out = V{};
        return false;
    }
    *out = entries<K, V>(d)[at].value;
    return true;
}

template <typename K, typename V>
bool Dict_ContainsKey(obj d, K k) { return Dict_find<K, V>(d, k) >= 0; }

template <typename K, typename V>
bool Dict_ContainsValue(obj d, V v) {
    auto *o = (DictObj *)rt_nc(d);
    for (int32_t i = 0; i < o->count; i++)
        if (entries<K, V>(d)[i].hash >= 0 && eq(entries<K, V>(d)[i].value, v)) return true;
    return false;
}

template <typename K, typename V>
bool Dict_Remove2(obj d, K k, V *out) {
    auto *o = (DictObj *)rt_nc(d);
    int32_t h = hash(k);
    int32_t bi = h % o->capacity, prev = 0;
    for (int32_t i = buckets(d)[bi]; i;) {
        Entry<K, V> &e = entries<K, V>(d)[i - 1];
        if (e.hash == h && eq(e.key, k)) {
            if (prev) entries<K, V>(d)[prev - 1].next = e.next;
            else buckets(d)[bi] = e.next;
            if (out) *out = e.value;
            memset(&e, 0, sizeof(e));
            e.hash = -1;
            e.next = o->free_list + 1;
            o->free_list = i - 1;
            o->free_count++;
            o->version++;
            return true;
        }
        prev = i;
        i = e.next;
    }
    if (out) *out = V{};
    return false;
}

template <typename K, typename V>
bool Dict_Remove(obj d, K k) { return Dict_Remove2<K, V>(d, k, nullptr); }

template <typename K, typename V>
int32_t Dict_get_Count(obj d) {
    auto *o = (DictObj *)rt_nc(d);
    return o->count - o->free_count;
}

template <typename K, typename V>
void Dict_Clear(obj d) {
    auto *o = (DictObj *)rt_nc(d);
    memset(data(o->buckets), 0, sizeof(int32_t) * o->capacity);
    memset(data(o->entries), 0, sizeof(Entry<K, V>) * o->capacity);
    o->count = o->free_count = 0;
    o->free_list = -1;
    o->version++;
}

template <typename K, typename V>
bool Dict_next(obj d, int32_t &index) {  // advance to the next used entry; index is 1 past it on success
    auto *o = (DictObj *)d;
    while (index < o->count) {
        if (entries<K, V>(d)[index].hash >= 0) {
            index++;
            return true;
        }
        index++;
    }
    return false;
}

template <typename K, typename V>
void Dict_CopyFrom(obj d, obj src) {  // Dictionary(IDictionary) from another emulated dictionary
    rt_nc(src);
    for (int32_t i = 0; Dict_next<K, V>(src, i);) {
        Entry<K, V> &e = entries<K, V>(src)[i - 1];
        Dict_insert<K, V>(d, e.key, e.value, 0);
    }
}

template <typename K, typename V>
DictEnum<K, V> Dict_GetEnumerator(obj d) {
    DictEnum<K, V> e{};
    e.dict = rt_nc(d);
    e.version = ((DictObj *)d)->version;
    e.ret_type = 2;
    return e;
}

template <typename K, typename V>
bool DictEnum_MoveNext(DictEnum<K, V> *e) {
    if (e->version != ((DictObj *)e->dict)->version) modified();
    if (!Dict_next<K, V>(e->dict, e->index)) return false;
    Entry<K, V> &x = entries<K, V>(e->dict)[e->index - 1];
    e->current.key = x.key;
    e->current.value = x.value;
    return true;
}
template <typename K, typename V>
KVP<K, V> DictEnum_get_Current(DictEnum<K, V> *e) { return e->current; }
template <typename K, typename V>
void DictEnum_Dispose(DictEnum<K, V> *) {}
template <typename K, typename V>
void DictEnum_Dispose(KeyEnum<K, V> *) {}
template <typename K, typename V>
void DictEnum_Dispose(ValEnum<K, V> *) {}

template <typename K, typename V>
K KVP_get_Key(KVP<K, V> *p) { return p->key; }
template <typename K, typename V>
V KVP_get_Value(KVP<K, V> *p) { return p->value; }

// Keys / Values
template <typename K, typename V>
obj Dict_get_Keys(obj d) {
    obj v = rt_new_mod(&d_keys);
    ((CollView *)v)->dict = rt_nc(d);
    return v;
}
template <typename K, typename V>
obj Dict_get_Values(obj d) {
    obj v = rt_new_mod(&d_values);
    ((CollView *)v)->dict = rt_nc(d);
    return v;
}
template <typename K, typename V>
int32_t View_get_Count(obj v) { return Dict_get_Count<K, V>(((CollView *)rt_nc(v))->dict); }

template <typename K, typename V>
KeyEnum<K, V> Keys_GetEnumerator(obj v) {
    KeyEnum<K, V> e{};
    e.dict = ((CollView *)rt_nc(v))->dict;
    e.version = ((DictObj *)e.dict)->version;
    return e;
}
template <typename K, typename V>
bool KeyEnum_MoveNext(KeyEnum<K, V> *e) {
    if (e->version != ((DictObj *)e->dict)->version) modified();
    if (!Dict_next<K, V>(e->dict, e->index)) return false;
    e->current = entries<K, V>(e->dict)[e->index - 1].key;
    return true;
}
template <typename K, typename V>
K KeyEnum_get_Current(KeyEnum<K, V> *e) { return e->current; }

template <typename K, typename V>
ValEnum<K, V> Values_GetEnumerator(obj v) {
    ValEnum<K, V> e{};
    e.dict = ((CollView *)rt_nc(v))->dict;
    e.version = ((DictObj *)e.dict)->version;
    return e;
}
template <typename K, typename V>
bool ValEnum_MoveNext(ValEnum<K, V> *e) {
    if (e->version != ((DictObj *)e->dict)->version) modified();
    if (!Dict_next<K, V>(e->dict, e->index)) return false;
    e->current = entries<K, V>(e->dict)[e->index - 1].value;
    return true;
}
template <typename K, typename V>
V ValEnum_get_Current(ValEnum<K, V> *e) { return e->current; }

// HashSet<T>: a Dictionary<T, bool>
struct Unit {
    uint8_t _;
};

template <typename T>
struct SetEnum {  // == HashSet`1/Enumerator
    obj set;
    int32_t index, version;
    T current;
};

template <typename T>
obj Set_new(int32_t cap) { return Dict_new_d<T, Unit>(&d_set, cap); }
template <typename T>
bool Set_Add(obj s, T v) { return Dict_insert<T, Unit>(s, v, Unit{}, 2); }
template <typename T>
bool Set_Contains(obj s, T v) { return Dict_find<T, Unit>(s, v) >= 0; }
template <typename T>
bool Set_Remove(obj s, T v) { return Dict_Remove2<T, Unit>(s, v, nullptr); }
template <typename T>
int32_t Set_get_Count(obj s) { return Dict_get_Count<T, Unit>(s); }
template <typename T>
void Set_Clear(obj s) { Dict_Clear<T, Unit>(s); }

template <typename T>
SetEnum<T> Set_GetEnumerator(obj s) {
    SetEnum<T> e{};
    e.set = rt_nc(s);
    e.version = ((DictObj *)s)->version;
    return e;
}
template <typename T>
bool SetEnum_MoveNext(SetEnum<T> *e) {
    if (e->version != ((DictObj *)e->set)->version) modified();
    if (!Dict_next<T, Unit>(e->set, e->index)) return false;
    e->current = entries<T, Unit>(e->set)[e->index - 1].key;
    return true;
}
template <typename T>
T SetEnum_get_Current(SetEnum<T> *e) { return e->current; }

// set algebra with another emulated set
template <typename T>
void Set_UnionWith(obj s, obj other) {
    for (int32_t i = 0; Dict_next<T, Unit>(other, i);) Set_Add<T>(s, entries<T, Unit>(other)[i - 1].key);
}
template <typename T>
void Set_ExceptWith(obj s, obj other) {
    for (int32_t i = 0; Dict_next<T, Unit>(other, i);) Set_Remove<T>(s, entries<T, Unit>(other)[i - 1].key);
}
template <typename T>
bool Set_IsSupersetOf(obj s, obj other) {
    for (int32_t i = 0; Dict_next<T, Unit>(other, i);)
        if (!Set_Contains<T>(s, entries<T, Unit>(other)[i - 1].key)) return false;
    return true;
}

// ---------------------------------------------------------------- interface-typed access
// Code that sees a converted collection only as IEnumerable<T> / ICollection<T> / IList<T> / IDictionary<K,V>
// (e.g. a List<ModType> passed as IReadOnlyList<ModType>) is routed here at runtime; the call site knows T.
extern rt_modtype d_iter;
struct IterObj : ModObj {  // IEnumerator<T> over any converted collection
    obj coll;
    int32_t index, version;
};

inline bool is_emu(obj o) {
    if (!o || !rt_is_mod(o)) return false;
    const rt_modtype *d = ((ModObj *)o)->desc;
    return d == &d_list || d == &d_dict || d == &d_set || d == &d_keys || d == &d_values || d == &d_iter;
}
inline obj backing_dict(obj c) {  // dictionary behind a dict/set/view, or null for lists
    const rt_modtype *d = ((ModObj *)c)->desc;
    if (d == &d_keys || d == &d_values) return ((CollView *)c)->dict;
    return d == &d_list ? nullptr : c;
}
inline int32_t version_of(obj c) {
    obj d = backing_dict(c);
    return d ? ((DictObj *)d)->version : ((ListObj *)c)->version;
}
obj Iter_new(obj coll);
bool Iter_MoveNext(obj it);
int32_t Coll_Count(obj c);
[[noreturn]] void coll_unsupported(obj c, const char *what);

template <typename T>
T Iter_Current(obj it) {
    auto *t = (IterObj *)rt_nc(it);
    obj c = t->coll;
    const rt_modtype *d = ((ModObj *)c)->desc;
    if (d == &d_list) return items<T>(c)[t->index - 1];
    auto *o = (DictObj *)backing_dict(c);
    uint8_t *e = data(o->entries) + (size_t)(t->index - 1) * o->entry_size;
    return *(T *)(e + (d == &d_values ? o->val_off : o->key_off));
}
template <typename K, typename V>
KVP<K, V> Iter_CurrentKVP(obj it) {  // IEnumerator<KeyValuePair<K,V>> over a dictionary
    auto *t = (IterObj *)rt_nc(it);
    Entry<K, V> &e = entries<K, V>(t->coll)[t->index - 1];
    return KVP<K, V>{e.key, e.value};
}
obj Iter_CurrentObj(obj it);  // non-generic IEnumerator.Current (reference elements)

template <typename T>
T Coll_Item(obj c, int32_t i) {
    if (((ModObj *)c)->desc != &d_list) coll_unsupported(c, "indexer");
    return List_get_Item<T>(c, i);
}
template <typename T>
void Coll_SetItem(obj c, int32_t i, T v) {
    if (((ModObj *)c)->desc != &d_list) coll_unsupported(c, "indexer");
    List_set_Item<T>(c, i, v);
}
template <typename T>
bool Coll_Contains(obj c, T v) {
    const rt_modtype *d = ((ModObj *)c)->desc;
    if (d == &d_list) return List_Contains<T>(c, v);
    if (d == &d_set) return Set_Contains<T>(c, v);
    coll_unsupported(c, "Contains");
}
template <typename T>
bool Coll_Add(obj c, T v) {
    const rt_modtype *d = ((ModObj *)c)->desc;
    if (d == &d_list) {
        List_Add<T>(c, v);
        return true;
    }
    if (d == &d_set) return Set_Add<T>(c, v);
    coll_unsupported(c, "Add");
}
template <typename T>
bool Coll_Remove(obj c, T v) {
    const rt_modtype *d = ((ModObj *)c)->desc;
    if (d == &d_list) return List_Remove<T>(c, v);
    if (d == &d_set) return Set_Remove<T>(c, v);
    coll_unsupported(c, "Remove");
}
template <typename T>
void Coll_Clear(obj c) {
    const rt_modtype *d = ((ModObj *)c)->desc;
    if (d == &d_list) return List_Clear<T>(c);
    if (d == &d_set) return Set_Clear<T>(c);
    coll_unsupported(c, "Clear");
}
template <typename T>
int32_t Coll_IndexOf(obj c, T v) {
    if (((ModObj *)c)->desc != &d_list) coll_unsupported(c, "IndexOf");
    return List_IndexOf<T>(c, v);
}

// ---------------------------------------------------------------- LINQ over element types the game lacks
// Enumerable.X<T> with T a mod-defined struct (or another type the Switch build never instantiated) can't run
// natively. These eager equivalents take any source: a converted collection, a mod-struct array, or a native
// IEnumerable; sequence results are converted List<T>s.
obj src_enumerator(obj src);  // native IEnumerable.GetEnumerator()
bool src_movenext(obj e);     // native IEnumerator.MoveNext()
obj src_current(obj e);       // native IEnumerator.Current (boxed for value types)
bool is_struct_array(obj o);  // mod-struct array: raw storage with the element count tagged in `bounds`

template <typename T>
inline T unbox_as(obj o) {
    if constexpr (__is_same(T, obj)) {
        return o;
    } else {
        return *(T *)((uint8_t *)rt_nc(o) + 0x10);
    }
}

template <typename T, typename F>
void for_each(obj src, F f) {
    rt_nc(src);
    if (is_emu(src)) {
        obj it = Iter_new(src);
        while (Iter_MoveNext(it))
            if (!f(Iter_Current<T>(it))) return;
        return;
    }
    if (is_struct_array(src)) {
        intptr_t n = (intptr_t)((uintptr_t)((Il2CppArray *)src)->bounds >> 16);
        T *p = (T *)((uint8_t *)src + 0x20);
        for (intptr_t i = 0; i < n; i++)
            if (!f(p[i])) return;
        return;
    }
    obj e = src_enumerator(src);
    while (src_movenext(e))
        if (!f(unbox_as<T>(src_current(e)))) return;
}

template <typename T>
int32_t Linq_Count(obj src) {
    int32_t n = 0;
    for_each<T>(src, [&](const T &) { return ++n, true; });
    return n;
}
template <typename T>
int32_t Linq_CountP(obj src, obj pred) {
    int32_t n = 0;
    for_each<T>(src, [&](const T &v) { return n += call_delegate<bool>(pred, v) ? 1 : 0, true; });
    return n;
}
template <typename T>
bool Linq_Any(obj src) {
    bool r = false;
    for_each<T>(src, [&](const T &) { return r = true, false; });
    return r;
}
template <typename T>
bool Linq_AnyP(obj src, obj pred) {
    bool r = false;
    for_each<T>(src, [&](const T &v) { return r = call_delegate<bool>(pred, v), !r; });
    return r;
}
template <typename T>
bool Linq_All(obj src, obj pred) {
    bool r = true;
    for_each<T>(src, [&](const T &v) { return r = call_delegate<bool>(pred, v), r; });
    return r;
}
template <typename T>
obj Linq_Where(obj src, obj pred) {
    obj l = List_new<T>(4);
    for_each<T>(src, [&](const T &v) {
        if (call_delegate<bool>(pred, v)) List_Add<T>(l, v);
        return true;
    });
    return l;
}
template <typename T, typename R>
obj Linq_Select(obj src, obj fn) {
    obj l = List_new<R>(4);
    for_each<T>(src, [&](const T &v) { return List_Add<R>(l, call_delegate<R>(fn, v)), true; });
    return l;
}
template <typename T>
obj Linq_ToList(obj src) {
    obj l = List_new<T>(4);
    for_each<T>(src, [&](const T &v) { return List_Add<T>(l, v), true; });
    return l;
}
template <typename T>
obj Linq_ToArray(obj src) {  // mod-struct array (see rt_new_array)
    obj l = Linq_ToList<T>(src);
    int32_t n = ((ListObj *)l)->size;
    obj a = rt_new_raw_storage((intptr_t)(((size_t)n * sizeof(T) + 7) / 8 + 1));
    ((Il2CppArray *)a)->bounds = (void *)(((uintptr_t)n << 16) | 1);
    memcpy((uint8_t *)a + 0x20, items<T>(l), (size_t)n * sizeof(T));
    return a;
}
template <typename T>
obj Linq_Take(obj src, int32_t count) {
    obj l = List_new<T>(4);
    int32_t n = 0;
    for_each<T>(src, [&](const T &v) {
        if (n++ >= count) return false;
        return List_Add<T>(l, v), true;
    });
    return l;
}
template <typename T>
T Linq_First(obj src, obj pred, bool or_default) {
    T r{};
    bool found = false;
    for_each<T>(src, [&](const T &v) {
        if (pred && !call_delegate<bool>(pred, v)) return true;
        return r = v, found = true, false;
    });
    if (!found && !or_default) coll_unsupported(src, "First() on an empty sequence");
    return r;
}
int32_t compare_strings(obj a, obj b);  // String.CompareOrdinal
template <typename K>
inline int32_t linq_cmp(const K &a, const K &b) {
    if constexpr (__is_same(K, obj)) {
        return compare_strings(a, b);
    } else {
        return a < b ? -1 : (b < a ? 1 : 0);
    }
}
template <typename T, typename K>
obj Linq_OrderBy(obj src, obj key, bool descending) {  // stable
    obj l = Linq_ToList<T>(src);
    int32_t n = ((ListObj *)l)->size;
    obj kb = buffer(sizeof(K) * (size_t)(n + 1));
    K *keys = (K *)data(kb);
    T *it = items<T>(l);
    for (int32_t i = 0; i < n; i++) keys[i] = call_delegate<K>(key, it[i]);
    for (int32_t i = 1; i < n; i++) {
        T v = it[i];
        K k = keys[i];
        int32_t j = i - 1;
        while (j >= 0 && (descending ? linq_cmp<K>(keys[j], k) < 0 : linq_cmp<K>(keys[j], k) > 0)) {
            it[j + 1] = it[j];
            keys[j + 1] = keys[j];
            j--;
        }
        it[j + 1] = v;
        keys[j + 1] = k;
    }
    return l;
}
template <typename T, typename R>
R Linq_Max(obj src, obj sel) {
    R best{};
    bool any = false;
    for_each<T>(src, [&](const T &v) {
        R x = call_delegate<R>(sel, v);
        if (!any || best < x) best = x;
        return any = true, true;
    });
    if (!any) coll_unsupported(src, "Max() on an empty sequence");
    return best;
}
template <typename T, typename R>
R Linq_Min(obj src, obj sel) {
    R best{};
    bool any = false;
    for_each<T>(src, [&](const T &v) {
        R x = call_delegate<R>(sel, v);
        if (!any || x < best) best = x;
        return any = true, true;
    });
    if (!any) coll_unsupported(src, "Min() on an empty sequence");
    return best;
}
template <typename T, typename R>
R Linq_Sum(obj src, obj sel) {
    R total{};
    for_each<T>(src, [&](const T &v) { return total += call_delegate<R>(sel, v), true; });
    return total;
}

}  // namespace silk_coll
