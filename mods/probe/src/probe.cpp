// Logs the process memory map so we can confirm where each module is loaded.
#include "silk.h"

void silk_main() {
    uint64_t addr = 0;
    for (int i = 0; i < 400; i++) {
        MemoryInfo mi{};
        uint32_t pi;
        if (svcQueryMemory(&mi, &pi, addr)) break;
        if (mi.type != 0)
            silk::log("map %lx-%lx type=%x perm=%x", mi.addr, mi.addr + mi.size, mi.type, mi.perm);
        uint64_t next = mi.addr + mi.size;
        if (next <= addr) break;
        addr = next;
    }
    uint64_t v = 0;
    static const uint32_t ids[] = {2, 3, 4, 5, 12, 13, 14, 15};
    for (uint32_t id : ids) {
        svcGetInfo(&v, id, 0xFFFF8001, 0);
        silk::log("info %u = %lx", id, v);
    }
}
