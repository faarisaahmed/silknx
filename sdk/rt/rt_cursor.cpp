// SilkNX runtime: a virtual mouse for the controller.
//
// Mods built for PC expect a mouse (Unity UI buttons, IMGUI windows, Input.mousePosition). L3 + R3 toggles a
// cursor: the left stick moves it, A clicks, the right stick scrolls, B (or L3 + R3 again) hides it. While it
// is shown the game gets no controller input, so clicking doesn't also make Hornet jump.
//
// The cursor is fed to everything that reads the mouse:
//   * Unity UI (EventSystem): BaseInput's mouse getters are hooked, and the game's HollowKnightInputModule
//     (which only handles a real mouse) gets StandaloneInputModule's mouse processing run after it.
//   * UnityEngine.Input mouse getters, for mods that poll the mouse themselves.
//   * IMGUI (OnGUI): the current event's mouse position is moved to the cursor; clicks/scrolls are delivered by
//     turning that frame's Repaint into the MouseDown/MouseUp/ScrollWheel event (mods' OnGUI runs once for it).
#include "rt.h"
#include "unity.h"
#include "cursor_img.h"

using namespace rva;
using unity::Rect;
using unity::Color;
using unity::Vector3;

void rt_key_combos(char *out, int cap);

namespace {

struct Vec2 {
    float x, y;
};

// InControl.InputControlType
enum {
    kLeftUp = 1, kLeftDown, kLeftLeft, kLeftRight, kL3,
    kRightUp, kRightDown, kRightLeft, kRightRight, kR3,
    kDUp, kDDown, kDLeft, kDRight,
    kButtonB = 19, kButtonA = 20,  // Action1 / Action2 (Nintendo layout)
};
// UnityEngine.EventType
enum { EV_MouseDown = 0, EV_MouseUp = 1, EV_MouseDrag = 3, EV_ScrollWheel = 6, EV_Repaint = 7 };

bool g_active;
float g_x, g_y;          // screen pixels, origin bottom-left (like Input.mousePosition)
bool g_btn, g_btn_prev;  // left button this frame / last frame
float g_scroll;          // wheel ticks this frame (positive = up)
bool g_hint_shown;
int g_saved_lock = -1;  // Cursor.lockState to put back when the cursor is hidden

// IMGUI events waiting for a Repaint to replace
int g_queue[8];
float g_queue_scroll[8];
int g_qhead, g_qlen;
int g_drag_frames;

obj g_tex;  // cursor texture (rooted)
constexpr float kHotX = 2, kHotY = 1;  // tip of the pointer in the image
obj *g_root;

obj control(int type) {
    obj dev = rt_call<obj>(InputManager_get_ActiveDevice);
    return dev ? rt_call<obj>(InputDevice_GetControl, dev, (int32_t)type) : nullptr;
}

bool held(int type) {
    obj c = control(type);
    return c && rt_call<bool>(OneAxisInputControl_get_IsPressed, c);
}

bool down(int type) {
    obj c = control(type);
    return c && rt_call<bool>(OneAxisInputControl_get_WasPressed, c);
}

float value(int type) {
    obj c = control(type);
    return c ? rt_call<float>(OneAxisInputControl_get_Value, c) : 0.0f;
}

void enqueue(int type, float scroll = 0) {
    if (g_qlen == 8) return;
    int i = (g_qhead + g_qlen++) % 8;
    g_queue[i] = type;
    g_queue_scroll[i] = scroll;
}

void set_active(bool on) {
    g_active = on;
    g_btn = g_btn_prev = false;
    g_qlen = 0;
    if (!on && g_saved_lock >= 0) {
        rt_call<void>(Cursor_set_lockState, (int32_t)g_saved_lock);
        g_saved_lock = -1;
    }
    if (on) {
        if (g_x == 0 && g_y == 0) {
            g_x = rt_call<int>(Screen_get_width) * 0.5f;
            g_y = rt_call<int>(Screen_get_height) * 0.5f;
        }
        rt_console("Cursor on: left stick moves, A clicks, right stick scrolls, B closes");
        char keys[300];
        rt_key_combos(keys, sizeof(keys));
        if (keys[0]) rt_console("Mod keys: %s", keys);
        silk::log("cursor: on, screen %dx%d, dt %d ms", rt_call<int>(Screen_get_width), rt_call<int>(Screen_get_height),
                  (int)(rt_call<float>(Time_get_unscaledDeltaTime) * 1000));
    }
}

float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }

}  // namespace

bool rt_cursor_active() { return g_active; }

// Once per frame (before most game updates).
void rt_input_frame() {
    if (!g_hint_shown && rt_call<float>(Time_get_unscaledTime) > 8.0f) {
        g_hint_shown = true;
        rt_console("Mouse cursor for mod menus: click both sticks (L3 + R3)");
    }
    bool sticks = held(kL3) && held(kR3) && (down(kL3) || down(kR3));
    if (sticks || (g_active && down(kButtonB))) set_active(!g_active && sticks);
    if (!g_active) return;

    float w = (float)rt_call<int>(Screen_get_width), h = (float)rt_call<int>(Screen_get_height);
    float dt = rt_call<float>(Time_get_unscaledDeltaTime);
    if (dt > 0.1f) dt = 0.1f;
    float sx = value(kLeftRight) - value(kLeftLeft), sy = value(kLeftUp) - value(kLeftDown);
    float mag = __builtin_sqrtf(sx * sx + sy * sy);
    float speed = h * (0.3f + 2.2f * mag);  // ~2.5 screen heights/s at full tilt, slow near the centre
    float nx = g_x + sx * speed * dt, ny = g_y + sy * speed * dt;
    nx += (held(kDRight) - held(kDLeft)) * h * 0.25f * dt;
    ny += (held(kDUp) - held(kDDown)) * h * 0.25f * dt;
    nx = clampf(nx, 0, w - 1);
    ny = clampf(ny, 0, h - 1);
    bool moved = nx != g_x || ny != g_y;
    g_x = nx;
    g_y = ny;

    g_btn_prev = g_btn;
    g_btn = held(kButtonA) && !held(kL3);  // L3 + A is a keyboard combo, not a click
    float r = value(kRightUp) - value(kRightDown);
    g_scroll = (r > 0.2f || r < -0.2f) ? r * 0.5f : 0.0f;

    if (g_btn && !g_btn_prev) enqueue(EV_MouseDown);
    if (!g_btn && g_btn_prev) enqueue(EV_MouseUp);
    if (g_btn && moved && !g_qlen && ++g_drag_frames % 3 == 0) enqueue(EV_MouseDrag);
    if (g_scroll != 0 && !g_qlen) enqueue(EV_ScrollWheel, g_scroll);
}

// ---------------------------------------------------------------- IMGUI

// Before mods' OnGUI: returns the event type to restore afterwards (-1: nothing replaced).
int rt_cursor_gui_begin() {
    if (!g_active) return -1;
    void *ev = rt_call<void *>(Event_get_current);
    if (!ev) return -1;
    float h = (float)rt_call<int>(Screen_get_height);
    rt_call<void>(Event_set_mousePosition, ev, Vec2{g_x, h - g_y});
    if (!g_qlen || rt_call<int>(Event_get_type, ev) != EV_Repaint) return -1;
    int i = g_qhead;
    g_qhead = (g_qhead + 1) % 8;
    g_qlen--;
    rt_call<void>(Event_set_type, ev, (int32_t)g_queue[i]);
    if (g_queue[i] == EV_ScrollWheel) rt_call<void>(Event_set_delta, ev, Vec2{0, -g_queue_scroll[i] * 3});
    return EV_Repaint;
}

void rt_cursor_gui_end(int restore) {
    if (restore < 0) return;
    void *ev = rt_call<void *>(Event_get_current);
    if (ev) rt_call<void>(Event_set_type, ev, (int32_t)restore);
}

static obj make_cursor_texture() {
    obj t = rt_call<obj>(Type_GetType, rt_str("UnityEngine.Texture2D, UnityEngine.CoreModule"));
    obj tex = rt_call<obj>(FormatterServices_GetUninitializedObject, t);
    rt_call<void>(Texture2D_ctor_fmt_mip, tex, (int32_t)kCursorW, (int32_t)kCursorH, (int32_t)4 /*RGBA32*/, false);
    rt_call<void>(Texture2D_LoadRawTextureData_ptr, tex, (void *)kCursorRGBA, (int32_t)sizeof(kCursorRGBA));
    rt_call<void>(Texture2D_Apply2, tex, false, false);
    rt_call<void>(Object_set_hideFlags, tex, (int32_t)(1 | 4 | 8 | 16 | 32) /*HideAndDontSave*/);
    return tex;
}

void rt_cursor_draw() {
    if (!g_active) return;
    void *ev = rt_call<void *>(Event_get_current);
    if (!ev || rt_call<int>(Event_get_type, ev) != EV_Repaint) return;
    if (!g_tex) {
        if (!g_root) g_root = (obj *)rt_root_alloc(16);
        g_tex = g_root[0] = make_cursor_texture();
        if (!g_tex) return;
    }
    float h = (float)rt_call<int>(Screen_get_height);
    float k = h / 1080.0f * 1.25f;  // the PC game shows it at 37px; a bit bigger reads better on a TV
    rt_call<void>(GUI_set_color, Color{1, 1, 1, 1});
    rt_call<void>(GUI_DrawTexture, Rect{g_x - kHotX * k, h - g_y - kHotY * k, kCursorW * k, kCursorH * k}, g_tex);
}

// ---------------------------------------------------------------- hooks: mouse getters

// Unity UI reads the mouse through BaseInput.
SILK_HOOK(CU_BI_mousePresent, bool, obj self, const void *mi) {
    return g_active || orig_CU_BI_mousePresent(self, mi);
}
SILK_HOOK(CU_BI_GetMouseButton, bool, obj self, int32_t b, const void *mi) {
    if (!g_active) return orig_CU_BI_GetMouseButton(self, b, mi);
    return b == 0 && g_btn;
}
SILK_HOOK(CU_BI_GetMouseButtonDown, bool, obj self, int32_t b, const void *mi) {
    if (!g_active) return orig_CU_BI_GetMouseButtonDown(self, b, mi);
    return b == 0 && g_btn && !g_btn_prev;
}
SILK_HOOK(CU_BI_GetMouseButtonUp, bool, obj self, int32_t b, const void *mi) {
    if (!g_active) return orig_CU_BI_GetMouseButtonUp(self, b, mi);
    return b == 0 && !g_btn && g_btn_prev;
}
SILK_HOOK(CU_BI_mousePosition, Vec2, obj self, const void *mi) {
    if (!g_active) return orig_CU_BI_mousePosition(self, mi);
    return Vec2{g_x, g_y};
}
SILK_HOOK(CU_BI_mouseScrollDelta, Vec2, obj self, const void *mi) {
    if (!g_active) return orig_CU_BI_mouseScrollDelta(self, mi);
    return Vec2{0, g_scroll};
}

// UnityEngine.Input, for mods (and game code) polling the mouse directly.
SILK_HOOK(CU_In_mousePresent, bool, const void *mi) { return g_active || orig_CU_In_mousePresent(mi); }
SILK_HOOK(CU_In_GetMouseButton, bool, int32_t b, const void *mi) {
    if (!g_active) return orig_CU_In_GetMouseButton(b, mi);
    return b == 0 && g_btn;
}
SILK_HOOK(CU_In_GetMouseButtonDown, bool, int32_t b, const void *mi) {
    if (!g_active) return orig_CU_In_GetMouseButtonDown(b, mi);
    return b == 0 && g_btn && !g_btn_prev;
}
SILK_HOOK(CU_In_GetMouseButtonUp, bool, int32_t b, const void *mi) {
    if (!g_active) return orig_CU_In_GetMouseButtonUp(b, mi);
    return b == 0 && !g_btn && g_btn_prev;
}
SILK_HOOK(CU_In_mousePosition, Vector3, const void *mi) {
    if (!g_active) return orig_CU_In_mousePosition(mi);
    return Vector3{g_x, g_y, 0};
}
SILK_HOOK(CU_In_mouseScrollDelta, Vec2, const void *mi) {
    if (!g_active) return orig_CU_In_mouseScrollDelta(mi);
    return Vec2{0, g_scroll};
}

// Unity UI's StandaloneInputModule polls PC keyboard buttons/axes ("Submit", "Horizontal"...) every frame before
// it looks at the mouse; the Switch build has no such axes. While the cursor is shown they read as idle.
SILK_HOOK(CU_BI_GetButtonDown, bool, obj self, obj name, const void *mi) {
    if (g_active) return false;
    return orig_CU_BI_GetButtonDown(self, name, mi);
}
SILK_HOOK(CU_BI_GetAxisRaw, float, obj self, obj name, const void *mi) {
    if (g_active) return 0.0f;
    return orig_CU_BI_GetAxisRaw(self, name, mi);
}

// Make sure the current EventSystem's mouse handling runs once per frame, whatever its module decided (the
// game's HollowKnightInputModule only does it for a real mouse).
namespace {
int g_mouse_frame = -1;
obj g_sim_type;
}  // namespace

SILK_HOOK(CU_SIM_ProcessMouseEvent, void, obj self, int32_t id, const void *mi) {
    g_mouse_frame = rt_call<int>(Time_get_frameCount);
    orig_CU_SIM_ProcessMouseEvent(self, id, mi);
}

SILK_HOOK(CU_ES_Update, void, obj self, const void *mi) {
    // Unity UI drops the mouse position (-1, -1) while the cursor is locked, which the game does on Switch
    if (g_active && rt_call<int>(Cursor_get_lockState) != 0) {
        if (g_saved_lock < 0) g_saved_lock = rt_call<int>(Cursor_get_lockState);
        rt_call<void>(Cursor_set_lockState, (int32_t)0);
    }
    orig_CU_ES_Update(self, mi);
    if (!g_active || self != rt_call<obj>(EventSystem_get_current)) return;
    if (g_mouse_frame == rt_call<int>(Time_get_frameCount)) return;
    if (!g_sim_type) {
        if (!g_root) g_root = (obj *)rt_root_alloc(16);
        g_sim_type = g_root[1] =
            rt_call<obj>(Type_GetType, rt_str("UnityEngine.EventSystems.StandaloneInputModule, UnityEngine.UI"));
    }
    obj go = rt_call<obj>(Component_get_gameObject, self);
    obj module = go && g_sim_type ? rt_call<obj>(GameObject_GetComponent_Type, go, g_sim_type) : nullptr;
    if (!module || !rt_call<bool>(Behaviour_get_isActiveAndEnabled, module)) return;
    static int logged;
    if (logged++ < 3) silk::log("cursor: driving mouse for EventSystem %p via %s", self,
                                  *(const char **)((uint8_t *)module->klass + 0x10));
    orig_CU_SIM_ProcessMouseEvent(module, 0, nullptr);
    g_mouse_frame = rt_call<int>(Time_get_frameCount);
}

// While the cursor is shown, every InControl action set (Hornet's controls, menu navigation) sees no input.
SILK_HOOK(CU_ActionSet_Update, void, obj self, uint64_t tick, float dt, const void *mi) {
    if (!g_active) return orig_CU_ActionSet_Update(self, tick, dt, mi);
    bool &enabled = silk::field<bool>(self, off::PlayerActionSet_Enabled);
    bool was = enabled;
    enabled = false;
    orig_CU_ActionSet_Update(self, tick, dt, mi);
    enabled = was;
}
