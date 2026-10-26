// SilkNX runtime core: libc shims, formatting, logging, module init.
#include "silk.h"

extern "C" {

void *memcpy(void *dst, const void *src, size_t n) {
    auto *d = static_cast<uint8_t *>(dst);
    auto *s = static_cast<const uint8_t *>(src);
    while (n--) *d++ = *s++;
    return dst;
}

void *memmove(void *dst, const void *src, size_t n) {
    auto *d = static_cast<uint8_t *>(dst);
    auto *s = static_cast<const uint8_t *>(src);
    if (d < s) {
        while (n--) *d++ = *s++;
    } else {
        while (n--) d[n] = s[n];
    }
    return dst;
}

void *memset(void *dst, int c, size_t n) {
    auto *d = static_cast<uint8_t *>(dst);
    while (n--) *d++ = static_cast<uint8_t>(c);
    return dst;
}

int memcmp(const void *a, const void *b, size_t n) {
    auto *x = static_cast<const uint8_t *>(a);
    auto *y = static_cast<const uint8_t *>(b);
    for (; n; n--, x++, y++)
        if (*x != *y) return *x - *y;
    return 0;
}

size_t strlen(const char *s) {
    size_t n = 0;
    while (s[n]) n++;
    return n;
}

// Exported by the game's main module; used to find main's load address.
extern void nnMain();
}

uintptr_t silk_main_base;

namespace silk {

static void put(char *buf, size_t cap, size_t &n, char c) {
    if (n + 1 < cap) buf[n] = c;
    n++;
}

static void put_uint(char *buf, size_t cap, size_t &n, uint64_t v, unsigned base, int width, char pad) {
    char tmp[24];
    int k = 0;
    do {
        unsigned d = v % base;
        tmp[k++] = static_cast<char>(d < 10 ? '0' + d : 'a' + d - 10);
        v /= base;
    } while (v);
    while (k < width) tmp[k++] = pad;
    while (k) put(buf, cap, n, tmp[--k]);
}

int vsnfmt(char *buf, size_t cap, const char *fmt, __builtin_va_list ap) {
    size_t n = 0;
    for (; *fmt; fmt++) {
        if (*fmt != '%') {
            put(buf, cap, n, *fmt);
            continue;
        }
        fmt++;
        char pad = ' ';
        if (*fmt == '0') {
            pad = '0';
            fmt++;
        }
        int width = 0;
        while (*fmt >= '0' && *fmt <= '9') width = width * 10 + (*fmt++ - '0');
        bool lng = false;
        while (*fmt == 'l') {
            lng = true;
            fmt++;
        }
        switch (*fmt) {
            case 's': {
                const char *s = __builtin_va_arg(ap, const char *);
                for (s = s ? s : "(null)"; *s; s++) put(buf, cap, n, *s);
                break;
            }
            case 'c':
                put(buf, cap, n, static_cast<char>(__builtin_va_arg(ap, int)));
                break;
            case 'd': {
                int64_t v = lng ? __builtin_va_arg(ap, int64_t) : __builtin_va_arg(ap, int);
                if (v < 0) {
                    put(buf, cap, n, '-');
                    v = -v;
                }
                put_uint(buf, cap, n, static_cast<uint64_t>(v), 10, width, pad);
                break;
            }
            case 'u':
                put_uint(buf, cap, n, lng ? __builtin_va_arg(ap, uint64_t) : __builtin_va_arg(ap, unsigned), 10,
                         width, pad);
                break;
            case 'x':
                put_uint(buf, cap, n, lng ? __builtin_va_arg(ap, uint64_t) : __builtin_va_arg(ap, unsigned), 16,
                         width, pad);
                break;
            case 'p':
                put(buf, cap, n, '0');
                put(buf, cap, n, 'x');
                put_uint(buf, cap, n, reinterpret_cast<uintptr_t>(__builtin_va_arg(ap, void *)), 16, width, pad);
                break;
            case 'f': {
                double v = __builtin_va_arg(ap, double);
                if (v < 0) {
                    put(buf, cap, n, '-');
                    v = -v;
                }
                uint64_t whole = static_cast<uint64_t>(v);
                put_uint(buf, cap, n, whole, 10, width, pad);
                put(buf, cap, n, '.');
                put_uint(buf, cap, n, static_cast<uint64_t>((v - whole) * 100.0 + 0.5) % 100, 10, 2, '0');
                break;
            }
            case '%':
                put(buf, cap, n, '%');
                break;
            default:
                put(buf, cap, n, '?');
                break;
        }
    }
    if (cap) buf[n < cap ? n : cap - 1] = 0;
    return static_cast<int>(n);
}

int snfmt(char *buf, size_t cap, const char *fmt, ...) {
    __builtin_va_list ap;
    __builtin_va_start(ap, fmt);
    int r = vsnfmt(buf, cap, fmt, ap);
    __builtin_va_end(ap);
    return r;
}

void log(const char *fmt, ...) {
    char buf[512];
    int n = snfmt(buf, sizeof(buf), "[silknx] ");
    __builtin_va_list ap;
    __builtin_va_start(ap, fmt);
    n += vsnfmt(buf + n, sizeof(buf) - n, fmt, ap);
    __builtin_va_end(ap);
    if (n > static_cast<int>(sizeof(buf)) - 1) n = sizeof(buf) - 1;
    svcOutputDebugString(buf, n);
}

}  // namespace silk

// Implemented by each mod; called once from the module initializer.
void silk_main();

// Provided by the generated hook table: offset of this module from main, as assumed by the IPS patch.
extern "C" const uint64_t silk_expected_module_offset;

SILK_EXPORT void silk_module_init() {
    static bool done;
    if (done) return;
    done = true;
    silk_main_base = reinterpret_cast<uintptr_t>(&nnMain) - SILK_NNMAIN_RVA;
    uintptr_t self = reinterpret_cast<uintptr_t>(__module_start);
    silk::log("loaded: main=%p self=%p offset=%lx (expected %lx)", reinterpret_cast<void *>(silk_main_base),
              reinterpret_cast<void *>(self), static_cast<uint64_t>(self - silk_main_base),
              silk_expected_module_offset);
    if (self - silk_main_base != silk_expected_module_offset)
        silk::log("FATAL: module layout differs from the IPS patch; hooks would jump to the wrong address");
    silk_main();
}
