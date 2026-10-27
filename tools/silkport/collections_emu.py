"""Route List/Dictionary/HashSet instantiations the Switch build never compiled to sdk/rt/rt_collections.h."""
from .model import sig_str
from .types import norm
from .switchdb import dkey, dump_type_name

G = 'System.Collections.Generic.'
LIST, DICT, SET, KVP = G + 'List`1', G + 'Dictionary`2', G + 'HashSet`1', G + 'KeyValuePair`2'

# (owner key, method, param count) -> (template function, kind)
#   kind: 'obj' receiver is the collection object, 'ptr' receiver is a struct pointer (enumerators/KVP),
#         'new' constructor, 'enum' returns an enumerator struct, 'arr' needs the element type reference
TABLE = {
    (LIST, '.ctor', 0): ('List_new', 'new0'), (LIST, '.ctor', 1): ('List_new', 'new1'),
    (LIST, 'get_Count', 0): ('List_get_Count', 'obj'), (LIST, 'Add', 1): ('List_Add', 'obj'),
    (LIST, 'get_Item', 1): ('List_get_Item', 'obj'), (LIST, 'set_Item', 2): ('List_set_Item', 'obj'),
    (LIST, 'Insert', 2): ('List_Insert', 'obj'), (LIST, 'RemoveAt', 1): ('List_RemoveAt', 'obj'),
    (LIST, 'IndexOf', 1): ('List_IndexOf', 'obj'), (LIST, 'Contains', 1): ('List_Contains', 'obj'),
    (LIST, 'Remove', 1): ('List_Remove', 'obj'), (LIST, 'Clear', 0): ('List_Clear', 'obj'),
    (LIST, 'RemoveAll', 1): ('List_RemoveAll', 'obj'), (LIST, 'Exists', 1): ('List_Exists', 'obj'),
    (LIST, 'Find', 1): ('List_Find', 'obj'), (LIST, 'FindIndex', 1): ('List_FindIndex', 'obj'),
    (LIST, 'ForEach', 1): ('List_ForEach', 'obj'), (LIST, 'Sort', 1): ('List_Sort', 'obj'),
    (LIST, 'Reverse', 0): ('List_Reverse', 'obj'), (LIST, 'ToArray', 0): ('List_ToArray', 'arr'),
    (LIST, 'AddRange', 1): ('List_AddRangeList', 'obj'), (LIST, 'GetEnumerator', 0): ('List_GetEnumerator', 'enum'),
    (LIST + '/Enumerator', 'MoveNext', 0): ('ListEnum_MoveNext', 'ptr'),
    (LIST + '/Enumerator', 'get_Current', 0): ('ListEnum_get_Current', 'ptr'),
    (LIST + '/Enumerator', 'Dispose', 0): ('ListEnum_Dispose', 'ptr'),
    (DICT, '.ctor', 0): ('Dict_new', 'new0'), (DICT, '.ctor', 1): ('Dict_new', 'new1'),
    (DICT, '.ctor', 2): ('Dict_new', 'new1'),
    (DICT, 'Add', 2): ('Dict_Add', 'obj'), (DICT, 'TryAdd', 2): ('Dict_TryAdd', 'obj'),
    (DICT, 'set_Item', 2): ('Dict_set_Item', 'obj'), (DICT, 'get_Item', 1): ('Dict_get_Item', 'obj'),
    (DICT, 'TryGetValue', 2): ('Dict_TryGetValue', 'obj'), (DICT, 'ContainsKey', 1): ('Dict_ContainsKey', 'obj'),
    (DICT, 'ContainsValue', 1): ('Dict_ContainsValue', 'obj'), (DICT, 'Remove', 1): ('Dict_Remove', 'obj'),
    (DICT, 'Remove', 2): ('Dict_Remove2', 'obj'), (DICT, 'get_Count', 0): ('Dict_get_Count', 'obj'),
    (DICT, 'Clear', 0): ('Dict_Clear', 'obj'), (DICT, 'GetEnumerator', 0): ('Dict_GetEnumerator', 'enum'),
    (DICT, 'get_Keys', 0): ('Dict_get_Keys', 'obj'), (DICT, 'get_Values', 0): ('Dict_get_Values', 'obj'),
    (DICT + '/Enumerator', 'MoveNext', 0): ('DictEnum_MoveNext', 'ptr'),
    (DICT + '/Enumerator', 'get_Current', 0): ('DictEnum_get_Current', 'ptr'),
    (DICT + '/Enumerator', 'Dispose', 0): ('DictEnum_Dispose', 'ptr'),
    (DICT + '/KeyCollection', 'get_Count', 0): ('View_get_Count', 'obj'),
    (DICT + '/KeyCollection', 'GetEnumerator', 0): ('Keys_GetEnumerator', 'enum'),
    (DICT + '/KeyCollection/Enumerator', 'MoveNext', 0): ('KeyEnum_MoveNext', 'ptr'),
    (DICT + '/KeyCollection/Enumerator', 'get_Current', 0): ('KeyEnum_get_Current', 'ptr'),
    (DICT + '/KeyCollection/Enumerator', 'Dispose', 0): ('DictEnum_Dispose', 'void'),
    (DICT + '/ValueCollection', 'get_Count', 0): ('View_get_Count', 'obj'),
    (DICT + '/ValueCollection', 'GetEnumerator', 0): ('Values_GetEnumerator', 'enum'),
    (DICT + '/ValueCollection/Enumerator', 'MoveNext', 0): ('ValEnum_MoveNext', 'ptr'),
    (DICT + '/ValueCollection/Enumerator', 'get_Current', 0): ('ValEnum_get_Current', 'ptr'),
    (DICT + '/ValueCollection/Enumerator', 'Dispose', 0): ('DictEnum_Dispose', 'void'),
    (KVP, 'get_Key', 0): ('KVP_get_Key', 'ptr'), (KVP, 'get_Value', 0): ('KVP_get_Value', 'ptr'),
    (SET, '.ctor', 0): ('Set_new', 'new0'), (SET, '.ctor', 1): ('Set_new', 'new1'),
    (SET, '.ctor', 2): ('Set_new', 'new1'),
    (SET, 'Add', 1): ('Set_Add', 'obj'), (SET, 'Contains', 1): ('Set_Contains', 'obj'),
    (SET, 'Remove', 1): ('Set_Remove', 'obj'), (SET, 'Clear', 0): ('Set_Clear', 'obj'),
    (SET, 'get_Count', 0): ('Set_get_Count', 'obj'), (SET, 'GetEnumerator', 0): ('Set_GetEnumerator', 'enum'),
    (SET, 'UnionWith', 1): ('Set_UnionWith', 'obj'), (SET, 'ExceptWith', 1): ('Set_ExceptWith', 'obj'),
    (SET, 'IsSupersetOf', 1): ('Set_IsSupersetOf', 'obj'),
    (SET + '/Enumerator', 'MoveNext', 0): ('SetEnum_MoveNext', 'ptr'),
    (SET + '/Enumerator', 'get_Current', 0): ('SetEnum_get_Current', 'ptr'),
    (SET + '/Enumerator', 'Dispose', 0): ('ListEnum_Dispose', 'void'),
}
# C++ struct template that mirrors each BCL value type
STRUCTS = {LIST + '/Enumerator': 'ListEnum', DICT + '/Enumerator': 'DictEnum', SET + '/Enumerator': 'SetEnum',
           DICT + '/KeyCollection/Enumerator': 'KeyEnum', DICT + '/ValueCollection/Enumerator': 'ValEnum',
           KVP: 'KVP'}
OWNERS = {k[0] for k in TABLE}


class CollectionEmu:
    def __init__(self, tr):
        self.tr = tr
        self.available = set(tr.db.generic_instances)

    def canon(self, sig):
        if self.tr.ts.is_ref(sig) or self.tr.is_mod_obj_sig(sig):
            return 'ref'
        return dump_type_name(norm(sig))

    def native(self, owner):
        """Does the Switch build have compiled code for this instantiation? Nested types (enumerators,
        Keys/Values) are compiled together with their outer generic type, so the outer type decides."""
        outer = owner[1].split('/')[0]
        if outer == KVP:
            return True  # trivial struct accessors exist for every KeyValuePair the game uses
        name = dkey(outer)
        return f'{name}|{",".join(self.canon(a) for a in owner[2])}' in self.available

    def emulated(self, owner):
        owner = norm(owner)
        return owner[0] == 'gi' and owner[1] in OWNERS and not self.native(owner)

    def targs(self, owner):
        return ', '.join(self.tr.ts.cty(a) for a in owner[2])

    def call(self, c, thisx, argx):
        """C++ expression for an emulated member call, or None."""
        owner = c['owner']
        if not self.emulated(owner):
            return None
        entry = TABLE.get((owner[1], c['name'], len(c['params'])))
        if entry is None:
            self.tr.report.add('error', f'{sig_str(owner)}.{c["name"]} is not supported for element types the '
                                        f'Switch build lacks')
            return None
        fn, kind = entry
        ta = self.targs(owner)
        call = f'silk_coll::{fn}<{ta}>'
        ret = self.tr.ts.cty(c['ret'])
        if kind in ('ptr', 'void'):
            st = STRUCTS[owner[1]]
            recv = f'(silk_coll::{st}<{ta}>*)({thisx})'
            expr = f'{call}({", ".join([recv] + argx)})'
        elif kind == 'arr':
            expr = f'{call}({thisx}, {self.tr.array_elem_type_ref(owner[2][0])})'
        else:
            expr = f'{call}({", ".join([thisx] + argx)})'
        if kind == 'enum' or (ret.startswith('V_') and kind != 'void'):
            expr = f'rt_bitcast<{ret}>({expr})'
        return expr

    def construct(self, c, argx):
        owner = c['owner']
        if not self.emulated(owner):
            return None
        entry = TABLE.get((owner[1], '.ctor', len(c['params'])))
        if owner[1] == KVP:
            ta = self.targs(owner)
            return f'rt_bitcast<{self.tr.ts.cty(owner)}>(silk_coll::KVP<{ta}>{{{", ".join(argx)}}})'
        if entry is None:
            # Never fall back to the native constructor: every other use of this type is emulated.
            self.tr.report.add('error', f'new {sig_str(owner)} with {len(c["params"])} arguments is not supported '
                                        f'for element types the Switch build lacks')
            return f'(rt_unsupported("new {sig_str(owner)}"), (obj) nullptr)'
        fn, kind = entry
        ta = self.targs(owner)
        params = [norm(p) for p in c['params']]
        if kind == 'new0':
            return f'silk_coll::{fn}<{ta}>(0)'
        p0 = params[0]
        if any(self._is_comparer(p) for p in params):
            self.tr.report.add('warn', f'new {sig_str(owner)}(comparer): custom comparer ignored on Switch '
                                       f'(default equality is used)')
        if p0 == ('prim', 'i4'):
            return f'silk_coll::{fn}<{ta}>({argx[0]})'
        if self._is_comparer(p0):
            return f'silk_coll::{fn}<{ta}>(0)'
        # copy constructor (IEnumerable<T> / IDictionary<K,V> / ISet<T>) from an emulated collection
        copy = {LIST: 'List_AddRangeList', DICT: 'Dict_CopyFrom', SET: 'Set_UnionWith'}[owner[1]]
        return f'({{ obj __l = silk_coll::{fn}<{ta}>(4); silk_coll::{copy}<{ta}>(__l, {argx[0]}); __l; }})'

    @staticmethod
    def _is_comparer(sig):
        return sig[0] in ('gi', 'class') and sig[1].split('`')[0] in (
            'System.Collections.Generic.IEqualityComparer', 'System.Collections.Generic.IComparer')
