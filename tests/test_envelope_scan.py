"""The cached energy envelope: Smart Rescan from it, envelope.npy reuse, GET /api/scan.

Envelopes are synthetic numpy arrays (10 ms frames) unless a test is marked as needing
ffmpeg.  ``get_envelope`` is replaced where a test only cares about what an endpoint does
with the envelope, not how it was computed."""

import shutil
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.api as api
from audio_processor import ENVELOPE_FRAME_S, AudioProcessor, run_ffmpeg

LOUD, QUIET, FADE = -15.0, -60.0, -35.0


def make_env(segments):
    """Envelope from [(seconds, level_db), ...] at ENVELOPE_FRAME_S per frame."""
    return np.concatenate(
        [np.full(int(round(sec / ENVELOPE_FRAME_S)), level, dtype=np.float32) for sec, level in segments]
    )


# --- tracks_for_count_from_envelope (Smart Rescan, in memory) -------------------------------

def test_rescan_ladder_escalates_until_the_count_matches():
    # The second gap is only -28 dB: missed at -30 dB, found at -25 dB (the second rung).
    env = make_env([(60, LOUD), (2, QUIET), (60, LOUD), (2, -28.0), (60, LOUD)])
    tracks, info = AudioProcessor(min_track_length=30).tracks_for_count_from_envelope(env, 3)
    assert len(tracks) == 3 and info["matched"] is True
    assert info["silence_threshold"] == -25.0


def test_rescan_ladder_returns_the_closest_when_nothing_matches():
    env = make_env([(60, LOUD), (2, QUIET), (60, LOUD)])
    tracks, info = AudioProcessor(min_track_length=30).tracks_for_count_from_envelope(env, 5)
    assert len(tracks) == 2 and info["matched"] is False


def test_rescan_keeps_a_fade_out_above_the_configured_threshold():
    # The -35 dB fade counts as silence at the ladder's -30 dB but is still audio at the
    # user's -40 dB: refine_cuts gives it back to the first track.
    env = make_env([(60, LOUD), (2, FADE), (2, QUIET), (60, LOUD)])
    tracks, _ = AudioProcessor(silence_threshold=-40, min_track_length=30).tracks_for_count_from_envelope(env, 2)
    assert tracks[0].end == pytest.approx(62.0, abs=0.02)


# --- envelope.npy cache -------------------------------------------------------------------

def test_envelope_is_decoded_once_and_then_read_from_the_cache(tmp_path, monkeypatch):
    calls = []

    def fake_compute(path):
        calls.append(path)
        return make_env([(1, LOUD)])

    monkeypatch.setattr(api, "get_session_path", lambda file_id, name=None: tmp_path / (name or ""))
    monkeypatch.setattr(api.audio_processor, "compute_envelope", fake_compute)
    first = api._load_or_compute_envelope("id", Path("source.wav"))
    second = api._load_or_compute_envelope("id", Path("source.wav"))
    assert len(calls) == 1 and (tmp_path / "envelope.npy").exists()
    assert np.array_equal(first, second)
    assert not list(tmp_path.glob("*.part"))   # written via a temp file + rename


def test_a_corrupt_cache_is_recomputed(tmp_path, monkeypatch):
    (tmp_path / "envelope.npy").write_bytes(b"not numpy")
    monkeypatch.setattr(api, "get_session_path", lambda file_id, name=None: tmp_path / (name or ""))
    monkeypatch.setattr(api.audio_processor, "compute_envelope", lambda path: make_env([(1, LOUD)]))
    assert len(api._load_or_compute_envelope("id", Path("source.wav"))) == 100


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_compute_envelope_from_a_real_file(tmp_path):
    """10 s tone, 3 s digital silence, 10 s tone: one gap at 10-13 s."""
    source = tmp_path / "side.wav"
    run_ffmpeg(["-y", "-f", "lavfi", "-i", "sine=f=440:d=10", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
                "-f", "lavfi", "-i", "sine=f=660:d=10", "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[out]",
                "-map", "[out]", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(source)],
               capture_output=True, check=True)
    from track_boundaries import gaps_from_envelope
    env = AudioProcessor().compute_envelope(source)
    assert len(env) == pytest.approx(2300, abs=2)
    (gap,) = gaps_from_envelope(env, ENVELOPE_FRAME_S, -40.0, 1.0)
    assert gap.start == pytest.approx(10.0, abs=0.02) and gap.end == pytest.approx(13.0, abs=0.02)


# --- endpoints ----------------------------------------------------------------------------

@pytest.fixture
def session(tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    source = tmp_path / "side.wav"
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename="side.wav",
                             source_duration=136, source_size=0)
    monkeypatch.setattr(api, "_envelope_tasks", {})
    monkeypatch.setattr(api, "get_session_path", lambda file_id, name=None: tmp_path / (name or ""))
    yield sid
    api.session_store.remove(sid)


def test_rescan_reads_the_envelope_without_decoding_again(session, monkeypatch):
    env = make_env([(4, QUIET), (60, LOUD), (2, QUIET), (60, LOUD), (10, QUIET)])

    async def cached_envelope(_session):
        return env

    def no_ffmpeg(*args, **kwargs):
        raise AssertionError("Smart Rescan must not run ffmpeg")

    monkeypatch.setattr(api, "get_envelope", cached_envelope)
    monkeypatch.setattr("audio_processor.run_ffmpeg", no_ffmpeg)
    body = TestClient(api.app).post("/api/rescan", json={"file_id": session, "expected_tracks": 2}).json()
    assert body["matched"] is True and len(body["tracks"]) == 2


def test_scan_is_not_ready_before_the_envelope_exists(session):
    assert TestClient(api.app).get(f"/api/scan/{session}").json() == {"ready": False}


def test_scan_reports_the_music_span(session, tmp_path):
    np.save(tmp_path / "envelope.npy", make_env([(8, QUIET), (120, LOUD), (8, QUIET)]))
    body = TestClient(api.app).get(f"/api/scan/{session}").json()
    assert body["ready"] is True
    assert body["music_start"] == pytest.approx(8.0, abs=0.05)
    assert body["music_end"] == pytest.approx(128.0, abs=0.05)
    assert body["music_duration"] == pytest.approx(120.0, abs=0.1)


def test_scan_unknown_file_is_404():
    assert TestClient(api.app).get(f"/api/scan/{uuid.uuid4()}").status_code == 404


def test_removing_a_recording_drops_its_envelope_task(session, monkeypatch):
    monkeypatch.setitem(api._envelope_tasks, session, object())
    assert TestClient(api.app).delete(f"/api/queue/{session}").status_code == 200
    assert session not in api._envelope_tasks
