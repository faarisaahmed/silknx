// Enemy health bars + floating damage numbers for Silksong (Switch).
//
// Rendering: a CodeProfiler component (an existing MonoBehaviour with OnGUI) is attached to the
// persistent GameManager object; its Awake/Update are neutralised and its OnGUI draws our overlay
// with IMGUI. Damage is observed by hooking HealthManager.TakeDamage.
#include "game.h"
#include "unity.h"

using silk::call;
using silk::field;
using namespace unity;

namespace {

// ---- tuning (screen sizes are for 1080p and scaled to the real resolution) ----
constexpr int kBossHp = 105;        // max HP above which an enemy gets the big boss bar
constexpr int kBossHpCap = 3000;    // ignore absurd HP pools (invulnerable props)
constexpr float kBarW = 96, kBarH = 10;
constexpr float kBossW = 900, kBossH = 22;
constexpr float kPopupLife = 1.0f;  // seconds a damage number stays up
constexpr float kPopupRise = 70;    // pixels it floats upwards

struct Tracked {
    void *hm;
    int max_hp;
    float chip;  // trailing "recent damage" segment, in HP
};

struct Popup {
    Vector3 world;
    int dmg;
    float age;
    bool active;
};

Tracked g_tracked[64];
int g_count;
Popup g_popups[48];
void *g_boss;  // HealthManager shown in the boss bar

void *g_overlay;       // CodeProfiler component we draw from
void *g_overlay_host;  // GameManager it is attached to
int g_frames_since_gui = 1000;
float g_watermark = 10.0f;
bool g_first_repaint = true;

bool alive(void *obj) { return obj && field<void *>(obj, off::Object_m_CachedPtr); }

void *mkstr(const char *s) {
    return call<void *>(rva::String_CreateString, (void *)nullptr, s, 0, (int)strlen(s), (void *)nullptr);
}

Tracked *find(void *hm) {
    for (int i = 0; i < g_count; i++)
        if (g_tracked[i].hm == hm) return &g_tracked[i];
    return nullptr;
}

void untrack(void *hm) {
    for (int i = 0; i < g_count; i++)
        if (g_tracked[i].hm == hm) g_tracked[i] = g_tracked[--g_count];
    if (g_boss == hm) g_boss = nullptr;
}

// Top-centre of the enemy in world space (collider top if it has one).
Vector3 anchor(void *hm) {
    void *col = field<void *>(hm, off::HM_boxCollider);
    if (alive(col)) {
        Bounds b = call<Bounds>(rva::Collider2D_get_bounds, col, (void *)nullptr);
        return {b.center.x, b.center.y + b.extents.y, b.center.z};
    }
    void *tr = call<void *>(rva::Component_get_transform, hm, (void *)nullptr);
    Vector3 p = call<Vector3>(rva::Transform_get_position, tr, (void *)nullptr);
    p.y += 1.5f;
    return p;
}

void spawn_popup(Vector3 w, int dmg) {
    Popup *slot = &g_popups[0];
    for (auto &p : g_popups) {
        if (!p.active) {
            slot = &p;
            break;
        }
        if (p.age > slot->age) slot = &p;
    }
    *slot = {w, dmg, 0.0f, true};
}

// ---- drawing helpers ----
void *g_white;

void color(float r, float g, float b, float a) { call<void>(rva::GUI_set_color, Color{r, g, b, a}, (void *)nullptr); }

void rect(float x, float y, float w, float h) {
    call<void>(rva::GUI_DrawTexture, Rect{x, y, w, h}, g_white, (void *)nullptr);
}

void label(float x, float y, float w, float h, void *str, float r, float g, float b, float a, float outline) {
    color(0, 0, 0, a * 0.9f);
    for (int dx = -1; dx <= 1; dx++)
        for (int dy = -1; dy <= 1; dy++)
            if (dx || dy)
                call<void>(rva::GUI_Label, Rect{x + dx * outline, y + dy * outline, w, h}, str, (void *)nullptr);
    color(r, g, b, a);
    call<void>(rva::GUI_Label, Rect{x, y, w, h}, str, (void *)nullptr);
}

void *style_label() {
    void *skin = call<void *>(rva::GUI_get_skin, (void *)nullptr);
    return skin ? call<void *>(rva::GUISkin_get_label, skin, (void *)nullptr) : nullptr;
}

void bar(float x, float y, float w, float h, float frac, float chip_frac, bool boss) {
    color(0, 0, 0, 0.65f);
    rect(x - 2, y - 2, w + 4, h + 4);
    color(0.25f, 0.05f, 0.05f, 0.85f);
    rect(x, y, w, h);
    if (chip_frac > frac) {
        color(1.0f, 0.9f, 0.6f, 0.9f);
        rect(x + w * frac, y, w * (chip_frac - frac), h);
    }
    if (boss)
        color(0.85f, 0.12f, 0.15f, 1.0f);
    else
        color(0.9f - 0.6f * frac, 0.2f + 0.6f * frac, 0.2f, 1.0f);
    rect(x, y, w * frac, h);
}

// Copy an enemy's GameObject name into `out`, minus Unity's "(Clone)"/" (1)" suffixes.
void clean_name(void *hm, char *out, int cap) {
    out[0] = 0;
    void *go = call<void *>(rva::Component_get_gameObject, hm, (void *)nullptr);
    auto *s = static_cast<String *>(call<void *>(rva::Object_get_name, go, (void *)nullptr));
    if (!s) return;
    int n = 0;
    for (int i = 0; i < s->length && n < cap - 1; i++) {
        char16_t c = s->chars[i];
        if (c == '(') break;
        out[n++] = (c >= 32 && c < 127) ? (char)c : '?';
    }
    while (n && out[n - 1] == ' ') n--;
    out[n] = 0;
}

void draw() {
    void *ev = call<void *>(rva::Event_get_current, (void *)nullptr);
    if (!ev || call<int>(rva::Event_get_type, ev, (void *)nullptr) != EventType_Repaint) return;
    g_frames_since_gui = 0;
    if (g_first_repaint) {
        g_first_repaint = false;
        silk::log("first repaint");
    }

    float dt = call<float>(rva::Time_get_unscaledDeltaTime, (void *)nullptr);
    if (dt > 0.1f) dt = 0.1f;
    float sw = (float)call<int>(rva::Screen_get_width, (void *)nullptr);
    float sh = (float)call<int>(rva::Screen_get_height, (void *)nullptr);
    float k = sh / 1080.0f;

    g_white = call<void *>(rva::Texture2D_get_whiteTexture, (void *)nullptr);
    void *style = style_label();
    if (!g_white || !style) return;
    call<void>(rva::GUIStyle_set_alignment, style, TextAnchor_MiddleCenter, (void *)nullptr);

    if (g_watermark > 0) {
        g_watermark -= dt;
        call<void>(rva::GUIStyle_set_fontSize, style, (int)(22 * k), (void *)nullptr);
        float a = g_watermark < 1 ? g_watermark : 1;
        label(20 * k, sh - 60 * k, 420 * k, 40 * k, mkstr("SilkNX: Health Bars loaded"), 1, 1, 1, a, 1.5f * k);
    }

    void *cam = call<void *>(rva::Camera_get_main, (void *)nullptr);
    if (alive(cam)) {
        // Small bars above regular enemies.
        for (int i = 0; i < g_count; i++) {
            Tracked &t = g_tracked[i];
            int hp = field<int>(t.hm, off::HM_hp);
            if (hp <= 0 || field<bool>(t.hm, off::HM_isDead)) {
                untrack(t.hm);
                i--;
                continue;
            }
            if (t.chip > hp) t.chip -= (t.chip - hp) * (dt * 2.5f) + dt * 2;
            if (t.chip < hp) t.chip = (float)hp;
            if (t.hm == g_boss) continue;
            void *go = call<void *>(rva::Component_get_gameObject, t.hm, (void *)nullptr);
            if (!call<bool>(rva::GameObject_get_activeInHierarchy, go, (void *)nullptr)) continue;
            Vector3 s = call<Vector3>(rva::Camera_WorldToScreenPoint, cam, anchor(t.hm), (void *)nullptr);
            if (s.z < 0 || s.x < -200 || s.x > sw + 200) continue;
            float w = kBarW * k, h = kBarH * k;
            float frac = (float)hp / t.max_hp, chip = t.chip / t.max_hp;
            bar(s.x - w / 2, sh - s.y - 22 * k, w, h, frac, chip, false);
        }

        // Floating damage numbers.
        call<void>(rva::GUIStyle_set_fontSize, style, (int)(34 * k), (void *)nullptr);
        for (auto &p : g_popups) {
            if (!p.active) continue;
            p.age += dt;
            if (p.age >= kPopupLife) {
                p.active = false;
                continue;
            }
            Vector3 s = call<Vector3>(rva::Camera_WorldToScreenPoint, cam, p.world, (void *)nullptr);
            if (s.z < 0) continue;
            float t = p.age / kPopupLife;
            float a = t < 0.6f ? 1.0f : 1.0f - (t - 0.6f) / 0.4f;
            float y = sh - s.y - 50 * k - kPopupRise * k * t;
            char buf[16];
            silk::snfmt(buf, sizeof(buf), "%d", p.dmg);
            label(s.x - 100 * k, y, 200 * k, 50 * k, mkstr(buf), 1.0f, 0.85f, 0.3f, a, 2 * k);
        }
    }

    // Boss bar: bottom centre with name and numbers.
    if (g_boss) {
        Tracked *t = find(g_boss);
        if (!t) {
            g_boss = nullptr;
            return;
        }
        int hp = field<int>(g_boss, off::HM_hp);
        float w = kBossW * k, h = kBossH * k, x = (sw - w) / 2, y = sh - 70 * k;
        bar(x, y, w, h, (float)hp / t->max_hp, t->chip / t->max_hp, true);
        char name[48], buf[96];
        clean_name(g_boss, name, sizeof(name));
        call<void>(rva::GUIStyle_set_fontSize, style, (int)(24 * k), (void *)nullptr);
        label(x, y - 36 * k, w, 32 * k, mkstr(name), 1, 1, 1, 1, 1.5f * k);
        silk::snfmt(buf, sizeof(buf), "%d / %d", hp, t->max_hp);
        call<void>(rva::GUIStyle_set_fontSize, style, (int)(18 * k), (void *)nullptr);
        label(x, y, w, h, mkstr(buf), 1, 1, 1, 1, 1.5f * k);
    }
    color(1, 1, 1, 1);
}

void ensure_overlay(void *gm) {
    if (g_overlay && g_overlay_host == gm && g_frames_since_gui < 120) return;
    g_frames_since_gui = 0;  // give a fresh component time to receive its first OnGUI
    if (!alive(gm)) return;
    void *go = call<void *>(rva::Component_get_gameObject, gm, (void *)nullptr);
    void *type = call<void *>(rva::Type_GetType, mkstr("CodeProfiler, Assembly-CSharp"), (void *)nullptr);
    if (!go || !type) {
        silk::log("overlay: GetType failed (go=%p type=%p)", go, type);
        return;
    }
    g_overlay = call<void *>(rva::GameObject_AddComponent, go, type, (void *)nullptr);
    g_overlay_host = gm;
    silk::log("overlay attached: %p", g_overlay);
}

}  // namespace

SILK_HOOK(GM_Update, void, void *self, void *mi) {
    orig_GM_Update(self, mi);
    g_frames_since_gui++;
    ensure_overlay(self);
}

// CodeProfiler is only a vehicle for OnGUI: skip its own logic entirely.
SILK_HOOK(CP_Awake, void, void *self, void *mi) {}
SILK_HOOK(CP_Update, void, void *self, void *mi) {}

SILK_HOOK(CP_OnGUI, void, void *self, void *mi) {
    if (self == g_overlay) draw();
}

SILK_HOOK(HM_TakeDamage, void, void *self, void *hit, void *mi) {
    int before = field<int>(self, off::HM_hp);
    orig_HM_TakeDamage(self, hit, mi);
    int after = field<int>(self, off::HM_hp);
    int dmg = before - (after < 0 ? 0 : after);
    if (dmg <= 0) return;

    Tracked *t = find(self);
    if (!t && g_count < (int)(sizeof(g_tracked) / sizeof(g_tracked[0]))) {
        int init = field<int>(self, off::HM_initHp);
        t = &g_tracked[g_count++];
        *t = {self, before > init ? before : init, (float)before};
    }
    if (t) {
        if (before > t->max_hp) t->max_hp = before;
        if (t->max_hp >= kBossHp && t->max_hp <= kBossHpCap && after > 0) g_boss = self;
    }
    spawn_popup(anchor(self), dmg);
}

SILK_HOOK(HM_OnDisable, void, void *self, void *mi) {
    untrack(self);
    orig_HM_OnDisable(self, mi);
}

void silk_main() { silk::log("HealthBars ready"); }
