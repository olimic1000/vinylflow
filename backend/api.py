"""
VinylFlow - FastAPI Backend

Provides REST API and WebSocket endpoints for automated vinyl record digitization.
Handles file uploads, audio analysis, metadata fetching, and track processing.
Supports WAV and AIFF input, with FLAC, MP3, and AIFF output.
"""

import os
import re
import uuid
import shutil
from pathlib import Path
from stat import S_ISREG
from subprocess import CalledProcessError
from typing import Annotated, List, Optional
import asyncio
import json
from datetime import datetime, timedelta

import logging

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from backend.security import SameOriginMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Import our existing modules
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import import_folder
from config import TRACK_NUMBERING_SCHEMES, Config
from audio_processor import (
    AudioProcessor, ENVELOPE_FRAME_S, SUPPORTED_INPUT_EXTENSIONS, OUTPUT_FORMATS, run_ffmpeg,
)
from track_boundaries import (
    MIN_ALIGNED_TRACK_S,
    boundaries_from_expected_durations,
    find_side_changes,
    gaps_from_envelope,
    guided_by_alignment,
    music_span,
    smooth_envelope,
)
from metadata_handler import MetadataHandler
from backend.sessions import (
    OutputSpec,
    ProgressEvent,
    Session,
    SessionState,
    SessionStore,
    pipeline,
)

# Initialize FastAPI app
app = FastAPI(title="VinylFlow API", version="1.0.0")

# The UI is same-origin, so no CORS: `*` let any website drive this local API.
# Reject foreign Host/Origin instead (DNS rebinding, cross-site form posts and WebSockets).
app.add_middleware(SameOriginMiddleware)

# Global state management.  ``session_store`` owns every Session;
# ``websocket_connections`` is the list of subscribers for progress
# broadcasts (orthogonal to per-Session state).  See CONTEXT.md for the
# Session lifecycle.
session_store = SessionStore()
websocket_connections: List[WebSocket] = []


def _session_status(session: Session) -> str:
    """Map Session.state → the UI status string the frontend expects.

    The frontend distinguishes "uploaded" (READY without boundaries) from
    "analyzed" (READY with boundaries detected) — derive that here so
    the wire format stays unchanged after the dict-level state went away.
    """
    if session.state == SessionState.PROCESSING:
        return "processing"
    if session.state == SessionState.COMPLETE:
        return "completed"
    if session.state == SessionState.FAILED:
        return "error"
    return "analyzed" if session.boundaries else "uploaded"


def _serialize_uploaded(session: Session) -> dict:
    """Wire shape returned by /api/queue and friends — matches the dict
    shape the frontend used to consume from ``uploaded_files``."""
    return {
        "id": session.id,
        "filename": session.source_filename,
        "path": str(session.source_audio),
        "in_place": session.in_place,
        "size": session.source_size,
        "duration": session.source_duration,
        "status": _session_status(session),
    }


def _serialize_job(session: Session) -> dict:
    """Wire shape returned by /api/process and /api/process/{job_id}.

    job_id == session_id post-Step-4: there's no separate per-run
    identifier any more.  Re-running a Session overwrites ``last_run``;
    callers polling the same id keep seeing fresh status.
    """
    run = session.last_run
    state = session.state
    if state == SessionState.PROCESSING:
        status = "processing"
    elif state == SessionState.COMPLETE:
        status = "complete"
    elif state == SessionState.FAILED:
        status = "error"
    else:
        status = "processing"  # only reached if /api/process called but pipeline not yet started

    out: dict = {
        "job_id": session.id,
        "file_id": session.id,
        "status": status,
        "progress": run.final_fraction if run is not None else 0.0,
        "message": (run.message if run is not None else "Starting processing..."),
    }
    if run is not None:
        if run.output_folder is not None:
            out["output_path"] = str(run.output_folder)
        if run.output_files:
            out["tracks"] = list(run.output_files)
        if run.error:
            out["error"] = run.error
    return out

# Initialize config and handlers
config = Config()
audio_processor = AudioProcessor(
    silence_threshold=config.default_silence_threshold,
    min_silence_duration=config.default_min_silence_duration,
    min_track_length=config.default_min_track_length,
    flac_compression=config.default_flac_compression,
)
metadata_handler = MetadataHandler(config.discogs_token, config.discogs_user_agent)

# Temp directory for uploads.
# In desktop/bundled mode the launcher sets VINYLFLOW_UPLOAD_DIR to a
# writable location inside the user's AppData folder.  Fall back to a path
# relative to the source tree when running in development.
_upload_dir_env = os.getenv("VINYLFLOW_UPLOAD_DIR")
UPLOAD_DIR = Path(_upload_dir_env) if _upload_dir_env else Path(__file__).parent.parent / "temp_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _require_source(session: Session) -> None:
    """410 when an in-place source has vanished from the import folder, 409 when it changed
    since its import: the session's caches and splits describe the old audio."""
    if not session.in_place:
        return
    try:
        stat = session.source_audio.stat()
    except OSError:
        stat = None
    if stat is None or not S_ISREG(stat.st_mode):
        raise HTTPException(
            status_code=410,
            detail=f"The source file is no longer in the import folder: {session.source_filename}",
        )
    if _source_changed(session, stat):
        raise HTTPException(
            status_code=409,
            detail=f"{session.source_filename} changed on disk since it was imported. "
            "Import it again to split the new recording.",
        )


def get_session_path(file_id: str, filename: str = None) -> Path:
    """Get path within session directory."""
    session_dir = UPLOAD_DIR / file_id
    return session_dir if filename is None else session_dir / filename


def _cancel_task(task) -> None:
    """Cancel an asyncio Task from the event loop or from a worker thread.

    ``DELETE /api/queue`` runs ``cleanup_session`` in ``asyncio.to_thread``, and
    ``Task.cancel()`` is not thread-safe (asyncio debug mode raises "Non-thread-safe
    operation"), so the cancel is handed to the task's loop; from the loop thread it
    lands on the next iteration."""
    if task is not None and not task.done():
        task.get_loop().call_soon_threadsafe(task.cancel)


def _forget_envelope(file_id: str) -> None:
    """Drop and cancel a removed Session's envelope scan.

    Cancelling cannot stop a decode already running in a worker thread: ffmpeg
    runs to the end and its cache write into the removed session dir fails with
    OSError (caught).  Only the broadcast and the work after it are skipped, and
    a request awaiting the task gets 404."""
    _envelope_errors.pop(file_id, None)
    _cancel_task(_envelope_tasks.pop(file_id, None))
    # The MP3 preview encode goes with it.  Cancelling stops waiting, not the ffmpeg thread: that
    # finishes into its .part file, which preconvert_to_mp3 unlinks on cancel (and the session
    # dir removal takes whatever is left).
    _cancel_task(_preconvert_tasks.pop(file_id, None))
    _preconvert_failed.discard(file_id)


def cleanup_session(file_id: str) -> bool:
    """Drop a Session and its on-disk source folder."""
    # Forget the scan first: removing a large folder takes a while, and a decode finishing
    # meanwhile must not announce a recording that is going away.  The finally below catches
    # a scan started during the removal (the Session still exists until then).
    _forget_envelope(file_id)
    session_dir = get_session_path(file_id)
    try:
        if session_dir.exists():
            shutil.rmtree(session_dir)
        session_store.remove(file_id)
        return True
    except FileNotFoundError:
        session_store.remove(file_id)
        return True
    except Exception as e:
        print(f"Cleanup failed for {file_id}: {e}")
        session_store.remove(file_id)
        return False
    finally:
        _forget_envelope(file_id)


def reap_expired() -> None:
    """One TTL pass: drop stale Sessions and orphan folders under UPLOAD_DIR.

    Only session dirs are removed; an in-place source lives outside UPLOAD_DIR."""
    ttl = config.temp_ttl_hours
    cutoff = datetime.now() - timedelta(hours=ttl)

    # Drop stale Sessions (the store skips PROCESSING entries),
    # then remove their on-disk source folders.
    for sid in session_store.reap_stale(ttl_hours=ttl):
        _forget_envelope(sid)
        session_dir = get_session_path(sid)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    # Drop orphan dirs — folders with no live Session that have
    # been on disk past the TTL (e.g. crash-recovery leftovers).
    for entry in UPLOAD_DIR.iterdir():
        if not entry.is_dir():
            continue
        if session_store.get(entry.name) is not None:
            continue
        try:
            if datetime.fromtimestamp(entry.stat().st_mtime) < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except (FileNotFoundError, OSError):
            pass


# Background cleanup task
async def cleanup_old_files():
    """Delete Sessions and source folders older than the configured TTL."""
    while True:
        try:
            reap_expired()

        except Exception as e:
            print(f"Cleanup error: {e}")

        # Run every 30 minutes
        await asyncio.sleep(30 * 60)


@app.on_event("startup")
async def startup_event():
    """Start background tasks on app startup."""
    asyncio.create_task(cleanup_old_files())


# Pydantic models for API requests/responses
class AnalyzeRequest(BaseModel):
    file_id: str


# Most tracks one rescan / guided detection may place; keep in step with MAX_GUIDED_TRACKS in app.js
MAX_EXPECTED_TRACKS = 99


class RescanRequest(BaseModel):
    file_id: str
    expected_tracks: int


class GuidedAnalyzeRequest(BaseModel):
    file_id: str
    expected_tracks: int
    # One entry per expected track; null for tracks Discogs has no length for.
    durations: Optional[List[Optional[float]]] = None
    # Internal track counts before Discogs side transitions (A -> B, B -> C, ...).
    side_breaks: Optional[List[int]] = None


# Discogs search pages one query may fetch ("Load more"); must match SEARCH_MAX_PAGES in app.js.
SEARCH_MAX_PAGES = 3
# Largest page size one search request may ask for.
SEARCH_MAX_RESULTS = 100


class SearchRequest(BaseModel):
    query: str
    max_results: int = Field(5, ge=1, le=SEARCH_MAX_RESULTS)
    page: int = Field(1, ge=1, le=SEARCH_MAX_PAGES)


class TrackMapping(BaseModel):
    detected: int
    discogs: str


class TrackBoundary(BaseModel):
    number: int
    start: float
    end: float
    duration: float


class ProcessRequest(BaseModel):
    file_id: str
    release_id: int
    track_mapping: List[TrackMapping]
    reversed: bool = False
    track_boundaries: Optional[List[TrackBoundary]] = None
    output_format: str = "flac"  # 'flac', 'mp3', or 'aiff'
    restoration_level: int = 0   # 0=none, 1=light clean, 2=full restore
    hum_freq: int = 50           # Electrical hum frequency in Hz (50=EU, 60=US)
    album_description: Optional[bool] = None  # Wikipedia lead paragraph in the Comment tag; the request can only turn the saved setting off


class ConfigUpdate(BaseModel):
    silence_threshold: Optional[float] = None
    min_silence_duration: Optional[float] = None
    min_track_length: Optional[float] = None
    output_dir: Optional[str] = None
    track_numbering: Optional[str] = None
    hide_support_ask: Optional[bool] = None
    album_description: Optional[bool] = None


class DiscogsSetupRequest(BaseModel):
    token: str
    user_agent: Optional[str] = "VinylFlow/1.0"


# WebSocket broadcast helper
async def broadcast_message(message: dict):
    """Broadcast message to all connected WebSocket clients."""
    disconnected = []
    for ws in websocket_connections:
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.append(ws)

    # Clean up disconnected clients
    for ws in disconnected:
        if ws in websocket_connections:
            websocket_connections.remove(ws)


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_connections.append(websocket)

    try:
        await websocket.send_json({"type": "connected", "message": "WebSocket connected"})

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        if websocket in websocket_connections:
            websocket_connections.remove(websocket)


# API Routes
@app.get("/")
async def read_root():
    """Serve the main HTML page."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<h1>VinylFlow</h1><p>Frontend not found. Please create static/index.html</p>"
    )


@app.get("/api/formats")
async def get_output_formats():
    """Return available output formats for the UI."""
    return {
        "formats": [
            {"id": fmt_id, "label": fmt["label"], "extension": fmt["extension"]}
            for fmt_id, fmt in OUTPUT_FORMATS.items()
        ]
    }


def _discard_part(part_path: Path) -> None:
    """Best-effort removal of a half-written preview file."""
    try:
        part_path.unlink(missing_ok=True)
    except OSError:
        pass


async def preconvert_to_mp3(file_id: str, file_path: Path):
    """
    Convert audio to MP3 in background for faster waveform loading.
    Uses lower bitrate (128k) for quick conversion and smaller file size.
    """
    mp3_path = get_session_path(file_id, "full.mp3")
    # Encode to a temp name and atomically rename on success: ffmpeg fills
    # the output over tens of seconds, and /api/audio must never see (and
    # then permanently cache) a half-written file.
    part_path = get_session_path(file_id, f"full.{uuid.uuid4().hex[:8]}.part")

    if not mp3_path.exists():
        try:
            await asyncio.to_thread(
                run_ffmpeg,
                [
                    "-y",
                    "-i",
                    str(file_path),
                    "-acodec",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    "-f",
                    "mp3",
                    str(part_path),
                ],
                check=True,
                capture_output=True,
            )
            os.replace(part_path, mp3_path)
            print(
                f"Pre-converted {file_id} to MP3 ({mp3_path.stat().st_size // 1024 // 1024}MB)"
            )
        except Exception as e:
            _discard_part(part_path)
            print(f"MP3 conversion failed for {file_id}: {e}")
        except BaseException:
            # Cancelled (recording removed): CancelledError skips the handler above and the
            # rename, so drop the half-written file here.  The ffmpeg thread may still be
            # writing it; on POSIX the unlink is fine, on Windows it is retried by the session
            # dir removal.
            _discard_part(part_path)
            raise


# MP3 preview encodes, one task per Session, so the background prep and an on-demand
# /api/audio request share a single ffmpeg run.  file_id -> Task
_preconvert_tasks: dict = {}
# Sessions whose encode ended without a full.mp3: later requests answer 503 at once instead of
# re-running ffmpeg over a possibly 1.4 GB source.  A new Session (re-import) starts clean.
_preconvert_failed: set = set()


def start_preconvert(file_id: str, file_path: Path) -> Optional["asyncio.Task"]:
    """Start (or join) the MP3 preview encode for a Session.

    Like start_envelope: 404 for a recording that is gone.  The task never raises (a failed
    encode is logged and leaves no full.mp3) and is dropped from the registry when it ends.
    A failed encode is remembered per session (_preconvert_failed): this then returns None
    instead of running ffmpeg over the source again, whoever asks (prep or a request); a
    re-import of the file clears it.  Callers must cope with None."""
    if session_store.get(file_id) is None:
        raise HTTPException(status_code=404, detail="File not found")
    if file_id in _preconvert_failed:
        return None
    task = _preconvert_tasks.get(file_id)
    if task is None:
        task = asyncio.create_task(preconvert_to_mp3(file_id, file_path))
        _preconvert_tasks[file_id] = task

        def _done(finished: "asyncio.Task") -> None:
            if _preconvert_tasks.get(file_id) is not finished:
                return   # forgotten: the recording was removed
            _preconvert_tasks.pop(file_id, None)
            if finished.cancelled():
                return
            if finished.exception() is not None:
                print(f"MP3 preview task failed for {file_id}: {finished.exception()}")
            if finished.exception() is not None or not get_session_path(file_id, "full.mp3").exists():
                _preconvert_failed.add(file_id)

        task.add_done_callback(_done)
    return task


# Energy envelope (per-frame RMS dBFS), computed once per Session in the
# background at upload and cached as envelope.npy in the session dir.  The
# task's result is the array itself, so endpoints just await it instead of
# re-decoding.  file_id -> Task[np.ndarray]
_envelope_tasks: dict = {}
_envelope_errors: dict = {}  # file_id -> message of the last failed scan (cleared on success)


def _write_cache_atomically(cache_path: Path, write) -> None:
    """Write a session cache via a temp file + atomic rename so a reader never
    sees a half-written file.  Best-effort: the session dir may have been
    removed while we were decoding."""
    part_path = cache_path.with_name(f"{cache_path.stem}.{uuid.uuid4().hex[:8]}.part")
    try:
        with open(part_path, "wb") as f:
            write(f)
        os.replace(part_path, cache_path)
    except OSError:
        part_path.unlink(missing_ok=True)


def _decode_and_cache(file_id: str, file_path: Path):
    """Blocking: the one decode per recording.  Writes envelope.npy and
    peaks_v2.json together and returns ``(envelope, peaks)``.  Two racing
    callers write identical deterministic content, so the atomic renames
    make that harmless."""
    import numpy as np

    db, peaks = audio_processor.compute_envelope_and_peaks(file_path)
    if not get_session_path(file_id).is_dir():
        # The recording was removed while it decoded.  The writes below would fail with an
        # OSError anyway; this makes the case explicit and skips the work.
        return db, peaks
    _write_cache_atomically(get_session_path(file_id, "envelope.npy"), lambda f: np.save(f, db))
    _write_cache_atomically(
        get_session_path(file_id, "peaks_v2.json"), lambda f: f.write(json.dumps(peaks).encode("utf-8"))
    )
    return db, peaks


def _load_or_compute_envelope(file_id: str, file_path: Path):
    """Blocking: load envelope.npy if cached, else decode and cache it
    (together with the waveform peaks, from the same decode)."""
    import numpy as np

    cache_path = get_session_path(file_id, "envelope.npy")
    if cache_path.exists():
        try:
            return np.load(cache_path)
        except Exception:
            cache_path.unlink(missing_ok=True)  # corrupt cache: recompute

    return _decode_and_cache(file_id, file_path)[0]


async def _run_envelope(file_id: str, file_path: Path):
    # A cancel (recording removed) is delivered at this await, so nothing after
    # it runs for a removed recording; the decode thread itself still finishes.
    return await asyncio.to_thread(_load_or_compute_envelope, file_id, file_path)


def start_envelope(file_id: str, file_path: Path) -> "asyncio.Task":
    """Start (or join) the envelope computation for a Session.

    404 for a recording that is gone: a request that passed its own session
    check just before a delete must not start a scan for a dead id."""
    if session_store.get(file_id) is None:
        raise HTTPException(status_code=404, detail="File not found")
    task = _envelope_tasks.get(file_id)
    if task is None:
        task = asyncio.create_task(_run_envelope(file_id, file_path))
        _envelope_tasks[file_id] = task

        def _done(t: "asyncio.Task") -> None:
            if _envelope_tasks.get(file_id) is not t:
                return  # forgotten: the recording was removed
            if t.cancelled() or t.exception() is not None:
                # Failed scans must not poison later requests: drop the task so
                # the next caller (GET /api/scan included) retries, and keep the
                # message for /api/scan to report.
                _envelope_tasks.pop(file_id, None)
                if not t.cancelled():
                    print(f"Envelope scan failed for {file_id}: {t.exception()}")
                    _envelope_errors[file_id] = str(t.exception()) or type(t.exception()).__name__
                return
            _envelope_errors.pop(file_id, None)
            if session_store.get(file_id) is None:
                return
            # Sent from here, not from inside the task, so a client reacting to
            # it always finds the task done and /api/scan ready.
            start, end = music_span(t.result(), ENVELOPE_FRAME_S)
            asyncio.ensure_future(broadcast_message({
                "type": "scan_complete",
                "file_id": file_id,
                "music_start": start,
                "music_end": end,
                "music_duration": max(0.0, end - start),
            }))

        task.add_done_callback(_done)
    return task


async def get_envelope(session):
    """Awaitable accessor: in-flight task -> cache on disk -> compute now.

    404 when the recording is removed while waiting (its task is cancelled)."""
    task = start_envelope(session.id, session.source_audio)
    try:
        # shield: a cancelled request must not cancel the shared background scan.
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.cancelled():
            raise HTTPException(status_code=404, detail="File not found")
        raise  # the request itself was cancelled


# Background prep runs for this many files at a time: "Select all" in the import folder must
# not queue hundreds of decodes in the thread pool ahead of the endpoints' own work.
PREP_CONCURRENCY = 2
_prep_slots = asyncio.Semaphore(PREP_CONCURRENCY)


async def _prepare_source(file_id: str, file_path: Path) -> None:
    """MP3 preview and envelope for a new Session, once a slot is free. A recording opened
    meanwhile starts its envelope on demand (start_envelope), which this then joins."""
    async with _prep_slots:
        if session_store.get(file_id) is None:
            return  # removed while it waited
        envelope = start_envelope(file_id, file_path)
        # shield: this prep being cancelled must not cancel an encode a request is waiting on
        preview_task = start_preconvert(file_id, file_path)   # None: it already failed, not re-run
        pending = [envelope] if preview_task is None else [asyncio.shield(preview_task), envelope]
        await asyncio.gather(*pending, return_exceptions=True)


async def _register_source(
    file_id: str, file_path: Path, filename: str, file_size: int, in_place: bool = False
) -> dict:
    """Measure a source file, create its Session and start the background
    tasks (MP3 preview, envelope). Shared by upload and import; returns the
    upload-entry wire shape. Derived files always go in the session dir."""
    get_session_path(file_id).mkdir(parents=True, exist_ok=True)

    # Get duration using audio processor (off the event loop)
    try:
        duration = await asyncio.to_thread(audio_processor.get_audio_duration, file_path)
        if duration is None:
            duration = 0
    except Exception:
        duration = 0

    session_store.create(
        session_id=file_id,
        source_audio=file_path,
        source_filename=filename,
        source_duration=float(duration),
        source_size=file_size,
        in_place=in_place,
        source_mtime=file_path.stat().st_mtime if in_place else None,
    )

    # MP3 conversion (fast waveform loading) and the energy envelope (music span + instant
    # silence rescans) in the background, a few files at a time; the response doesn't wait.
    asyncio.create_task(_prepare_source(file_id, file_path))

    return {
        "id": file_id,
        "filename": filename,
        "size": file_size,
        "duration": duration,
        "in_place": in_place,
    }


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """
    Upload audio file(s) for processing.
    Supports WAV and AIFF formats.
    Returns file IDs and metadata.
    """
    uploaded = []

    for file in files:
        # Check file extension against supported formats
        file_ext = Path(file.filename).suffix.lower()
        if file_ext not in SUPPORTED_INPUT_EXTENSIONS:
            continue

        # Generate unique file ID, create session folder
        file_id = str(uuid.uuid4())
        session_dir = get_session_path(file_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        file_path = session_dir / f"source{file_ext}"

        # Save uploaded file in chunks — never buffer a whole multi-hundred-MB
        # recording in memory.
        with open(file_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)

        uploaded.append(
            await _register_source(file_id, file_path, file.filename, file_path.stat().st_size)
        )

    return {"files": uploaded}


# --- Import from folder ------------------------------------------------------
# Files already on disk become Sessions that reference the file in place.
# The source is never copied into, or linked from, the session dir, so the
# rmtree-based cleanup paths can't touch it.

# Absolute paths granted by the desktop file dialog (in-process only; the
# pywebview bridge is not reachable over HTTP).
_granted_import_paths: set[Path] = set()


def _vet_granted(real: Path) -> Path:
    """Vet a dialog-granted path; the import folder's overlap rule covers ``names``, this ``paths``."""
    if UPLOAD_DIR.resolve() in real.parents:
        raise ValueError("File is inside the uploads folder")
    return import_folder.vet_file(real)


def grant_import_paths(paths) -> list[str]:
    """Allow POST /api/import to take these absolute paths. Returns the
    resolved paths that are regular supported files."""
    granted: list[str] = []
    for p in paths or []:
        try:
            real = _vet_granted(Path(str(p)).expanduser().resolve())
        except (ValueError, OSError):
            continue
        _granted_import_paths.add(real)
        granted.append(str(real))
    return granted


def _import_dir() -> Optional[Path]:
    """The configured import folder, or None when import is disabled."""
    value = config.import_dir
    return Path(value).expanduser() if value else None


def _import_state() -> tuple[Optional[Path], Optional[str]]:
    """(import dir or None, error or None)."""
    d = _import_dir()
    if d is None:
        return None, None
    return d, import_folder.dir_error(d, UPLOAD_DIR)


def _source_changed(session: Session, stat: os.stat_result) -> bool:
    """True when the file ``stat`` describes is no longer the one this session's caches and
    boundaries describe."""
    return stat.st_size != session.source_size or stat.st_mtime != session.source_mtime


def _session_for_source(real: Path) -> Optional[Session]:
    """The session reading ``real`` in place, if any."""
    for s in session_store.list():
        try:
            if s.source_audio.resolve() == real:
                return s
        except OSError:
            continue
    return None


class ImportRequest(BaseModel):
    names: Optional[List[str]] = None
    paths: Optional[List[str]] = None


@app.get("/api/import")
async def list_import():
    """List audio files in the import folder (read-only)."""
    d, error = _import_state()
    if d is None:
        return {"enabled": False, "dir": None, "error": None, "files": []}
    out = {"enabled": True, "dir": str(d), "error": error, "files": []}
    if error:
        return out
    entries = await asyncio.to_thread(import_folder.list_audio, d)
    for e in entries:
        s = _session_for_source(e.pop("path"))
        e["imported_id"] = s.id if s else None
        e["imported_status"] = _session_status(s) if s else None
    out["files"] = entries
    return out


@app.post("/api/import")
async def import_files(req: ImportRequest):
    """Create Sessions for files in the import folder (``names``) or files
    granted by the desktop dialog (``paths``), without copying them."""
    if req.names is None and req.paths is None:
        raise HTTPException(status_code=400, detail="Provide names and/or paths")

    d, error = _import_state()
    imported: list[dict] = []
    skipped: list[dict] = []

    # (label, resolver) so both kinds share the vet + register logic.
    candidates: list[tuple[str, Path]] = []
    for name in req.names or []:
        try:
            if d is None:
                raise ValueError("Import folder is not configured")
            if error:
                raise ValueError(error)
            candidates.append((name, import_folder.resolve_name(d, name)))
        except (ValueError, OSError) as e:
            skipped.append({"name": str(name), "reason": str(e)})
    for p in req.paths or []:
        try:
            real = Path(str(p)).expanduser().resolve()
            if real not in _granted_import_paths:
                raise ValueError("Path was not chosen in the file dialog")
            candidates.append((str(p), _vet_granted(real)))
        except (ValueError, OSError) as e:
            skipped.append({"name": str(p), "reason": str(e)})

    for label, real in candidates:
        try:
            if not import_folder.is_stable(real):
                skipped.append({"name": label, "reason": "File is still being copied"})
                continue
            existing = _session_for_source(real)
            replaces = None
            if existing is not None:
                if not _source_changed(existing, real.stat()):
                    _preconvert_failed.discard(existing.id)   # re-adding a file retries its preview
                    imported.append({
                        "id": existing.id,
                        "status": _session_status(existing),
                        "filename": existing.source_filename,
                        "size": existing.source_size,
                        "duration": existing.source_duration,
                        "in_place": True,
                    })
                    continue
                if existing.state == SessionState.PROCESSING:
                    skipped.append({"name": label, "reason": "File changed while it was exporting"})
                    continue
                # A re-recorded side under the same name: its caches and boundaries belong to
                # the old audio, and the export would cut the new audio at the old splits.
                cleanup_session(existing.id)
                replaces = existing.id
            entry = await _register_source(
                str(uuid.uuid4()), real, real.name, real.stat().st_size, in_place=True
            )
            if replaces:
                entry["replaces"] = replaces
            imported.append(entry)
        except Exception as e:
            logger.warning("Import of %s failed: %s", label, e)
            skipped.append({"name": label, "reason": "Could not read the file"})

    return {"files": imported, "skipped": skipped}


@app.post("/api/analyze")
async def analyze_file(request: AnalyzeRequest):
    """
    Analyze audio file for silence detection and track boundaries.
    Returns detected track segments.
    """
    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    file_path = session.source_audio

    await broadcast_message(
        {
            "type": "progress",
            "file_id": request.file_id,
            "step": "detecting",
            "progress": 0.1,
            "message": "Detecting silence boundaries...",
        }
    )

    try:
        # detect_silence is a full-file ffmpeg decode — run it off the event
        # loop so WebSocket pings and other requests keep flowing.
        tracks = await asyncio.to_thread(
            audio_processor.detect_silence, file_path, verbose=False
        )

        tracks_data = [
            {"number": i + 1, "start": track.start, "end": track.end, "duration": track.duration}
            for i, track in enumerate(tracks)
        ]

        session.set_boundaries(tracks)

        await broadcast_message(
            {
                "type": "step_complete",
                "file_id": request.file_id,
                "step": "detection",
                "message": f"Detected {len(tracks)} tracks",
            }
        )

        return {"tracks": tracks_data}

    except Exception as e:
        await broadcast_message(
            {
                "type": "error",
                "file_id": request.file_id,
                "message": f"Silence detection failed: {str(e)}",
                "recoverable": True,
            }
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rescan")
async def rescan_file(request: RescanRequest):
    """
    Smart Rescan: sweep silence-detection settings automatically until the
    analysis yields the expected number of tracks.  Falls back to the
    closest result when no setting matches exactly.
    """
    if not 1 <= request.expected_tracks <= MAX_EXPECTED_TRACKS:
        raise HTTPException(status_code=422, detail=f"expected_tracks must be between 1 and {MAX_EXPECTED_TRACKS}")

    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    await broadcast_message(
        {
            "type": "progress",
            "file_id": request.file_id,
            "step": "detecting",
            "progress": 0.1,
            "message": f"Rescanning for {request.expected_tracks} tracks...",
        }
    )

    try:
        # Ladder sweep runs in memory over the cached envelope (awaiting the
        # background scan if it is still decoding) — no per-rung ffmpeg run.
        db = await get_envelope(session)
        tracks, scan_info = await asyncio.to_thread(
            audio_processor.tracks_for_count_from_envelope, db, request.expected_tracks
        )

        tracks_data = [
            {"number": i + 1, "start": track.start, "end": track.end, "duration": track.duration}
            for i, track in enumerate(tracks)
        ]

        session.set_boundaries(tracks)

        await broadcast_message(
            {
                "type": "step_complete",
                "file_id": request.file_id,
                "step": "detection",
                "message": f"Smart rescan detected {len(tracks)} tracks",
            }
        )

        return {"tracks": tracks_data, "matched": scan_info["matched"], "scan": scan_info}

    except HTTPException:
        raise
    except Exception as e:
        await broadcast_message(
            {
                "type": "error",
                "file_id": request.file_id,
                "message": f"Smart rescan failed: {str(e)}",
                "recoverable": True,
            }
        )
        raise HTTPException(status_code=500, detail=str(e))


MAX_SIDE_CHANGES = 8  # most side flips a scan request may ask for


# Annotated keeps a plain default of 0, so direct calls (tests/test_envelope_scan.py) work; a
# Query(...) default would arrive there as a Query object.
@app.get("/api/scan/{file_id}")
async def get_scan(file_id: str, side_changes: Annotated[int, Query(ge=0, le=MAX_SIDE_CHANGES)] = 0):
    """Music span (lead-in and run-out trimmed) from the background envelope
    scan; never blocks on it: ``{"ready": false}`` while it is still decoding.

    ``side_changes=N`` (> 0) also returns ``side_changes: [[start, end], ...]``, the N longest
    non-music stretches inside the music span in time order (fewer when the record has fewer).

    A failed scan is restarted here, and the response carries the last failure
    as ``"error"`` until a scan succeeds."""
    session = session_store.get(file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    # Never block: get_envelope joins a running task, and the cache file exists
    # before the task finishes (between the thread's os.replace and its return),
    # so the running check must come before the cache check.
    task = _envelope_tasks.get(file_id)
    if task is not None:
        if not task.done() or task.cancelled():
            return {"ready": False}
        if task.exception() is not None:  # failed; its done callback has not dropped it yet
            return {"ready": False, "error": str(task.exception())}
        db = task.result()
    elif get_session_path(file_id, "envelope.npy").exists():
        db = await get_envelope(session)  # cached on disk: quick load
    else:
        # No scan running and none cached: (re)start it.  A decode that always
        # fails restarts once per call (no backoff), so poll this sparingly.
        error = _envelope_errors.get(file_id)
        start_envelope(file_id, session.source_audio)
        return {"ready": False, "error": error} if error else {"ready": False}

    start, end = music_span(db, ENVELOPE_FRAME_S)
    scan = {
        "ready": True,
        "music_start": start,
        "music_end": end,
        "music_duration": max(0.0, end - start),
    }
    if side_changes > 0:
        # Smoothing a long recording's envelope takes a moment: keep it off the event loop
        stretches = await asyncio.to_thread(
            lambda: find_side_changes(smooth_envelope(db, ENVELOPE_FRAME_S), ENVELOPE_FRAME_S, start, end, side_changes))
        scan["side_changes"] = [[round(c.start, 2), round(c.end, 2)] for c in stretches]
    return scan


@app.post("/api/analyze-guided")
async def analyze_guided(request: GuidedAnalyzeRequest):
    """
    Place ``expected_tracks`` boundaries using the cached envelope and the
    Discogs lengths.  Best-match alignment runs first (a length that is
    unknown costs nothing; with none at all, sides and clear gaps decide);
    the gap/scale model is the fallback when every length is known, and
    count-based detection (the Smart Rescan ladder) when neither model can
    place the tracks.  Count-based detection may find fewer tracks than asked
    for: ``matched`` is then False.
    """
    if not 1 <= request.expected_tracks <= MAX_EXPECTED_TRACKS:
        raise HTTPException(status_code=422, detail=f"expected_tracks must be between 1 and {MAX_EXPECTED_TRACKS}")

    if request.durations is not None and len(request.durations) != request.expected_tracks:
        raise HTTPException(status_code=422, detail="durations must have one entry per expected track")

    side_breaks = request.side_breaks or []
    if side_breaks != sorted(set(side_breaks)) or any(
        not 1 <= index < request.expected_tracks for index in side_breaks
    ):
        raise HTTPException(status_code=422, detail="side_breaks must be sorted unique internal track indices")

    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    await broadcast_message(
        {
            "type": "progress",
            "file_id": request.file_id,
            "step": "detecting",
            "progress": 0.1,
            "message": f"Placing {request.expected_tracks} tracks...",
        }
    )

    try:
        db = await get_envelope(session)

        # A length that can't be real counts as unknown: zero, negative, longer than the
        # recording, or non-finite (JSON 1e400 parses as inf; NaN fails the comparison too).
        recording_s = len(db) * ENVELOPE_FRAME_S
        known = [d if d is not None and 0 < d <= recording_s else None for d in request.durations or []]
        all_known = bool(known) and None not in known
        try_align = len(known) >= 2  # no length at all: sides and clear gaps decide (even split)

        def _compute():
            music_start, music_end = music_span(db, ENVELOPE_FRAME_S)
            result = None
            if try_align:
                result = guided_by_alignment(
                    db, ENVELOPE_FRAME_S, music_start, music_end, known, side_breaks=side_breaks,
                )
            if result is None and all_known:
                gaps = gaps_from_envelope(
                    db,
                    ENVELOPE_FRAME_S,
                    audio_processor.SMART_RESCAN_LADDER[0][0],
                    audio_processor.SMART_RESCAN_LADDER[0][1],
                )
                placed = boundaries_from_expected_durations(
                    gaps, music_start, music_end, known, side_breaks=side_breaks,
                )
                # It places every track whatever the span, clamping surplus cuts to the end
                # of the music, so a track too short to be one means the tracks don't fit.
                if min(track.duration for track in placed.tracks) >= MIN_ALIGNED_TRACK_S:
                    result = placed
            if result is not None:
                # Aligned cuts touch, so for them only the outer edges widen; the gap/scale
                # model's snapped cuts can widen into their gaps.
                refined = audio_processor.refine_tracks(db, result.tracks)
                return refined, True, result, music_start, music_end
            tracks, info = audio_processor.tracks_for_count_from_envelope(
                db, request.expected_tracks
            )
            return tracks, info["matched"], None, music_start, music_end

        tracks, matched, guided, music_start, music_end = await asyncio.to_thread(_compute)
        session.set_boundaries(tracks)

        boundaries = []
        if guided is not None:
            boundaries = [
                {
                    "index": b.index,
                    "expected": b.expected,
                    "snapped": b.snapped,
                    "position": b.position,
                    "gap": {"start": b.gap.start, "end": b.gap.end} if b.gap else None,
                }
                for b in guided.boundaries
            ]

        await broadcast_message(
            {
                "type": "step_complete",
                "file_id": request.file_id,
                "step": "detection",
                "message": f"Guided detection placed {len(tracks)} tracks",
            }
        )

        return {
            "tracks": [
                {"number": i + 1, "start": t.start, "end": t.end, "duration": t.duration}
                for i, t in enumerate(tracks)
            ],
            # No lengths = placed by count (the UI draws no ghost markers and says so); model tells which.
            "method": "durations" if guided is not None and guided.model != "align-even" else "count",
            "matched": matched,
            "boundaries": boundaries,
            "scale": guided.scale if guided else None,
            "scale_clamped": guided.scale_clamped if guided else None,
            "model": guided.model if guided else None,
            "gap_s": guided.gap_s if guided else None,
            "music_start": music_start,
            "music_end": music_end,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Guided analysis failed: {e}", exc_info=True)
        await broadcast_message(
            {
                "type": "error",
                "file_id": request.file_id,
                "message": f"Guided detection failed: {str(e)}",
                "recoverable": True,
            }
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/preview/{file_id}/{track_number}")
async def preview_track(
    file_id: str, track_number: int, start: Optional[float] = None, end: Optional[float] = None
):
    """
    Generate and serve a preview of a detected track (first 30 seconds).
    Supports custom start/end times for manual adjustments.
    """
    from fastapi.responses import FileResponse

    session = session_store.get(file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    boundaries = session.boundaries
    file_path = session.source_audio

    if start is not None and end is not None:
        # Explicit range (e.g. a manual split): no stored boundary needed.
        track_start, track_end = start, end
    else:
        if track_number < 1 or track_number > len(boundaries):
            raise HTTPException(status_code=404, detail="Track not found")
        track = boundaries[track_number - 1]
        track_start = start if start is not None else track.start
        track_end = end if end is not None else track.end
    track_duration = track_end - track_start

    if track_duration <= 0 or track_start < 0:
        raise HTTPException(status_code=400, detail="Invalid preview range: end must be after start")

    import hashlib

    params_hash = hashlib.md5(f"{track_start}_{track_end}".encode()).hexdigest()[:8]
    preview_path = get_session_path(file_id, f"preview_track{track_number}_{params_hash}.mp3")

    try:
        duration = min(30, track_duration)

        # Serve from cache when this exact start/end was already rendered.
        # Encode to a unique temp name + atomic rename so concurrent or
        # interrupted encodes never leave a truncated file in the cache.
        if not preview_path.exists():
            part_path = preview_path.with_name(f"{preview_path.stem}.{uuid.uuid4().hex[:8]}.part")
            # -ss before -i: input-side seek, so encoding a preview near the
            # end of a side no longer decodes everything before it.
            try:
                await asyncio.to_thread(
                    run_ffmpeg,
                    [
                        "-y",
                        "-ss",
                        str(track_start),
                        "-t",
                        str(duration),
                        "-i",
                        str(file_path),
                        "-acodec",
                        "libmp3lame",
                        "-b:a",
                        "128k",
                        "-f",
                        "mp3",
                        str(part_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                os.replace(part_path, preview_path)
            except Exception:
                part_path.unlink(missing_ok=True)
                raise

        return FileResponse(
            preview_path,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f"inline; filename=track{track_number}_preview.mp3"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview generation failed: {str(e)}")


def _cached_peaks_response(cache_path: Path):
    """Stream the cached peaks file as is: at 10 ms per bin it is ~13 MB for a 4 h album, and
    parsing then re-serialising that would block the event loop.  The file is replaced
    atomically, so a reader never sees a partial one."""
    from fastapi.responses import FileResponse

    return FileResponse(cache_path, media_type="application/json")


@app.get("/api/waveform-peaks/{file_id}")
async def get_waveform_peaks(file_id: str):
    """Equal-width waveform peaks (<= 10 ms per bin) for visualization."""

    session = session_store.get(file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    file_path = session.source_audio

    # peaks_v2.json replaces the old peaks.json (3,000 bins per recording, which
    # lost sub-second timing on albums); an old cache is simply ignored.
    peaks_cache_path = get_session_path(file_id, "peaks_v2.json")
    if peaks_cache_path.exists():
        return _cached_peaks_response(peaks_cache_path)

    try:
        # Prep's decode writes the peaks too: wait for it (or its cache) rather than
        # starting a second decode.  Decoding here is only the rare leftover: the envelope
        # was cached (or loaded) but its peaks write failed.  Sessions live in memory, so
        # no older on-disk session lacks peaks; concurrent requests in that case each decode,
        # and a recording removed during it still gets this reply (nothing is written).
        await get_envelope(session)
        if peaks_cache_path.exists():
            return _cached_peaks_response(peaks_cache_path)
        _, response_data = await asyncio.to_thread(_decode_and_cache, file_id, file_path)

        return JSONResponse(content=response_data)

    except HTTPException:
        raise   # 404: the recording was removed while waiting for its decode
    except CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Peaks generation failed: {str(e)}")


# Browsers ask for long byte ranges and hang up after a few MB: Chromium loads with an open-ended
# `bytes=N-` and seeks with closed ones to the end of the file, WebKit (the macOS app) always
# uses closed ones.  Starlette's FileResponse keeps reading to the end of the range (newer
# versions >= 1.8 also stop when the client disconnects, older installs do not), so a seek would
# read up to the rest of a 1.4 GB recording.  Every range longer than this is answered with
# its first AUDIO_RANGE_CAP_BYTES bytes as a 206 with a matching Content-Range; browsers just ask
# for the next piece.
AUDIO_RANGE_CAP_BYTES = 8 * 1024 * 1024
_RANGE_UNIT = re.compile(r"^\s*bytes\s*=(.*)$", re.IGNORECASE | re.DOTALL)
# Range numbers are judged by value, not by how they are written: leading zeros are dropped first.
# A start longer than _MAX_RANGE_DIGITS is left to Starlette unparsed (a 5000-digit int made int()
# and then the response fail with a 500).  An end that long is past any file, so it reads as the end
# of the file (Starlette would clamp it there too) and is capped like an open range.
_MAX_RANGE_DIGITS = 18
_RANGE_ITEM = re.compile(r"^(\d+)\s*-\s*(\d*)$")
_SUFFIX_RANGE_ITEM = re.compile(r"^-\s*\d+$")

# How long a request for the MP3 preview waits for the encode.  ffmpeg encodes ~150x real time
# (a 40-minute side takes ~15 s), so 180 s covers a multi-hour album.  The wait is on the
# encode itself: an on-demand request does not queue behind _prep_slots (the user is waiting on
# this file), so it only ever waits for its own encode.  The ``wait=0`` probe also starts an
# encode outside PREP_CONCURRENCY; that is bounded by the user opening files.  After that the client is told to retry;
# the encode keeps running.
AUDIO_PREVIEW_WAIT_S = 180.0
PREVIEW_PREPARING_DETAIL = "The audio preview is still being prepared. Try again in a moment."
PREVIEW_RETRY_AFTER_S = "3"   # Retry-After of the "still being prepared" 503 (the client polls on it)


def _cap_long_ranges(scope: dict, file_size: int, cap: Optional[int] = None) -> dict:
    """A copy of the ASGI scope whose Range header has every item longer than the cap closed to
    its first ``cap`` bytes (default AUDIO_RANGE_CAP_BYTES).

    Only the first Range header is kept (that is the one Starlette's Headers.get reads anyway, so
    a later duplicate cannot sneak an uncapped range past the rewrite).  Items are parsed here:
    empty ones (``bytes=100-,``, ``bytes=0-,-``) are dropped, which Starlette treats as one open
    range.  Suffix items (``-N``, the last N bytes) are left as they are: they are bounded by N
    and browsers use them only for small tail probes.  An item starting at or past the end of
    the file is left for Starlette to answer 416.  If the header cannot be parsed, or nothing
    needs changing, the scope is returned for Starlette to handle (the original is never mutated).

    This bounds what browsers send (single ``N-`` / ``N-M`` items), not every possible header: odd
    non-browser forms (``bytes=+5-``, ``bytes=1_0-``, many small ranges, suffix ranges) are parsed
    by Starlette itself and are not capped."""
    cap = AUDIO_RANGE_CAP_BYTES if cap is None else cap
    headers = scope["headers"]
    range_positions = [index for index, (name, _) in enumerate(headers) if name == b"range"]
    if not range_positions:
        return scope
    only_first = [pair for index, pair in enumerate(headers) if index not in range_positions[1:]]
    deduplicated = scope if len(range_positions) == 1 else {**scope, "headers": only_first}
    unit_match = _RANGE_UNIT.match(headers[range_positions[0]][1].decode("latin-1"))
    if unit_match is None:
        return deduplicated
    items = []
    for raw_item in unit_match.group(1).split(","):
        item = raw_item.strip()
        if item in ("", "-"):
            continue
        closed = _RANGE_ITEM.match(item)
        if closed is not None:
            try:
                start_digits = closed.group(1).lstrip("0") or "0"
                if len(start_digits) > _MAX_RANGE_DIGITS:
                    return deduplicated
                first = int(start_digits)
                end_digits = closed.group(2).lstrip("0") or ("0" if closed.group(2) else "")
                open_ended = not end_digits or len(end_digits) > _MAX_RANGE_DIGITS
                last = file_size - 1 if open_ended else int(end_digits)
            except ValueError:   # defensive: the regex already limits the digits
                return deduplicated
            if first < file_size and min(last, file_size - 1) - first + 1 > cap:
                item = f"{first}-{first + cap - 1}"
        elif _SUFFIX_RANGE_ITEM.match(item) is None:
            return deduplicated   # not a shape we know: leave it to Starlette
        items.append(item)
    if not items:
        return deduplicated
    rewritten = f"bytes={','.join(items)}".encode("latin-1")
    if rewritten == headers[range_positions[0]][1]:
        return deduplicated
    new_headers = [(name, rewritten if name == b"range" else value) for name, value in only_first]
    return {**scope, "headers": new_headers}


class _CappedRangeFileResponse(FileResponse):
    """FileResponse that caps long Range requests (see AUDIO_RANGE_CAP_BYTES).  The header is
    rewritten before Starlette sees it, so 206, Content-Range, Content-Length, 416 and If-Range
    handling stay Starlette's.  Needs ``stat_result`` (the caller stats once, off the loop)."""

    async def __call__(self, scope, receive, send):
        if self.stat_result is not None:
            scope = _cap_long_ranges(scope, self.stat_result.st_size)
        await super().__call__(scope, receive, send)


async def _ensure_preview(session, wait: bool = True) -> Path:
    """The session's MP3 preview, waiting for its encode when it does not exist yet.

    503 when the encode is still running after AUDIO_PREVIEW_WAIT_S (or at once with
    ``wait=False``: the status probe never blocks; it still starts an encode nobody started,
    with a Retry-After header) or when it failed (remembered per session, so it is not re-run).
    Only the MP3 is ever returned: the source may be a WAV the browser just failed to play, or
    an AIFF."""
    mp3_path = get_session_path(session.id, "full.mp3")
    if mp3_path.exists():
        return mp3_path
    preview_failed = HTTPException(
        status_code=503, detail="The audio preview could not be made for this recording. "
        "Remove it from the queue and add it again to retry.")
    preparing = HTTPException(
        status_code=503, detail=PREVIEW_PREPARING_DETAIL, headers={"Retry-After": PREVIEW_RETRY_AFTER_S})
    if session.id in _preconvert_failed:
        raise preview_failed
    task = start_preconvert(session.id, session.source_audio)
    if not wait:
        raise preparing
    try:
        # shield: a client hanging up (or timing out here) must not cancel the shared encode.
        await asyncio.wait_for(asyncio.shield(task), AUDIO_PREVIEW_WAIT_S)
    except asyncio.TimeoutError:
        raise preparing
    except asyncio.CancelledError:
        if task.cancelled():
            raise HTTPException(status_code=404, detail="File not found")  # recording removed
        raise  # the request itself was cancelled
    if not mp3_path.exists():
        raise preview_failed
    return mp3_path


@app.get("/api/audio/{file_id}")
async def get_audio_file(file_id: str, precise: bool = False, wait: bool = True):
    """Stream the source WAV for precise editing, or the MP3 preview otherwise.

    ``wait=0`` is the status probe for the preview: it never waits for the encode (see _ensure_preview)."""
    session = session_store.get(file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    if precise and session.source_audio.suffix.lower() == ".wav":
        # Cut-point playback uses the same source as the waveform, removing
        # MP3 seeking/decoder behavior from this path. FileResponse streams
        # byte ranges; the album is not decoded into browser JS memory.
        file_path = session.source_audio
        media_type = "audio/wav"
        filename = session.source_filename
    else:
        # Browser-compatible preview only: this URL is the fallback for a source the browser
        # cannot play (AIFF always, a WAV on some builds), so it never serves the source and
        # never pins it.  Pin the MP3 so an in-progress range request keeps its bytes.
        pinned = session.playback_audio
        if pinned is None or pinned.suffix.lower() != ".mp3" or not pinned.exists():
            session.playback_audio = await _ensure_preview(session, wait)
        file_path = session.playback_audio
        media_type = "audio/mpeg"
        filename = Path(session.source_filename).stem + ".mp3"

    # filename= lets Starlette RFC-5987-encode non-latin-1 characters
    # (e.g. "Björk – Debut") — a hand-built header crashes on them.
    try:
        # One stat, off the event loop; the response then neither stats again nor blocks.
        stat_result = await asyncio.to_thread(os.stat, file_path)
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="The audio file is no longer available.")
    return _CappedRangeFileResponse(
        file_path,
        stat_result=stat_result,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )


def _serialize_release(release) -> dict:
    """Full release payload of /api/release/{id} (tracks always set)."""
    return {
        "id": release.id,
        "artist": release.artist,
        "title": release.title,
        "year": release.year,
        "label": release.label,
        "format": release.format,
        "cover_url": release.cover_url,
        "uri": release.uri,
        "tracks": [
            {"position": t.position, "title": t.title, "duration": t.duration_str}
            for t in release.tracks
        ],
    }


@app.post("/api/search")
async def search_discogs(request: SearchRequest):
    """Search Discogs; basic cards only (``tracks: null`` = not loaded yet).

    Full tracklists come from GET /api/release/{id}, which the UI calls per card.
    ``has_more``: a full page below SEARCH_MAX_PAGES, so "Load more" may ask for the next one.
    """
    try:
        # One search round-trip (rate-limit sleep inside) -- keep it off the event loop.
        basic = await asyncio.to_thread(
            metadata_handler.search_basic, request.query, request.max_results, request.page
        )
        has_more = request.page < SEARCH_MAX_PAGES and len(basic) >= request.max_results
        return {"results": [{**result, "tracks": None} for result in basic], "has_more": has_more}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/release/{release_id}")
async def get_release(release_id: int):
    """Full release details including tracklist (server-side cached)."""
    release = await asyncio.to_thread(metadata_handler.get_release_by_id, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found or Discogs unavailable")
    return _serialize_release(release)


@app.get("/api/release/{release_id}/description")
async def get_release_description(release_id: int):
    """The album's Wikipedia lead paragraph (cached); ``disabled`` without any lookup when the setting is off."""
    if not config.album_description:
        return {"status": "disabled"}
    release = await asyncio.to_thread(metadata_handler.get_release_by_id, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found or Discogs unavailable")
    result = await asyncio.to_thread(
        metadata_handler.album_descriptions.lookup, release.id, getattr(release, "master_id", None)
    )
    return {"status": result.status, "text": result.text, "title": result.title, "url": result.url}


@app.post("/api/process")
async def process_file(request: ProcessRequest):
    """
    Process file: split tracks, tag with metadata, and save.
    Supports FLAC, MP3, and AIFF output formats.
    """
    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")
    _require_source(session)

    # Validate output format
    if request.output_format not in OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output format: {request.output_format}. "
            f"Choose from: {', '.join(OUTPUT_FORMATS.keys())}",
        )

    if request.track_boundaries:
        session.set_boundaries(list(request.track_boundaries))
    session.set_mappings({m.detected: m.discogs for m in request.track_mapping})

    # job_id == session_id post-Step-4: re-runs reuse the same id, since
    # ``last_run`` is overwritten on each invocation and there's no
    # per-run tracking surface to preserve across restarts.
    asyncio.create_task(process_file_background(request))

    return {"job_id": session.id, "status": "processing"}


# Map Pipeline phase names → existing UI step names so the frontend
# WebSocket consumer keeps working unchanged during the strangler.
_PHASE_TO_UI_STEP = {
    "fetching_release": "fetching",
    "downloading_cover": "cover_art",
    "extracting": "splitting",
}


def _wants_album_description(request: ProcessRequest) -> bool:
    """The saved setting, which a request may only turn off (``False``); it can never switch it on."""
    return bool(config.album_description) and request.album_description is not False


async def process_file_background(request: ProcessRequest):
    """Run the Pipeline for ``request`` and bridge progress events to the
    WebSocket broadcaster.

    Per-run state lives on ``session.last_run`` (mutated by the Pipeline);
    this coroutine just translates Pipeline ProgressEvents into the
    WebSocket message shapes the frontend expects, plus updates
    ``last_run.message`` / ``last_run.final_fraction`` so polling
    /api/process/{job_id} returns fresh status.

    The Pipeline itself is sync; we run it in a worker thread so ffmpeg
    no longer blocks the FastAPI event loop.  Progress callbacks are
    re-marshalled onto the main loop via ``run_coroutine_threadsafe``.
    """
    file_id = request.file_id
    session = session_store.get(file_id)
    if session is None:
        return

    spec = OutputSpec(
        output_format=request.output_format,
        output_dir=Path(config.default_output_dir),
        flac_compression=config.default_flac_compression,
        restoration_level=request.restoration_level,
        hum_freq=request.hum_freq,
        track_numbering=config.track_numbering,
        album_description=_wants_album_description(request),
    )

    loop = asyncio.get_running_loop()

    async def emit(event: ProgressEvent) -> None:
        run = session.last_run
        if event.phase == "complete":
            await broadcast_message({
                "type": "complete",
                "file_id": file_id,
                "output_path": str(run.output_folder) if run and run.output_folder else "",
                "tracks": list(run.output_files) if run else [],
            })
        elif event.phase == "error":
            await broadcast_message({
                "type": "error",
                "file_id": file_id,
                "message": event.message,
                "recoverable": False,
            })
        else:
            if run is not None:
                run.final_fraction = event.fraction
                run.message = event.message
            await broadcast_message({
                "type": "progress",
                "file_id": file_id,
                "step": _PHASE_TO_UI_STEP.get(event.phase, event.phase),
                "progress": event.fraction,
                "message": event.message,
            })

    def on_progress(event: ProgressEvent) -> None:
        # pipeline.run executes in a worker thread; bounce each event back
        # to the main loop so broadcasts stay single-threaded.
        asyncio.run_coroutine_threadsafe(emit(event), loop)

    try:
        await asyncio.to_thread(
            pipeline.run,
            session,
            spec,
            request.release_id,
            audio_processor,
            metadata_handler,
            on_progress,
        )
    except Exception:
        # The Pipeline already emitted an "error" event and marked the
        # Session FAILED.  Nothing to clean up here.
        pass


@app.get("/api/process/{job_id}")
async def get_process_job(job_id: str):
    """Get processing job status and progress for polling fallbacks.

    ``job_id`` is the Session id post-Step-4: every process invocation
    against the same Session updates ``last_run`` in place.
    """
    session = session_store.get(job_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # _serialize_job copes with last_run==None — returns a "processing"
    # stub for the brief window between /api/process kickoff and the
    # Pipeline calling mark_processing.
    return _serialize_job(session)


@app.get("/api/queue")
async def get_queue():
    """Get current Session queue + per-Session run status."""
    sessions = session_store.list()
    return {
        "uploaded": [_serialize_uploaded(s) for s in sessions],
        "processing": [_serialize_job(s) for s in sessions if s.last_run is not None],
    }


@app.delete("/api/queue/{file_id}")
async def remove_from_queue(file_id: str):
    """Remove a Session and its on-disk source folder."""
    if session_store.get(file_id) is None:
        raise HTTPException(status_code=404, detail="File not found")
    await asyncio.to_thread(cleanup_session, file_id)
    return {"status": "removed"}


@app.delete("/api/temp/clear-all")
async def clear_all_temp():
    """Drop every Session and source folder except those mid-Pipeline."""
    cleared = 0
    keep_ids = {s.id for s in session_store.list() if s.state == SessionState.PROCESSING}

    for entry in list(UPLOAD_DIR.iterdir()):
        if entry.is_dir() and entry.name not in keep_ids:
            try:
                shutil.rmtree(entry)
                cleared += 1
            except Exception as e:
                print(f"Failed to remove {entry.name}: {e}")

    for session in session_store.list():
        if session.id not in keep_ids:
            session_store.remove(session.id)
            _forget_envelope(session.id)

    return {"status": "cleared", "sessions_removed": cleared}


@app.get("/api/config")
async def get_config():
    """Get current configuration."""
    return {
        "silence_threshold": audio_processor.silence_threshold,
        "min_silence_duration": audio_processor.min_silence_duration,
        "min_track_length": audio_processor.min_track_length,
        "flac_compression": audio_processor.flac_compression,
        "output_dir": config.default_output_dir,
        "track_numbering": config.track_numbering,
        "album_description": config.album_description,
        "import_dir": str(_import_dir()) if _import_dir() else None,  # read-only
        "hide_support_ask": config.hide_support_ask,
    }


@app.put("/api/config")
async def update_config(updates: ConfigUpdate):
    """Update configuration parameters."""
    if updates.silence_threshold is not None:
        audio_processor.silence_threshold = updates.silence_threshold
    if updates.min_silence_duration is not None:
        audio_processor.min_silence_duration = updates.min_silence_duration
    if updates.min_track_length is not None:
        audio_processor.min_track_length = updates.min_track_length
    if updates.output_dir is not None:
        output_dir = str(Path(updates.output_dir).expanduser())
        config.default_output_dir = output_dir
        config.save_output_dir(output_dir)
    if updates.track_numbering is not None:
        if updates.track_numbering in TRACK_NUMBERING_SCHEMES:
            config.track_numbering = updates.track_numbering
            config.save_track_numbering(updates.track_numbering)
    if updates.hide_support_ask is not None:
        config.hide_support_ask = updates.hide_support_ask
        config.save_hide_support_ask(updates.hide_support_ask)

    if updates.album_description is not None:
        config.album_description = updates.album_description
        config.save_album_description(updates.album_description)

    return await get_config()


@app.get("/api/status")
async def get_status():
    """Check app configuration status."""
    token_exists = bool(config.discogs_token and config.discogs_token != "your_token_here")

    response = {"discogs_configured": token_exists}

    # If configured, try to get username
    if token_exists:
        try:
            success, message = await asyncio.to_thread(config.test_discogs_connection)
            if success and ": " in message:
                username = message.split(": ")[-1]
                response["discogs_username"] = username
        except:
            pass

    return response


@app.post("/api/setup/discogs-token")
async def setup_discogs_token(request: DiscogsSetupRequest):
    """
    Configure Discogs API token via web UI.
    Validates token, saves to persistent config, reinitializes without restart.
    """
    try:
        # Validate token
        import discogs_client

        test_client = discogs_client.Client(
            request.user_agent,
            user_token=request.token
        )

        try:
            identity = test_client.identity()
            username = identity.username
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Discogs token: {str(e)}"
            )

        # Save to persistent config
        if not config.save_token(request.token, request.user_agent):
            raise HTTPException(
                status_code=500,
                detail="Failed to save token to config file"
            )

        # Reload config and reinitialize handler
        config.reload()
        global metadata_handler
        metadata_handler.reinitialize(config.discogs_token, config.discogs_user_agent)

        return {
            "success": True,
            "username": username,
            "message": f"Connected as {username}"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Setup failed: {str(e)}"
        )


# Mount static files (must be last)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
