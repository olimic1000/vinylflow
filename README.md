# Vinyl Digitizer

Automated vinyl record digitization tool for house/techno 12-inch EPs. Converts WAV recordings to FLAC with intelligent silence detection, Discogs metadata tagging, and vinyl-style track numbering.

## Features

- **Web Interface** - Modern browser-based UI with drag-and-drop uploads
- **Automatic silence detection** - Intelligently splits albums into tracks
- **Duration-based splitting** - Fallback for seamlessly mixed tracks
- **Discogs integration** - Visual search with album artwork
- **Vinyl-style numbering** - Proper A1, A2, B1, B2 track notation
- **Cover art** - Downloads and embeds album artwork
- **High-quality FLAC** - Lossless compression (configurable level)
- **Batch queue management** - Process multiple files with real-time progress
- **Interactive workflow** - Manual confirmation for accurate metadata
- **Remote access** - Control from any device on your network

## Time Savings

- **Manual workflow:** 20-30 min per album (Audacity + manual tagging)
- **Automated workflow:** 3-5 min per album (mostly Discogs confirmation)
- **Time savings:** ~85% reduction

## Installation

### 1. Install System Dependencies

```bash
# Install ffmpeg and flac via Homebrew
brew install ffmpeg flac
```

### 2. Install Python Dependencies

```bash
# Install required Python packages (including web interface)
python3 -m pip install --user -r requirements.txt

# Install web server dependencies
python3 -m pip install --user fastapi "uvicorn[standard]" python-multipart websockets aiofiles
```

### 3. Set Up Configuration

```bash
# Create configuration file
./vinyl_digitizer.py init

# Edit .env and add your Discogs token
# Get token from: https://www.discogs.com/settings/developers
nano .env
```

### 4. Verify Installation

```bash
# Check that all dependencies are installed
./vinyl_digitizer.py check
```

## Usage

### Web Interface (Recommended)

The easiest way to use Vinyl Digitizer is through the web interface:

```bash
# Start the web server
./start_web.sh

# Or manually:
python3 -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
```

Then open your browser to:
- **Local:** http://localhost:8000
- **From other devices:** http://[your-mac-ip]:8000

**Web Interface Features:**
- 📤 **Drag & drop** WAV files directly from Finder
- 🎵 **Visual queue** - See all uploaded files and their status
- 🔍 **Interactive search** - Browse Discogs results with album artwork
- 🎚️ **Track mapping** - Visual side-by-side comparison with reverse option
- 📊 **Real-time progress** - Watch processing happen live
- ⚙️ **Settings** - Adjust silence detection parameters on the fly
- 📱 **Mobile friendly** - Control from iPad or phone on same network

**Workflow:**
1. Upload WAV file(s) via drag-and-drop
2. Click "Analyze & Detect Tracks"
3. Search Discogs or use auto-suggested query
4. Select matching album from visual grid
5. Verify track mapping (use Reverse if needed)
6. Click "Process & Save"
7. Watch progress in real-time
8. Process next file when done

### Command Line Interface

For automation or scripting, use the CLI:

#### Process Single Album

```bash
# Basic usage
./vinyl_digitizer.py process "/path/to/album.wav"

# Dry run (test without actually processing)
./vinyl_digitizer.py process "/path/to/album.wav" --dry-run

# With custom output directory
./vinyl_digitizer.py process "/path/to/album.wav" -o "/custom/output/"

# Verbose output
./vinyl_digitizer.py process "/path/to/album.wav" -v

# Adjust silence detection
./vinyl_digitizer.py process "/path/to/album.wav" --silence-threshold -35
```

### Batch Process Directory

```bash
# Process all WAV files in directory
./vinyl_digitizer.py batch "/Users/oliviermichelet/Music/to convert/"

# Dry run for entire batch
./vinyl_digitizer.py batch "/path/to/albums/" --dry-run -v
```

## Workflow Example

Here's what happens when you process a file:

```
./vinyl_digitizer.py process "aril brikha departure.wav"
```

1. **Validates input file** - Checks that file exists and is valid audio
2. **Detects silence** - Finds track boundaries automatically
3. **Searches Discogs** - Uses filename as search query
4. **Interactive selection** - Shows top 5 matches, you select the correct one
5. **Maps tracks** - Matches detected splits to Discogs vinyl positions (A1, A2, B1, B2)
6. **Extracts tracks** - Splits audio and converts to FLAC
7. **Downloads cover art** - Fetches album artwork from Discogs
8. **Tags files** - Writes metadata (artist, album, track names, vinyl numbers)
9. **Organizes output** - Creates folder: `new 12-inches/Aril Brikha - Departure/`

Result:
```
new 12-inches/Aril Brikha - Departure/
  ├── A1-Groove La Chord.flac
  ├── A2-Art Of Vengeance.flac
  ├── B1-Ambiogenesis.flac
  ├── B2-Deeparture In Mars.flac
  └── folder.jpg
```

## Configuration

Edit `.env` to customize settings:

```ini
# Discogs API (required)
DISCOGS_USER_TOKEN=your_token_here

# Output location
DEFAULT_OUTPUT_DIR=/Users/oliviermichelet/Music/new 12-inches

# Silence detection (adjust if tracks not splitting correctly)
DEFAULT_SILENCE_THRESHOLD=-40          # dB level (more negative = quieter)
DEFAULT_MIN_SILENCE_DURATION=1.5       # seconds
DEFAULT_MIN_TRACK_LENGTH=30            # seconds

# FLAC compression (0-8, higher = more compression)
DEFAULT_FLAC_COMPRESSION=8
```

### Adjusting Silence Detection

If tracks aren't splitting correctly:

- **Tracks merging together:** Increase threshold (e.g., `-35` instead of `-40`)
- **Too many splits:** Decrease threshold (e.g., `-45` instead of `-40`)
- **Splitting on brief silence:** Increase min silence duration (e.g., `2.0` instead of `1.5`)

## Troubleshooting

### Tracks Not Splitting Correctly

If silence detection misses track boundaries:

1. The tool will warn you about duration mismatches
2. Options provided:
   - **Adjust parameters** - Try different silence threshold
   - **Duration-based split** - Use Discogs track durations instead
   - **Skip** - Handle manually in Audacity

### Album Not in Discogs

If your album isn't found:

1. Try a custom search query (enter artist or catalog number)
2. Check Discogs website manually to confirm it exists
3. Skip and tag manually later

### Cover Art Issues

If cover art fails to download:

- Processing continues without cover art
- You can add it manually later
- Check Discogs page has images

### Rate Limiting

Free Discogs API tier: 60 requests per minute

- Tool automatically rate-limits to 1 req/sec
- Batch processing paces requests appropriately
- If you hit limits, wait a minute and continue

## Tips

### Filename Conventions

Your WAV filenames should contain artist/album info:
- ✓ "aril brikha departure.wav"
- ✓ "kassem mosse aqueous haze.wav"
- ✓ "dj assault i'm nigga.wav"

The tool uses filenames as initial Discogs search queries.

### Best Practices

1. **Test with dry-run first** - Verify detection before processing
2. **Start with known albums** - Test with albums you can verify
3. **Adjust parameters per genre** - House vs techno may need different thresholds
4. **Batch similar albums** - Process albums from same era/style together

### Seamless Mixes

For tracks that blend together without silence:

- The tool detects when a segment is 2x expected duration
- Offers duration-based splitting using Discogs track lengths
- Works well for DJ mixes and continuous sides

## Commands Reference

```bash
# Check installation and configuration
./vinyl_digitizer.py check

# Create default .env file
./vinyl_digitizer.py init

# Process single file
./vinyl_digitizer.py process FILE [OPTIONS]
  -o, --output-dir DIR       Custom output directory
  --silence-threshold DB     Silence threshold in dB
  --min-silence-duration SEC Minimum silence duration
  --dry-run                  Test without processing
  -v, --verbose              Detailed output

# Batch process directory
./vinyl_digitizer.py batch DIRECTORY [OPTIONS]
  -o, --output-dir DIR       Custom output directory
  --dry-run                  Test without processing
  -v, --verbose              Detailed output
```

## Technology Stack

**Backend:**
- **FastAPI** - Modern async Python web framework
- **WebSockets** - Real-time progress updates
- **ffmpeg** - Audio processing, silence detection, format conversion
- **mutagen** - FLAC metadata tagging and cover art embedding
- **discogs-client** - Discogs API integration
- **Pillow** - Image processing for cover art

**Frontend:**
- **Alpine.js** - Lightweight reactive framework
- **Tailwind CSS** - Utility-first styling
- **Vanilla JavaScript** - WebSocket client and API integration

## Metadata Tags

Each FLAC file includes:

- **ARTIST** - Artist name
- **ALBUM** - Album title
- **TITLE** - Track title
- **TRACKNUMBER** - Vinyl position (A1, B2, etc.)
- **DATE** - Release year
- **LABEL** - Record label
- **DISCOGS_RELEASE_ID** - Discogs reference
- **COMMENT** - "Digitized from vinyl"
- **Cover art** - Embedded front cover image

## Output Structure

```
new 12-inches/
├── Artist - Album/
│   ├── A1-Track Name.flac
│   ├── A2-Track Name.flac
│   ├── B1-Track Name.flac
│   ├── B2-Track Name.flac
│   └── folder.jpg
└── Another Artist - Another Album/
    └── ...
```

## License

Personal use. Not for distribution.

## Support

For issues or questions, refer to the implementation plan or source code comments.
