#!/usr/bin/env python3
"""Extract ExeFS (PFS0) and RomFS sections from a plaintext (decrypted) NCA.

Usage: nca_extract.py <decrypted.nca> <out_dir> [romfs path prefixes...] [--list]
"""
import bisect
import os
import struct
import sys

import lz4.block


def rd(f, off, n):
    f.seek(off)
    return f.read(n)


def sections(f):
    hdr = rd(f, 0, 0xC00)
    assert hdr[0x200:0x204] == b'NCA3', 'not a plaintext NCA3'
    for i in range(4):
        start, end = struct.unpack_from('<II', hdr, 0x240 + i * 0x10)
        if not end:
            continue
        fsh = hdr[0x400 + i * 0x200:0x600 + i * 0x200]
        yield i, start * 0x200, (end - start) * 0x200, fsh


def pfs0(f, base, out, listing):
    h = rd(f, base, 16)
    assert h[:4] == b'PFS0'
    n, strsz = struct.unpack_from('<II', h, 4)
    ents = [struct.unpack('<QQII', rd(f, base + 16 + 24 * k, 24)) for k in range(n)]
    st = rd(f, base + 16 + 24 * n, strsz)
    data = base + 16 + 24 * n + strsz
    os.makedirs(out, exist_ok=True)
    for off, sz, no, _ in ents:
        name = st[no:st.index(b'\0', no)].decode()
        if listing:
            print(f'  exefs/{name} {sz}')
        else:
            open(os.path.join(out, name), 'wb').write(rd(f, data + off, sz))


class Compressed:
    """Virtual view of a compressed NCA data level (BucketTree of CompressedStorage entries)."""

    NODE = 0x4000

    def __init__(self, f, base, table_off, count):
        self.f, self.base = f, base
        per_set = (self.NODE - 0x10) // 0x18
        sets = (count + per_set - 1) // per_set
        ents = []
        for k in range(sets):
            node = rd(f, base + table_off + self.NODE * (1 + k), self.NODE)
            _idx, n, _end = struct.unpack_from('<IIq', node, 0)
            for j in range(n):
                ents.append(struct.unpack_from('<qqBbHI', node, 0x10 + 0x18 * j))
        assert len(ents) == count, (len(ents), count)
        self.ents = ents
        self.voffs = [e[0] for e in ents]
        self.end = _end
        self.cache = (None, b'')

    def _block(self, i):
        if self.cache[0] == i:
            return self.cache[1]
        voff, poff, ctype, _lvl, _r, psize = self.ents[i]
        vsize = (self.voffs[i + 1] if i + 1 < len(self.ents) else self.end) - voff
        if ctype == 1:
            data = bytes(vsize)
        else:
            raw = rd(self.f, self.base + poff, psize if ctype == 3 else vsize)
            data = lz4.block.decompress(raw, uncompressed_size=vsize) if ctype == 3 else raw
        assert ctype in (0, 1, 3), ctype
        self.cache = (i, data)
        return data

    def seek(self, pos):
        self.pos = pos

    def read(self, n):
        out = bytearray()
        while n:
            i = bisect.bisect_right(self.voffs, self.pos) - 1
            blk = self._block(i)
            k = self.pos - self.voffs[i]
            chunk = blk[k:k + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def romfs(f, base, out, listing):
    hs, = struct.unpack('<Q', rd(f, base, 8))
    (dir_hash_off, dir_hash_sz, dir_meta_off, dir_meta_sz,
     file_hash_off, file_hash_sz, file_meta_off, file_meta_sz,
     data_off) = struct.unpack('<9Q', rd(f, base + 8, 72))
    dmeta = rd(f, base + dir_meta_off, dir_meta_sz)
    fmeta = rd(f, base + file_meta_off, file_meta_sz)
    only = _only

    def walk_file(off, path):
        while off != 0xFFFFFFFF:
            parent, sib, doff, dsz, _h, nlen = struct.unpack_from('<IIQQII', fmeta, off)
            name = fmeta[off + 0x20:off + 0x20 + nlen].decode()
            p = path + '/' + name
            if listing:
                print(f'  romfs{p} {dsz}')
            elif only and not any(p.startswith(o) for o in only):
                pass
            else:
                dst = out + p
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                f.seek(base + data_off + doff)
                with open(dst, 'wb') as o:
                    left = dsz
                    while left:
                        b = f.read(min(left, 1 << 24))
                        o.write(b)
                        left -= len(b)
            off = sib

    def walk_dir(off, path):
        parent, sib, child, file, _h, nlen = struct.unpack_from('<IIIIII', dmeta, off)
        name = dmeta[off + 0x18:off + 0x18 + nlen].decode()
        p = path + ('/' + name if name else '')
        walk_file(file, p)
        c = child
        while c != 0xFFFFFFFF:
            walk_dir(c, p)
            c = struct.unpack_from('<I', dmeta, c + 4)[0]

    walk_dir(0, '')


def extract(f, out, only=(), listing=False):
    """Extract ExeFS + RomFS (optionally only RomFS paths starting with one of `only`) from a plaintext NCA."""
    global _only
    _only = list(only)
    for i, off, size, fsh in sections(f):
        hash_type = fsh[3]
        if hash_type == 2:  # HierarchicalSha256 -> PFS0
            layer_cnt, = struct.unpack_from('<I', fsh, 0x8 + 0x24)
            pfs_off, pfs_sz = struct.unpack_from('<QQ', fsh, 0x8 + 0x28 + 16 * (layer_cnt - 1))
            pfs0(f, off + pfs_off, os.path.join(out, 'exefs'), listing)
        elif hash_type == 3:  # IVFC -> RomFS at level 6
            lvl_off, lvl_sz = struct.unpack_from('<QQ', fsh, 0x8 + 0x10 + 24 * 5)
            src, base = f, off + lvl_off
            if fsh[0x188:0x18C] == b'BKTR':
                table_off, = struct.unpack_from('<Q', fsh, 0x178)
                count, = struct.unpack_from('<I', fsh, 0x190)
                src, base = Compressed(f, off + lvl_off, table_off, count), 0
            romfs(src, base, os.path.join(out, 'romfs'), listing)


_only = []


def main():
    nca, out = sys.argv[1], sys.argv[2]
    with open(nca, 'rb') as f:
        extract(f, out, [a for a in sys.argv[3:] if not a.startswith('--')], '--list' in sys.argv)


if __name__ == '__main__':
    main()
