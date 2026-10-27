// Developer self-test (built only with SILKNX_AUTOTEST=1): once in gameplay, hits nearby enemies through the
// game's own HealthManager.Hit so mod hooks run exactly as when the player attacks.
#include "rt.h"

#ifdef SILKNX_AUTOTEST
using namespace rva;

namespace {
struct HitInstanceRaw {
    uint8_t b[184];
};
template <typename T>
void put(HitInstanceRaw &h, uint32_t boxed_off, T v) {
    memcpy(h.b + boxed_off - 0x10, &v, sizeof(T));
}
float g_next;
int g_hits;
}  // namespace

void rt_autotest_frame() {
    float now = rt_call<float>(Time_get_unscaledTime);
    obj hero = rt_call<obj>(HeroController_get_instance);
    if (!hero) {
        // On the title screen: load save slot 2 ourselves (what pressing Start does).
        static bool loading;
        static float title_seen;
        obj gm = rt_call<obj>(GameManager_get_instance);
        if (!gm || loading) return;
        if (!title_seen) title_seen = now;
        if (now - title_seen < 6.0f) return;
        loading = true;
        silk::log("autotest: loading save slot 2");
        RT_GUARD_BEGIN
        rt_call<void>(GameManager_LoadGameFromUI, gm, (int32_t)2);
        RT_GUARD_END
        return;
    }
    if (now < g_next) return;
    g_next = now + 0.7f;
    static obj hm_type;
    if (!hm_type) {
        hm_type = rt_call<obj>(Type_GetType, rt_str("HealthManager, Assembly-CSharp"));
        *(obj *)rt_root_alloc(8) = hm_type;
    }
    obj all = rt_call<obj>(Object_FindObjectsOfType_Type, hm_type);
    intptr_t n = all ? (intptr_t)((Il2CppArray *)all)->max_length : 0;
    int done = 0;
    for (intptr_t i = 0; i < n && done < 4; i++) {
        obj hm = RT_ELEM(all, i, obj);
        int hp = *(int32_t *)((uint8_t *)hm + off::HealthManager_hp);
        if (hp <= 0 || !rt_call<bool>(GameObject_get_activeInHierarchy, rt_call<obj>(Component_get_gameObject, hm)))
            continue;
        HitInstanceRaw h{};
        put<obj>(h, 0x10, rt_call<obj>(Component_get_gameObject, hero));
        put<bool>(h, 0x18, true);              // IsFirstHit
        put<int32_t>(h, 0x54, 7);              // DamageDealt
        put<float>(h, 0x80, 1.0f);             // MagnitudeMultiplier
        put<float>(h, 0x9C, 1.0f);             // Multiplier
        put<bool>(h, 0x7C, true);              // IgnoreInvulnerable
        char name[64];
        rt_str_to_utf8(rt_call<obj>(Object_get_name, rt_call<obj>(Component_get_gameObject, hm)), name, sizeof(name));
        RT_GUARD_BEGIN
        ((int32_t(*)(obj, HitInstanceRaw, const void *))(silk_main_base + HealthManager_Hit))(hm, h, nullptr);
        RT_GUARD_END
        int after = *(int32_t *)((uint8_t *)hm + off::HealthManager_hp);
        silk::log("autotest: hit #%d %s hp %d -> %d", ++g_hits, name, hp, after);
        done++;
    }
    static int idle;
    static int scene_i;
    static const char *const scenes[] = {"Tut_02", "Tut_03", "Bone_01", "Mosstown_01", "Bone_02", "Tut_04"};
    if (done) {
        idle = 0;
        return;
    }
    if (++idle == 8 && scene_i < 6) {  // ~5s with nothing to hit: go somewhere with enemies
        idle = 0;
        const char *sc = scenes[scene_i++];
        silk::log("autotest: no enemies here (%d HealthManagers); going to %s", (int)n, sc);
        obj gm = rt_call<obj>(GameManager_get_instance);
        RT_GUARD_BEGIN
        rt_call<void>(GameManager_ChangeToScene, gm, rt_str(sc), rt_str("left1"), 0.0f);
        RT_GUARD_END
    }
}
#else
void rt_autotest_frame() {}
#endif
