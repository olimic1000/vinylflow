"""
VinylFlow - FastAPI Backend

Provides REST API and WebSocket endpoints for automated vinyl record digitization.
Handles file uploads, audio analysis, metadata fetching, and track processing.
Supports WAV and AIFF input, with FLAC, MP3, and AIFF output.
"""

import os
import uuid
import shutil
from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Optional
import asyncio
import json
from datetime import datetime, timedelta

import logging

from fastapi import FastAPI, File, UploadFile, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Import our existing modules
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from audio_processor import AudioProcessor, SUPPORTED_INPUT_EXTENSIONS, OUTPUT_FORMATS, run_ffmpeg
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

# Enable CORS
# Note: allow_origins=["*"] is fine for local/self-hosted use.
# If exposing publicly, restrict this to your specific domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def get_session_path(file_id: str, filename: str = None) -> Path:
    """Get path within session directory."""
    session_dir = UPLOAD_DIR / file_id
    return session_dir if filename is None else session_dir / filename


def cleanup_session(file_id: str) -> bool:
    """Drop a Session and its on-disk source folder."""
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


# Background cleanup task
async def cleanup_old_files():
    """Delete Sessions and source folders older than the configured TTL."""
    while True:
        try:
            ttl = config.temp_ttl_hours
            cutoff = datetime.now() - timedelta(hours=ttl)

            # Drop stale Sessions (the store skips PROCESSING entries),
            # then remove their on-disk source folders.
            for sid in session_store.reap_stale(ttl_hours=ttl):
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


class DurationBasedAnalyzeRequest(BaseModel):
    file_id: str
    discogs_durations: List[float]


class SearchRequest(BaseModel):
    query: str
    max_results: int = 5


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


class ConfigUpdate(BaseModel):
    silence_threshold: Optional[float] = None
    min_silence_duration: Optional[float] = None
    min_track_length: Optional[float] = None
    output_dir: Optional[str] = None


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


async def preconvert_to_mp3(file_id: str, file_path: Path):
    """
    Convert audio to MP3 in background for faster waveform loading.
    Uses lower bitrate (128k) for quick conversion and smaller file size.
    """
    mp3_path = get_session_path(file_id, "full.mp3")

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
                    str(mp3_path),
                ],
                check=True,
                capture_output=True,
            )
            print(
                f"Pre-converted {file_id} to MP3 ({mp3_path.stat().st_size // 1024 // 1024}MB)"
            )
        except Exception as e:
            print(f"MP3 conversion failed for {file_id}: {e}")


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

        # Save uploaded file
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # Get file metadata
        file_size = file_path.stat().st_size

        # Get duration using audio processor
        try:
            duration = audio_processor.get_audio_duration(file_path)
            if duration is None:
                duration = 0
        except Exception:
            duration = 0

        session_store.create(
            session_id=file_id,
            source_audio=file_path,
            source_filename=file.filename,
            source_duration=float(duration),
            source_size=file_size,
        )

        # Start MP3 conversion in background (for fast waveform loading)
        asyncio.create_task(preconvert_to_mp3(file_id, file_path))

        uploaded.append(
            {"id": file_id, "filename": file.filename, "size": file_size, "duration": duration}
        )

    return {"files": uploaded}


@app.post("/api/analyze")
async def analyze_file(request: AnalyzeRequest):
    """
    Analyze audio file for silence detection and track boundaries.
    Returns detected track segments.
    """
    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")

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
        tracks = audio_processor.detect_silence(file_path, verbose=False)

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


@app.post("/api/analyze-duration-based")
async def analyze_duration_based(request: DurationBasedAnalyzeRequest):
    """
    Create track boundaries based on Discogs track durations.
    Fallback method when silence detection fails.
    """
    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = session.source_audio
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    try:
        # Use existing duration-based splitting method
        processor = AudioProcessor(
            silence_threshold=config.default_silence_threshold,
            min_silence_duration=config.default_min_silence_duration,
            min_track_length=config.default_min_track_length,
        )

        tracks = processor.split_tracks_duration_based(
            file_path,
            request.discogs_durations,
            verbose=True
        )

        session.set_boundaries(tracks)

        # Broadcast via WebSocket
        await broadcast_message({
            "type": "analysis_complete",
            "file_id": request.file_id,
            "track_count": len(tracks),
            "method": "duration_based"
        })

        return {
            "tracks": [
                {
                    "number": track.number,
                    "start": track.start,
                    "end": track.end,
                    "duration": track.duration,
                }
                for track in tracks
            ]
        }

    except Exception as e:
        logger.error(f"Duration-based analysis failed: {e}", exc_info=True)
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

    boundaries = session.boundaries
    if track_number < 1 or track_number > len(boundaries):
        raise HTTPException(status_code=404, detail="Track not found")

    track = boundaries[track_number - 1]
    file_path = session.source_audio

    track_start = start if start is not None else track.start
    track_end = end if end is not None else track.end
    track_duration = track_end - track_start

    import hashlib

    params_hash = hashlib.md5(f"{track_start}_{track_end}".encode()).hexdigest()[:8]
    preview_path = get_session_path(file_id, f"preview_track{track_number}_{params_hash}.mp3")

    try:
        duration = min(30, track_duration)

        run_ffmpeg(
            [
                "-y",
                "-i",
                str(file_path),
                "-ss",
                str(track_start),
                "-t",
                str(duration),
                "-acodec",
                "libmp3lame",
                "-b:a",
                "128k",
                str(preview_path),
            ],
            check=True,
            capture_output=True,
        )

        return FileResponse(
            preview_path,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f"inline; filename=track{track_number}_preview.mp3"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview generation failed: {str(e)}")


@app.get("/api/waveform-peaks/{file_id}")
async def get_waveform_peaks(file_id: str):
    """Generate waveform peaks for visualization."""
    import numpy as np

    session = session_store.get(file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = session.source_audio

    peaks_cache_path = get_session_path(file_id, "peaks.json")
    if peaks_cache_path.exists():
        with open(peaks_cache_path, "r") as f:
            cached_data = json.load(f)
            return JSONResponse(content=cached_data)

    try:
        # text=False — we want raw PCM bytes on stdout, not utf-8 text.
        result = run_ffmpeg(
            [
                "-i",
                str(file_path),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-",
            ],
            text=False,
            capture_output=True,
            check=True,
        )
        audio_data = np.frombuffer(result.stdout, dtype=np.int16)

        target_peaks = 3000
        samples_per_peak = max(1, len(audio_data) // target_peaks)

        peaks = []
        for i in range(0, len(audio_data), samples_per_peak):
            chunk = audio_data[i : i + samples_per_peak]
            if len(chunk) > 0:
                peak_value = np.max(np.abs(chunk))
                normalized = peak_value / 32768.0
                peaks.append(normalized)

        if len(peaks) == 0:
            peaks = [0.0]

        response_data = {
            "peaks": peaks,
            "length": len(peaks),
            "duration": session.source_duration,
        }

        with open(peaks_cache_path, "w") as f:
            json.dump(response_data, f)

        return JSONResponse(content=response_data)

    except CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Peaks generation failed: {str(e)}")


@app.get("/api/audio/{file_id}")
async def get_audio_file(file_id: str):
    """Serve audio file for waveform playback."""
    from fastapi.responses import FileResponse

    session = session_store.get(file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = session.source_audio

    # Determine media type from extension
    ext = file_path.suffix.lower()
    media_types = {
        ".wav": "audio/wav",
        ".aiff": "audio/aiff",
        ".aif": "audio/aiff",
    }
    media_type = media_types.get(ext, "audio/wav")

    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename={file_info['filename']}"},
    )


@app.post("/api/search")
async def search_discogs(request: SearchRequest):
    """Search Discogs for releases."""
    try:
        releases = metadata_handler.search_releases(request.query, max_results=request.max_results)

        results = []
        for idx, release in releases:
            results.append(
                {
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
            )

        return {"results": results}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/process")
async def process_file(request: ProcessRequest):
    """
    Process file: split tracks, tag with metadata, and save.
    Supports FLAC, MP3, and AIFF output formats.
    """
    session = session_store.get(request.file_id)
    if session is None:
        raise HTTPException(status_code=404, detail="File not found")

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

    return await get_config()


@app.get("/api/status")
async def get_status():
    """Check app configuration status."""
    token_exists = bool(config.discogs_token and config.discogs_token != "your_token_here")

    response = {"discogs_configured": token_exists}

    # If configured, try to get username
    if token_exists:
        try:
            success, message = config.test_discogs_connection()
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
    uvicorn.run(app, host="0.0.0.0", port=port)
