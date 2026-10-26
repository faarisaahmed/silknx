// SilkNX runtime: freestanding helpers for native Silksong (Switch, IL2CPP) mods.
#pragma once

#include <stddef.h>
#include <stdint.h>

#define SILK_EXPORT extern "C" __attribute__((visibility("default")))
#define SILK_HIDDEN __attribute__((visibility("hidden")))

typedef uint32_t Result;

struct MemoryInfo {
    uint64_t addr;
    uint64_t size;
    uint32_t type;
    uint32_t attr;
    uint32_t perm;
    uint32_t ipc_refcount;
    uint32_t device_refcount;
    uint32_t padding;
};

extern "C" {
Result svcOutputDebugString(const char *str, uint64_t size);
Result svcGetInfo(uint64_t *out, uint32_t id0, uint32_t handle, uint64_t id1);
Result svcQueryMemory(MemoryInfo *out, uint32_t *pageinfo, uint64_t addr);
void svcSleepThread(int64_t ns);
uint64_t svcGetSystemTick(void);
[[noreturn]] void svcBreak(uint32_t reason, uint64_t addr, uint64_t size);

void *memcpy(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
void *memmove(void *dst, const void *src, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);

extern char __module_start[];

// Base address of the game's `main` module; set before any hook can run.
extern uintptr_t silk_main_base;
}

namespace silk {

// --- logging -------------------------------------------------------------
// printf subset: %s %d %u %x %p %c %f (fixed 2 decimals), optional width for x/d.
int vsnfmt(char *buf, size_t cap, const char *fmt, __builtin_va_list ap);
int snfmt(char *buf, size_t cap, const char *fmt, ...);
void log(const char *fmt, ...);

// --- calling game code ---------------------------------------------------
template <typename Ret, typename... Args>
inline Ret call(uintptr_t rva, Args... args) {
    using Fn = Ret (*)(Args...);
    return reinterpret_cast<Fn>(silk_main_base + rva)(args...);
}

template <typename T>
inline T &field(void *obj, uintptr_t off) {
    return *reinterpret_cast<T *>(reinterpret_cast<uint8_t *>(obj) + off);
}

}  // namespace silk

// Hook declaration: SILK_HOOK(Name, ret, args...) { ... orig_Name(args...); }
// `Name` must match an entry in the mod's mod.json "hooks" list.
#define SILK_HOOK(name, ret, ...)              \
    extern "C" ret orig_##name(__VA_ARGS__);   \
    extern "C" ret hook_##name(__VA_ARGS__)
