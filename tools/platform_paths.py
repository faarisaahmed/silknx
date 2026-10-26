"""Where things live on this machine: Ryujinx, prod.keys, the Silksong NSP, a PC copy of the game, LLVM."""
import glob
import os
import shutil
import sys

TITLE_ID = '010013c00e930000'
HOME = os.path.expanduser('~')


def ryujinx_dir():
    if sys.platform == 'darwin':
        cands = [os.path.join(HOME, 'Library', 'Application Support', 'Ryujinx')]
    elif sys.platform == 'win32':
        cands = [os.path.join(os.environ.get('APPDATA', ''), 'Ryujinx')]
    else:
        cands = [os.path.join(HOME, '.config', 'Ryujinx')]
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


def ryujinx_mod_dir(name='SilkNX'):
    return os.path.join(ryujinx_dir(), 'mods', 'contents', TITLE_ID, name)


def ryujinx_log_dir():
    if sys.platform == 'darwin':
        return os.path.join(HOME, 'Library', 'Logs', 'Ryujinx')
    return os.path.join(ryujinx_dir(), 'Logs')


def find_keys():
    cands = [os.path.join(ryujinx_dir(), 'system', 'prod.keys'), os.path.join(HOME, '.switch', 'prod.keys'),
             os.path.join(os.environ.get('APPDATA', ''), 'yuzu', 'keys', 'prod.keys')]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def nsp_title_ids(path):
    """Title ids named by the tickets inside an NSP (cheap check that it is Silksong)."""
    import struct
    try:
        with open(path, 'rb') as f:
            h = f.read(16)
            if h[:4] != b'PFS0':
                return []
            n, strsz = struct.unpack_from('<II', h, 4)
            f.seek(16 + 24 * n)
            st = f.read(strsz).decode(errors='replace')
        return [s[:16].lower() for s in st.split('\0') if s.endswith('.tik')]
    except OSError:
        return []


def find_nsp():
    for base in (os.path.join(HOME, 'Downloads'), os.path.join(HOME, 'Desktop'), os.path.join(HOME, 'Documents'),
                 os.path.join(HOME, 'Games'), os.getcwd()):
        if not os.path.isdir(base):
            continue
        root_depth = base.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith('.nsp') and TITLE_ID in nsp_title_ids(os.path.join(root, f)):
                    return os.path.join(root, f)
            depth = root.count(os.sep) - root_depth
            dirs[:] = [] if depth >= 3 else [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS
                                            and not d.endswith('.app')]
    return None


DATA_DIRS = ('Hollow Knight Silksong_Data', os.path.join('Hollow Knight Silksong.app', 'Contents', 'Resources', 'Data'))
SKIP_DIRS = {'node_modules', '.git', 'Library', 'Caches', '.Trash', 'Photos Library.photoslibrary', '__pycache__'}


def _managed_in(game_dir):
    for d in DATA_DIRS:
        m = os.path.join(game_dir, d, 'Managed')
        if os.path.isfile(os.path.join(m, 'Assembly-CSharp.dll')):
            return m
    return None


def find_pc_managed():
    """A PC (Windows/Mac/Linux) Silksong install's Managed folder (fast, bounded search)."""
    direct = [os.path.join(HOME, 'Library', 'Application Support', 'Steam', 'steamapps', 'common', 'Hollow Knight Silksong'),
              os.path.join(HOME, '.local', 'share', 'Steam', 'steamapps', 'common', 'Hollow Knight Silksong'),
              'C:\\Program Files (x86)\\Steam\\steamapps\\common\\Hollow Knight Silksong',
              'C:\\GOG Games\\Hollow Knight Silksong', '/Applications']
    for d in direct:
        m = _managed_in(d)
        if m:
            return m
    for base in (os.path.join(HOME, 'Documents'), os.path.join(HOME, 'Applications'), '/Applications',
                 os.path.join(HOME, 'Desktop'), os.path.join(HOME, 'Downloads'), os.path.join(HOME, 'Games')):
        if not os.path.isdir(base):
            continue
        root_depth = base.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(base):
            m = _managed_in(root)
            if m:
                return m
            depth = root.count(os.sep) - root_depth
            dirs[:] = [] if depth >= 9 else [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS]
    return None


def managed_from_user_path(p):
    """Accept the game folder, the _Data folder, the .app, or the Managed folder itself."""
    p = os.path.abspath(os.path.expanduser(p))
    for c in (p, os.path.join(p, 'Managed'), os.path.join(p, 'Hollow Knight Silksong_Data', 'Managed'),
              os.path.join(p, 'Contents', 'Resources', 'Data', 'Managed')):
        if os.path.isfile(os.path.join(c, 'Assembly-CSharp.dll')):
            return c
    hits = glob.glob(os.path.join(p, '**', 'Managed', 'Assembly-CSharp.dll'), recursive=True)
    return os.path.dirname(hits[0]) if hits else None


def bepinex_core_near(managed):
    """BepInEx/core next to a PC install (optional)."""
    d = os.path.dirname(managed)
    for _ in range(5):
        c = os.path.join(d, 'BepInEx', 'core')
        if os.path.isdir(c):
            return c
        d = os.path.dirname(d)
    return None


def find_llvm():
    """Directory containing clang and ld.lld (LLVM 18 recommended)."""
    env = os.environ.get('SILKNX_LLVM')
    cands = [env] if env else []
    cands += ['/opt/homebrew/opt/llvm@18/bin', '/usr/local/opt/llvm@18/bin', '/usr/lib/llvm-18/bin',
              '/opt/homebrew/opt/llvm/bin', '/usr/local/opt/llvm/bin']
    for c in cands:
        if c and os.path.isfile(os.path.join(c, 'clang')) and (os.path.isfile(os.path.join(c, 'ld.lld')) or
                                                               shutil.which('ld.lld')):
            return c
    clang, lld = shutil.which('clang'), shutil.which('ld.lld')
    if clang and lld:
        return os.path.dirname(clang)
    return None


def lld_path(llvm_dir):
    p = os.path.join(llvm_dir, 'ld.lld')
    return p if os.path.isfile(p) else shutil.which('ld.lld')
