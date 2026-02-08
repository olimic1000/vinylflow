# VinylFlow - Docker Quick Setup Guide

Complete setup in 3 steps and 2 minutes.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop) (Mac/Windows)
- OR Docker Engine + Docker Compose (Linux)
- A [Discogs API token](https://www.discogs.com/settings/developers) (free, takes 30 seconds)

**Note:** Modern Docker Desktop uses `docker compose` (with space). Older versions use `docker compose` (with hyphen). This guide uses the newer syntax.

## Setup

### 1. Clone and Navigate
```bash
git clone <repository-url>
cd vinylflow
```

### 2. Configure
```bash
# Copy environment template
cp .env.example .env

# Edit with your Discogs token
nano .env  # or use any text editor
```

Add your Discogs token:
```bash
DISCOGS_USER_TOKEN=your_actual_token_here
```

### 3. Launch
```bash
docker compose up -d
```

Done! Open http://localhost:8000

## Verification

Check that everything is working:

```bash
# View container status
docker compose ps

# Should show:
# NAME        STATE    PORTS
# vinylflow   Up       0.0.0.0:8000->8000/tcp
```

View logs:
```bash
docker compose logs -f
```

## Daily Usage

**Start VinylFlow:**
```bash
docker compose up -d
```

**Stop VinylFlow:**
```bash
docker compose stop
```

**Restart after config changes:**
```bash
docker compose restart
```

**View real-time logs:**
```bash
docker compose logs -f
```

**Update to latest version:**
```bash
git pull
docker compose build
docker compose up -d
```

## File Locations

- **Processed files:** `./output/` (on your host machine)
- **Temp uploads:** `./temp_uploads/` (cleared periodically)
- **Config:** `.env` (edit and restart container)

## Customization

Edit `.env` to customize settings:

```bash
# Change web interface port
PORT=8080

# Adjust silence detection sensitivity
DEFAULT_SILENCE_THRESHOLD=-35  # More aggressive
# or
DEFAULT_SILENCE_THRESHOLD=-45  # More permissive

# Change output directory (inside container)
DEFAULT_OUTPUT_DIR=/app/output
```

After changes:
```bash
docker compose restart
```

## Troubleshooting

### Port already in use
```bash
# Change PORT in .env
PORT=8080

# Restart
docker compose down && docker compose up -d
```

### Can't access web interface
```bash
# Check container status
docker compose ps

# Check logs for errors
docker compose logs -f

# Try restarting
docker compose restart
```

### Files not appearing in output/
```bash
# Check permissions
chmod -R 755 ./output

# Verify volume mount
docker compose exec vinylflow ls -la /app/output

# Restart
docker compose restart
```

### Want to reset everything
```bash
# Stop and remove container (keeps your files)
docker compose down

# Remove images too
docker compose down --rmi all

# Start fresh
docker compose up -d
```

## Accessing from Other Devices

1. Find your machine's IP address:
   ```bash
   # Mac
   ipconfig getifaddr en0

   # Linux
   hostname -I | awk '{print $1}'

   # Windows
   ipconfig
   ```

2. Open browser on other device to:
   ```
   http://YOUR-IP:8000
   ```

## Next Steps

1. Open http://localhost:8000
2. Upload a WAV file
3. Click "Analyze & Detect Tracks"
4. Search for the album on Discogs
5. Verify track mapping
6. Click "Process & Save"
7. Find your tagged FLAC files in `./output/`

## Support

- Full documentation: [README.md](README.md)
- Troubleshooting: See README.md Troubleshooting section
- Configuration reference: See [.env.example](.env.example) for all options
