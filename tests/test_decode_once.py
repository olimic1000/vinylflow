"""One decode per recording: envelope + waveform peaks from a single spooled pass.

The ``legacy_*`` functions below are verbatim copies of the two pre-change
implementations (``compute_envelope`` decoding into memory, and the blockwise
``compute_waveform_peaks``).  They are the reference the single-pass function
must match exactly; the envelope is compared with ``np.array_equal`` (bit
identical float32, since each frame is computed independently of the others).
"""
import asyncio
import shutil
from subprocess import CalledProcessError
import io
import json
import subprocess
import tempfile
import uuid
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import audio_processor as ap_module
import backend.api as api
from audio_processor import ENVELOPE_FRAME_S, ENVELOPE_SAMPLE_RATE, AudioProcessor, run_ffmpeg
from track_boundaries import envelope_from_pcm

FRAME = int(ENVELOPE_SAMPLE_RATE * ENVELOPE_FRAME_S)
DECODE_ARGS = ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(ENVELOPE_SAMPLE_RATE), "-"]


def legacy_envelope(path):
    result = run_ffmpeg(["-i", str(path)] + DECODE_ARGS, text=False, capture_output=True, check=True)
    return envelope_from_pcm(np.frombuffer(result.stdout, dtype=np.int16), FRAME)


def legacy_peaks(path):
    peaks = []
    with tempfile.TemporaryFile() as pcm:
        run_ffmpeg(["-v", "error", "-i", str(path)] + DECODE_ARGS,
                   text=False, stdout=pcm, stderr=subprocess.PIPE, check=True)
        sample_count = pcm.tell() // 2
        peak_count = (sample_count + FRAME - 1) // FRAME
        for first in range(0, peak_count, 1000):
            last = min(first + 1000, peak_count)
            edges = np.arange(first, last + 1, dtype=np.int64) * sample_count // peak_count
            pcm.seek(int(edges[0]) * 2)
            block = pcm.read(int(edges[-1] - edges[0]) * 2)
            samples = np.frombuffer(block, dtype="<i2").astype(np.int32)
            values = np.maximum.reduceat(np.abs(samples), edges[:-1] - edges[0]) / 32768.0
            peaks.extend(np.round(values, 6).tolist())
    return {"peaks": peaks or [0.0], "length": len(peaks) or 1, "duration": sample_count / ENVELOPE_SAMPLE_RATE}


def write_wav(path, seconds, channels, sampwidth, rate=44100, seed=0):
    """Noise with varying loudness, a digital-silence stretch and a full-scale spike."""
    rng = np.random.default_rng(seed)
    count = int(seconds * rate)
    envelope = np.abs(np.sin(np.linspace(0, 20, count)))
    scale = 2 ** (8 * sampwidth - 1) - 1
    data = (rng.uniform(-1, 1, (count, channels)) * envelope[:, None] * scale * 0.6)
    data[count // 3: count // 3 + rate // 2] = 0
    data[count // 2] = -scale - 1
    data = data.astype("<i2") if sampwidth == 2 else data.astype("<i4")
    if sampwidth == 3:
        raw = np.ascontiguousarray(data.astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]).tobytes()
    else:
        raw = data.tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(rate)
        out.writeframes(raw)


# seconds: short (< one block at 3 frames of 10 ms), not a whole number of 10 ms frames
# at 8 kHz (partial tail), and long enough to span several injected blocks.
@pytest.mark.parametrize("channels,sampwidth", [(1, 2), (2, 2), (2, 3), (1, 3)])
@pytest.mark.parametrize("seconds,block_frames", [(0.0173, 1000), (1.2345, 1000), (7.3337, 50), (3.001, 7)])
def test_single_pass_equals_the_two_old_decodes(tmp_path, channels, sampwidth, seconds, block_frames):
    source = tmp_path / "s.wav"
    write_wav(source, seconds, channels, sampwidth)
    env, peaks = AudioProcessor().compute_envelope_and_peaks(source, block_frames=block_frames)
    old_env, old_peaks = legacy_envelope(source), legacy_peaks(source)
    assert env.dtype == old_env.dtype == np.float32
    assert np.array_equal(env, old_env)
    assert peaks == old_peaks
    # Thin wrappers keep their callers working.
    assert np.array_equal(AudioProcessor().compute_envelope(source), old_env)
    assert AudioProcessor().compute_waveform_peaks(source) == old_peaks


def test_decode_is_spooled_to_a_file_not_captured(tmp_path, monkeypatch):
    source = tmp_path / "s.wav"
    write_wav(source, 1.0, 1, 2)
    calls = []
    real = ap_module.run_ffmpeg

    def spy(args, **kwargs):
        calls.append(kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(ap_module, "run_ffmpeg", spy)
    AudioProcessor().compute_envelope_and_peaks(source)
    assert len(calls) == 1
    assert calls[0].get("stdout") is not None and not calls[0].get("capture_output")


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "_envelope_tasks", {})
    sid = str(uuid.uuid4())
    source = tmp_path / "source.wav"
    write_wav(source, 2.5, 2, 2)
    api.session_store.create(session_id=sid, source_audio=source, source_filename="a.wav",
                             source_duration=2.5, source_size=0)
    api.get_session_path(sid).mkdir(parents=True)
    yield sid, source
    api.session_store.remove(sid)


def count_decodes(monkeypatch):
    calls = []
    real = ap_module.run_ffmpeg

    def spy(args, **kwargs):
        calls.append(args)
        return real(args, **kwargs)

    monkeypatch.setattr(ap_module, "run_ffmpeg", spy)
    return calls


def test_prep_writes_both_caches_and_the_peaks_endpoint_needs_no_decode(session, monkeypatch):
    sid, source = session
    calls = count_decodes(monkeypatch)
    db = api._load_or_compute_envelope(sid, source)
    folder = api.get_session_path(sid)
    assert (folder / "envelope.npy").exists() and (folder / "peaks_v2.json").exists()
    assert not list(folder.glob("*.part"))
    assert np.array_equal(np.load(folder / "envelope.npy"), db)
    response = TestClient(api.app).get(f"/api/waveform-peaks/{sid}")
    assert response.status_code == 200
    assert response.json() == json.loads(json.dumps(legacy_peaks(source)))
    assert len(calls) == 1, "prep + peaks must decode the recording once"


def test_peaks_endpoint_without_cache_decodes_once_and_writes_both_caches(session, monkeypatch):
    sid, source = session
    calls = count_decodes(monkeypatch)
    client = TestClient(api.app)
    assert client.get(f"/api/waveform-peaks/{sid}").status_code == 200
    folder = api.get_session_path(sid)
    assert (folder / "envelope.npy").exists() and (folder / "peaks_v2.json").exists()
    # The envelope task now finds its cache: still one decode in total.
    api._load_or_compute_envelope(sid, source)
    client.get(f"/api/waveform-peaks/{sid}")
    assert len(calls) == 1


def test_peaks_requested_while_prep_decodes_wait_for_it(session, monkeypatch):
    """A peaks request racing the prep task must reuse its decode, not start a second one."""
    sid, source = session
    calls = count_decodes(monkeypatch)

    async def race():
        api.start_envelope(sid, source)   # prep's task: created, its decode not finished yet
        return await api.get_waveform_peaks(sid)

    response = asyncio.run(race())
    assert response.status_code == 200
    # the cached file served as is (prep's write), so read it from the response's path
    assert json.loads(open(response.path, "rb").read()) == json.loads(json.dumps(legacy_peaks(source)))
    assert len(calls) == 1, "the peaks request started its own decode"


def test_a_decode_for_a_removed_session_writes_nothing(session):
    """The session can be removed while its decode runs: no error, no dir recreated, no .part."""
    sid, source = session
    folder = api.get_session_path(sid)
    shutil.rmtree(folder)
    writes = []
    real_write = api._write_cache_atomically
    api._write_cache_atomically = lambda *args: writes.append(args) or real_write(*args)
    try:
        db, peaks = api._decode_and_cache(sid, source)
    finally:
        api._write_cache_atomically = real_write
    assert len(db) and peaks["length"] > 1
    assert not folder.exists()
    assert writes == [], "no write is even attempted for a removed recording"


def test_peaks_endpoint_reports_an_ffmpeg_failure(session, monkeypatch):
    sid, _ = session

    def fail(path):
        raise CalledProcessError(1, "ffmpeg", stderr=b"broken input")

    monkeypatch.setattr(api.audio_processor, "compute_envelope_and_peaks", fail)
    response = TestClient(api.app).get(f"/api/waveform-peaks/{sid}")
    assert response.status_code == 500
    assert response.json()["detail"] == "FFmpeg failed: broken input"


def test_peaks_request_for_a_recording_removed_while_waiting_is_a_404(session):
    """Removing the recording (the real cleanup path) cancels its prep task; the waiting peaks
    request must answer 404, not the generic 500 "Peaks generation failed"."""
    sid, source = session
    from fastapi import HTTPException

    async def race():
        api.start_envelope(sid, source)

        async def remove_recording():
            await asyncio.sleep(0)   # the peaks request is now waiting on the task
            api.cleanup_session(sid)

        asyncio.get_running_loop().create_task(remove_recording())
        await api.get_waveform_peaks(sid)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(race())
    assert excinfo.value.status_code == 404


def test_cached_peaks_are_served_as_the_files_own_bytes(session):
    """A cached peaks file (~13 MB for a 4 h album) is streamed as is, not parsed and re-serialised
    on the event loop: odd whitespace in the file survives only if it is not re-dumped."""
    sid, _ = session
    body = b'{ "peaks": [0.5,  1.0],\n  "length": 2, "duration": 0.02 }'
    (api.get_session_path(sid) / "peaks_v2.json").write_bytes(body)
    response = TestClient(api.app).get(f"/api/waveform-peaks/{sid}")
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"peaks": [0.5, 1.0], "length": 2, "duration": 0.02}
