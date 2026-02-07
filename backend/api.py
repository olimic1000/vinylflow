"""
FastAPI backend for Vinyl Digitizer web interface.
Provides REST API and WebSocket endpoints for the vinyl digitization workflow.
"""

import os
import uuid
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
from audio_processor import AudioProcessor
from metadata_handler import MetadataHandler

# Initialize FastAPI app
app = FastAPI(title="Vinyl Digitizer API", version="1.0.0")

# Enable CORS for development
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
    flac_compression=config.default_flac_compression
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
            for file_path in UPLOAD_DIR.glob("*.wav"):
                if datetime.fromtimestamp(file_path.stat().st_mtime) < cutoff:
                    file_path.unlink()
                    # Also delete associated files (previews, peaks, etc.)
                    file_id = file_path.stem
                    for pattern in ["*_preview_*.mp3", "*_peaks.json"]:
                        for related in UPLOAD_DIR.glob(f"{file_id}{pattern}"):
                            related.unlink()

            # Clean up from in-memory state
            expired_ids = [
                file_id for file_id, info in uploaded_files.items()
                if datetime.fromtimestamp(Path(info["path"]).stat().st_mtime) < cutoff
            ]
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
        except:
            disconnected.append(ws)

    # Clean up disconnected clients
    for ws in disconnected:
        websocket_connections.remove(ws)


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_connections.append(websocket)

    try:
        # Send initial connection confirmation
        await websocket.send_json({
            "type": "connected",
            "message": "WebSocket connected"
        })

        # Keep connection alive
        while True:
            # Wait for ping/pong or messages from client
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")

    except WebSocketDisconnect:
        websocket_connections.remove(websocket)


# API Routes
@app.get("/")
async def read_root():
    """Serve the main HTML page."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        with open(html_path) as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Vinyl Digitizer</h1><p>Frontend not found. Please create static/index.html</p>")


async def preconvert_to_mp3(file_id: str, file_path: Path):
    """
    Convert WAV to MP3 in background for faster waveform loading.
    Uses lower bitrate (128k) for quick conversion and smaller file size.
    """
    import subprocess

    mp3_path = UPLOAD_DIR / f"{file_id}_full.mp3"

    if not mp3_path.exists():
        try:
            # Convert using lower bitrate for speed (128k instead of 192k)
            await asyncio.to_thread(subprocess.run, [
                'ffmpeg', '-y',
                '-i', str(file_path),
                '-acodec', 'libmp3lame',
                '-b:a', '128k',  # Lower bitrate = faster conversion
                str(mp3_path)
            ], check=True, capture_output=True)
            print(f"✅ Pre-converted {file_id} to MP3 ({mp3_path.stat().st_size // 1024 // 1024}MB)")
        except Exception as e:
            print(f"❌ MP3 conversion failed for {file_id}: {e}")


@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """
    Upload WAV file(s) for processing.
    Returns file IDs and metadata.
    """
    uploaded = []

    for file in files:
        if not file.filename.lower().endswith('.wav'):
            continue

        # Generate unique file ID
        file_id = str(uuid.uuid4())
        file_path = UPLOAD_DIR / f"{file_id}.wav"

        # Save uploaded file
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # Get file metadata
        file_size = file_path.stat().st_size

        # Get duration using audio processor
        try:
            duration = audio_processor._get_duration(file_path)
        except:
            duration = 0

        # Store file info
        uploaded_files[file_id] = {
            "id": file_id,
            "filename": file.filename,
            "path": str(file_path),
            "size": file_size,
            "duration": duration,
            "status": "uploaded"
        }

        # Start MP3 conversion in background (for fast waveform loading)
        asyncio.create_task(preconvert_to_mp3(file_id, file_path))

        uploaded.append({
            "id": file_id,
            "filename": file.filename,
            "size": file_size,
            "duration": duration
        })

    return {"files": uploaded}


@app.post("/api/analyze")
async def analyze_file(request: AnalyzeRequest):
    """
    Analyze WAV file for silence detection and track boundaries.
    Returns detected track segments.
    """
    file_info = uploaded_files.get(request.file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])

    # Broadcast progress
    await broadcast_message({
        "type": "progress",
        "file_id": request.file_id,
        "step": "detecting",
        "progress": 0.1,
        "message": "Detecting silence boundaries..."
    })

    # Run silence detection
    try:
        tracks = audio_processor.detect_silence(file_path, verbose=False)

        # Convert to JSON-serializable format
        tracks_data = [
            {
                "number": i + 1,
                "start": track.start,
                "end": track.end,
                "duration": track.duration
            }
            for i, track in enumerate(tracks)
        ]

        # Update file info
        file_info["detected_tracks"] = tracks
        file_info["status"] = "analyzed"

        await broadcast_message({
            "type": "step_complete",
            "file_id": request.file_id,
            "step": "detection",
            "message": f"✓ Detected {len(tracks)} tracks"
        })

        return {"tracks": tracks_data}

    except Exception as e:
        await broadcast_message({
            "type": "error",
            "file_id": request.file_id,
            "message": f"Silence detection failed: {str(e)}",
            "recoverable": True
        })
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/preview/{file_id}/{track_number}")
async def preview_track(
    file_id: str,
    track_number: int,
    start: Optional[float] = None,
    end: Optional[float] = None
):
    """
    Generate and serve a preview of a detected track (first 30 seconds).
    Supports custom start/end times for manual adjustments.
    Returns audio file for playback.
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

    # Use custom start/end if provided, otherwise use detected values
    track_start = start if start is not None else track.start
    track_end = end if end is not None else track.end
    track_duration = track_end - track_start

    # Create preview directory
    preview_dir = UPLOAD_DIR / "previews"
    preview_dir.mkdir(exist_ok=True)

    # Generate preview file path with hash of params to handle custom times
    import hashlib
    params_hash = hashlib.md5(f"{track_start}_{track_end}".encode()).hexdigest()[:8]
    preview_path = preview_dir / f"{file_id}_track{track_number}_{params_hash}.mp3"

    # Extract first 30 seconds of track as MP3 preview
    try:
        duration = min(30, track_duration)  # Max 30 seconds preview

        subprocess.run([
            'ffmpeg', '-y',
            '-i', str(file_path),
            '-ss', str(track_start),
            '-t', str(duration),
            '-acodec', 'libmp3lame',
            '-b:a', '128k',
            str(preview_path)
        ], check=True, capture_output=True)

        return FileResponse(
            preview_path,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f"inline; filename=track{track_number}_preview.mp3"}
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview generation failed: {str(e)}")


@app.get("/api/waveform-peaks/{file_id}")
async def get_waveform_peaks(file_id: str):
    """
    Generate waveform peaks for visualization.
    Extracts ~3000 peaks from audio file using FFmpeg.
    Caches result for subsequent requests.
    Returns JSON with peaks array.
    """
    import subprocess
    import numpy as np

    file_info = uploaded_files.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])

    # Check for cached peaks
    peaks_cache_path = UPLOAD_DIR / f"{file_id}_peaks.json"
    if peaks_cache_path.exists():
        with open(peaks_cache_path, 'r') as f:
            cached_data = json.load(f)
            return JSONResponse(content=cached_data)

    try:
        # Extract audio samples using ffmpeg
        # Convert to mono, 8000 Hz sample rate for faster processing
        cmd = [
            'ffmpeg', '-i', str(file_path),
            '-f', 's16le',  # 16-bit PCM
            '-acodec', 'pcm_s16le',
            '-ac', '1',  # Mono
            '-ar', '8000',  # 8kHz sample rate
            '-'
        ]

        result = subprocess.run(cmd, capture_output=True, check=True)
        audio_data = np.frombuffer(result.stdout, dtype=np.int16)

        # Calculate number of samples per peak
        target_peaks = 3000
        samples_per_peak = max(1, len(audio_data) // target_peaks)

        # Downsample by taking max absolute value in each chunk
        peaks = []
        for i in range(0, len(audio_data), samples_per_peak):
            chunk = audio_data[i:i + samples_per_peak]
            if len(chunk) > 0:
                # Get max absolute value (represents amplitude)
                peak_value = np.max(np.abs(chunk))
                # Normalize to -1 to 1 range
                normalized = peak_value / 32768.0
                peaks.append(normalized)

        # Ensure we have at least some peaks
        if len(peaks) == 0:
            peaks = [0.0]

        # Prepare response
        response_data = {
            "peaks": peaks,
            "length": len(peaks),
            "duration": file_info.get("duration", 0)
        }

        # Cache the result
        with open(peaks_cache_path, 'w') as f:
            json.dump(response_data, f)

        return JSONResponse(content=response_data)

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {e.stderr.decode()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Peaks generation failed: {str(e)}")


@app.get("/api/audio/{file_id}")
async def get_audio_file(file_id: str):
    """
    Serve audio file for waveform playback.
    Serves original WAV file directly (MP3 conversion was truncating long files).
    """
    from fastapi.responses import FileResponse

    file_info = uploaded_files.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = Path(file_info["path"])

    # Serve WAV file directly - works reliably for all file lengths
    return FileResponse(
        file_path,
        media_type="audio/wav",
        headers={"Content-Disposition": f"inline; filename={file_info['filename']}"}
    )


@app.post("/api/search")
async def search_discogs(request: SearchRequest):
    """
    Search Discogs for releases.
    Returns list of matching releases with metadata.
    """
    try:
        releases = metadata_handler.search_releases(request.query, max_results=request.max_results)

        results = []
        for idx, release in releases:
            results.append({
                "id": release.id,
                "artist": release.artist,
                "title": release.title,
                "year": release.year,
                "label": release.label,
                "format": release.format,
                "cover_url": release.cover_url,
                "tracks": [
                    {
                        "position": t.position,
                        "title": t.title,
                        "duration": t.duration_str
                    }
                    for t in release.tracks
                ]
            })

        return {"results": results}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/process")
async def process_file(request: ProcessRequest):
    """
    Process file: split tracks, tag with metadata, and save as FLAC.
    This is an async operation - returns job_id immediately and broadcasts progress via WebSocket.
    """
    file_info = uploaded_files.get(request.file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")

    # Generate job ID
    job_id = str(uuid.uuid4())
    processing_jobs[job_id] = {
        "file_id": request.file_id,
        "status": "processing",
        "progress": 0
    }

    # Start background processing task
    asyncio.create_task(process_file_background(request, job_id))

    return {"job_id": job_id, "status": "processing"}


async def process_file_background(request: ProcessRequest, job_id: str):
    """Background task to process the file."""
    file_info = uploaded_files[request.file_id]
    file_path = Path(file_info["path"])

    try:
        # Get release from Discogs
        await broadcast_message({
            "type": "progress",
            "file_id": request.file_id,
            "step": "fetching",
            "progress": 0.2,
            "message": "Fetching release metadata from Discogs..."
        })

        release = metadata_handler.get_release_by_id(request.release_id)
        if not release:
            raise Exception("Failed to fetch Discogs release")

        # Build track mapping
        detected_tracks = file_info.get("detected_tracks", [])

        # Apply custom track boundaries if provided
        if request.track_boundaries:
            for boundary in request.track_boundaries:
                track_idx = boundary.number - 1
                if track_idx < len(detected_tracks):
                    detected_tracks[track_idx].start = boundary.start
                    detected_tracks[track_idx].end = boundary.end
                    detected_tracks[track_idx].duration = boundary.duration

        discogs_tracks = list(reversed(release.tracks)) if request.reversed else release.tracks

        for mapping in request.track_mapping:
            track_idx = mapping.detected - 1
            if track_idx < len(detected_tracks):
                detected_tracks[track_idx].vinyl_number = mapping.discogs

        # Create output directory
        output_base = Path(config.default_output_dir)
        album_folder = output_base / metadata_handler.create_album_folder_name(release)
        album_folder.mkdir(parents=True, exist_ok=True)

        # Download cover art
        await broadcast_message({
            "type": "progress",
            "file_id": request.file_id,
            "step": "cover_art",
            "progress": 0.3,
            "message": "Downloading cover art..."
        })

        cover_data = None
        if release.cover_url:
            cover_path = album_folder / "folder.jpg"
            if metadata_handler.download_cover_art(release.cover_url, cover_path):
                cover_data = metadata_handler.prepare_cover_for_embedding(cover_path)

        # Split and tag tracks
        output_files = []
        for i, track in enumerate(detected_tracks):
            progress = 0.4 + (i / len(detected_tracks)) * 0.5

            await broadcast_message({
                "type": "progress",
                "file_id": request.file_id,
                "step": "splitting",
                "progress": progress,
                "message": f"Processing track {i+1}/{len(detected_tracks)}..."
            })

            # Create temp FLAC file
            temp_output = album_folder / f"temp_{track.vinyl_number}.flac"

            # Split and convert to FLAC
            audio_processor.extract_track(file_path, track, temp_output)

            # Tag with metadata
            metadata_handler.tag_flac_file(temp_output, track, release, cover_data)

            # Rename to final filename
            final_filename = metadata_handler.create_track_filename(track, release)
            final_path = album_folder / final_filename
            temp_output.rename(final_path)

            output_files.append(final_filename)

        # Complete
        await broadcast_message({
            "type": "complete",
            "file_id": request.file_id,
            "output_path": str(album_folder),
            "tracks": output_files
        })

        processing_jobs[job_id]["status"] = "complete"
        processing_jobs[job_id]["progress"] = 1.0
        processing_jobs[job_id]["output_path"] = str(album_folder)

    except Exception as e:
        await broadcast_message({
            "type": "error",
            "file_id": request.file_id,
            "message": f"Processing failed: {str(e)}",
            "recoverable": False
        })
        processing_jobs[job_id]["status"] = "error"
        processing_jobs[job_id]["error"] = str(e)


@app.get("/api/queue")
async def get_queue():
    """Get current processing queue status."""
    return {
        "uploaded": list(uploaded_files.values()),
        "processing": list(processing_jobs.values())
    }


@app.delete("/api/queue/{file_id}")
async def remove_from_queue(file_id: str):
    """Remove file from queue."""
    if file_id in uploaded_files:
        # Delete file
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
        "flac_compression": audio_processor.flac_compression
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
    # Use PORT from environment (Railway sets this) or default to 8000
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
