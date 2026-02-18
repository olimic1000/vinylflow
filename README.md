# 🎵 VinylFlow

**Digitize vinyl 10x faster. Open source.**

Turn your vinyl recordings into perfectly tagged, organized digital files in minutes — not hours. VinylFlow automates track splitting, Discogs metadata tagging, cover art embedding, and vinyl-style numbering (A1, A2, B1, B2).

![VinylFlow Demo](docs/demo.gif)

---

## The Problem

Digitizing a vinyl record manually takes **20–30 minutes per album**: record in Audacity, manually find track boundaries, split, export, look up metadata, type it all in, find cover art, embed it. Multiply that by a collection of hundreds of records and it's a weekend project that never ends.

## The Solution

VinylFlow does it in **3 minutes**. Upload your recording, let it detect the tracks, pick the album from Discogs, and hit process. Done.

---

## Features

- **Automatic silence detection** — intelligently finds track boundaries in your recording
- **Duration-based splitting** — fallback for seamlessly mixed tracks with no gaps
- **Discogs integration** — visual search with album artwork, metadata, and track listings
- **Multiple output formats** — FLAC (lossless), MP3 (320kbps), or AIFF (lossless)
- **Multiple input formats** — WAV and AIFF recordings supported
- **Vinyl-style numbering** — proper A1, A2, B1, B2 track notation
- **Cover art** — downloads and embeds album artwork automatically
- **Interactive waveform editor** — drag regions to fine-tune track boundaries
- **Batch queue** — process multiple records with real-time progress
- **Remote access** — control from any device on your network (phone, tablet, laptop)

---

## Quick Start (Docker — Recommended / Stable)

**Setup Options**: You can run VinylFlow using Docker (**recommended stable path**) or manually with Python.

Desktop builds are currently available as an **experimental beta** for users who want to try them. See [Desktop Beta (Experimental)](#desktop-beta-experimental) below.

### Branch and Release Channel Context

This repo currently uses a two-track model:

- `main` = stable, Docker-first releases
- `desktop-beta` = ongoing macOS + Windows desktop beta work

If you're trying desktop beta features from source, switch to:

```bash
git checkout desktop-beta
```

Desktop beta tags use a pre-release format such as `v1.1.0-beta1`.

### Prerequisites

You'll need:
- **Docker Desktop** (free) — [Download here](https://www.docker.com/products/docker-desktop/)

That's it! Git is optional (see Step 1 below). No need to mess with configuration files — VinylFlow will guide you through setup in your browser.

## 1. Get VinylFlow

### Option A: Download ZIP (easiest — no Git required)

1. Go to [github.com/olimic1000/vinylflow](https://github.com/olimic1000/vinylflow)
2. Click the green **Code** button, then **Download ZIP**
3. Unzip the folder to a location you can find (like your Downloads or Desktop)
4. Open the unzipped `vinylflow` folder

### Option B: Clone with Git (for terminal users)

```bash
git clone https://github.com/olimic1000/vinylflow.git
cd vinylflow
```

## 2. Start VinylFlow

1. **Open your terminal:**
   - **Mac:** Open "Terminal" app (or iTerm)
   - **Windows:** Open "Command Prompt" or "PowerShell"
   - **Linux:** Open your terminal application

2. **Navigate to the VinylFlow folder:**
   - If you downloaded the ZIP, type `cd ` (with a space after) and drag the `vinylflow` folder into the terminal window, then press Enter
   - Or type the full path, like: `cd ~/Downloads/vinylflow` or `cd C:\Users\YourName\Downloads\vinylflow`
   - **Windows shortcut:** In File Explorer, open the vinylflow folder, click the address bar, type `cmd` and press Enter

3. **Start VinylFlow:**
   ```bash
   docker compose up -d
   ```

4. **Open your browser** and go to **http://localhost:8000**

## 3. First-Run Setup

When you open VinylFlow for the first time, you'll see a welcome screen that guides you through setup in seconds:

![VinylFlow Setup Screen](docs/modal.jpg)

1. **Get your free Discogs API token** — Click the link in the setup screen or visit [discogs.com/settings/developers](https://www.discogs.com/settings/developers)
2. **Generate a new token** — Click "Generate new token" on the Discogs settings page
3. **Copy and paste** — Paste your token into VinylFlow's setup screen
4. **Click Continue** — Done! VinylFlow validates the token and you're ready to digitize

**That's it!** 🎵 No hidden files, no terminal commands, no restart needed. Your token is saved securely and persists across Docker restarts.

**Tip:** You can update your token anytime from the Settings (⚙️) menu in VinylFlow.

---

## Manual Setup (Non-Docker)

For tech-savvy users who prefer managing their own Python environment.

### Prerequisites

You'll need to install these system dependencies first:

- **Python 3.11 or later**
- **FFmpeg** (handles all audio processing and format conversion: MP3, FLAC, AIFF)
- **FLAC encoder** (optional, provides dedicated FLAC encoding tools)
- **libsndfile1** (optional, audio I/O library)

**Installation by OS:**

**macOS** (using Homebrew):
```bash
brew install python@3.11 ffmpeg flac libsndfile
```

**Ubuntu/Debian**:
```bash
sudo apt-get update
sudo apt-get install python3.11 python3.11-venv ffmpeg flac libsndfile1-dev
```

**Windows**:
- Install [Python 3.11+](https://www.python.org/downloads/) (check "Add to PATH" during installation)
- Download [FFmpeg](https://ffmpeg.org/download.html) and add it to your PATH
- FLAC and libsndfile are bundled with FFmpeg on Windows

### Installation Steps

```bash
# 1. Clone the repository
git clone https://github.com/olimic1000/vinylflow.git
cd vinylflow

# 2. Create and activate a Python virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Start the server
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser.

## Desktop Beta (Experimental)

Desktop mode is currently a **beta channel**.

- It is available for testers who want to try Docker-free usage
- It may be less stable than Docker
- macOS builds may require security bypass steps until signed/notarized releases are available
- Docker remains the recommended production path
- Beta builds/releases should come from `desktop-beta`, not `main`

### Desktop Launcher (No Docker, easiest local mode)

For a simpler local run without Docker commands, use the desktop launcher:

```bash
python desktop_launcher.py
```

This mode automatically:
- opens VinylFlow in your browser
- stores config in `~/Library/Application Support/VinylFlow/config`
- stores temp uploads in `~/Library/Application Support/VinylFlow/temp_uploads`
- stores output in `~/Music/VinylFlow`
- uses bundled FFmpeg in packaged desktop builds when available

### Windows Desktop Beta (experimental)

On Windows, you can build an unsigned desktop beta package with PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release_unsigned_windows.ps1 -Tag v0.2.0-beta1
```

To only build/package without creating a draft release:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release_unsigned_windows.ps1 -Tag v0.2.0-beta1 -SkipDraft
```

Expected artifact:
- `dist/VinylFlow-windows-unsigned.zip`

### macOS Packaging Spike (PyInstaller)

To build a local `.app` prototype for testing:

```bash
bash scripts/build_desktop_macos.sh
```

Output app:

```bash
dist/VinylFlow.app
```

The build script bundles your local FFmpeg binary into the `.app` and the runtime uses:
1. bundled FFmpeg (desktop app)
2. system FFmpeg from PATH (fallback)

### Unsigned macOS Beta Install

If you are not enrolled in the Apple Developer Program yet, you can still distribute unsigned beta builds.

Recommended release artifact:
- `VinylFlow-macos-unsigned.zip`

Create it with one command:

```bash
bash scripts/package_unsigned_macos.sh
```

Create a GitHub draft release with the unsigned artifact:

```bash
bash scripts/draft_unsigned_release.sh v0.2.0-beta1
```

Or run the full unsigned beta release flow in one command:

```bash
bash scripts/release_unsigned_macos.sh v0.2.0-beta1
```

To only build/package without creating a draft release:

```bash
SKIP_DRAFT=1 bash scripts/release_unsigned_macos.sh v0.2.0-beta1
```

Install steps for testers:
1. Download and unzip the app bundle
2. Move `VinylFlow.app` to `Applications`
3. First launch: right-click `VinylFlow.app` → `Open`
4. If blocked: macOS `System Settings` → `Privacy & Security` → click `Open Anyway`

Notes:
- First launch may show a security warning because the app is unsigned
- This is expected for beta builds without Apple signing/notarization
- After first approval, app launches normally

### macOS Signing & Notarization

After building `dist/VinylFlow.app`, you can sign and notarize it for end-user distribution:

```bash
export MACOS_SIGN_IDENTITY="Developer ID Application: YOUR NAME (TEAMID)"
bash scripts/sign_notarize_macos.sh
```

Optional notarization (if you configured an App Store Connect keychain profile):

```bash
export APPLE_NOTARY_PROFILE="vinylflow-notary"
bash scripts/sign_notarize_macos.sh
```

If `APPLE_NOTARY_PROFILE` is not set, the script signs and verifies the app, then skips notarization.

Preflight your local release setup before signing:

```bash
bash scripts/preflight_macos_release.sh
```

### GitHub Actions macOS Release (optional)

This repo includes `.github/workflows/release-macos.yml` to build on tag push (`v*`) and upload app zips to GitHub Releases.

Required GitHub secrets for signed builds:
- `MACOS_SIGN_IDENTITY` (example: `Developer ID Application: NAME (TEAMID)`)
- `MACOS_CERTIFICATE_P12_BASE64` (base64 of your Developer ID `.p12` certificate)
- `MACOS_CERTIFICATE_PASSWORD`

Optional secrets for notarization:
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_PASSWORD` (app-specific password)

Optional keychain secret:
- `MACOS_KEYCHAIN_PASSWORD` (if omitted, a temporary password is used)

For an unsigned beta release, you can omit all signing/notarization secrets and publish `VinylFlow-macos-unsigned.zip`.

Windows beta release artifacts can also be generated in CI using `.github/workflows/release-windows.yml`.

### First-Run Setup (Non-Docker)

Just like with Docker, VinylFlow will show you a welcome screen on first run:

1. Visit [discogs.com/settings/developers](https://www.discogs.com/settings/developers) and generate a free API token
2. Paste it into the VinylFlow setup screen
3. Click Continue — done!

Your token is saved to `config/settings.json` and works immediately without restart.

**Alternative (for advanced users):** You can still use a `.env` file if you prefer:

```bash
cp .env.example .env
```

Edit `.env` and add your token:

```ini
DISCOGS_USER_TOKEN=your_token_here
DEFAULT_OUTPUT_DIR=~/Music/VinylFlow
```

**Notes:**
- The `output/` and `temp_uploads/` directories are created automatically
- You can adjust silence detection and other settings in `.env` (see [Configuration](#configuration) below)
- To stop the server, press `Ctrl+C` in the terminal

---

## How It Works

1. **Upload** — drag and drop your WAV or AIFF recording
2. **Analyze** — VinylFlow detects track boundaries using silence detection
3. **Search** — find your album on Discogs with visual artwork results
4. **Map** — match detected tracks to Discogs track listings
5. **Choose format** — FLAC, MP3, or AIFF output
6. **Process** — tracks are split, converted, tagged, and saved with cover art

Your files appear in the `output/` folder, organized as `Artist - Album/A1-Track Name.flac`.

---

## Output Example

```
output/
└── Aril Brikha - Departure/
    ├── A1-Groove La Chord.flac
    ├── A2-Art Of Vengeance.flac
    ├── B1-Ambiogenesis.flac
    ├── B2-Deeparture In Mars.flac
    └── folder.jpg
```

Each file includes embedded metadata: artist, album, title, track number, year, label, Discogs ID, and cover art.

---

## Who Is This For?

- **DJs** digitizing crate finds for digital sets
- **Vinyl collectors** preserving and cataloguing collections
- **Record labels** archiving back catalogs
- **Music lovers** who want their vinyl in lossless digital

---

## Configuration

**Discogs Token**: Managed via the web UI (Settings ⚙️ menu)

**Audio Processing Settings**: Adjust in the app via Settings (⚙️), or for advanced users, edit `config/settings.json` or `.env`:

```ini
# Silence detection (adjust if tracks aren't splitting correctly)
DEFAULT_SILENCE_THRESHOLD=-40      # dB — increase to -35 if tracks are merging
DEFAULT_MIN_SILENCE_DURATION=1.5   # seconds — decrease to 1.0 for short gaps
DEFAULT_MIN_TRACK_LENGTH=30        # seconds — ignore segments shorter than this

# FLAC compression (0-8, higher = smaller files)
DEFAULT_FLAC_COMPRESSION=8
```

**Config Priority**: `config/settings.json` (UI-editable) → `.env` (manual) → environment variables (Docker)

### Silence Detection Tips

| Problem | Fix |
|---|---|
| Tracks merging together | Increase threshold (e.g. `-35` instead of `-40`) |
| Too many splits | Decrease threshold (e.g. `-45` instead of `-40`) |
| Splitting on brief silence | Increase min silence duration (e.g. `2.0`) |

---

## Managing Docker

```bash
# View logs
docker compose logs -f

# Stop VinylFlow
docker compose stop

# Restart
docker compose restart

# Remove containers (keeps your files in ./output)
docker compose down
```

---

## Troubleshooting

**See the setup screen on first run?**
This is normal! VinylFlow guides you through adding your Discogs token via the web interface. Just follow the on-screen instructions — it takes 30 seconds.

**"command not found: git"?**
You don't need Git! Use the **Download ZIP** option in [Step 1](#1-get-vinylflow) instead.

**"command not found: docker"?**
Make sure Docker Desktop is installed and **running**. You should see the Docker icon in your system tray (Mac menu bar or Windows taskbar).

**Can't navigate to the VinylFlow folder in terminal?**
- **Mac/Linux:** Type `cd ` (with a space) and drag the vinylflow folder into the terminal window, then press Enter
- **Windows:** Open the vinylflow folder in File Explorer, click the address bar, type `cmd` and press Enter — this opens a terminal already in the right folder

**Container won't start?**
Check if port 8000 is in use: `lsof -i :8000` (Mac/Linux) or `netstat -ano | findstr :8000` (Windows). Change the port in `.env` with `PORT=8080`.

**Files not appearing in output/?**
Make sure the `output/` directory exists and has write permissions: `chmod -R 755 ./output`

**Discogs search returns no results?**
Your API token might be invalid or revoked. Click the Settings (⚙️) button and update your token, or generate a new one at [discogs.com/settings/developers](https://www.discogs.com/settings/developers)

**Tracks not splitting correctly?**
Try adjusting silence detection in Settings (⚙️), or use duration-based splitting after selecting a Discogs release.

**Need runtime diagnostics (FFmpeg/path checks)?**
Open `http://localhost:8000/api/diagnostics` to see FFmpeg source (bundled/system), version, and key runtime paths.

---

## Technology Stack

| Component | Technology |
|---|---|
| Backend | Python, FastAPI, uvicorn |
| Audio processing | FFmpeg |
| Metadata tagging | Mutagen (FLAC, MP3, AIFF) |
| Music database | Discogs API |
| Frontend | Alpine.js, Tailwind CSS |
| Waveform display | WaveSurfer.js |
| Deployment | Docker |

---

## Roadmap

### Shipped (v1.0)
- Core digitization workflow
- Discogs integration with visual search
- Interactive waveform editor with draggable track boundaries
- Manual track splitting and deletion
- Vinyl-style track numbering (A1, A2, B1, B2)
- FLAC, MP3, and AIFF output
- WAV and AIFF input
- Duration-based splitting fallback
- WebSocket real-time progress
- Docker one-command setup
- **Web-based first-run setup** — no more hidden `.env` files!

### Planned
- BPM and key detection
- Rekordbox / Traktor export
- Click and pop removal
- MusicBrainz integration
- Cloud-hosted option

---

## Contributing

Found a bug? Have a feature idea? [Open an issue](https://github.com/olimic1000/vinylflow/issues) — contributions welcome.

### Branching Workflow (Current)

- Desktop-only changes: branch from `desktop-beta` and open PRs back to `desktop-beta`
- Stable fixes for all users: branch from `main` and open PRs back to `main`
- Keep branches aligned by back-merging or cherry-picking stable fixes from `main` into `desktop-beta`

### Release Channels

- Stable releases come from `main`
- Desktop pre-releases (beta) come from `desktop-beta`

### Beta → Stable Promotion Criteria

Promote desktop flow into `main` only when:

- install flow is consistently successful
- processing reliability matches Docker path
- support load is manageable
- macOS signing/notarization (or accepted alternative) is in place
- Windows beta is at acceptable stability

Full workflow details remain documented in [docs/BRANCHING_STRATEGY.md](docs/BRANCHING_STRATEGY.md).

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.

---

**VinylFlow** — Built with ❤️ by DJs, for DJs.