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
counter), `-o out.ipa`, `--keep-extracted` (keep the extracted files to rebuild faster).

## nxkit's changes to nxnative

The cloud build applies the patches in `patches/` to nxnative before compiling the app:

- **Display options** in the launcher: Resolution (Native, 1920x1080, 1600x900, 1280x720: the size the
  picture is scaled to on screen) and Screen Scaling (Fit 16:9, Stretch full screen, Stretch 1.943:1,
  Zoom full screen). Touches on the game's touch screen follow the picture.
- **zstd without the command** (PC shader split): Windows has no `zstd` command, so plain zstd files
  (Rhythm Heaven Groove's) are decompressed with Python's own zstd when it is missing.
- **Memory recorder from env.txt** (iOS): `DM_MEMSTAT=memstat.tsv` in `Documents/env.txt` now starts
  nxnative's per-second memory breakdown (it used to start before env.txt was read); the file goes
  beside `log.txt`.
- **Idle texture eviction** (testing, made for Luigi's Mansion 3): GPU copies of compressed textures on
  GPU-only pools that nothing used for about 7 seconds are dropped and rebuilt from game memory when
  needed again, so games that never free their textures don't run the iPhone out of memory.
  `DM_NVN_EVICT_IDLE=0` in `env.txt` turns it off; the log line `idle texture eviction:` shows what it freed.

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
