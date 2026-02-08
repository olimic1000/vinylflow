# VinylFlow Quick Start Guide

Turn your vinyl recordings into properly tagged FLAC files in 5 minutes.

## What You Need

1. **Docker Desktop** - Download from [docker.com](https://www.docker.com/products/docker-desktop)
2. **A Discogs account** - Free sign-up at [discogs.com](https://www.discogs.com)
3. **Your vinyl recordings as WAV files**

## Setup (First Time Only)

### Step 1: Get Your Discogs Token

1. Log in to Discogs
2. Go to [Settings → Developers](https://www.discogs.com/settings/developers)
3. Click "Generate new token"
4. Copy the token (long string of letters/numbers)

### Step 2: Install VinylFlow

Open Terminal (Mac) or Command Prompt (Windows) and run:

```bash
# Download VinylFlow
git clone <repository-url>
cd vinylflow

# Create config file
cp .env.example .env

# Edit config file and paste your Discogs token
nano .env
```

In the `.env` file, replace `your_token_here` with your actual token, then save (Ctrl+X, then Y, then Enter).

### Step 3: Start VinylFlow

```bash
docker compose up -d
```

Wait 30 seconds for everything to start, then open your browser to:
**http://localhost:8000**

## How to Use It

### The 5-Step Process

1. **Upload** - Drag your WAV file into the browser
2. **Analyze** - Click "Analyze & Detect Tracks" (waits for silence between tracks)
3. **Search** - Type the album name and search Discogs
4. **Select** - Click the matching album from the results
5. **Process** - Click "Process & Save" and wait

Your tagged FLAC files appear in the `output` folder!

### Example Workflow

**You have:** `aril-brikha-departure.wav` (one WAV file with both sides recorded)

1. Drag the file into VinylFlow
2. Click "Analyze & Detect Tracks" → finds 4 tracks (A1, A2, B1, B2)
3. Search "aril brikha departure"
4. Click the matching album
5. Verify the track mapping looks right
6. Click "Process & Save"

**You get:**
```
output/Aril Brikha - Departure/
  ├── A1-Groove La Chord.flac
  ├── A2-Art Of Vengeance.flac
  ├── B1-Ambiogenesis.flac
  ├── B2-Deeparture In Mars.flac
  └── folder.jpg
```

All tracks properly tagged with artist, album, track names, and cover art!

## Tips

### Recording Your Vinyl

- Record both sides as one long WAV file, or side A and side B as separate files
- Use Audacity, Logic, or your audio interface software
- 16-bit/44.1kHz or higher quality
- Name files like: "artist - album.wav"

### If Tracks Don't Split Right

The app detects silence between tracks. If it misses a split:

1. Click the ⚙️ settings icon
2. Adjust "Silence Threshold":
   - **-35 dB** = more sensitive (splits easier)
   - **-45 dB** = less sensitive (needs louder silence)
3. Click "Analyze & Detect Tracks" again

### Common Issues

**Can't access http://localhost:8000**
```bash
# Check if it's running
docker compose ps

# See the logs
docker compose logs -f

# Restart it
docker compose restart
```

**Wrong track count detected**
- Adjust silence threshold in settings
- Or choose "Use duration-based splitting" (uses Discogs track lengths)

**Album not on Discogs**
- Try searching by catalog number (on the record label)
- Skip and tag manually later

## Daily Usage

**Start VinylFlow:**
```bash
cd vinylflow
docker compose up -d
```

**Stop VinylFlow:**
```bash
docker compose stop
```

**Check on progress:**
```bash
docker compose logs -f
```

## Need Help?

- Full documentation: See [README.md](README.md)
- Docker issues: See [DOCKER-SETUP.md](DOCKER-SETUP.md)

## What Makes This Cool

- **Automatic track detection** - No manual splitting in Audacity
- **Proper vinyl numbering** - A1, A2, B1, B2 (not just Track 1, 2, 3, 4)
- **Complete metadata** - Artist, album, year, label, cover art
- **Lossless FLAC** - Maximum quality, smaller than WAV
- **Batch processing** - Queue up multiple albums
- **Works on any device** - Control from your phone/tablet on the same network

Saves 20+ minutes per album compared to manual Audacity + tagging!
