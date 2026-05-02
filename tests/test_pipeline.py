"""Pipeline tests using fakes for AudioProcessor / MetadataHandler.

These verify orchestration: progress events fire in the right order,
the Session moves through Processing → Complete (or Failed) correctly,
and adapter calls happen with the expected arguments.  Real ffmpeg /
Mutagen behavior is covered separately by integration runs in the
desktop app — these tests exist so the orchestration can be changed
with confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from backend.sessions import (
    OutputSpec,
    ProgressEvent,
    Session,
    SessionState,
    SessionStore,
    pipeline,
)


@dataclass
class FakeRelease:
    id: int
    artist: str = "Test Artist"
    title: str = "Test Album"
    cover_url: Optional[str] = None


class FakeMetadata:
    def __init__(self, release: Optional[FakeRelease] = None) -> None:
        self.release = release
        self.tag_calls: list[tuple[Any, ...]] = []

    def get_release_by_id(self, release_id: int):
        return self.release

    def create_album_folder_name(self, release: FakeRelease) -> str:
        return f"{release.artist} - {release.title}"

    def download_cover_art(self, url: str, path: Path) -> bool:
        path.write_bytes(b"fake-cover")
        return True

    def prepare_cover_for_embedding(self, path: Path) -> bytes:
        return b"fake-cover-bytes"

    def tag_file(self, file_path, track, release, cover_data, output_format):
        self.tag_calls.append((file_path, track.vinyl_number, output_format))

    def create_track_filename(self, track, release, output_format) -> str:
        return f"{track.vinyl_number}-track.{output_format}"


class FakeAudio:
    def __init__(self) -> None:
        self.extract_calls: list[tuple[Path, str]] = []

    def extract_track(self, source, track, output, output_format, **kwargs):
        # Touch the file so the rename in pipeline.run succeeds.
        Path(output).write_bytes(b"fake-audio")
        self.extract_calls.append((Path(output), track.vinyl_number))


@dataclass
class FakeBoundary:
    number: int
    start: float
    end: float


@pytest.fixture
def session(tmp_path: Path) -> Session:
    src = tmp_path / "source.wav"
    src.write_bytes(b"fake-source")
    s = SessionStore().create(
        session_id="sess-1",
        source_audio=src,
        source_filename="source.wav",
        source_duration=120.0,
        source_size=len(b"fake-source"),
    )
    s.set_boundaries([FakeBoundary(1, 0.0, 60.0), FakeBoundary(2, 60.0, 120.0)])
    s.set_mappings({1: "A1", 2: "A2"})
    return s


@pytest.fixture
def spec(tmp_path: Path) -> OutputSpec:
    return OutputSpec(output_format="flac", output_dir=tmp_path / "out")


def test_happy_path(session: Session, spec: OutputSpec):
    audio = FakeAudio()
    metadata = FakeMetadata(release=FakeRelease(id=99, cover_url="https://example/cover.jpg"))
    events: list[ProgressEvent] = []

    pipeline.run(
        session=session,
        spec=spec,
        release_id=99,
        audio_processor=audio,
        metadata_handler=metadata,
        on_progress=events.append,
    )

    assert session.state == SessionState.COMPLETE
    assert session.last_run is not None
    assert session.last_run.output_files == ["A1-track.flac", "A2-track.flac"]
    assert session.last_run.output_folder == spec.output_dir / "Test Artist - Test Album"

    phases = [e.phase for e in events]
    assert phases == [
        "fetching_release",
        "downloading_cover",
        "extracting",
        "extracting",
        "complete",
    ]
    assert events[-1].fraction == 1.0

    extract_outputs = [c[1] for c in audio.extract_calls]
    assert extract_outputs == ["A1", "A2"]
    tag_formats = [c[2] for c in metadata.tag_calls]
    assert tag_formats == ["flac", "flac"]


def test_skips_cover_download_when_release_has_no_cover(session, spec):
    audio = FakeAudio()
    metadata = FakeMetadata(release=FakeRelease(id=99, cover_url=None))
    events: list[ProgressEvent] = []

    pipeline.run(
        session=session, spec=spec, release_id=99,
        audio_processor=audio, metadata_handler=metadata, on_progress=events.append,
    )

    assert session.state == SessionState.COMPLETE
    # Cover phase event still fires; just no download.
    assert "downloading_cover" in [e.phase for e in events]


def test_release_not_found_marks_session_failed_and_raises(session, spec):
    audio = FakeAudio()
    metadata = FakeMetadata(release=None)
    events: list[ProgressEvent] = []

    with pytest.raises(RuntimeError, match="Failed to fetch"):
        pipeline.run(
            session=session, spec=spec, release_id=99,
            audio_processor=audio, metadata_handler=metadata, on_progress=events.append,
        )

    # Session was never moved to PROCESSING (release lookup failed first),
    # so it stays READY — but an error event still fires for the UI.
    assert session.state == SessionState.READY
    assert events[-1].phase == "error"
    assert "Failed to fetch" in events[-1].message


def test_extract_failure_marks_session_failed(session, spec):
    class BrokenAudio:
        def extract_track(self, *a, **kw):
            raise RuntimeError("ffmpeg crashed")

    metadata = FakeMetadata(release=FakeRelease(id=99))
    events: list[ProgressEvent] = []

    with pytest.raises(RuntimeError, match="ffmpeg"):
        pipeline.run(
            session=session, spec=spec, release_id=99,
            audio_processor=BrokenAudio(),
            metadata_handler=metadata, on_progress=events.append,
        )

    assert session.state == SessionState.FAILED
    assert session.last_run is not None
    assert "ffmpeg" in (session.last_run.error or "")
    assert events[-1].phase == "error"


def test_pipeline_requires_boundaries(session, spec):
    session.boundaries = []
    metadata = FakeMetadata(release=FakeRelease(id=99))

    with pytest.raises(ValueError, match="boundaries"):
        pipeline.run(
            session=session, spec=spec, release_id=99,
            audio_processor=FakeAudio(),
            metadata_handler=metadata,
            on_progress=lambda e: None,
        )


def test_re_processing_a_complete_session(session, spec):
    audio = FakeAudio()
    metadata = FakeMetadata(release=FakeRelease(id=99))

    pipeline.run(
        session=session, spec=spec, release_id=99,
        audio_processor=audio, metadata_handler=metadata,
        on_progress=lambda e: None,
    )
    assert session.state == SessionState.COMPLETE

    # User adjusts mapping and re-runs.
    session.set_mappings({1: "B1", 2: "B2"})
    pipeline.run(
        session=session, spec=spec, release_id=99,
        audio_processor=audio, metadata_handler=metadata,
        on_progress=lambda e: None,
    )
    assert session.state == SessionState.COMPLETE
    assert session.last_run.output_files == ["B1-track.flac", "B2-track.flac"]
