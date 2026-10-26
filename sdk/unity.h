// Unity value types as IL2CPP lays them out (passed as HFAs / sret exactly like the game's own code).
#pragma once

#include "silk.h"

namespace unity {

struct Vector3 {
    float x, y, z;
};

struct Rect {
    float x, y, w, h;
};

struct Color {
    float r, g, b, a;
};

struct Bounds {
    Vector3 center, extents;
};

// System.String: klass, monitor, int length, UTF-16 chars.
struct String {
    void *klass;
    void *monitor;
    int32_t length;
    char16_t chars[1];
};

constexpr int EventType_Repaint = 7;
constexpr int TextAnchor_MiddleCenter = 4;
constexpr int TextAnchor_MiddleLeft = 3;

}  // namespace unity
