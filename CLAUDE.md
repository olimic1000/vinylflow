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
├── import_folder.py        # Pure import-folder path logic: listing, name resolution (no traversal/symlink escape), overlap check
├── track_boundaries.py     # Pure track-boundary math (gaps, envelope, music span, guided placement, refine_cuts)
├── album_description.py    # Wikipedia lead paragraph for the Comment tag, found via Wikidata by Discogs master/release id (cached)
├── track_matching.py       # Pure title normalization + tracklist matching (borrowed lengths)
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

**User flow (Discogs first):**
1. Selecting a file (`selectFile`) starts a Discogs search from the cleaned filename without blocking the UI (skipped when no token is configured; "Skip Discogs" still works). The search card sits above the waveform and is hidden once a release is picked.
2. `POST /api/search` returns basic cards only (`MetadataHandler.search_basic`: one request, reads each hit's `.data`, never lazy-fetches a release; `tracks: null`). The UI then loads tracklists from `GET /api/release/{id}` two at a time (`loadAllReleaseDetails`); a card click on an unloaded card waits for its load (`chooseResult`, retry on failure). Only a release whose tracklist loaded can be selected.
3. Cards show the track count, length badges, the "Only show releases with track lengths" filter and a fit indicator against the music span (`/api/scan/{id}` / WebSocket `scan_complete`, else the file length). `searchSeq` is bumped by every search and file switch; replies of an older sequence are dropped.
3b. Paging: the UI asks for 12 per page (`SEARCH_PAGE_SIZE`) and "Load more results" (`loadMoreResults`, `searchedQuery`/`searchPage`/`searchHasMore`) appends the next page of the *searched* query (not the edited box), at most `SEARCH_MAX_PAGES` (3, mirrored in `backend/api.py`) fetches, skipping ids already shown; its reply is dropped when `searchSeq` moved. The page size is set on the result list (`results.per_page`), never as a `per_page` search field: the library appends its own, so Discogs would get two. A page past the end (Discogs 404) is `[]`.
3c. "Know the exact pressing?" in the search card (`openReleaseById`, `parseDiscogsRelease`: `2874426`, `r2874426`, `[r…]`, `discogs.com/[xx/][sell/]release/<id>`; the whole input must be the reference: optional `http(s)://`, `www.`/`m.`, `-slug` `/` `?` `#` after the id, also `api.discogs.com/releases|masters/<id>`; ids are 1-10 digits and never 0 or leading-zero; one wrapping `<>`/quotes/`()` and trailing punctuation are stripped; other hosts or text around the link give null) fetches `GET /api/release/{id}` and calls `selectRelease`, skipping search; master links / `m…` get an explanation, no fetch. It is a pick like a card click: token `id:${call}` in `_pendingPickId` (`_releaseIdCall`), and the reply is dropped when another pick, Change release, a file switch or removing the open file superseded it or the file changed; `releaseIdLoading` is reset on file switch / removal.
4. The release card (cover, View on Discogs, Change release) sits above the waveform, with the Export card (format, Audio Restoration, Process) under the track table. "Skip Discogs" runs plain silence detection (`analyzeFile`) without a release; "Try Discogs again" returns to the search when tracks exist and no release is chosen.
5. Picking a release (`selectRelease`) suggests which side(s) the recording covers (`suggestSides`: the run of sides whose Discogs lengths fit the music span, else the full album; a release with several sides shows a "Full album" / per-disc preset row and one chip per side, `applyPreset` / `toggleSide`) and runs guided detection (`redetectForTargets` -> `POST /api/analyze-guided` with `expected_tracks`, `durations`, and `side_breaks` from `sideBreaks`) for exactly the chosen tracks (`targetTracks`, each with its `releaseIdx` into `selectedRelease.tracks`). `customMapping` holds those indices, so a side can be a subset; `resetMappingToTargets` rebuilds it and `trackCountMismatch`. When the response `method` is `durations`, `expectedBoundaries` are drawn as dashed ghost markers on the waveform (`addExpectedMarkers`, region ids `expected-N`, never tracks); `count` draws none.
5b. If the chosen sides lack lengths, the Release card (above the side picker) offers "Find lengths from other pressings" (`findLengthDonors`, `GET /api/release/{id}/length-donors`): durations are copied (missing ones only, marked "Lengths borrowed from ...", with Undo; `applyDonor`) onto a copy of the selected release (copy-on-write, the object may be a search result) and detection re-runs through `afterTargetsChanged` (ask-first after hand edits); refused while detecting or exporting, and a reply that a release/file change or Undo has superseded is dropped (`_donorSeq`). Tags still come from the selected release.
6. Hand edits (drag, inline times, split, delete) call `markEdited()` (`boundariesEdited`). Changing the release or sides after that does not re-detect: `afterTargetsChanged` shows the `editBanner` ("Snap to Discogs" / "Keep my edits"). **Snap to Discogs** (`snapToDiscogs`, also in the side picker and the count-mismatch card) re-runs guided detection and offers an Undo toast (`undoSnap`). Anything that installs tracks without detection (e.g. imported labels) must call `markEdited()` and use `resetMappingToTargets()`, not a `0..n` mapping. `/api/analyze-duration-based` is gone: guided detection covers it.
7. **Track table and player:** one table (`.track-grid`, header and each row are separate grids, so only the single `fr` column may be flexible; narrow windows scroll `.track-table-scroll` sideways) replaces the old "Detected Tracks" and "Track Mapping" cards. Each row: Discogs-track select (`setMapping`, `mappedTrack`: `customMapping` holds indices into `selectedRelease.tracks`), Start–End Adjust fields (`updateTrackStart`/`End` -> `applyTrackBounds`: a neighbour only moves when the cuts would overlap, every track keeps `MIN_TRACK_LENGTH`), typed Length (`setTrackLength`, `parseLengthInput`: m:ss, m:ss.s, h:mm:ss or seconds; end = start + length capped at the recording's end; a next track that started exactly at the old end follows), delta vs Discogs, ⇤ ⇥ cut audition (`auditionCut`, ±`AUDITION_PADDING_S`), ▶️ `toggleTrackPlay`, Ignore, Delete. The one waveform Play/Pause is `togglePlay` (state follows the player's own play/pause events: `isPlaying`); Space toggles it (`onGlobalKeydown`) except in fields, dialogs and on a focused button (`SPACE_OWNING_TAGS`); a click on a region seeks (`onRegionClicked`). There is no Stop and no 30-second row preview player (`/api/preview` is still served). Edit controls are disabled while `currentFileExporting`; playback stays usable.

**Processing pipeline** (`POST /api/process` → `process_file_background`):
1. Fetch release from Discogs
2. Apply user's track→position mapping (A1, B2, etc.)
3. Create output folder: `{Artist} - {Album}/`
4. Download + embed cover art
5. For each track: ffmpeg extract → convert to format → tag → rename
6. Broadcast completion via WebSocket

**Upload session storage:**
- Source file: `VINYLFLOW_UPLOAD_DIR/{file_id}/source.{ext}` (upload) or the original file in the import folder (`Session.in_place`; never copied into or linked from the session dir)
- Derived files (MP3 preview, peaks, `envelope.npy`, caches, A/B renders) always live in `VINYLFLOW_UPLOAD_DIR/{file_id}/`; all cleanup (`DELETE /api/queue`, clear-all, TTL reaper) is `rmtree` of that dir only, so an in-place source is never deleted. An import dir that equals, contains or sits inside the uploads dir is refused. A vanished in-place source gives HTTP 410, and one changed since its import (size or mtime) 409, from every endpoint that reads it (`_require_source`). An export never writes over its own source (`pipeline.run` checks every temp and final path first)
- Energy envelope: `VINYLFLOW_UPLOAD_DIR/{file_id}/envelope.npy` (per-frame RMS dBFS, mono 8 kHz, 10 ms frames), decoded once in the background after upload (`AudioProcessor.compute_envelope_and_peaks`: one mono 8 kHz decode spooled to a temp file, ~16 KB per second of audio, and reduced in `DECODE_BLOCK_FRAMES` blocks, so RAM stays bounded for a ~1.5 GB album and prep needs that much free temp disk) together with the waveform peaks (`peaks_v2.json`); Smart Rescan and the music span read the envelope from memory. Removed with the session dir
- Output: `DEFAULT_OUTPUT_DIR/{Artist} - {Album}/{position}-{title}.{fmt}`

---

## Key API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/upload` | Upload WAV/AIFF, create session UUID, queue MP3 preview |
| GET | `/api/import` | Import folder listing `{enabled, dir, error, files: [{name, size, mtime, stable, imported_id, imported_status}]}` (recursive <=3 levels, hidden/`._*` skipped, <=500; `stable: false` = changed <10 s ago) |
| POST | `/api/import` | `{names?, paths?}` -> in-place Sessions (same shape as upload + `in_place`; `skipped` with reasons; an already imported file returns its existing session). `names` must resolve inside the import dir (no traversal or symlink escape); `paths` only if granted by the desktop file dialog. A file changed since its import (size or mtime) starts a new session and the entry carries `replaces: <old id>` (`DesktopApi.choose_import_files`, in-process) |
| POST | `/api/analyze` | Silence detection → track boundaries |
| POST | `/api/analyze-guided` | `{file_id, expected_tracks, durations?, side_breaks?}` (`durations`: one per track, `null` = unknown; `side_breaks`: track counts before lettered Discogs side changes) -> `expected_tracks` tracks from the cached envelope (`matched: false` with fewer when they can't fit). Best-match alignment first (`model: "align"`), then the gap/scale model when every length is known, then count-based detection (Smart Rescan ladder). With no length at all (`durations` all `null`) it places sides first, then an even split between clear gaps (`model: "align-even"`, `method: "count"` so no ghost markers); no `durations` field at all uses the count ladder. `durations` of another size is a 422; a length that is zero, negative or longer than the recording counts as unknown. Response: `tracks`, `method`, `matched`, `boundaries` (`expected`, `snapped`, `position`, `gap`), `model`, `scale`, `gap_s`, `music_start`, `music_end` |
| GET | `/api/scan/{file_id}?side_changes=N` | Background envelope scan, never blocks: `{ready: false}` while decoding (plus `error` with the last failure; a failed scan is restarted), else `{ready: true, music_start, music_end, music_duration}` (lead-in/run-out trimmed); N in 0-8 also returns `side_changes: [[start, end], ...]`, the N longest non-music stretches in time order (fewer when the record has fewer); WebSocket `scan_complete` when it finishes |
| GET | `/api/preview/{file_id}/{track_number}` | Generate 30s MP3 preview |
| GET | `/api/waveform-peaks/{file_id}` | `{peaks, length, duration}`: equal-width peaks at <= 10 ms per bin (max \|x\|), cached as `peaks_v2.json` (an old coarse `peaks.json` is ignored). Written by prep's single decode, so the request waits for it instead of decoding again (it decodes itself only if the peaks write failed); the cached file is streamed as is, never parsed on the event loop |
| GET | `/api/audio/{file_id}` | Playback audio with HTTP Range (long ranges, open `N-` or closed `N-M` (WebKit always asks closed), are capped to `AUDIO_RANGE_CAP_BYTES` (8 MiB) as a 206; browsers ask for the next piece, so a seek never reads the rest of a 1.4 GB file; suffix `-N` is left alone). Default: the pinned MP3 preview only, never the source: it waits (`AUDIO_PREVIEW_WAIT_S`) for the shared preview encode (`start_preconvert`), 503 (with Retry-After) while still running, 503 when it failed (remembered per session, never re-run); `?wait=0` is the non-waiting status probe the player polls after a failed preview, then reloads once. `?precise=true` streams the source WAV itself (cut points are heard without MP3 seek/decoder drift, nothing is decoded into browser memory); an AIFF source ignores it and always uses the MP3 preview, since Chromium/Firefox cannot play AIFF (WKWebView could; a possible later improvement) |
| POST | `/api/search` | Discogs search `{query, max_results, page}` (`max_results` 1-`SEARCH_MAX_RESULTS` = 100, page 1-`SEARCH_MAX_PAGES`, else 422) -> `{results, has_more}`: basic cards with `tracks: null` (one Discogs request, no per-release fetch); the UI uses 12 per page and "Load more"; `has_more` = a full page below the page limit |
| GET | `/api/release/{release_id}` | Full release + tracklist (server-side cached); fills a search card |
| GET | `/api/release/{release_id}/description` | `{status: found/none/error, text, title, url}` (album's Wikipedia lead paragraph; `{status: disabled}` without any lookup when the setting is off) |
| GET | `/api/release/{release_id}/length-donors` | Other pressings of the same master with matching track lengths (rate-limited, <=12 fetches, cached; progress via WS `donor_progress`) |
| POST | `/api/process` | Start background processing job (`album_description` optional: `false` turns the saved setting off for this export; it can never turn it on) |
| GET | `/api/process/{job_id}` | Poll job status |
| GET/DELETE | `/api/queue`, `/api/queue/{file_id}` | Manage upload queue |
| GET/PUT | `/api/config` | Read/write audio settings |
| POST | `/api/setup/discogs-token` | Save Discogs token |
| WS | `/ws` | Real-time progress updates |

---

## Detection internals

`/api/analyze-guided` tries best-match alignment, then the gap/scale model (all lengths known), then count-based detection. How each step works, the constants and how they were tuned: [docs/guided-detection.md](docs/guided-detection.md).

---

## Environment Variables

All set by `desktop_launcher.py` before starting FastAPI. **Never hardcode paths in api.py or audio_processor.py.**

| Variable | Set by | Purpose |
|----------|--------|---------|
| `VINYLFLOW_CONFIG_DIR` | launcher | Platform config dir (AppData / Library / .config) |
| `VINYLFLOW_UPLOAD_DIR` | launcher | Temp uploads dir |
| `VINYLFLOW_IMPORT_DIR` | launcher / compose | Import folder read in place (desktop `~/Music/VinylFlow/Incoming`, Docker `/app/incoming` mounted `:ro`). settings.json `IMPORT_DIR` overrides; **not** settable via `PUT /api/config` (only shown read-only by `GET`), so a web page can never widen what is importable. Unset = import disabled |
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
- **WaveSurfer.js** handles waveform rendering with draggable region markers for track boundaries. The WaveSurfer instance and its regions plugin are stored through `markRaw` (`__v_skip`) so Alpine never proxies them: through a proxy WaveSurfer's identity checks fail (a removed region stays in the plugin's list and the next `remove()` throws). Any new code that keeps a WaveSurfer object in Alpine state must do the same (`tests/frontend/wavesurfer_raw.test.js`).
- **Waveform scrolling and zoom:** WaveSurfer's own scrollbar is hidden (`hideScrollbar`); `bindWaveScrollbar` mirrors its scroll element into `waveView` (visible fractions) and `waveScrollable` (wider than the view by more than 1 px: WaveSurfer's `Math.ceil` makes "fit" 1 px too wide, so a fraction test would show the bar at full zoom-out) for an always-visible scrollbar under the waveform that only shows when zoomed in. It is a pointer convenience (`aria-hidden`; the zoom buttons and wheel remain). The thumb is drawn and dragged with one geometry (`waveThumbGeometry` / `waveStartForThumbLeft`, mirrored by the CSS variables of `waveThumbStyle`): at least `WAVE_THUMB_MIN_PX` (24) wide, travelling the bar's width minus its own, so it never sticks out and a press on the drawn thumb is never a track press. `waveScrollbarDown`: press the track to centre the thumb there, drag the thumb (grab offset in pixels); the drag also ends on `lostpointercapture`. Cmd/Ctrl + wheel, which browsers also send for a trackpad pinch, zooms around the pointer (`wheelZoom`, `WHEEL_ZOOM_RATE`) between `fitToWidthZoom` (the one zoom-out floor, shared with the 🔍 − button and the initial zoom: long recordings open fitted, below 1 px/s; it returns 0 while the view has no width, which WaveSurfer's `zoom(0)` reads as "fill the parent", and the zoom buttons start from `effectiveZoom()` = max(`currentZoom`, live fit)) and `MAX_ZOOM_PX_PER_SEC` (500); a plain wheel is left to the page, and a step is dropped when it changes the zoom by a negligible fraction (relative, so a slow pinch works at 0.17 px/s) or when it lands on a bound (fit or 500) and the rendered width, which WaveSurfer rounds up, is within one pixel of it (so wheel events at the limit neither re-render nor stop following). Scrollbar and wheel zoom stop playback from pulling the view back (`followPlayhead(false)` sets `waveform.options.autoScroll`, which the renderer shares; `setOptions` would re-render); `seekTo` and `startPlayback` (waveform click, Play, row play, cut audition) resume following, the 🔍 buttons leave it alone, and a rebuilt waveform follows again. Scroll and wheel listeners belong to one waveform and are removed on its `destroy`. `tameRegionDragScroll` replaces the regions plugin's `adjustScroll` (vendored WaveSurfer 7.8.10 internals: re-check on update): a whole-region drag of a region wider than the view follows its leading edge (direction taken from the first move after the pointerdown, then fixed for the drag), kept `DRAG_EDGE_MARGIN` (15 %) in from the screen edge, instead of flipping the view between start and end. `guardRegionDragRelease` ends a drag whose pointerup was lost (a press while one is active, or a move with no button held) with a synthetic document `pointerup`; its capture-phase document listeners are installed once per app.
- **Waveform audio and peaks:** the waveform loads `/api/audio/{id}?precise=true` (the original WAV) when `media.canPlayType('audio/wav')` allows, else the preview. If the WAV fails to play (the media element's error code 3/4: the vendored WaveSurfer re-emits every media error, network ones too, as `error` with no argument, so its event alone is no evidence) `fallBackToPreview` reloads the preview exactly once, keeping the peaks, duration, zoom, playhead and paused/playing state, and shows the muted `playerInfo` note (`PREVIEW_AUDIO_NOTICE`); an error on the preview itself, or any other media error, shows the normal `playerNotice` (message from the error code) and never reloads, a `play()` that rejects after the fallback took over is ignored (`generation`), and a late result for a replaced waveform or another file is dropped. Peaks are scaled once to the whole recording (`scalePeaksToRecording`) with WaveSurfer `normalize: false`, so a zoomed-in quiet gap keeps its real height instead of each canvas stretching itself. The loading overlay (`waveformLoading`, `role="status"`) covers the fixed-height waveform area, so it never shifts the layout, and also shows during the fallback reload (`tests/frontend/precise_playback.test.js`).
- WebSocket (`/ws`) receives real-time progress updates during processing.
- **Add Files card:** header row `flex flex-wrap` with the title and the source tabs ("Import from folder" shown when an import dir is configured or the desktop dialog exists, default tab when enabled; "Upload"). The Upload tab keeps the progress bar and notice. **Import retries:** every stable folder row stays selectable and the server reuses an existing session; "In queue" and "Complete" (from the queue entry's `completed` status or the listing's `imported_status`) are distinct labels. The list refreshes after an import, after a queue removal is committed and after an export completes.

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
- **Album description** (setting `album_description`, key `ALBUM_DESCRIPTION`, default off; `PUT /api/config` accepts it): COMMENT / COMM is "Digitized from vinyl", or with the setting on, the album's English Wikipedia lead paragraph + LF + "Digitized from vinyl" (`album_description.compose_comment`). Lookup: Wikidata `haswbstatement:P1954=<master>` (else `P2206=<release>` only when the master search has no hits) -> exactly one item with an enwiki sitelink (several = none, never guessed) -> REST `page/summary` (`type == "standard"`). In-memory LRU per release id (`found`/`none` cached, `error` retried and logged at warning level; lock never held across HTTP), 12 s chain budget, stops at the first connection error. Only the backend contacts Wikidata/Wikipedia (descriptive User-Agent). The pipeline looks it up when `OutputSpec.album_description` (setting on and `ProcessRequest.album_description` not `false`) and passes `comment=` to `tag_file` only when found; the lookup runs after the overwrite-source check and the output-folder `mkdir`, so a refused or unwritable export makes no request; an error, or a crash inside the lookup (logged with traceback), is only a `Warning:` progress event, never a failed job. Release card preview: `loadAlbumDescription` (from `selectRelease`, `restoreEdits` when an Undo puts back a different release id, `saveConfig`, Retry; stale replies dropped, and a dropped reply that still owns the `loading` state resets it to idle so a later Undo reloads), shown only while `albumDescription.releaseId === selectedRelease.id`.

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
