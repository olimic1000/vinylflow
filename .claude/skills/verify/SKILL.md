---
name: verify
description: Build/launch/drive recipe for verifying VinylFlow changes end-to-end (FastAPI backend + Alpine.js frontend).
---

# Verifying VinylFlow

## Launch (sandboxed — never pollutes real config/output)

```bash
SCRATCH=<scratchpad>/verify
mkdir -p "$SCRATCH"/{uploads,output,config}
export VINYLFLOW_UPLOAD_DIR="$SCRATCH/uploads" DEFAULT_OUTPUT_DIR="$SCRATCH/output" VINYLFLOW_CONFIG_DIR="$SCRATCH/config"
python3 -m uvicorn backend.api:app --port 8765   # run in background
```

A real Discogs token lives at `~/Library/Application Support/VinylFlow/config/settings.json`
(key `DISCOGS_USER_TOKEN`). Copy just that key into `$SCRATCH/config/settings.json` to
enable search/process without touching the user's real output dir.

## Test audio (3 known tracks: 440/660/880 Hz tones, 3s silence gaps, 126s total)

```bash
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=40" -f lavfi -i "anullsrc=r=44100:cl=mono:d=3" \
  -f lavfi -i "sine=frequency=660:duration=40" -f lavfi -i "anullsrc=r=44100:cl=mono:d=3" \
  -f lavfi -i "sine=frequency=880:duration=40" \
  -filter_complex "[0][1][2][3][4]concat=n=5:v=0:a=1[out]" -map "[out]" -ar 44100 -ac 2 \
  -c:a pcm_s16le "$SCRATCH/Test Récord – Side A.wav"
```

Use a non-ASCII filename on purpose — Content-Disposition regressions show up as 500s.

## Drive the API surface

1. `curl -F "files=@<wav>" localhost:8765/api/upload` → expect duration 126.0
2. `POST /api/analyze {"file_id":...}` → expect 3 tracks at ~0/43/86s
3. `GET /api/waveform-peaks/<id>` → 200, ~40KB JSON
4. `GET /api/audio/<id>` → after preconvert finishes: `content-type: audio/mpeg`,
   RFC-5987 `filename*=utf-8''...` header (GET only; HEAD 405s — known)
5. `GET /api/preview/<id>/3` twice → second call must be cached (<50ms)
6. `POST /api/process` with release_id 2879 (Daft Punk – Discovery), mapping A1–A3 →
   poll `GET /api/process/<id>` until complete
7. Verify output FLACs: each exactly 40.00s, dominant FFT frequency 440/660/880 Hz
   respectively (proves seek accuracy), Mutagen tags + 1 embedded picture each.

## Drive the UI (gstack /browse)

- `$B goto http://localhost:8765`, `$B upload 'input[type="file"]' <wav>`
- Click Analyze via `$B js` (button text "Analyze & Detect Tracks"), expect
  "Detecting tracks…" spinner, then 3 tracks + waveform regions render from peaks.
- `$B network` should show `/api/audio` as **206** range requests of the ~2MB MP3,
  not the 22MB WAV.
- WaveSurfer canvas lives in shadow DOM — `#waveform *` counts only 1 light-DOM child;
  screenshot to confirm rendering instead.

## Gotchas

- Session store is in-memory: restarting uvicorn invalidates all file_ids.
- Tests: `python3 -m pytest tests/ -q` (fast, no ffmpeg needed for most).
- `node --check backend/static/app.js` catches JS syntax errors (no build step).
