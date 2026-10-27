// Module header for a Switch NSO loaded by the game's rtld (as subsdk9).
.section ".text.crt0", "ax"
.global __module_start
__module_start:
    .word 0
    .word __nx_mod0 - __module_start

.section ".rodata.mod0", "a"
.align 2
__nx_mod0:
    .ascii "MOD0"
    .word __dynamic_start - __nx_mod0
    .word __bss_start - __nx_mod0
    .word __bss_end - __nx_mod0
    .word __eh_frame_hdr_start - __nx_mod0
    .word __eh_frame_hdr_end - __nx_mod0
    .word __module_object - __nx_mod0

.section ".bss.module_object", "aw", %nobits
.align 3
__module_object:
    .space 0x100
