"""Small ASTC LDR encoder (NumPy) for mod textures on Switch.

Mods ship sprites as PNG; decoded to RGBA32 they can need gigabytes, far beyond what the Switch gives the game.
ASTC is the Switch's native GPU format and (unlike DXT in Unity) accepts any texture size.

One fixed, robust configuration per block size: a single partition, RGBA direct endpoints (CEM 12) quantized to
192 levels, and a 4x4 weight grid at 8 levels (bilinearly infilled by the GPU for larger blocks). Blocks whose
texels are all identical (typically fully transparent) are stored exactly as void-extent blocks.

Blocks are emitted in Unity's raw-data order: block rows from the bottom of the image up.
"""
import numpy as np

GRID = 4  # weight grid is GRID x GRID
WEIGHT_LEVELS = 8  # weight range 0..7 (3 bits each): 48 bits of weights
COLOR_RANGE = 192  # 8 endpoint values: trit + 6 bits each, 61 bits (largest range that fits the 63 left)

# TextureFormat values (Unity): ASTC_4x4 = 48, ASTC_5x5 = 49, ASTC_6x6 = 50, ASTC_8x8 = 51
UNITY_FORMAT = {4: 48, 5: 49, 6: 50, 8: 51}


# ------------------------------------------------------------------------------------------- tables
def _trit_decode(T):
    def bits(v, hi, lo):
        return (v >> lo) & ((1 << (hi - lo + 1)) - 1)
    if bits(T, 4, 2) == 0b111:
        C = (bits(T, 7, 5) << 2) | bits(T, 1, 0)
        t4 = t3 = 2
    else:
        C = bits(T, 4, 0)
        if bits(T, 6, 5) == 0b11:
            t4, t3 = 2, bits(T, 7, 7)
        else:
            t4, t3 = bits(T, 7, 7), bits(T, 6, 5)
    if bits(C, 1, 0) == 0b11:
        t2, t1 = 2, bits(C, 4, 4)
        t0 = (bits(C, 3, 3) << 1) | (bits(C, 2, 2) & (1 - bits(C, 3, 3)))
    elif bits(C, 3, 2) == 0b11:
        t2, t1, t0 = 2, 2, bits(C, 1, 0)
    else:
        t2, t1 = bits(C, 4, 4), bits(C, 3, 2)
        t0 = (bits(C, 1, 1) << 1) | (bits(C, 0, 0) & (1 - bits(C, 1, 1)))
    return t0, t1, t2, t3, t4


TRIT_ENCODE = np.zeros((3, 3, 3, 3, 3), np.uint8)
TRIT_DECODE = np.zeros((256, 5), np.uint8)
for _T in range(255, -1, -1):  # prefer the lowest T for each tuple
    _t = _trit_decode(_T)
    TRIT_DECODE[_T] = _t
    TRIT_ENCODE[_t] = _T


def _color_unquant_192():
    """ISE value v = trit * 64 + m (6 bits) -> 8-bit endpoint value (spec: trits, 6 bits, C = 5)."""
    out = np.zeros(192, np.int32)
    for v in range(192):
        d, m = v >> 6, v & 63
        a, b, c, dd, e, f = ((m >> i) & 1 for i in range(6))
        A = 0x1FF if a else 0
        B = (f << 8) | (e << 7) | (dd << 6) | (c << 5) | (b << 4) | f
        T = d * 5 + B
        T ^= A
        out[v] = (A & 0x80) | (T >> 2)
    return out


COLOR_UNQ = _color_unquant_192()
COLOR_NEAREST = np.abs(COLOR_UNQ[None, :] - np.arange(256)[:, None]).argmin(axis=1)  # 8-bit value -> ISE value
WEIGHT_UNQ = np.array([0, 9, 18, 27, 37, 46, 55, 64], np.int32)  # 3-bit weights, bit-replicated, >32 -> +1


def infill_matrix(bw, bh, n=GRID):
    """(bw*bh, n*n) matrix of the decoder's bilinear weight infill, in 1/16ths (spec 3.9)."""
    M = np.zeros((bw * bh, n * n), np.int64)
    ds = (1024 + bw // 2) // (bw - 1)
    dt = (1024 + bh // 2) // (bh - 1)
    for t in range(bh):
        for s in range(bw):
            gs = (ds * s * (n - 1) + 32) >> 6
            gt = (dt * t * (n - 1) + 32) >> 6
            js, fs, jt, ft = gs >> 4, gs & 15, gt >> 4, gt & 15
            w11 = (fs * ft + 8) >> 4
            w10, w01 = ft - w11, fs - w11
            w00 = 16 - fs - ft + w11
            i = t * bw + s
            v0 = js + jt * n
            for dv, w in ((0, w00), (1, w01), (n, w10), (n + 1, w11)):
                if w:
                    M[i, v0 + dv] += w
    return M


# ------------------------------------------------------------------------------------------- encoder
def _bits_table(bw, bh):
    """Block mode for a 4x4 weight grid, weight range 8 (R = 7, H = 0), single plane, no D."""
    # layout: W = B + 4, H = A + 2  ->  B = 0, A = 2;  bits: R1 R2 0 0 R0 A A B B H D
    r0, r1, r2 = 1, 1, 1
    return r1 | (r2 << 1) | (r0 << 4) | (2 << 5)


BLOCK_MODE = _bits_table(4, 4)
CEM_RGBA_DIRECT = 12


def _put(lo, hi, value, pos, nbits):
    """OR `value` (uint64 array, nbits wide) into the 128-bit blocks at bit `pos`."""
    value = value.astype(np.uint64) & np.uint64((1 << nbits) - 1)
    if pos >= 64:
        hi |= value << np.uint64(pos - 64)
    elif pos + nbits <= 64:
        lo |= value << np.uint64(pos)
    else:
        lo |= value << np.uint64(pos)
        hi |= value >> np.uint64(64 - pos)


def _pack_colors(lo, hi, v):
    """ISE (trits, 6 bits) of the 8 endpoint values v[:, 0..7] starting at bit 17."""
    pos = 17
    for g0, count in ((0, 5), (5, 3)):
        vals = [v[:, g0 + i] if i < count else np.zeros(len(v), np.int64) for i in range(5)]
        t = [x >> 6 for x in vals]
        m = [x & 63 for x in vals]
        T = TRIT_ENCODE[t[0], t[1], t[2], t[3], t[4]].astype(np.int64)
        fields = [(m[0], 6), (T & 3, 2), (m[1], 6), ((T >> 2) & 3, 2), (m[2], 6), ((T >> 4) & 1, 1),
                  (m[3], 6), ((T >> 5) & 3, 2), (m[4], 6), ((T >> 7) & 1, 1)]
        need = 6 * count + (8 * count + 4) // 5
        used = 0
        for val, nb in fields:
            if used >= need:
                break
            nb = min(nb, need - used)
            _put(lo, hi, val, pos, nb)
            pos += nb
            used += nb
    return pos


def _pack_weights(lo, hi, w):
    """16 weights (3 bits each) as a bit-reversed stream from the top of the block."""
    for i in range(w.shape[1]):
        for b in range(3):
            bit = (w[:, i] >> b) & 1
            _put(lo, hi, bit, 127 - (i * 3 + b), 1)


def encode_blocks(blocks, bw, bh):
    """blocks: (N, bh*bw, 4) uint8 -> (N, 16) uint8 ASTC blocks."""
    n = len(blocks)
    P = blocks.astype(np.float64)
    lo = np.zeros(n, np.uint64)
    hi = np.zeros(n, np.uint64)

    const = np.all(blocks == blocks[:, :1, :], axis=(1, 2))

    # --- endpoints along the principal axis (RGBA)
    mean = P.mean(axis=1, keepdims=True)
    D = P - mean
    cov = np.einsum('nki,nkj->nij', D, D)
    axis = np.ones((n, 4)) / 2.0
    for _ in range(6):
        axis = np.einsum('nij,nj->ni', cov, axis)
        axis /= np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-9)
    proj = np.einsum('nki,ni->nk', D, axis)
    e0 = mean[:, 0] + axis * proj.min(axis=1, keepdims=True)
    e1 = mean[:, 0] + axis * proj.max(axis=1, keepdims=True)

    M = infill_matrix(bw, bh) / 16.0  # texel weight = M @ grid weights
    Mp = np.linalg.pinv(M)

    def fit_weights(a, b):
        d = b - a
        dd = np.maximum((d * d).sum(axis=1), 1e-9)
        t = np.einsum('nki,ni->nk', P - a[:, None, :], d) / dd[:, None]
        g = np.clip(t, 0, 1) @ Mp.T  # least-squares grid weights
        return np.clip(g, 0, 1)

    # one refinement: endpoints by least squares given the texel weights the grid can express
    for _ in range(2):
        g = fit_weights(e0, e1)
        tw = g @ M.T  # (n, texels)
        A00 = ((1 - tw) ** 2).sum(1)
        A01 = ((1 - tw) * tw).sum(1)
        A11 = (tw ** 2).sum(1)
        det = A00 * A11 - A01 * A01
        ok = np.abs(det) > 1e-6
        b0 = np.einsum('nk,nki->ni', 1 - tw, P)
        b1 = np.einsum('nk,nki->ni', tw, P)
        ne0 = (A11[:, None] * b0 - A01[:, None] * b1) / np.where(ok, det, 1)[:, None]
        ne1 = (A00[:, None] * b1 - A01[:, None] * b0) / np.where(ok, det, 1)[:, None]
        e0 = np.where(ok[:, None], np.clip(ne0, 0, 255), e0)
        e1 = np.where(ok[:, None], np.clip(ne1, 0, 255), e1)

    # --- quantize endpoints to the 192-level ISE grid
    def quant(e):
        idx = COLOR_NEAREST[np.clip(np.rint(e), 0, 255).astype(np.int64)]
        return idx, COLOR_UNQ[idx]
    q0, u0 = quant(e0)
    q1, u1 = quant(e1)
    # decoder rule for CEM 12: if sum(rgb of endpoint 1) < sum(rgb of endpoint 0) it blue-contracts; avoid by
    # swapping the endpoints (and mirroring the weights)
    swap = u1[:, :3].sum(1) < u0[:, :3].sum(1)
    q0, q1 = np.where(swap[:, None], q1, q0), np.where(swap[:, None], q0, q1)
    u0, u1 = np.where(swap[:, None], u1, u0), np.where(swap[:, None], u0, u1)

    # --- weights against the quantized endpoints
    g = fit_weights(u0.astype(np.float64), u1.astype(np.float64))
    wq = np.abs(WEIGHT_UNQ[None, None, :] / 64.0 - g[..., None]).argmin(axis=-1).astype(np.int64)

    # --- pack normal blocks
    _put(lo, hi, np.full(n, BLOCK_MODE, np.int64), 0, 11)
    _put(lo, hi, np.zeros(n, np.int64), 11, 2)  # 1 partition
    _put(lo, hi, np.full(n, CEM_RGBA_DIRECT, np.int64), 13, 4)
    v = np.stack([q0[:, 0], q1[:, 0], q0[:, 1], q1[:, 1], q0[:, 2], q1[:, 2], q0[:, 3], q1[:, 3]], axis=1)
    _pack_colors(lo, hi, v.astype(np.int64))
    _pack_weights(lo, hi, wq)

    # --- void-extent blocks for constant blocks (exact)
    if const.any():
        c = blocks[const, 0, :].astype(np.uint64) * np.uint64(257)
        vlo = np.full(const.sum(), 0x1FC | (0b11 << 10), np.uint64) | (np.uint64((1 << 52) - 1) << np.uint64(12))
        vhi = c[:, 0] | (c[:, 1] << np.uint64(16)) | (c[:, 2] << np.uint64(32)) | (c[:, 3] << np.uint64(48))
        lo[const] = vlo
        hi[const] = vhi

    out = np.empty((n, 2), '<u8')
    out[:, 0] = lo
    out[:, 1] = hi
    return out.view(np.uint8).reshape(n, 16)


def encode_image(rgba, block):
    """rgba: (H, W, 4) uint8, top row first -> ASTC data in Unity order (bottom block row first)."""
    h, w = rgba.shape[:2]
    bw = bh = block
    img = rgba[::-1]  # Unity raw data starts at the bottom row
    ph, pw = -(-h // bh) * bh, -(-w // bw) * bw
    pad = np.zeros((ph, pw, 4), np.uint8)
    pad[:h, :w] = img
    if pw > w:  # replicate edges into the unused part of partial blocks (keeps endpoints tight)
        pad[:h, w:] = img[:, w - 1:w]
    if ph > h:
        pad[h:, :] = pad[h - 1:h, :]
    blocks = pad.reshape(ph // bh, bh, pw // bw, bw, 4).transpose(0, 2, 1, 3, 4).reshape(-1, bh * bw, 4)
    step = 32768
    return b''.join(encode_blocks(blocks[i:i + step], bw, bh).tobytes() for i in range(0, len(blocks), step))


# ------------------------------------------------------------------------------------------- decoder (tests)
def decode_block(blk, bw, bh):
    """Reference decoder for the blocks this module produces (and void extents) -> (bh*bw, 4) uint8."""
    x = int.from_bytes(bytes(blk), 'little')

    def bits(pos, n):
        return (x >> pos) & ((1 << n) - 1)
    if bits(0, 9) == 0x1FC:
        c = [bits(64 + 16 * i, 16) >> 8 for i in range(4)]
        return np.tile(np.array(c, np.uint8), (bw * bh, 1))
    assert bits(0, 11) == BLOCK_MODE and bits(11, 2) == 0 and bits(13, 4) == CEM_RGBA_DIRECT
    vals, pos = [], 17
    for count in (5, 3):
        m, T = [0] * 5, 0
        order = [('m', 0, 6), ('T', 0, 2), ('m', 1, 6), ('T', 2, 2), ('m', 2, 6), ('T', 4, 1), ('m', 3, 6),
                 ('T', 5, 2), ('m', 4, 6), ('T', 7, 1)]
        need, used = 6 * count + (8 * count + 4) // 5, 0
        for kind, i, nb in order:
            if used >= need:
                break
            nb = min(nb, need - used)
            v = bits(pos, nb)
            pos += nb
            used += nb
            if kind == 'm':
                m[i] |= v
            else:
                T |= v << i
        t = TRIT_DECODE[T]
        vals += [int(t[i]) * 64 + m[i] for i in range(count)]
    u = [int(COLOR_UNQ[v]) for v in vals]
    e0 = np.array([u[0], u[2], u[4], u[6]])
    e1 = np.array([u[1], u[3], u[5], u[7]])
    assert e1[:3].sum() >= e0[:3].sum()
    g = np.array([sum(((x >> (127 - (i * 3 + b))) & 1) << b for b in range(3)) for i in range(16)])
    gu = WEIGHT_UNQ[g]
    M = infill_matrix(bw, bh)
    tw = (M @ gu + 8) >> 4
    c0 = e0 * 257
    c1 = e1 * 257
    col = (c0[None, :] * (64 - tw[:, None]) + c1[None, :] * tw[:, None] + 32) >> 6
    return (col >> 8).astype(np.uint8)


def decode_image(data, w, h, block):
    nbx, nby = -(-w // block), -(-h // block)
    raw = np.frombuffer(data, np.uint8).reshape(nby * nbx, 16)
    out = np.zeros((nby * block, nbx * block, 4), np.uint8)
    for i, blk in enumerate(raw):
        by, bx = divmod(i, nbx)
        out[by * block:(by + 1) * block, bx * block:(bx + 1) * block] = decode_block(blk, block, block).reshape(
            block, block, 4)
    return out[:h, :w][::-1]
