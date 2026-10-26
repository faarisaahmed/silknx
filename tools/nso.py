#!/usr/bin/env python3
"""NSO helpers: decompress to a flat memory image, read dynamic symbols."""
import struct
import sys

import lz4.block


class Nso:
    def __init__(self, path):
        d = open(path, 'rb').read()
        assert d[:4] == b'NSO0'
        flags, = struct.unpack_from('<I', d, 0xC)
        self.segs = []  # (file_off, mem_off, size, file_size)
        image = bytearray()
        for i, (h, fs) in enumerate([(0x10, 0x60), (0x20, 0x64), (0x30, 0x68)]):
            foff, moff, size = struct.unpack_from('<III', d, h)
            fsize, = struct.unpack_from('<I', d, fs)
            raw = d[foff:foff + fsize]
            data = lz4.block.decompress(raw, uncompressed_size=size) if flags & (1 << i) else raw
            if len(image) < moff:
                image += bytes(moff - len(image))
            image[moff:moff + size] = data
            self.segs.append((moff, size))
        self.bss_size, = struct.unpack_from('<I', d, 0x3C)
        self.build_id = d[0x40:0x60]
        self.image = bytes(image)
        self.text, self.ro, self.data = self.segs

    def u32(self, o):
        return struct.unpack_from('<I', self.image, o)[0]

    def u64(self, o):
        return struct.unpack_from('<Q', self.image, o)[0]

    def dynamic(self):
        mod0 = self.u32(4)
        assert self.image[mod0:mod0 + 4] == b'MOD0'
        dyn = mod0 + struct.unpack_from('<i', self.image, mod0 + 4)[0]
        out = {}
        o = dyn
        while True:
            tag, val = struct.unpack_from('<QQ', self.image, o)
            if tag == 0:
                break
            out.setdefault(tag, []).append(val)
            o += 16
        return out

    def symbols(self):
        dy = self.dynamic()
        strtab, symtab = dy[5][0], dy[6][0]
        # DT_HASH nchain gives symbol count; fall back to strtab bound.
        if 4 in dy:
            n = self.u32(dy[4][0] + 4)
        else:
            n = (strtab - symtab) // 24
        for i in range(n):
            name_off, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', self.image, symtab + 24 * i)
            e = self.image.index(b'\0', strtab + name_off)
            yield self.image[strtab + name_off:e].decode(errors='replace'), value, size, shndx, info


if __name__ == '__main__':
    n = Nso(sys.argv[1])
    print('segments', [(hex(a), hex(b)) for a, b in n.segs], 'bss', hex(n.bss_size), 'build_id', n.build_id.hex())
    if '--syms' in sys.argv:
        for name, val, size, shndx, info in n.symbols():
            print(f'{val:#010x} {size:#x} {"D" if shndx else "U"} {name}')
