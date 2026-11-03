"""The cached energy envelope: Smart Rescan from it, envelope.npy reuse, GET /api/scan.

Envelopes are synthetic numpy arrays (10 ms frames) unless a test is marked as needing
ffmpeg.  ``get_envelope`` is replaced where a test only cares about what an endpoint does
with the envelope, not how it was computed.

The background-task tests (removal cancels, failed scans retry, ``scan_complete`` order)
await the endpoint functions directly inside ``asyncio.run``, so the envelope task shares
the request's event loop; ``DELETE /api/queue`` still runs ``cleanup_session`` in a worker
thread, as in production.  The decode is faked with a function that blocks on a
``threading.Event`` so a task can be caught mid-decode; every test releases it, or the
loop's executor shutdown would wait for the blocked thread forever."""

import asyncio
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
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
    monkeypatch.setattr(api, "_envelope_errors", {})
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


class RunningTask:
    """Stands in for an unfinished asyncio.Task: records the cancel.

    Cancels go through the task's loop (``api._cancel_task`` uses
    ``call_soon_threadsafe``, as removal runs in a worker thread); there is no
    loop here, so the fake loop runs the callback at once."""

    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True

    def get_loop(self):
        return SimpleNamespace(call_soon_threadsafe=lambda callback: callback())


def test_removing_a_recording_drops_and_cancels_its_envelope_task(session, monkeypatch):
    task = RunningTask()
    monkeypatch.setitem(api._envelope_tasks, session, task)
    monkeypatch.setitem(api._envelope_errors, session, "decode failed")
    assert TestClient(api.app).delete(f"/api/queue/{session}").status_code == 200
    assert session not in api._envelope_tasks and session not in api._envelope_errors
    assert task.cancelled


# --- background envelope task: removal cancels it, failed scans retry -----------------------

ENVELOPE = make_env([(1, QUIET), (58, LOUD), (1, QUIET)])


class BlockingDecode:
    """Fake ``compute_envelope``: signals ``started``, then waits for ``release``.

    ``fail_first`` makes the first call raise instead (a decode error).  ``write_cache``
    writes envelope.npy before blocking, like the window between the real thread's
    ``os.replace`` and the task finishing."""

    def __init__(self, fail_first=False, block=True, write_cache=None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail_first = fail_first
        self.write_cache = write_cache
        if not block:
            self.release.set()

    def __call__(self, path):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("decode failed")
        if self.write_cache is not None:
            np.save(self.write_cache, ENVELOPE)
        self.started.set()
        assert self.release.wait(10), "test never released the decode"
        return ENVELOPE


@pytest.fixture
def recording(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(api, "_envelope_tasks", {})
    monkeypatch.setattr(api, "_envelope_errors", {})
    sent = []

    async def broadcast(message):
        sent.append(message)

    monkeypatch.setattr(api, "broadcast_message", broadcast)
    sid = str(uuid.uuid4())
    (tmp_path / sid).mkdir()
    source = tmp_path / sid / "source.wav"
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename="side.wav",
                             source_duration=60, source_size=0)
    yield {"id": sid, "source": source, "sent": sent, "dir": tmp_path / sid}
    api.session_store.remove(sid)


def use_decode(monkeypatch, decode):
    monkeypatch.setattr(api.audio_processor, "compute_envelope", decode)
    return decode


async def until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def settle(task):
    """Let a cancel scheduled with call_soon_threadsafe land, and the task finish."""
    await asyncio.wait([task], timeout=2)
    await asyncio.sleep(0.01)  # done callbacks and the broadcasts they schedule


def scan_completes(rec):
    return [m for m in rec["sent"] if m["type"] == "scan_complete" and m["file_id"] == rec["id"]]


def test_delete_mid_decode_cancels_the_scan_and_sends_no_scan_complete(recording, monkeypatch):
    decode = use_decode(monkeypatch, BlockingDecode())

    async def scenario():
        task = api.start_envelope(recording["id"], recording["source"])
        await until(decode.started.is_set)
        try:
            await api.remove_from_queue(recording["id"])
        finally:
            decode.release.set()
        await settle(task)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert scan_completes(recording) == []
    assert recording["id"] not in api._envelope_tasks


def test_decode_finishing_during_the_folder_removal_sends_no_scan_complete(recording, monkeypatch):
    # Deleting a large session folder takes a while; a decode that ends meanwhile must not
    # announce a recording that is being removed.  The fake rmtree (in the worker thread, like
    # the real one) lets the decode finish and waits for the task before deleting.
    decode = use_decode(monkeypatch, BlockingDecode())
    real_rmtree = shutil.rmtree
    tasks = {}

    def slow_rmtree(path, *args, **kwargs):
        decode.release.set()
        deadline = time.monotonic() + 2
        while not tasks["task"].done() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # done callbacks and the broadcast they schedule
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(api.shutil, "rmtree", slow_rmtree)

    async def scenario():
        tasks["task"] = task = api.start_envelope(recording["id"], recording["source"])
        await until(decode.started.is_set)
        try:
            await api.remove_from_queue(recording["id"])
        finally:
            decode.release.set()
        await settle(task)

    asyncio.run(scenario())
    assert scan_completes(recording) == []
    assert recording["id"] not in api._envelope_tasks


def test_clear_all_mid_decode_cancels_the_scan(recording, monkeypatch):
    decode = use_decode(monkeypatch, BlockingDecode())

    async def scenario():
        task = api.start_envelope(recording["id"], recording["source"])
        await until(decode.started.is_set)
        try:
            await api.clear_all_temp()
        finally:
            decode.release.set()
        await settle(task)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert scan_completes(recording) == []
    assert recording["id"] not in api._envelope_tasks


def test_ttl_reaper_cancels_the_scan(recording, monkeypatch):
    decode = use_decode(monkeypatch, BlockingDecode())
    api.session_store.get(recording["id"]).updated_at = datetime.now() - timedelta(days=30)

    async def scenario():
        task = api.start_envelope(recording["id"], recording["source"])
        await until(decode.started.is_set)
        reaper = asyncio.ensure_future(api.cleanup_old_files())  # one sweep, then a 30 min sleep
        try:
            await until(lambda: api.session_store.get(recording["id"]) is None)
        finally:
            decode.release.set()
        await settle(task)
        reaper.cancel()
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert scan_completes(recording) == []
    assert recording["id"] not in api._envelope_tasks


def test_rescan_waiting_on_a_removed_recording_gets_404(recording, monkeypatch):
    decode = use_decode(monkeypatch, BlockingDecode())

    async def scenario():
        request = asyncio.ensure_future(
            api.rescan_file(api.RescanRequest(file_id=recording["id"], expected_tracks=2)))
        await until(decode.started.is_set)
        try:
            await api.remove_from_queue(recording["id"])
        finally:
            decode.release.set()
        with pytest.raises(HTTPException) as err:
            await asyncio.wait_for(request, 5)
        return err.value

    assert asyncio.run(scenario()).status_code == 404


def test_start_envelope_for_an_unknown_recording_is_404(recording, monkeypatch):
    use_decode(monkeypatch, BlockingDecode(block=False))

    async def scenario():
        with pytest.raises(HTTPException) as err:
            api.start_envelope("no-such-id", recording["source"])
        return err.value

    assert asyncio.run(scenario()).status_code == 404
    assert "no-such-id" not in api._envelope_tasks


def test_removal_cancels_from_the_worker_thread_safely(recording, monkeypatch):
    # asyncio debug mode raises "Non-thread-safe operation" when Task.cancel runs off the
    # loop thread; DELETE /api/queue runs cleanup_session in a worker thread.
    decode = use_decode(monkeypatch, BlockingDecode())

    async def scenario():
        task = api.start_envelope(recording["id"], recording["source"])
        await until(decode.started.is_set)
        try:
            await api.remove_from_queue(recording["id"])
        finally:
            decode.release.set()
        await settle(task)
        return task

    # Own loop rather than asyncio.run(debug=True): an unsafe cancel can leave the task stuck
    # forever, and asyncio.run's final cancel-all would then hang instead of failing.
    loop = asyncio.new_event_loop()
    loop.set_debug(True)
    try:
        task = loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert task.cancelled()


def test_failed_scan_is_retried_and_reported(recording, monkeypatch):
    use_decode(monkeypatch, BlockingDecode(fail_first=True, block=False))

    async def scenario():
        first = api.start_envelope(recording["id"], recording["source"])
        await settle(first)
        failed = await api.get_scan(recording["id"])
        retry = api._envelope_tasks.get(recording["id"])
        assert retry is not None and retry is not first, "/api/scan must start a new scan"
        await settle(retry)
        return failed, await api.get_scan(recording["id"])

    failed, ready = asyncio.run(scenario())
    assert failed["ready"] is False and "decode failed" in failed["error"]
    assert ready["ready"] is True and "error" not in ready
    assert len(scan_completes(recording)) == 1
    assert recording["id"] not in api._envelope_errors


def test_scan_complete_is_sent_once_the_scan_is_ready(recording, monkeypatch):
    use_decode(monkeypatch, BlockingDecode(block=False))
    seen = []

    async def broadcast(message):
        if message["type"] == "scan_complete":
            seen.append(await api.get_scan(message["file_id"]))

    monkeypatch.setattr(api, "broadcast_message", broadcast)

    async def scenario():
        await settle(api.start_envelope(recording["id"], recording["source"]))

    asyncio.run(scenario())
    assert len(seen) == 1 and seen[0]["ready"] is True


def test_scan_never_blocks_on_a_running_task_even_when_the_cache_exists(recording, monkeypatch):
    decode = use_decode(monkeypatch, BlockingDecode(write_cache=recording["dir"] / "envelope.npy"))

    async def scenario():
        task = api.start_envelope(recording["id"], recording["source"])
        await until(decode.started.is_set)
        try:
            return await asyncio.wait_for(api.get_scan(recording["id"]), 1)
        finally:
            decode.release.set()
            await settle(task)

    assert asyncio.run(scenario()) == {"ready": False}
