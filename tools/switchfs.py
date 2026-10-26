"""Read a game NCA straight out of an NSP, decrypting on the fly with the user's own prod.keys.

PlainNca behaves like a seekable file containing the decrypted NCA, so nca_extract can read it directly.
"""
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def load_keys(path):
    keys = {}
    for line in open(path, encoding='utf-8', errors='replace'):
        if '=' in line:
            k, v = line.split('=', 1)
            keys[k.strip().lower()] = v.strip()
    return keys


def ecb_dec(key, data):
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def xts_sector(key, data, sector):
    d = Cipher(algorithms.AES(key), modes.XTS(sector.to_bytes(16, 'big'))).decryptor()
    return d.update(data) + d.finalize()


def pfs0_entries(f, base=0):
    f.seek(base)
    h = f.read(16)
    if h[:4] != b'PFS0':
        raise ValueError('not an NSP (PFS0) file')
    n, strsz = struct.unpack_from('<II', h, 4)
    ents = [struct.unpack('<QQII', f.read(24)) for _ in range(n)]
    st = f.read(strsz)
    data = base + 16 + 24 * n + strsz
    return [(st[no:st.index(b'\0', no)].decode(), data + off, size) for off, size, no, _ in ents]


class PlainNca:
    """Seekable, read-only view of one NCA inside an NSP, decrypted."""

    def __init__(self, f, base, size, keys, titlekeys):
        self.f, self.base, self.size, self.pos = f, base, size, 0
        hk = bytes.fromhex(keys['header_key'])
        f.seek(base)
        raw = f.read(0xC00)
        self.header = b''.join(xts_sector(hk, raw[i * 0x200:(i + 1) * 0x200], i) for i in range(6))
        if self.header[0x200:0x204] != b'NCA3':
            raise ValueError('cannot decrypt NCA header (wrong or outdated prod.keys?)')
        hdr = self.header
        gen = max(hdr[0x206], hdr[0x220])
        gen = gen - 1 if gen > 0 else 0
        rights = hdr[0x230:0x240]
        if any(rights):
            if rights not in titlekeys:
                raise ValueError('no ticket for this NCA in the NSP')
            kek = keys.get(f'titlekek_{gen:02x}')
            if not kek:
                raise ValueError(f'prod.keys is missing titlekek_{gen:02x} (update your keys)')
            self.key = ecb_dec(bytes.fromhex(kek), titlekeys[rights])
        else:
            kak_name = ['application', 'ocean', 'system'][hdr[0x207]]
            kak = keys.get(f'key_area_key_{kak_name}_{gen:02x}')
            if not kak:
                raise ValueError(f'prod.keys is missing key_area_key_{kak_name}_{gen:02x} (update your keys)')
            self.key = ecb_dec(bytes.fromhex(kak), hdr[0x300:0x340])[0x20:0x30]
        self.sections = []
        for i in range(4):
            s, e = struct.unpack_from('<II', hdr, 0x240 + i * 0x10)
            if e:
                fs = hdr[0x400 + i * 0x200:0x600 + i * 0x200]
                self.sections.append((s * 0x200, e * 0x200, fs[4], fs[0x140:0x148][::-1]))

    def seek(self, pos):
        self.pos = pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n < 0:
            n = self.size - self.pos
        out = bytearray()
        while n > 0:
            p = self.pos
            if p < 0xC00:
                chunk = self.header[p:p + n]
            else:
                sec = next((s for s in self.sections if s[0] <= p < s[1]), None)
                if sec:
                    end = min(p + n, sec[1])
                else:
                    nxt = min([s[0] for s in self.sections if s[0] > p] + [self.size])
                    end = min(p + n, nxt)
                a = p & ~0xF
                self.f.seek(self.base + a)
                raw = self.f.read(((end - a) + 15) & ~0xF)
                if sec and sec[2] == 3:  # AES-CTR
                    iv = sec[3] + (a >> 4).to_bytes(8, 'big')
                    d = Cipher(algorithms.AES(self.key), modes.CTR(iv)).decryptor()
                    raw = d.update(raw) + d.finalize()
                elif sec and sec[2] not in (0, 1):
                    raise ValueError(f'unsupported NCA encryption type {sec[2]}')
                chunk = raw[p - a:end - a]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def open_program_nca(nsp_path, keys):
    """Return (PlainNca of the largest NCA = the program, file handle)."""
    f = open(nsp_path, 'rb')
    ents = pfs0_entries(f)
    titlekeys = {}
    for name, off, size in ents:
        if name.endswith('.tik'):
            f.seek(off)
            t = f.read(size)
            titlekeys[t[0x2A0:0x2B0]] = t[0x180:0x190]
    ncas = [e for e in ents if e[0].endswith('.nca') and not e[0].endswith('.cnmt.nca')]
    name, off, size = max(ncas, key=lambda e: e[2])
    return PlainNca(f, off, size, keys, titlekeys), [e[0] for e in ents]
