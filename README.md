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

## Quick Start (Docker)

**Setup Options**: You can run VinylFlow using Docker (recommended for quick setup) or manually with Python (for more control). See [Manual Setup (Non-Docker)](#manual-setup-non-docker) below for the non-Docker approach.

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed. That's it.

```bash
# 1. Clone the repository
git clone https://github.com/olimic1000/vinylflow.git
cd vinylflow
```

## 2. Set up your environment file (.env)

VinylFlow needs a **Discogs API token** to search for album metadata and artwork. This token is stored in a `.env` file (a configuration file that lives in the VinylFlow folder).

**Get your free Discogs API token:** https://www.discogs.com/settings/developers

### Option 1: Using your file explorer (easiest for beginners)

1. Open the `vinylflow` folder in your file explorer
2. **Show hidden files** (the `.env.example` file starts with a dot, so it's hidden by default):
   - **Windows:** In File Explorer, click **View** > **Show** > **Hidden items**
   - **Mac:** Press **Cmd + Shift + .** (dot) in Finder
   - **Linux:** Press **Ctrl + H** in your file manager
3. Find the file named `.env.example`
4. Copy it and rename the copy to `.env` (just remove the `.example` part)
5. Open `.env` with any text editor (Notepad, TextEdit, VS Code, etc.)
6. Paste your Discogs API token after `DISCOGS_USER_TOKEN=`:

```ini
DISCOGS_USER_TOKEN=your_token_here
```

7. Save the file

### Option 2: Using the terminal

If you're comfortable with the command line:

```bash
cp .env.example .env
```

Then open `.env` in your preferred text editor and add your Discogs token.

### Troubleshooting

**"Discogs API token not configured" error on startup?** This means either:
- The `.env` file doesn't exist (you skipped step 2 above)
- The `.env` file exists but your token hasn't been added yet
- You need to restart Docker after adding the token: `docker compose restart`

## 3. Start VinylFlow

```bash
docker compose up -d
```

Open **http://localhost:8000** in your browser. You're done.

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

# 4. Configure your environment

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Edit `.env` and:
- **Add your Discogs API token** (get one free at [discogs.com/settings/developers](https://www.discogs.com/settings/developers))
- **Change `DEFAULT_OUTPUT_DIR`** from `/app/output` to a local path like `~/Music/VinylFlow`

```ini
DISCOGS_USER_TOKEN=your_token_here
DEFAULT_OUTPUT_DIR=~/Music/VinylFlow
```

**Can't find `.env.example`?** It's hidden by default. See step 2 in [Quick Start (Docker)](#quick-start-docker) above for instructions on showing hidden files.

```bash
# 5. Start the server
python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser. Done.

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

Edit `.env` to customize:

```ini
# Discogs API (required)
DISCOGS_USER_TOKEN=your_token_here

# Silence detection (adjust if tracks aren't splitting correctly)
DEFAULT_SILENCE_THRESHOLD=-40      # dB — increase to -35 if tracks are merging
DEFAULT_MIN_SILENCE_DURATION=1.5   # seconds — decrease to 1.0 for short gaps
DEFAULT_MIN_TRACK_LENGTH=30        # seconds — ignore segments shorter than this

# FLAC compression (0-8, higher = smaller files)
DEFAULT_FLAC_COMPRESSION=8
```

You can also adjust silence detection settings live in the app via the ⚙️ Settings button.

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

**"Discogs API token not configured" error on startup?**
The `.env` file is either missing or doesn't have your token yet. Go back to [step 2 in Quick Start](#quick-start-docker) and make sure you've created the `.env` file and added your Discogs API token. After adding it, restart with `docker compose restart`.

**Container won't start?**
Check if port 8000 is in use: `lsof -i :8000` (Mac/Linux) or `netstat -ano | findstr :8000` (Windows). Change the port in `.env` with `PORT=8080`.

**Files not appearing in output/?**
Make sure the `output/` directory exists and has write permissions: `chmod -R 755 ./output`

**Discogs search returns no results?**
Check your API token is set correctly in `.env`, then restart: `docker compose restart`

**Tracks not splitting correctly?**
Try adjusting silence detection in Settings (⚙️), or use duration-based splitting after selecting a Discogs release.

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

### Planned
- BPM and key detection
- Rekordbox / Traktor export
- Click and pop removal
- MusicBrainz integration
- Cloud-hosted option

---

## Contributing

Found a bug? Have a feature idea? [Open an issue](https://github.com/olimic1000/vinylflow/issues) — contributions welcome.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.

---

**VinylFlow** — Built with ❤️ by DJs, for DJs.