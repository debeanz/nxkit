# nxkit: Switch game → iPhone IPA from a Windows PC

[nxnative](https://github.com/seedds/nxnative) turns your own Switch game dump into an iPhone/iPad app,
but it only builds on a Mac. nxkit does everything it can on your Windows PC and borrows a GitHub Mac only
for the steps that need Xcode and Apple's Metal compiler:

| Where | What |
|---|---|
| **PC** | Extract the game, rewrite its code for iPhone, pull out its shaders, and pack a small **build kit** |
| **GitHub (Mac)** | Compile the shaders and the app → a "slim" IPA **without the game files** |
| **PC** | Add the game files to the slim IPA → the finished IPA |

The big game files never leave your PC. The build kit and the slim IPA are **encrypted** with a password
only you and your repository's secrets hold, so the repository can be **public** (free Mac build minutes).
The kit's release is deleted as soon as the Mac downloads it, and the slim IPA is deleted as soon as your PC
downloads it.

## What you need

- Windows PC with [Python 3.14](https://www.python.org/downloads/), [Git](https://git-scm.com/download/win)
  and the [GitHub CLI](https://cli.github.com/) (`gh auth login` once).
- Your own game dump (`.nsp`, `.xci`, `.nsz` or `.xcz`) in a folder of its own: the base game plus the
  update nxnative's game list names (without it the game may not run well). DLC is optional.
- `prod.keys` from your own console.
- Free space on the game's drive: about twice the game's size.
- An iPhone 15 Pro or newer, or an M1+ iPad, on iOS/iPadOS 26 or later.

## One-time setup

1. Create a **public** repository on GitHub from this folder:

   ```bash
   gh repo create nxkit --public --source . --push
   ```

2. Run setup. Use a `--home` folder on a drive with free space: it holds nxnative, the .NET SDK and
   temporary files (about 2 GB):

   ```bash
   py nxkit.py setup --repo <your-github-name>/nxkit --home F:\nxkit
   ```

   It installs the Python packages and the .NET 10 SDK, downloads nxnative, creates the kit password,
   saves it in `nxkit.local.json` (never committed) and stores it as the repository's `KIT_PASSWORD` secret.

## Building a game

```bash
py nxkit.py game "F:\Switch\Hades" -k "F:\keys\prod.keys"
```

It runs the PC part, uploads the encrypted kit, starts the cloud build, waits for it, downloads the
result and writes the finished IPA into the game's folder (for example `Hades.ipa`).
A first build takes roughly 15–60 minutes, depending on the game.

Options: `-p v589824` (version to pack), `--app-name`, `-l ja` (language), `--no-fps` (hide the frame
counter), `-o out.ipa`, `--keep-extracted` (keep the extracted files to rebuild faster), `--nxnative-ref` (the nxnative commit to build with: by default the one
nxkit's patches are made for; `main` takes the newest, which the patches may not apply to yet).

Deleted the NSPs? `py nxkit.py rebuild "F:\Switch\Hades"` builds the app again for nxkit's or nxnative's app changes from what the last
build kept in `<title ID>_build` (the rewritten code, the shader split) and the game files inside the last IPA, which it replaces. The
code is not rewritten again, so changes to nxnative's code rewriter or a game's code patches still need the NSPs (`game`).

## nxkit's changes to nxnative

nxkit applies its patches to nxnative before building (they are made for the nxnative commit in `NXNATIVE_REF`,
nxkit.py; currently 2db12f7, which has nxnative's MetalFX upscaling and single-screen launcher). **General**
patches (`patches/*.patch`) go into every game's build; a **game's own** patches (`patches/<title ID>/*.patch`) go
only into that game's build, so one game's fixes never run in another.

General, every game:

- **Windows paths** (PC): nxnative's PC-side scripts run on Windows.
- **Display options** in the launcher: Resolution (Native, 1920x1080, 1600x900, 1280x720: the size the
  picture is scaled to on screen) and Screen Scaling (Fit 16:9, Stretch full screen, Stretch 1.943:1,
  Zoom full screen). Touches on the game's touch screen follow the picture.
- **zstd without the command** (PC shader split): Windows has no `zstd` command, so plain zstd files
  (Rhythm Heaven Groove's) are decompressed with Python's own zstd when it is missing.
- **Memory recorder from env.txt** (iOS): `DM_MEMSTAT=memstat.tsv` in `Documents/env.txt` now starts
  nxnative's per-second memory breakdown (it used to start before env.txt was read); the file goes
  beside `log.txt`.
- **Movie events outside the decoder lock**: the host signalled a video decoder's events while holding its
  lock; a same-core wake handed the core to the thread waiting for that lock, and the game stopped (sound went
  on; found on Kirby's guide movies). The events are signalled after the lock is released.

Kirby and the Forgotten Land (`patches/01004D300C5AE000`, testing):

- The shader split reads Kirby's `.cmp` files (a size, then zstd), and a 60 fps patch for 1.1.0's frame pacer
  (one 1/60 s period per frame instead of two; the game steps by the measured time, so its speed stays right).

The Legend of Zelda: Link's Awakening (`patches/01006BB00C6F0000`):

- **No blur**, always on. For the game's depth-of-field / tilt-shift composite only, the host hands the GPU a
  copy of its uniform buffer with the blend strength zeroed, so it outputs the sharp frame; the game's own data
  is not touched. `DM_NO_BLUR=0` in `env.txt` turns it off.
- **Handheld picture**: the game draws handheld frames into 1280x720 of a 1920x1080 texture and crops its display
  layer (`vi::SetLayerCrop`) to that; the present follows that crop (it showed small in a corner).
- **Keep 60 fps**: the game switches itself to 30 fps after a few late frames, and the phone then lowers its
  clocks so it never gets back; its request is not followed. `DM_KEEP_60=0` follows it again.

Luigi's Mansion 3 (`patches/0100DCA0064A6000`, testing, against running out of memory):

- **Idle texture eviction**: GPU copies of compressed textures on GPU-only pools that nothing used for about
  7 seconds are dropped and rebuilt from game memory when needed again. `DM_NVN_EVICT_IDLE=0` in `env.txt`
  turns it off; the log line `idle texture eviction:` shows what it freed.
- **Idle render targets dropped**: render targets nothing drew into or sampled for 64 submissions (about a
  quarter second) are dropped too; dynamic resolution left a full set of them behind at every size it picked.
  `DM_NVN_EVICT_RT_IDLE=0` turns it off.
- **Half-res textures** (launcher switch under Performance, on by default): ASTC textures of 256x256 or more with
  mipmaps are made without their top level, so they take a quarter of the memory and look a little blurrier.
  Textures the game draws into are never changed. The log line `half-res textures:` shows how much it saved.

Games nxnative has no profile for are built from a **provisional profile**, as nxnative's own script
does: it surveys the game and guesses the profile (kept in `<NSP folder>/<title ID>_build/profile/`, reused
and editable). The heap check that boots the game on a Mac is skipped, so such a game may not boot at first.

## Installing on the iPhone (from Windows)

Use **Sideloadly**, **AltStore** (Windows) or **Impactor**. Heavy games need the *Increased Memory Limit*
entitlement (about 6 GB of memory instead of 3.4 GB). AltStore 2.2+ and Impactor can add it.

## Limits

- Only the games on nxnative's list are supported.
- Games whose build boots the game on a Mac can't be built here: those that compile or generate shaders
  at run time (Fire Emblem: Three Houses, Catherine: Full Body, Super Mario Galaxy 1 and 2) and those whose
  shaders the game's own decoder unpacks (Unreal games like Octopath Traveler 0, Pokémon Legends: Z-A,
  Bayonetta 3). nxkit says so before uploading anything.
- OpenGL games (Digimon Story Cyber Sleuth, LÖVE games such as Balatro) are not supported yet.

## When something fails

- PC-side errors print the step and the log file under `<game folder>\<title ID>_build\logs`.
- A failed cloud build downloads its encrypted logs and decrypts them to `<home>\logs\<kit tag>\logs`.
- The single steps can also be run by hand: `prepare`, `build` (on a Mac), `merge`, `encrypt`, `decrypt`
  (`py nxkit.py -h`).
