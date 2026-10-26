# SilkNX: PC Silksong mods on Nintendo Switch

SilkNX converts **BepInEx mods for Hollow Knight: Silksong (PC)** so they run on the **Switch version** of the
game, in the Ryujinx emulator (and experimentally on a modded Switch).

Drop your mod `.dll` files into a folder, run one command, start the game.

> **No game files are included in this repository.** SilkNX works from *your own* copies: a Switch NSP you
> dumped from your own console, your console's `prod.keys`, and your PC copy of Silksong.

---

## What you need

| | |
|---|---|
| **A Mac** (Apple Silicon or Intel) or **Linux** | Windows users: use WSL |
| **Ryujinx** | with Silksong already working in it |
| **Your Silksong `.nsp`** | dumped from your own Switch (base game) |
| **`prod.keys`** | the same keys Ryujinx uses (already in Ryujinx if the game runs) |
| **Silksong for PC** (Steam/GOG, any OS) | needed so SilkNX can understand PC mods; it is only read, never changed |
| **Python 3.9+** | comes with macOS developer tools / most Linux |
| **LLVM 18** | the compiler (install below) |

## Install (once)

Open **Terminal** and paste these lines one at a time:

```sh
git clone https://github.com/faarisaahmed/silknx.git
cd silknx
python3 -m pip install -r requirements.txt
brew install llvm@18          # Linux: sudo apt install clang-18 lld-18
python3 silknx.py setup
```

`setup` finds everything automatically (your NSP in Downloads/Desktop/Documents, the keys inside Ryujinx,
your PC copy of the game) and takes a few seconds. If it can't find something it tells you exactly what to
pass, for example:

```sh
python3 silknx.py setup --nsp ~/Games/Silksong.nsp --pc "/path/to/Hollow Knight Silksong"
```

## Use

1. Put your mods into the **`plugins`** folder inside `silknx`.
   You can copy a whole BepInEx `plugins` folder, mod folders (with their textures etc.), or single `.dll` files.
2. *(Optional)* Put your BepInEx **`.cfg`** files into the **`config`** folder to keep your settings.
3. Run:

   ```sh
   python3 silknx.py
   ```

   On a Mac you can instead **double-click `SilkNX.command`**.

4. Start Silksong in Ryujinx. Messages from your mods appear in the top-left corner of the screen.

To add or remove mods, change the `plugins` folder and run `python3 silknx.py` again.
To turn everything off: `python3 silknx.py uninstall` (or untick **SilkNX** in Ryujinx's *Manage Mods*).

After each conversion SilkNX prints a report per mod: `OK`, or the exact things that could not be converted.

## Controls

The Switch has no keyboard, so keyboard shortcuts used by mods become controller combos:
**hold L3 (click the left stick) + press a button**. The first time a mod uses a key, SilkNX shows the combo
on screen, e.g. `Keyboard F11 = hold L3 + press R`. Shift/Ctrl/Alt count as "holding L3".

## What works

SilkNX translates each mod's compiled C# into native Switch code and emulates the parts of BepInEx it needs.

| Works | Not (yet) |
|---|---|
| BepInEx plugins, `Logger`, config files (`Bind`, `.Value`) | Harmony **transpilers** and finalizers |
| Harmony **prefixes/postfixes** (`__instance`, `__result`, `__state`, `___fields`, attribute & `TargetMethod()` targets) | Windows/dialogs that need a mouse (e.g. config GUIs) |
| Your own `MonoBehaviour`s: `Awake/Start/Update/LateUpdate/FixedUpdate/OnGUI/OnEnable/OnDisable/OnDestroy` | Networking mods (e.g. multiplayer) |
| Coroutines, `GetComponent/AddComponent<T>`, textures from the mod folder, Unity UI, IMGUI | Mods that depend on APIs added after the Switch version's build |
| Generic collections, LINQ, delegates, events, exceptions thrown by mods | Some exceptions thrown *by the game itself* inside a mod |

Tested so far: **ShowDamage HealthBar**, **Skip Intro**, **Toggle HUD**, **MapMod**.

## Real Switch (experimental, untested)

```sh
python3 silknx.py --atmosphere ~/Desktop/sd
```

Copy the generated `atmosphere` folder to the root of your SD card (Atmosphère CFW).

## Troubleshooting

* **"Missing Python packages"**: run `python3 -m pip install -r requirements.txt`
  (if you have several Pythons, use the same `python3` for both commands).
* **"LLVM not found"**: `brew install llvm@18`. Already installed elsewhere? `export SILKNX_LLVM=/path/to/llvm/bin`.
* **"cannot decrypt NCA header" / "missing titlekek"**: your `prod.keys` are older than the game; dump fresh keys.
* **Game crashes or a mod misbehaves**: open the newest log in `~/Library/Logs/Ryujinx/` (Mac) and search for
  `silknx`, then open an issue with those lines and the mod name.
* Only one SilkNX build is active at a time; it always contains everything in your `plugins` folder.

## How it works (for developers)

* `tools/switchfs.py`, `tools/nca_extract.py`: read the NSP with your keys, extract the game code and IL2CPP metadata.
* `tools/il2cpp_dump.py`: maps every class, field offset and function address of the Switch build.
* `tools/silkport/`: the converter: reads mod DLLs (`model.py`), translates IL to C++ (`translate.py`,
  `translator.py`), turns Harmony patches into native hooks (`harmony.py`).
* `sdk/rt/`: the runtime the translated mods run on (object model, BepInEx/Harmony/Unity emulation, input,
  on-screen console). Shims are named `shim_<Type>__<Method>[__<params>]`; a missing one is listed in the report.
* `tools/silkbuild.py`: compiles everything into an extra game module (`subsdk9`) plus an IPS patch that hooks the
  game's functions. `mods/` holds small hand-written native examples.

## Legal

SilkNX contains no Nintendo, Team Cherry, or third-party mod code. You need legitimate copies of the game.
Mods remain the property of their authors; share converted builds only with their permission.

License: MIT
