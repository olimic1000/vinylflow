# VinylFlow — Claude Code Context

## Project Purpose
Desktop app to digitize vinyl records: upload a WAV/AIFF recording of a vinyl side, detect track boundaries via silence analysis, search Discogs for metadata, and export split + tagged FLAC/MP3/AIFF files.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Alpine.js + Tailwind CSS + WaveSurfer.js (no build step; libraries vendored in `backend/static/vendor/`, no CDN) |
| Backend | FastAPI + uvicorn (Python) |
| Desktop shell | pywebview (WKWebView on macOS, WebView2 on Windows) |
| Audio processing | FFmpeg (via subprocess) + Mutagen (tagging) + Pillow (cover art) |
| Discogs API | `discogs-client` Python library |
| Bundling | PyInstaller (`VinylFlow.spec`) |
| CI/CD | GitHub Actions (`.github/workflows/`) |

---

## Directory Structure

```
vinylflow/
├── backend/
│   ├── api.py              # FastAPI app — all REST endpoints + WebSocket
│   └── static/
│       ├── index.html      # Single-page UI (Alpine.js)
│       ├── app.js          # Main frontend logic (~1000+ lines)
│       └── fonts/          # Mirano Extended font family
├── rthooks/
│   └── rthook_vinylflow.py # PyInstaller runtime hook (runs before app code)
├── .github/workflows/
│   ├── windows-release.yml # Manual trigger — builds Windows .exe via PyInstaller
│   ├── privacy-guard.yml   # Scans commits for secrets
│   └── release-artifact-scan.yml
├── config.py               # Config management (settings.json > .env > env vars)
├── audio_processor.py      # Silence detection, track splitting, ffmpeg wrappers
├── track_boundaries.py     # Pure track-boundary math (gaps, envelope, music span, guided placement, refine_cuts)
├── metadata_handler.py     # Discogs fetch, cover art download, audio tagging
├── desktop_launcher.py     # Entry point — sets up dirs/env vars, starts FastAPI, opens window
├── VinylFlow.spec          # PyInstaller spec (macOS .app + Windows .exe)
├── requirements.txt
└── README.md
```

---

## Architecture & Data Flow

```
User browser / pywebview window
        │ HTTP + WebSocket (localhost:8000)
        ▼
FastAPI (backend/api.py)
        │
   ┌────┴──────────────┬────────────────┬────────────────┐
   ▼                   ▼                ▼                ▼
audio_processor.py  metadata_handler.py  config.py    Discogs API
(ffmpeg subprocess) (Mutagen + Pillow)  (settings)   (discogs-client)
```

**Processing pipeline** (`POST /api/process` → `process_file_background`):
1. Fetch release from Discogs
2. Apply user's track→position mapping (A1, B2, etc.)
3. Create output folder: `{Artist} - {Album}/`
4. Download + embed cover art
5. For each track: ffmpeg extract → convert to format → tag → rename
6. Broadcast completion via WebSocket

**Upload session storage:**
- Source file: `VINYLFLOW_UPLOAD_DIR/{file_id}/source.{ext}`
- Energy envelope: `VINYLFLOW_UPLOAD_DIR/{file_id}/envelope.npy` (per-frame RMS dBFS, mono 8 kHz, 10 ms frames), decoded once in the background after upload; Smart Rescan and the music span read it from memory. Removed with the session dir
- Output: `DEFAULT_OUTPUT_DIR/{Artist} - {Album}/{position}-{title}.{fmt}`

---

## Key API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/upload` | Upload WAV/AIFF, create session UUID, queue MP3 preview |
| POST | `/api/analyze` | Silence detection → track boundaries |
| POST | `/api/analyze-duration-based` | Fallback: tracks from Discogs durations |
| POST | `/api/analyze-guided` | `{file_id, expected_tracks, durations?, side_breaks?}` (`durations`: one per track, `null` = unknown; `side_breaks`: track counts before lettered Discogs side changes) -> exactly `expected_tracks` tracks from the cached envelope. Best-match alignment first (`model: "align"`), then the gap/scale model when every length is known, then count-based detection (Smart Rescan ladder; also when no length is known, `method: "count"`). Response: `tracks`, `method`, `matched`, `boundaries` (`expected`, `snapped`, `position`, `gap`), `model`, `scale`, `gap_s`, `music_start`, `music_end` |
| GET | `/api/scan/{file_id}` | Background envelope scan, never blocks: `{ready: false}` while decoding (plus `error` with the last failure; a failed scan is restarted), else `{ready: true, music_start, music_end, music_duration}` (lead-in/run-out trimmed); WebSocket `scan_complete` when it finishes |
| GET | `/api/preview/{file_id}/{track_number}` | Generate 30s MP3 preview |
| GET | `/api/waveform-peaks/{file_id}` | Waveform data for WaveSurfer |
| GET | `/api/audio/{file_id}` | Serve audio for playback |
| POST | `/api/search` | Discogs search |
| POST | `/api/process` | Start background processing job |
| GET | `/api/process/{job_id}` | Poll job status |
| GET/DELETE | `/api/queue`, `/api/queue/{file_id}` | Manage upload queue |
| GET/PUT | `/api/config` | Read/write audio settings |
| POST | `/api/setup/discogs-token` | Save Discogs token |
| WS | `/ws` | Real-time progress updates |

---

## Detection internals

- **Best-match alignment** (`track_boundaries.guided_by_alignment`, used by `/api/analyze-guided`): each Discogs length is matched to the music between two chosen gaps, so one wrong length no longer shifts later cuts and an unknown length simply costs nothing. Steps: `smooth_envelope` (310 ms rolling median, removes clicks/thumps) -> `gap_candidates` (strong: quiet runs below the span's 3rd percentile + 10 dB, >= 0.4 s, runs < 1 s apart merged, scored by length + contrast with the music +-8 s; weak: quietest 0.3 s per 5 s window for segues, penalised) -> `align_durations_to_gaps` (vectorised DP over n-1 candidates, minimising the sum of `log1p(((L-d)/sigma)^2)` with sigma = 3 s + 3 % per track, minus `GAP_STRENGTH_W` x gap strength, doubled at side changes; unknown d = no cost; < 5 s tracks infeasible) -> `onset_cut`: the next song's onset follows the candidate's *last* quiet run of >= `ONSET_LONG_QUIET_S` (a merged candidate can contain a soft intro's first notes or run-out thumps before a flip) where the smoothed level rises `ONSET_RISE_DB` above that run, refined on the raw envelope to a >= 50 ms sustained rise (a click is shorter). After a side flip (arm-up silence <= `ARM_UP_DB` before the first sound) `_after_needle_drop` skips the thump and lead-in crackle. The next track starts `PRE_ROLL_S` (0.8) before the onset and the previous track ends exactly there (a gap shorter than `PRE_ROLL_S` + `POST_ROLL_S` is shared in proportion), except silence beyond `MAX_TAIL_S` (4 s) after the fade is dropped. The first track starts and the last ends `EDGE_PAD_S` into the lead-in / run-out. Cuts are contiguous, so `refine_cuts` only widens the outer edges. `expected` = the track's own length from its song's onset; `snapped` = a real gap was used. With no known length at all it returns `None` and the endpoint uses count-based detection.
- **Side flips:** `side_breaks` are the track counts before lettered Discogs side changes; the alignment counts such a boundary's gap strength twice, and the fallback gap/scale model (`boundaries_from_expected_durations`) anchors the sides on a >= 10 s silence near the expected change. Numeric-only positions do not imply a flip.
- **Fallbacks, in order:** best-match alignment -> gap/scale model (all lengths known: leftover span split evenly into n-1 gaps, `MAX_GAP_S` = 8 s, else proportional scaling clamped 0.9-1.1) -> count-based detection.
- All constants are module-level and were tuned on real records with `tests/e2e/real_records_eval.py` (needs local `records/` + `output/` mounted read-only, see its docstring; not part of the suite). Synthetic cases: `tests/test_best_match_splits.py`. Not yet measured on classical / segued records.

---

## Environment Variables

All set by `desktop_launcher.py` before starting FastAPI. **Never hardcode paths in api.py or audio_processor.py.**

| Variable | Set by | Purpose |
|----------|--------|---------|
| `VINYLFLOW_CONFIG_DIR` | launcher | Platform config dir (AppData / Library / .config) |
| `VINYLFLOW_UPLOAD_DIR` | launcher | Temp uploads dir |
| `VINYLFLOW_FFMPEG_PATH` | launcher | Absolute path to ffmpeg binary |
| `DEFAULT_OUTPUT_DIR` | launcher | Default output folder |
| `DISCOGS_USER_TOKEN` | user / settings.json | Discogs API token |
| `DISCOGS_USER_AGENT` | user / settings.json | Discogs API user agent |
| `DEFAULT_SILENCE_THRESHOLD` | config | Silence detection threshold (dB) |
| `DEFAULT_MIN_SILENCE_DURATION` | config | Min silence gap (seconds) |
| `DEFAULT_MIN_TRACK_LENGTH` | config | Min track length (seconds) |
| `DEFAULT_FLAC_COMPRESSION` | config | FLAC compression level 0–8 |
| `TEMP_TTL_HOURS` | config | Temp file cleanup timeout |

**Config priority (highest → lowest):** `settings.json` → `.env` → environment vars → code defaults

---

## PyInstaller / Bundling Rules

These rules are **critical** — violating them breaks the bundled app silently.

1. **Never exclude** `pythonnet`, `clr`, `clr_loader` from spec — pywebview's `edgechromium` backend needs them on Windows.
2. **Collect** `certifi` data files in spec so `cacert.pem` is present → HTTPS to Discogs works.
3. **Collect** `pythonnet` data files so `Python.Runtime.dll` is bundled.
4. **UPX exclusions** — both must be in `upx_exclude`:
   - `Python.Runtime.dll` — UPX corrupts .NET assemblies
   - `ffmpeg.exe` — UPX-packed ffmpeg is flagged by Windows Defender
5. **Runtime hook** `rthooks/rthook_vinylflow.py` runs before any import; sets:
   - `PYTHONNET_RUNTIME_DLL` → bundled `Python.Runtime.dll`
   - `SSL_CERT_FILE` + `REQUESTS_CA_BUNDLE` → bundled `cacert.pem`
6. Bundle `backend/static/` as data files so the frontend is served correctly.

---

## FFmpeg Usage Rules

- `audio_processor.py` **must** call `_ffmpeg()` helper (not hardcode `"ffmpeg"`) — reads `VINYLFLOW_FFMPEG_PATH`.
- Use `encoding='utf-8', errors='replace'` on subprocess calls — **not** `text=True` (breaks on Windows with non-ASCII).
- `desktop_launcher.py` `_bundled_ffmpeg_path()` must try **both** `ffmpeg.exe` (Windows) and `ffmpeg` (macOS/Linux).

---

## Frontend Notes (app.js / index.html)

- **No build step, fully offline** — Alpine.js, Tailwind (v3 Play script), WaveSurfer + regions are vendored, version-pinned, in `backend/static/vendor/` (versions, SHA-256, update steps in its README). `tests/test_vendor_offline.py` fails if index.html loads anything external or a hash drifts. Don't add CDN tags.
- All UI state lives in one Alpine.js `x-data` object (see `app.js`).
- **WaveSurfer.js** handles waveform rendering with draggable region markers for track boundaries.
- WebSocket (`/ws`) receives real-time progress updates during processing.

### macOS Context Menu Fix (critical)
WKWebView intercepts right-click before JavaScript sees it. Fix applied in `init()`:
```javascript
// Capture phase — fires before WKWebView's shadow DOM handler
document.addEventListener('contextmenu', e => e.preventDefault(), true);
```
Also: guard `@click.away` with `!$event.ctrlKey` so Ctrl+click (which triggers `contextmenu` on macOS) doesn't immediately dismiss the custom context menu.

---

## Deployment Modes

| Mode | Command | Notes |
|------|---------|-------|
| Docker | `docker compose up` | Recommended for self-hosting. Published on `127.0.0.1` only; LAN access is opt-in via `BIND_ADDRESS=0.0.0.0` in `.env` (no auth, so trusted networks only) |
| Desktop (bundled) | `VinylFlow.app` / `VinylFlow.exe` | PyInstaller build |
| Local dev | `python desktop_launcher.py` | Opens browser or native window (binds 127.0.0.1) |
| Backend only | `uvicorn backend.api:app --reload` | For API development; `python -m backend.api` binds `HOST` (default 127.0.0.1) |

---

## GitHub Actions

- **`windows-release.yml`** — Manual trigger with `tag` input. Builds on `windows-latest`, installs FFmpeg via Chocolatey, runs PyInstaller, uploads `VinylFlow-windows-unsigned.zip` to the release.
- **`privacy-guard.yml`** — Scans commits for secrets.
- **`release-artifact-scan.yml`** — Scans release assets before publishing.

---

## Known Bugs Fixed (Feb 2026)

| File | Bug | Fix |
|------|-----|-----|
| `backend/api.py` | `UPLOAD_DIR` ignored `VINYLFLOW_UPLOAD_DIR` env var (hardcoded `__file__` path) | Read env var at startup |
| `backend/api.py` | `logger` NameError | Define logger before use |
| `config.py` | `config.DEFAULT_SILENCE_THRESHOLD` (uppercase) | Lowercase attribute name |
| `desktop_launcher.py` | `_bundled_ffmpeg_path()` only tried `'ffmpeg'`, not `'ffmpeg.exe'` | Try both |
| `audio_processor.py` | Hardcoded `"ffmpeg"` instead of `_ffmpeg()` | Use helper; use `encoding='utf-8', errors='replace'` |
| `backend/static/app.js` | Right-click opened WKWebView native menu on macOS | Capture-phase `preventDefault` in `init()` |
| `backend/static/app.js` | Ctrl+click dismissed custom context menu immediately | Guard `@click.away` with `!$event.ctrlKey` |
| `VinylFlow.spec` | `ffmpeg.exe` UPX-packed → blocked by Windows Defender | Add to `upx_exclude` |

---

## Audio Formats

- **Input:** `.wav`, `.aiff`, `.aif` — minimum 60 seconds
- **Output:** FLAC (configurable compression 0–8), MP3 (320 kbps), AIFF (PCM big-endian, keeps source bit depth: 16-bit sources stay 16-bit, wider or unknown depths export as 24-bit)
- **Tagging:** Mutagen handles all three formats; cover art embedded as bytes
- **Numbering:** `sequential_disc_per_lp` writes DISCNUMBER + TRACKNUMBER with `TRACKTOTAL`/`DISCTOTAL` (ID3 `n/total` in TRCK/TPOS). Numbering follows the release tracklist, so a side exported on its own continues after side A; a skipped track leaves a gap. DISCTOTAL = the highest LP the release reaches. `vinyl` writes no totals. Only one repeated letter is a side (`_disc_of`): `CD1` / `DVD1` tracks keep their position as TRACKNUMBER, with no disc or totals.
- **Default numbering** is `sequential_disc_per_lp` (`config.DEFAULT_TRACK_NUMBERING`, `OutputSpec`, the frontend `config` default); the `vinyl` scheme stays selectable in Settings. A non-empty settings.json without `TRACK_NUMBERING` predates that default and is pinned to `vinyl` on load (`Config._resolve_track_numbering`), unless the environment sets it; new installs therefore record the default on their first save (`_write_settings`).

---

## Python Dependencies (key ones)

```
fastapi, uvicorn[standard]   # API server
pywebview                    # Desktop window
mutagen                      # Audio tagging
discogs-client               # Discogs API
requests, certifi            # HTTP + SSL
pillow                       # Cover art processing
numpy                        # Waveform peak generation
python-dotenv                # .env parsing
python-multipart             # File upload parsing
aiofiles                     # Async file I/O
websockets                   # WebSocket support
```
