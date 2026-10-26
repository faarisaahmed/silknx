// Thin wrappers for the Horizon supervisor calls the runtime needs.
.macro SVC_FN name, id
.section ".text.\name", "ax"
.global \name
.hidden \name
.type \name, %function
.align 2
\name:
    svc \id
    ret
.endm

// Result svcOutputDebugString(const char *str, u64 size)
SVC_FN svcOutputDebugString, 0x27

// Result svcGetInfo(u64 *out, u32 id0, Handle handle, u64 id1)
.section ".text.svcGetInfo", "ax"
.global svcGetInfo
.hidden svcGetInfo
.type svcGetInfo, %function
.align 2
svcGetInfo:
    str x0, [sp, #-16]!
    svc 0x29
    ldr x2, [sp], #16
    str x1, [x2]
    ret

// Result svcQueryMemory(MemoryInfo *out, u32 *pageinfo, u64 addr)
.section ".text.svcQueryMemory", "ax"
.global svcQueryMemory
.hidden svcQueryMemory
.type svcQueryMemory, %function
.align 2
svcQueryMemory:
    str x1, [sp, #-16]!
    svc 0x06
    ldr x2, [sp], #16
    str w1, [x2]
    ret

// void svcSleepThread(s64 ns)
SVC_FN svcSleepThread, 0x0B

// u64 svcGetSystemTick(void)
SVC_FN svcGetSystemTick, 0x1E

// noreturn svcBreak(u32 reason, u64 addr, u64 size)
SVC_FN svcBreak, 0x26
