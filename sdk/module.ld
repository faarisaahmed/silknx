/* Layout for a subsdk NSO: text, rodata, data each page aligned, linked at 0. */
OUTPUT_ARCH(aarch64)
ENTRY(__module_start)

PHDRS
{
    text PT_LOAD FLAGS(5);
    rodata PT_LOAD FLAGS(4);
    data PT_LOAD FLAGS(6);
    dynamic PT_DYNAMIC;
}

SECTIONS
{
    . = 0;
    .text : {
        KEEP(*(.text.crt0))
        *(.text .text.*)
        *(.plt .plt.*)
    } :text

    . = ALIGN(0x1000);
    .rodata : {
        KEEP(*(.rodata.mod0))
        *(.rodata .rodata.*)
    } :rodata
    .dynsym   : { *(.dynsym) } :rodata
    .dynstr   : { *(.dynstr) } :rodata
    .hash     : { *(.hash) } :rodata
    .gnu.hash : { *(.gnu.hash) } :rodata
    .rela.dyn : { *(.rela.*) } :rodata
    .eh_frame_hdr : { *(.eh_frame_hdr) } :rodata
    .eh_frame : { KEEP(*(.eh_frame)) } :rodata

    . = ALIGN(0x1000);
    .data : {
        *(.data .data.*)
    } :data
    .init_array : { KEEP(*(.init_array .init_array.*)) } :data
    .dynamic : { __dynamic_start = .; *(.dynamic) } :data :dynamic
    .got : { *(.got) *(.got.plt) *(.igot .igot.plt) } :data
    .bss (NOLOAD) : {
        __bss_start = .;
        *(.bss .bss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } :data

    /DISCARD/ : { *(.comment) *(.note .note.*) *(.interp) }
}
