"""silknx setup: prepare everything the converter needs from YOUR copies of the game.

1. Decrypts the Switch game (NSP + prod.keys) in memory and extracts the code + IL2CPP metadata into game/.
2. Builds the IL2CPP dump (every class, field offset and function address) into game/dump.json.
3. Optional (--pc): copies a PC copy's managed DLLs, used only for the rare functions the Switch build stripped.
"""
import os
import shutil
import sys

import platform_paths as pp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.join(ROOT, 'game')
PCREF = os.path.join(ROOT, 'pcref')


def say(msg):
    print(f'  {msg}', flush=True)


def fail(msg):
    print(f'\n[!] {msg}\n')
    raise SystemExit(1)


def check_python_deps():
    missing = []
    for mod in ('lz4', 'cryptography'):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        fail(f'Missing Python packages: {", ".join(missing)}\n    Run:  python3 -m pip install -r requirements.txt')


def run(nsp=None, keys=None, pc=None):
    print('SilkNX setup\n', flush=True)
    check_python_deps()
    from switchfs import load_keys, open_program_nca
    import nca_extract

    # --- toolchain
    llvm = pp.find_llvm()
    if not llvm:
        hint = 'brew install llvm@18' if sys.platform == 'darwin' else 'install clang-18 and lld-18'
        fail(f'LLVM (clang + ld.lld) not found.\n    Run:  {hint}')
    say(f'compiler: {llvm}')

    # --- Switch game
    keys = keys or pp.find_keys()
    if not keys:
        fail('prod.keys not found. Pass it with  --keys /path/to/prod.keys  (Ryujinx keeps it in system/prod.keys)')
    say(f'keys:     {keys}')
    nsp = nsp or pp.find_nsp()
    if not nsp:
        fail('Could not find your Silksong .nsp. Pass it with  --nsp /path/to/Silksong.nsp')
    say(f'game:     {nsp}')
    if pp.TITLE_ID not in pp.nsp_title_ids(nsp):
        print('  (warning: this NSP does not look like Silksong; continuing anyway)')

    shutil.rmtree(GAME, ignore_errors=True)
    os.makedirs(GAME)
    say('decrypting and extracting game code (about a minute)...')
    try:
        nca, names = open_program_nca(nsp, load_keys(keys))
    except ValueError as e:
        fail(str(e))
    nca_extract.extract(nca, GAME, only=['/Data/Managed/Metadata', '/Data/StreamingAssets/BuildMetadata.json',
                                         '/Data/boot.config'])
    for need in ('exefs/main', 'exefs/subsdk0', 'romfs/Data/Managed/Metadata/global-metadata.dat'):
        if not os.path.exists(os.path.join(GAME, need)):
            fail(f'extraction failed: {need} missing (is this the base game NSP, not an update?)')
    ver = open(os.path.join(GAME, 'romfs/Data/StreamingAssets/BuildMetadata.json')).read() \
        if os.path.exists(os.path.join(GAME, 'romfs/Data/StreamingAssets/BuildMetadata.json')) else ''
    say(f'build:    {ver.strip()[:80]}')

    say('building the IL2CPP dump...')
    import il2cpp_dump
    sys.argv = ['il2cpp_dump', os.path.join(GAME, 'exefs', 'main'),
                os.path.join(GAME, 'romfs', 'Data', 'Managed', 'Metadata', 'global-metadata.dat'),
                os.path.join(GAME, 'dump.json'), '--cs', os.path.join(GAME, 'dump.cs')]
    il2cpp_dump.main()

    # --- optional: PC copy (only used for the rare functions the Switch build stripped out)
    managed = pp.managed_from_user_path(pc) if pc else None
    if pc and not managed:
        fail(f'No Silksong PC files found in {pc}')
    if managed:
        shutil.rmtree(PCREF, ignore_errors=True)
        os.makedirs(os.path.join(PCREF, 'Managed'))
        for f in os.listdir(managed):
            if f.endswith('.dll'):
                shutil.copy(os.path.join(managed, f), os.path.join(PCREF, 'Managed', f))
        say(f'PC copy:  {managed} (optional extras enabled)')
    else:
        say('PC copy:  not used (not needed; only a few rare mods ask for it)')

    print('\nSetup complete. Put mods in the plugins/ folder and run:  python3 silknx.py\n')
