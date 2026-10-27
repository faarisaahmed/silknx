"""Switch texture budget: mod PNGs -> ASTC sidecars + header-only stub PNGs.

A sprite pack decoded to RGBA32 can need gigabytes; the Switch gives the game a few hundred MB of headroom.
Each PNG in the mod folders is replaced (in the romfs only) by a stub that keeps the real PNG header - so code
reading the image size still works - plus a private chunk naming its ASTC version. The runtime's
ImageConversion.LoadImage shim recognises the stub and uploads the ASTC blocks directly (4x+ smaller, no decode).
"""
import hashlib
import os
import struct
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

ROM_BASE = 'rom:/Data/StreamingAssets/SilkNX'
MIN_SIDE = 8  # tiny images are cheap anyway
STUB_CHUNK = b'snXa'  # ancillary, private: ignored by PNG readers


def _chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)


def stub_png(w, h, rom_path):
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) + _chunk(STUB_CHUNK, rom_path.encode()) +
            _chunk(b'IEND', b''))


def _encode(job):
    src, block, cache = job
    import numpy as np
    from PIL import Image
    import astc
    data = open(src, 'rb').read()
    key = hashlib.sha1(data + bytes([block, 2])).hexdigest()
    cpath = os.path.join(cache, key + '.astc')
    with Image.open(src) as im:
        w, h = im.size
        if min(w, h) < MIN_SIDE:
            return src, None, w, h
        if os.path.exists(cpath):
            return src, cpath, w, h
        rgba = np.array(im.convert('RGBA'))
    payload = astc.encode_image(rgba, block)
    head = b'SNXA' + struct.pack('<HHBBBB', w, h, block, block, astc.UNITY_FORMAT[block], 0)
    tmp = cpath + f'.{os.getpid()}'
    open(tmp, 'wb').write(head + payload)
    os.replace(tmp, cpath)
    return src, cpath, w, h


def compress_pngs(base, build, block=4, log=print):
    """Convert every PNG under base/plugins (the romfs copy). Returns (converted, raw MB, ASTC MB)."""
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        log('  note: install numpy + Pillow (pip3 install -r requirements.txt) to shrink mod textures for the '
            'Switch memory budget; texture packs may run out of memory without it')
        return 0, 0, 0
    plugins = os.path.join(base, 'plugins')
    pngs = [os.path.join(r, f) for r, _, fs in os.walk(plugins) for f in fs if f.lower().endswith('.png')]
    if not pngs:
        return 0, 0, 0
    cache = os.path.join(build, 'astc_cache')
    os.makedirs(cache, exist_ok=True)
    raw = packed = done = 0
    jobs = [(p, block, cache) for p in pngs]
    workers = max(1, (os.cpu_count() or 2) - 1)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for src, cpath, w, h in ex.map(_encode, jobs, chunksize=8):
            if cpath is None:
                continue
            rel = os.path.relpath(src, plugins).replace(os.sep, '/')
            side_rel = f'astc/{rel}.astc'
            side = os.path.join(base, *side_rel.split('/'))
            os.makedirs(os.path.dirname(side), exist_ok=True)
            data = open(cpath, 'rb').read()
            open(side, 'wb').write(data)
            open(src, 'wb').write(stub_png(w, h, f'{ROM_BASE}/{side_rel}'))
            raw += w * h * 4
            packed += len(data)
            done += 1
    return done, raw / 2 ** 20, packed / 2 ** 20
