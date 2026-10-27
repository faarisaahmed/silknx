#!/usr/bin/env python3
"""SilkNX - run PC (BepInEx) Silksong mods on the Switch version.

  python3 silknx.py              convert everything in plugins/ and install it into Ryujinx
                                 (no Ryujinx on this computer? it makes modded-Switch SD files instead)
  python3 silknx.py setup        one-time setup (finds your NSP, keys and PC copy automatically)
  python3 silknx.py uninstall    remove the mod from Ryujinx

Options for the default command:
  --plugins DIR      folder with mods (default: plugins/)
  --config DIR       BepInEx config folder with .cfg files (default: config/)
  --atmosphere [DIR] also make files for a modded Switch (Atmosphere); default DIR: switch-sd/
  --no-install       build only
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'tools'))


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('command', nargs='?', default='build', choices=['build', 'setup', 'uninstall', 'help'])
    ap.add_argument('--plugins', default=os.path.join(ROOT, 'plugins'))
    ap.add_argument('--config', default=os.path.join(ROOT, 'config'))
    ap.add_argument('--atmosphere', nargs='?', const=os.path.join(ROOT, 'switch-sd'))
    ap.add_argument('--no-install', action='store_true')
    ap.add_argument('--nsp')
    ap.add_argument('--keys')
    ap.add_argument('--pc')
    ap.add_argument('--only', nargs='*')
    ap.add_argument('-h', '--help', action='store_true')
    a = ap.parse_args()
    if a.help or a.command == 'help':
        print(__doc__)
        return

    import platform_paths
    if a.command == 'setup':
        import setup_game
        setup_game.run(a.nsp, a.keys, a.pc)
        return
    if a.command == 'uninstall':
        d = platform_paths.ryujinx_mod_dir()
        shutil.rmtree(d, ignore_errors=True)
        print(f'Removed {d}')
        return

    if not os.path.exists(os.path.join(ROOT, 'game', 'dump.json')):
        print('First run: doing setup.\n')
        import setup_game
        setup_game.run(a.nsp, a.keys, a.pc)
    from silkport.driver import find_dlls, build
    if not os.path.isdir(a.plugins) or not find_dlls(a.plugins):
        print(f'No mods found. Put your BepInEx mods (.dll files or whole mod folders) into:\n  {a.plugins}')
        return
    has_ryujinx = os.path.isdir(platform_paths.ryujinx_dir())
    if not has_ryujinx and not a.no_install:
        a.no_install = True
        if not a.atmosphere:
            print('Ryujinx not found on this computer: making files for a modded Switch instead.\n')
            a.atmosphere = os.path.join(ROOT, 'switch-sd')
    build(a.plugins, a.config if os.path.isdir(a.config) else None, install=not a.no_install,
          atmosphere=a.atmosphere, only=a.only)
    where = []
    if not a.no_install:
        where.append('Ryujinx')
    if a.atmosphere:
        where.append('your Switch (after copying the atmosphere folder to the SD card)')
    if where:
        print(f'\nDone! Start Silksong in {" or ".join(where)}. Mod messages appear in the top-left corner.')


if __name__ == '__main__':
    main()
