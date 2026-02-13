"""
VinylFlow - FastAPI Backend

Provides REST API and WebSocket endpoints for automated vinyl record digitization.
Handles file uploads, audio analysis, metadata fetching, and track processing.
Supports WAV and AIFF input, with FLAC, MP3, and AIFF output.
"""

import os
import uuid
import copy
import shutil
from pathlib import Path
from typing import Dict, List, Optional
import asyncio
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, File, UploadFile, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Import our existing modules
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from audio_processor import AudioProcessor, Track, SUPPORTED_INPUT_EXTENSIONS, OUTPUT_FORMATS
from metadata_handler import MetadataHandler

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

# Global state management
uploaded_files: Dict[str, dict] = {}
processing_jobs: Dict[str, dict] = {}
websocket_connections: List[WebSocket] = []

# Initialize config and handlers
config = Config()
audio_processor = AudioProcessor(
    silence_threshold=config.default_silence_threshold,
    min_silence_duration=config.default_min_silence_duration,
    min_track_length=config.default_min_track_length,
    flac_compression=config.default_flac_compression,
)
metadata_handler = MetadataHandler(config.discogs_token, config.discogs_user_agent)

# Temp directory for uploads
UPLOAD_DIR = Path(__file__).parent.parent / "temp_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


# Background cleanup task
async def cleanup_old_files():
    """Delete uploaded files older than 24 hours to prevent disk filling."""
    while True:
        try:
            cutoff = datetime.now() - timedelta(hours=24)

            # Clean up audio files of all supported types
            for ext in ("*.wav", "*.aiff", "*.aif"):
                for file_path in UPLOAD_DIR.glob(ext):
                    try:
                        if datetime.fromtimestamp(file_path.stat().st_mtime) < cutoff:
                            file_path.unlink()
                            # Also delete associated files
                            file_id = file_path.stem
                            for pattern in ["*_preview_*.mp3", "*_peaks.json", "*_full.mp3"]:
                                for related in UPLOAD_DIR.glob(f"{file_id}{pattern}"):
                                    related.unlink(missing_ok=True)
                    except FileNotFoundError:
                        continue  # File already deleted

            # Clean up from in-memory state
            expired_ids = []
            for file_id, info in uploaded_files.items():
                try:
                    file_path = Path(info["path"])
                    if not file_path.exists():
                        expired_ids.append(file_id)
                    elif datetime.fromtimestamp(file_path.stat().st_mtime) < cutoff:
                        expired_ids.append(file_id)
                except (FileNotFoundError, OSError):
                    expired_ids.append(file_id)

            for file_id in expired_ids:
                del uploaded_files[file_id]

        except Exception as e:
            print(f"Cleanup error: {e}")

        # Run every 6 hours
        await asyncio.sleep(6 * 3600)


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


class ConfigUpdate(BaseModel):
    silence_threshold: Optional[float] = None
    min_silence_duration: Optional[float] = None
    min_track_length: Optional[float] = None
    output_dir: Optional[str] = None


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
        with open(html_path) as f:
            return HTMLResponse(content=f.read())
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
    import subprocess

    mp3_path = UPLOAD_DIR / f"{file_id}_full.mp3"

    if not mp3_path.exists():
        try:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "ffmpeg",
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

        # Generate unique file ID, preserve original extension
        file_id = str(uuid.uuid4())
        file_path = UPLOAD_DIR / f"{file_id}{file_ext}"

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

        # Store file info
        uploaded_files[file_id] = {
            "id": file_id,
            "filename": file.filename,
            "path": str(file_path),
            "size": file_size,
            "duration": duration,
            "status": "uploaded",
        }

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
    file_info = uploaded_files.get(request.file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])

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

        file_info["detected_tracks"] = tracks
        file_info["status"] = "analyzed"

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
    file_info = uploaded_files.get(request.file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    try:
        # Use existing duration-based splitting method
        processor = AudioProcessor(
            silence_threshold=config.DEFAULT_SILENCE_THRESHOLD,
            min_silence_duration=config.DEFAULT_MIN_SILENCE_DURATION,
            min_track_length=config.DEFAULT_MIN_TRACK_LENGTH,
        )

        tracks = processor.split_tracks_duration_based(
            file_path,
            request.discogs_durations,
            verbose=True
        )

        # Store tracks for this file
        file_info["detected_tracks"] = tracks
        file_info["detection_method"] = "duration_based"
        file_info["status"] = "analyzed"

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
    import subprocess

    file_info = uploaded_files.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    detected_tracks = file_info.get("detected_tracks", [])
    if track_number < 1 or track_number > len(detected_tracks):
        raise HTTPException(status_code=404, detail="Track not found")

    track = detected_tracks[track_number - 1]
    file_path = Path(file_info["path"])

    track_start = start if start is not None else track.start
    track_end = end if end is not None else track.end
    track_duration = track_end - track_start

    preview_dir = UPLOAD_DIR / "previews"
    preview_dir.mkdir(exist_ok=True)

    import hashlib

    params_hash = hashlib.md5(f"{track_start}_{track_end}".encode()).hexdigest()[:8]
    preview_path = preview_dir / f"{file_id}_track{track_number}_{params_hash}.mp3"

    try:
        duration = min(30, track_duration)

        subprocess.run(
            [
                "ffmpeg",
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
    import subprocess
    import numpy as np

    file_info = uploaded_files.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])

    peaks_cache_path = UPLOAD_DIR / f"{file_id}_peaks.json"
    if peaks_cache_path.exists():
        with open(peaks_cache_path, "r") as f:
            cached_data = json.load(f)
            return JSONResponse(content=cached_data)

    try:
        cmd = [
            "ffmpeg",
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
        ]

        result = subprocess.run(cmd, capture_output=True, check=True)
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
            "duration": file_info.get("duration", 0),
        }

        with open(peaks_cache_path, "w") as f:
            json.dump(response_data, f)

        return JSONResponse(content=response_data)

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {e.stderr.decode()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Peaks generation failed: {str(e)}")


@app.get("/api/audio/{file_id}")
async def get_audio_file(file_id: str):
    """Serve audio file for waveform playback."""
    from fastapi.responses import FileResponse

    file_info = uploaded_files.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])

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
    file_info = uploaded_files.get(request.file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    # Validate output format
    if request.output_format not in OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output format: {request.output_format}. "
            f"Choose from: {', '.join(OUTPUT_FORMATS.keys())}",
        )

    job_id = str(uuid.uuid4())
    processing_jobs[job_id] = {"file_id": request.file_id, "status": "processing", "progress": 0}

    asyncio.create_task(process_file_background(request, job_id))

    return {"job_id": job_id, "status": "processing"}


async def process_file_background(request: ProcessRequest, job_id: str):
    """Background task to process the file."""
    file_info = uploaded_files[request.file_id]
    file_path = Path(file_info["path"])
    output_format = request.output_format

    try:
        await broadcast_message(
            {
                "type": "progress",
                "file_id": request.file_id,
                "step": "fetching",
                "progress": 0.2,
                "message": "Fetching release metadata from Discogs...",
            }
        )

        release = metadata_handler.get_release_by_id(request.release_id)
        if not release:
            raise Exception("Failed to fetch Discogs release")

        # Build detected tracks from boundaries sent by frontend
        # This handles manually split tracks correctly
        if request.track_boundaries:
            # Create fresh Track objects from boundaries
            detected_tracks = []
            for boundary in request.track_boundaries:
                detected_tracks.append(
                    Track(
                        number=boundary.number,
                        start=boundary.start,
                        end=boundary.end,
                    )
                )
        else:
            # Fall back to original detected tracks if no boundaries provided
            detected_tracks = copy.deepcopy(file_info.get("detected_tracks", []))

        # Apply vinyl numbers from track mapping
        for mapping in request.track_mapping:
            # Find track by number (not by index, since numbers may not be sequential)
            track = next((t for t in detected_tracks if t.number == mapping.detected), None)
            if track:
                track.vinyl_number = mapping.discogs

        # Create output directory
        output_base = Path(config.default_output_dir)
        album_folder = output_base / metadata_handler.create_album_folder_name(release)
        album_folder.mkdir(parents=True, exist_ok=True)

        # Download cover art
        await broadcast_message(
            {
                "type": "progress",
                "file_id": request.file_id,
                "step": "cover_art",
                "progress": 0.3,
                "message": "Downloading cover art...",
            }
        )

        cover_data = None
        if release.cover_url:
            cover_path = album_folder / "folder.jpg"
            if metadata_handler.download_cover_art(release.cover_url, cover_path):
                cover_data = metadata_handler.prepare_cover_for_embedding(cover_path)

        # Split and tag tracks
        format_config = OUTPUT_FORMATS[output_format]
        ext = format_config["extension"]
        output_files = []

        for i, track in enumerate(detected_tracks):
            progress = 0.4 + (i / len(detected_tracks)) * 0.5

            await broadcast_message(
                {
                    "type": "progress",
                    "file_id": request.file_id,
                    "step": "splitting",
                    "progress": progress,
                    "message": f"Processing track {i+1}/{len(detected_tracks)}...",
                }
            )

            temp_output = album_folder / f"temp_{track.vinyl_number}{ext}"

            # Split and convert to chosen format
            audio_processor.extract_track(file_path, track, temp_output, output_format)

            # Tag with metadata
            metadata_handler.tag_file(temp_output, track, release, cover_data, output_format)

            # Rename to final filename
            final_filename = metadata_handler.create_track_filename(track, release, output_format)
            final_path = album_folder / final_filename
            temp_output.rename(final_path)

            output_files.append(final_filename)

        await broadcast_message(
            {
                "type": "complete",
                "file_id": request.file_id,
                "output_path": str(album_folder),
                "tracks": output_files,
            }
        )

        processing_jobs[job_id]["status"] = "complete"
        processing_jobs[job_id]["progress"] = 1.0
        processing_jobs[job_id]["output_path"] = str(album_folder)

    except Exception as e:
        await broadcast_message(
            {
                "type": "error",
                "file_id": request.file_id,
                "message": f"Processing failed: {str(e)}",
                "recoverable": False,
            }
        )
        processing_jobs[job_id]["status"] = "error"
        processing_jobs[job_id]["error"] = str(e)


@app.get("/api/queue")
async def get_queue():
    """Get current processing queue status."""
    return {"uploaded": list(uploaded_files.values()), "processing": list(processing_jobs.values())}


@app.delete("/api/queue/{file_id}")
async def remove_from_queue(file_id: str):
    """Remove file from queue."""
    if file_id in uploaded_files:
        file_path = Path(uploaded_files[file_id]["path"])
        if file_path.exists():
            file_path.unlink()
        del uploaded_files[file_id]
        return {"status": "removed"}
    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/config")
async def get_config():
    """Get current configuration."""
    return {
        "silence_threshold": audio_processor.silence_threshold,
        "min_silence_duration": audio_processor.min_silence_duration,
        "min_track_length": audio_processor.min_track_length,
        "output_dir": config.default_output_dir,
        "flac_compression": audio_processor.flac_compression,
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
        config.default_output_dir = updates.output_dir

    return await get_config()


# Mount static files (must be last)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
