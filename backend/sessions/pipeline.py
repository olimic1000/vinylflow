"""Pipeline — turns a Ready Session into a Complete Session.

Runs the mechanical sequence: fetch Release → write Output Folder →
download cover art → for each mapped boundary, ffmpeg-extract → tag →
rename.  Reports progress through a caller-supplied callback so
WebSocket and CLI can subscribe with tiny adapters.

The Pipeline mutates ``session`` (link_release, mark_processing,
mark_complete, mark_failed).  On any exception it marks the Session
``FAILED`` and re-raises.

This function is synchronous — wrap it in ``asyncio.to_thread`` if you
need the calling event loop to stay responsive while ffmpeg runs.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

# audio_processor and metadata_handler live at the project root, not
# under backend/.  api.py extends sys.path before importing them; do
# the same here so the package can be imported from a CLI entry point
# without depending on api.py to set up the path first.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from audio_processor import OUTPUT_FORMATS, AudioProcessor, Track
from metadata_handler import MetadataHandler

from .events import OutputSpec, ProcessingRun, ProgressEvent
from .session import Session, SessionState

ProgressCallback = Callable[[ProgressEvent], None]


def run(
    session: Session,
    spec: OutputSpec,
    release_id: int,
    audio_processor: AudioProcessor,
    metadata_handler: MetadataHandler,
    on_progress: ProgressCallback,
) -> None:
    if not session.boundaries:
        raise ValueError("session.boundaries must be set before pipeline.run")

    run_record = ProcessingRun(started_at=datetime.now())

    try:
        on_progress(
            ProgressEvent(
                phase="fetching_release",
                fraction=0.2,
                message="Fetching release metadata from Discogs...",
            )
        )

        release = metadata_handler.get_release_by_id(release_id)
        if not release:
            raise RuntimeError("Failed to fetch Discogs release")

        session.link_release(release)
        session.mark_processing(run_record)

        tracks = _build_tracks(session)

        album_folder = Path(spec.output_dir) / metadata_handler.create_album_folder_name(release)
        album_folder.mkdir(parents=True, exist_ok=True)

        on_progress(
            ProgressEvent(
                phase="downloading_cover",
                fraction=0.3,
                message="Downloading cover art...",
            )
        )

        cover_data = None
        if release.cover_url:
            cover_path = album_folder / "folder.jpg"
            if metadata_handler.download_cover_art(release.cover_url, cover_path):
                cover_data = metadata_handler.prepare_cover_for_embedding(cover_path)

        ext = OUTPUT_FORMATS[spec.output_format]["extension"]
        output_files: list[str] = []
        total = len(tracks)

        for i, track in enumerate(tracks):
            fraction = 0.4 + (i / total) * 0.5
            on_progress(
                ProgressEvent(
                    phase="extracting",
                    fraction=fraction,
                    message=f"Processing track {i + 1}/{total}...",
                    track_index=i + 1,
                    track_total=total,
                )
            )

            temp_output = album_folder / f"temp_{track.vinyl_number}{ext}"

            audio_processor.extract_track(
                session.source_audio,
                track,
                temp_output,
                spec.output_format,
                restoration_level=spec.restoration_level,
                hum_freq=spec.hum_freq,
            )

            metadata_handler.tag_file(
                temp_output, track, release, cover_data, spec.output_format
            )

            final_filename = metadata_handler.create_track_filename(
                track, release, spec.output_format
            )
            final_path = album_folder / final_filename
            temp_output.rename(final_path)

            output_files.append(final_filename)

        session.mark_complete(album_folder, output_files)

        on_progress(
            ProgressEvent(
                phase="complete",
                fraction=1.0,
                message="Complete!",
            )
        )

    except Exception as e:
        on_progress(
            ProgressEvent(
                phase="error",
                fraction=run_record.final_fraction,
                message=f"Processing failed: {e}",
                error=str(e),
            )
        )
        if session.state == SessionState.PROCESSING:
            session.mark_failed(str(e))
        raise


def _build_tracks(session: Session) -> list[Track]:
    """Convert Session boundaries into Tracks with vinyl_number applied.

    Boundaries may be either ``audio_processor.Track`` (set by /api/analyze)
    or pydantic ``TrackBoundary`` (set by /api/process when the user
    manually edited regions).  Both expose .number/.start/.end.
    """
    tracks: list[Track] = []
    for b in session.boundaries:
        t = Track(number=b.number, start=b.start, end=b.end)
        existing = getattr(b, "vinyl_number", None)
        if existing is not None:
            t.vinyl_number = existing
        if t.number in session.mappings:
            t.vinyl_number = session.mappings[t.number]
        tracks.append(t)
    return tracks
