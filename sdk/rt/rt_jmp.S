// setjmp/longjmp for translated exception handling (callee-saved state only).
.section ".text.rt_setjmp", "ax"
.global rt_setjmp
.hidden rt_setjmp
.type rt_setjmp, %function
.align 2
rt_setjmp:
    stp x19, x20, [x0, #0]
    stp x21, x22, [x0, #16]
    stp x23, x24, [x0, #32]
    stp x25, x26, [x0, #48]
    stp x27, x28, [x0, #64]
    stp x29, x30, [x0, #80]
    mov x2, sp
    str x2, [x0, #96]
    stp d8, d9, [x0, #104]
    stp d10, d11, [x0, #120]
    stp d12, d13, [x0, #136]
    stp d14, d15, [x0, #152]
    mov w0, #0
    ret

.section ".text.rt_longjmp", "ax"
.global rt_longjmp
.hidden rt_longjmp
.type rt_longjmp, %function
.align 2
rt_longjmp:
    ldp x19, x20, [x0, #0]
    ldp x21, x22, [x0, #16]
    ldp x23, x24, [x0, #32]
    ldp x25, x26, [x0, #48]
    ldp x27, x28, [x0, #64]
    ldp x29, x30, [x0, #80]
    ldr x2, [x0, #96]
    mov sp, x2
    ldp d8, d9, [x0, #104]
    ldp d10, d11, [x0, #120]
    ldp d12, d13, [x0, #136]
    ldp d14, d15, [x0, #152]
    cmp w1, #0
    csinc w0, w1, wzr, ne
    ret

// Current thread's TLS pointer (unique per thread).
.section ".text.rt_thread_id", "ax"
.global rt_thread_id
.hidden rt_thread_id
.type rt_thread_id, %function
.align 2
rt_thread_id:
    mrs x0, tpidrro_el0
    ret
