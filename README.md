# SilkNX: PC Silksong mods on Nintendo Switch

SilkNX converts **BepInEx mods for Hollow Knight: Silksong (PC)** so they run on the **Switch version** of the
game, on a **modded Switch (Atmosphère)** or in the **Ryujinx** emulator.

Drop your mod `.dll` files into a folder, run one command, start the game.

> **No game files are included in this repository.** SilkNX works from *your own* Switch game: an NSP you
> dumped from your own console and your console's `prod.keys`. **You do not need the PC version.**

---

## What you need

**Something to play on (either one):**

* a **modded Switch** running Atmosphère, **or**
* the **Ryujinx** emulator with Silksong working in it

**To build the mods, a computer with:**

| | |
|---|---|
| **macOS** (Apple Silicon or Intel) or **Linux** | Windows users: use WSL |
| **Your Silksong `.nsp`** | dumped from your own Switch |
| **Your `prod.keys`** | dumped from your own Switch (e.g. with Lockpick_RCM). Ryujinx users already have them; otherwise put them at `~/.switch/prod.keys` |
| **Python 3.9+** | comes with macOS developer tools / most Linux |
| **LLVM 18** | the compiler (install below) |

You do **not** need the PC version of Silksong, and you do **not** need Ryujinx if you play on a modded Switch.

## Install (once)

Open **Terminal** and paste these lines one at a time:

```sh
git clone https://github.com/faarisaahmed/silknx.git
cd silknx
python3 -m pip install -r requirements.txt
brew install llvm@18          # Linux: sudo apt install clang-18 lld-18
python3 silknx.py setup
```

`setup` finds everything automatically (your NSP in Downloads/Desktop/Documents, your keys in Ryujinx or
`~/.switch/prod.keys`)
and takes a few seconds. If it can't find something it tells you exactly what to pass, for example:

```sh
python3 silknx.py setup --nsp ~/Games/Silksong.nsp --keys ~/switch/prod.keys
```

Everything SilkNX needs to understand mods comes from the Switch game itself.

**Optional, rarely needed:** a few mods call functions that Nintendo's build of the game left out. The report
names them and says "needs a PC copy". Only if you want those, and you own Silksong on PC, run once:
`python3 silknx.py setup --pc "/path/to/Hollow Knight Silksong"`.

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
   **Playing on a modded Switch instead?** Use `python3 silknx.py --atmosphere` (see *Modded Switch* below).
   If Ryujinx isn't installed on your computer, SilkNX does this automatically.

To add or remove mods, change the `plugins` folder and run `python3 silknx.py` again.
To turn everything off: `python3 silknx.py uninstall` (or untick **SilkNX** in Ryujinx's *Manage Mods*).

After each conversion SilkNX prints a report per mod: `OK`, or the exact things that could not be converted.

## Controls

The Switch has no keyboard, so keyboard shortcuts used by mods become controller combos:
**hold L3 (click the left stick) + press a button**. To see which combo each key got, turn on the cursor (below):
it lists them, e.g. `Mod keys: 1 = L3+R, F11 = L3+ZR`. Shift/Ctrl/Alt count as "holding L3".

Mod menus made for a mouse (skin pickers, settings windows) get a **cursor**: click **both sticks (L3 + R3)** to
show it. Left stick moves it, **A** clicks (hold A to drag), right stick scrolls, **B** hides it. While it is
shown, the game ignores the controller so clicks don't make Hornet jump. Typing into text boxes isn't supported yet.

Messages on screen are kept short: mods' errors (each shown once) and messages meant for the player. Everything
mods log is in the Ryujinx log (see *Troubleshooting*).

## What works

SilkNX translates each mod's compiled C# into native Switch code and emulates the parts of BepInEx it needs.

| Works | Not (yet) |
|---|---|
| BepInEx plugins, `Logger`, config files (`Bind`, `.Value`) | Harmony **transpilers** and finalizers |
| Harmony **prefixes/postfixes** (`__instance`, `__result`, `__state`, `___fields`, attribute & `TargetMethod()` targets) | Windows/dialogs that need a mouse (e.g. config GUIs; settings still load from your `.cfg`) |
| Your own `MonoBehaviour`s: `Awake/Start/Update/LateUpdate/FixedUpdate/OnGUI/OnEnable/OnDisable/OnDestroy` | Networking mods (e.g. multiplayer) |
| Coroutines, `GetComponent/AddComponent<T>`, textures from the mod folder, Unity UI, IMGUI | Mods that depend on APIs added after the Switch version's build |
| Generic collections, LINQ, delegates, events, exceptions (from mods *and* from the game; they are logged instead of crashing) | `try/catch` inside a mod can't catch exceptions thrown *by the game* (they're caught and logged one level up) |

Tested so far: **ShowDamage HealthBar**, **Skip Intro**, **Toggle HUD**, **MapMod**, and **Stitchwork** skins
(e.g. **Nightmare Queen**: Hornet's skin in game).

> **Skins are a work in progress.** Hornet herself is reskinned, but not all of a skin pack's art shows up on
> Switch yet (some menu art, effects and particles). Most other mods work.

**Skins and texture packs:** the Switch gives the game far less memory than a PC, so SilkNX converts the PNGs in
your mod folders to the Switch's own GPU texture format (ASTC) when it builds — a skin that would need ~750 MB
needs ~190 MB. The first build of a big pack takes a couple of minutes; later builds reuse the result. Mods that
build large textures at runtime get them at about the Switch's normal resolution. Replacement *sounds* don't work
yet (the Switch version lacks Unity's audio-file loader).

## Modded Switch (Atmosphère)

1. Do the install above on your computer (setup needs the NSP + `prod.keys` dumped from your Switch).
2. Put mods in `plugins` and run:

   ```sh
   python3 silknx.py --atmosphere
   ```

3. Copy the `atmosphere` folder from `silknx/switch-sd/` to the **root of your SD card** (merge with the
   existing folder), or extract `switch-sd/SilkNX-atmosphere.zip` there.
4. Start Silksong. To remove the mods, delete `atmosphere/contents/010013C00E930000` and
   `atmosphere/exefs_patches/SilkNX` from the SD card.

> Status: built to Atmosphère's documented layout, but not yet confirmed on real hardware. If you try it,
> please open an issue saying whether it worked.

## Game version

The mods are built for the exact version of the game in the NSP you gave `setup`. Your Switch/Ryujinx must
run **that same version**. If you install a game update, dump the update too and run setup again
(update NSPs are not supported yet; this is being worked on). If mods silently do nothing, a version mismatch
is the most likely cause.

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
* `tools/silkport/il2cpp_model.py`: builds the converter's full type model from the Switch metadata (no PC files).
* `tools/silkport/`: the converter: reads mod DLLs (`model.py`), translates IL to C++ (`translate.py`,
  `translator.py`), turns Harmony patches into native hooks (`harmony.py`).
* `sdk/rt/`: the runtime the translated mods run on (object model, BepInEx/Harmony/Unity emulation, input,
  on-screen console). Shims are named `shim_<Type>__<Method>[__<params>]`; a missing one is listed in the report.
* `tools/silkbuild.py`: compiles everything into an extra game module (`subsdk9`) plus an IPS patch that hooks the
  game's functions. `mods/` holds small hand-written native examples.

## Legal

SilkNX contains no Nintendo, Team Cherry, or third-party mod code. The only game asset included is Silksong's
37×37 mouse-pointer image (`sdk/rt/cursor_img.h`), used for the controller cursor. You need legitimate copies of the game.
Mods remain the property of their authors; share converted builds only with their permission.

License: MIT
