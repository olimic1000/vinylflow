"""GET /api/audio/{file_id}: capped open-ended Range requests and the MP3-only preview URL.

Browsers ask for ``bytes=N-`` and hang up after a few MB; the server must not read the rest of
a 1.4 GB recording each time.  The non-precise URL is the fallback for a WAV the browser cannot
play, so it must never serve (or pin) the source: it waits for the MP3 preview instead.
"""

import asyncio
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.api as api
from backend.api import app, session_store

CAP = 1000
SOURCE_SIZE = 5000
SOURCE_BYTES = bytes(index % 251 for index in range(SOURCE_SIZE))


def _session(tmp_path, monkeypatch, suffix=".wav", data=SOURCE_BYTES):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    source = tmp_path / f"source{suffix}"
    source.write_bytes(data)
    session_id = str(uuid.uuid4())
    session_store.create(session_id=session_id, source_audio=source, source_filename=f"rec{suffix}",
                         source_duration=60.0, source_size=source.stat().st_size)
    api.get_session_path(session_id).mkdir(parents=True, exist_ok=True)
    return session_id


@pytest.fixture
def capped(monkeypatch):
    monkeypatch.setattr(api, "AUDIO_RANGE_CAP_BYTES", CAP)


def _precise(client, session_id, range_header=None, **extra):
    headers = dict(extra)
    if range_header is not None:
        headers["Range"] = range_header
    return client.get(f"/api/audio/{session_id}?precise=true", headers=headers)


def test_open_ended_range_from_zero_is_capped(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, "bytes=0-")
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 0-{CAP - 1}/{SOURCE_SIZE}"
        assert response.content == SOURCE_BYTES[:CAP]
    finally:
        session_store.remove(session_id)


def test_open_ended_range_mid_file_is_capped(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, " bytes = 500 - ")
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 500-{500 + CAP - 1}/{SOURCE_SIZE}"
        assert response.content == SOURCE_BYTES[500:500 + CAP]
    finally:
        session_store.remove(session_id)


def test_open_ended_range_near_the_end_returns_the_real_remainder(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        start = SOURCE_SIZE - CAP + 10   # N + cap > size: nothing to cap
        response = _precise(TestClient(app), session_id, f"bytes={start}-")
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes {start}-{SOURCE_SIZE - 1}/{SOURCE_SIZE}"
        assert response.content == SOURCE_BYTES[start:]
    finally:
        session_store.remove(session_id)


@pytest.mark.parametrize("range_header, expected", [
    ("bytes=0-1", (0, 1)),
    ("bytes=2000-2499", (2000, 2499)),
    ("bytes=-100", (SOURCE_SIZE - 100, SOURCE_SIZE - 1)),
])
def test_other_single_ranges_are_untouched(tmp_path, monkeypatch, capped, range_header, expected):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, range_header)
        first, last = expected
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes {first}-{last}/{SOURCE_SIZE}"
        assert response.content == SOURCE_BYTES[first:last + 1]
    finally:
        session_store.remove(session_id)


def test_no_range_is_the_whole_file(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id)
        assert response.status_code == 200
        assert response.content == SOURCE_BYTES
    finally:
        session_store.remove(session_id)


def test_multi_range_is_not_rewritten(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, "bytes=0-99,200-299")
        assert response.status_code == 206
        # Not asserting Content-Type: Starlette 0.41 puts multipart/byteranges in Content-Range
        # and leaves audio/wav as the type; both parts' bytes must be there, nothing else.
        assert SOURCE_BYTES[0:100] in response.content and SOURCE_BYTES[200:300] in response.content
        assert len(response.content) < CAP
    finally:
        session_store.remove(session_id)


def test_range_past_the_end_is_416(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        assert _precise(TestClient(app), session_id, f"bytes={SOURCE_SIZE + 5}-").status_code == 416
    finally:
        session_store.remove(session_id)


def test_if_range_mismatch_still_returns_the_whole_file(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, "bytes=0-", **{"If-Range": '"not-the-etag"'})
        assert response.status_code == 200
        assert response.content == SOURCE_BYTES
    finally:
        session_store.remove(session_id)


def test_closed_long_range_is_capped(tmp_path, monkeypatch, capped):
    # WebKit (the macOS app) and Chromium seeks ask for closed ranges to the end of the file
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, f"bytes=1000-{SOURCE_SIZE - 1}")
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 1000-{1000 + CAP - 1}/{SOURCE_SIZE}"
        assert response.content == SOURCE_BYTES[1000:1000 + CAP]
        whole = _precise(TestClient(app), session_id, f"bytes=0-{SOURCE_SIZE - 1}")
        assert whole.headers["content-range"] == f"bytes 0-{CAP - 1}/{SOURCE_SIZE}"
    finally:
        session_store.remove(session_id)


def test_closed_range_of_exactly_the_cap_is_unchanged(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, f"bytes=100-{100 + CAP - 1}")
        assert response.headers["content-range"] == f"bytes 100-{100 + CAP - 1}/{SOURCE_SIZE}"
        # open-ended with N + cap == size exactly: also the real remainder
        start = SOURCE_SIZE - CAP
        response = _precise(TestClient(app), session_id, f"bytes={start}-")
        assert response.headers["content-range"] == f"bytes {start}-{SOURCE_SIZE - 1}/{SOURCE_SIZE}"
        assert len(response.content) == CAP
    finally:
        session_store.remove(session_id)


def test_range_starting_at_the_size_is_416(tmp_path, monkeypatch, capped):
    session_id = _session(tmp_path, monkeypatch)
    try:
        assert _precise(TestClient(app), session_id, f"bytes={SOURCE_SIZE}-").status_code == 416
    finally:
        session_store.remove(session_id)


@pytest.mark.parametrize("range_header", ["bytes=100-,", "bytes=0-,0-", "bytes=0-,-", "bytes= , 0- ,"])
def test_open_range_shapes_starlette_treats_as_one_range_are_capped(tmp_path, monkeypatch, capped, range_header):
    # Starlette served the whole file for these (empty or repeated items)
    session_id = _session(tmp_path, monkeypatch)
    try:
        response = _precise(TestClient(app), session_id, range_header)
        assert response.status_code == 206
        assert len(response.content) <= 2 * CAP + 400, "the capped pieces, never the whole file"
        assert len(response.content) < SOURCE_SIZE // 2
    finally:
        session_store.remove(session_id)


def test_multi_range_with_one_long_item_caps_only_that_item():
    scope = {"type": "http", "headers": [(b"range", b"bytes=0-99,2000-4999")]}
    rewritten = api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP)
    assert dict(rewritten["headers"])[b"range"] == b"bytes=0-99,2000-2999"


def test_suffix_ranges_are_left_alone():
    # A suffix (-N) is the last N bytes: browsers use it for a small tail probe, and it is
    # bounded by N already, so it is deliberately not capped.
    scope = {"type": "http", "headers": [(b"range", b"bytes=-4000")]}
    assert api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP) is scope


def test_only_the_first_range_header_is_kept(tmp_path, monkeypatch, capped):
    scope = {"type": "http", "headers": [(b"range", b"bytes=0-"), (b"host", b"x"), (b"range", b"bytes=0-")]}
    rewritten = api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP)
    assert rewritten["headers"] == [(b"range", b"bytes=0-999"), (b"host", b"x")]

    session_id = _session(tmp_path, monkeypatch)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            return await http.get(f"/api/audio/{session_id}?precise=true",
                                  headers=[("Range", "bytes=0-"), ("Range", "bytes=0-")])
    try:
        response = _on_loop(scenario)
        assert response.status_code == 206 and len(response.content) == CAP
    finally:
        session_store.remove(session_id)


@pytest.mark.parametrize("range_header", ["bytes=" + "9" * 5000 + "-", "bytes=" + "9" * 19 + "-0"],
                         ids=["huge-first", "huge-first-19-digits"])
def test_huge_numbers_are_left_to_starlette_not_a_500(tmp_path, monkeypatch, capped, range_header):
    scope = {"type": "http", "headers": [(b"range", range_header.encode())]}
    assert api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP) is scope
    session_id = _session(tmp_path, monkeypatch)
    try:
        try:
            response = _precise(TestClient(app), session_id, range_header)
        except ValueError:
            pytest.skip("Starlette itself raises on a 5000-digit number on this version")
        if response.status_code == 500:
            # Starlette 0.41's own parser raises on a 5000-digit int (1.9 answers 400); our
            # parser must not be the reason, which the scope assertion above proves.
            pytest.skip("Starlette itself answers 500 for this header on this version")
        assert response.status_code in (400, 416)
    finally:
        session_store.remove(session_id)


@pytest.mark.parametrize("range_header, expected", [
    ("bytes=0-" + "9" * 19, f"bytes=0-{CAP - 1}"),
    ("bytes=0-" + "9" * 4000, f"bytes=0-{CAP - 1}"),
    ("bytes=0-99,5-" + "9" * 19, f"bytes=0-99,5-{5 + CAP - 1}"),
    ("bytes=" + "0" * 21 + "1-", f"bytes=1-{CAP}"),
    ("bytes=" + "0" * 19 + "-", f"bytes=0-{CAP - 1}"),
], ids=["19-digit-end", "4000-digit-end", "multi-item", "leading-zero-start", "all-zero-start"])
def test_a_huge_range_end_reads_as_the_end_of_the_file_and_is_capped(range_header, expected):
    # Any end past the file is clamped to the file by Starlette, so a huge end is open-ended:
    # leaving it unparsed would serve the whole file again.
    scope = {"type": "http", "headers": [(b"range", range_header.encode())]}
    assert (b"range", expected.encode()) in api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP)["headers"]


def test_leading_zeros_in_a_range_end_keep_its_value():
    # 0-000…0005 is the six bytes 0-5, not an open range
    scope = {"type": "http", "headers": [(b"range", ("bytes=0-" + "0" * 25 + "5").encode())]}
    assert api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP) is scope


def test_unparseable_range_is_left_to_starlette():
    scope = {"type": "http", "headers": [(b"range", b"bytes=abc-def")]}
    assert api._cap_long_ranges(scope, SOURCE_SIZE, cap=CAP) is scope


def test_range_rewrite_does_not_mutate_the_original_scope():
    headers = [(b"host", b"x"), (b"range", b"bytes=0-")]
    scope = {"type": "http", "headers": headers}
    rewritten = api._cap_long_ranges(scope, 10 * api.AUDIO_RANGE_CAP_BYTES)
    assert headers == [(b"host", b"x"), (b"range", b"bytes=0-")]
    assert (b"range", f"bytes=0-{api.AUDIO_RANGE_CAP_BYTES - 1}".encode()) in rewritten["headers"]


# ---- the preview URL never serves the source ----


def _on_loop(coroutine_factory):
    return asyncio.run(coroutine_factory())


async def _get(session_id, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        return await http.get(f"/api/audio/{session_id}", **kwargs)


def _fake_encoder(monkeypatch, calls, delay=0.05, succeed=True):
    async def fake_preconvert(file_id, file_path):
        calls.append(file_id)
        await asyncio.sleep(delay)
        if succeed:
            api.get_session_path(file_id, "full.mp3").write_bytes(b"ID3preview")
    monkeypatch.setattr(api, "preconvert_to_mp3", fake_preconvert)


@pytest.mark.parametrize("suffix", [".wav", ".aiff"])
def test_missing_preview_waits_for_the_encode_and_serves_the_mp3(tmp_path, monkeypatch, suffix):
    session_id = _session(tmp_path, monkeypatch, suffix=suffix)
    calls = []
    _fake_encoder(monkeypatch, calls)

    async def scenario():
        first = await _get(session_id)
        later = await _get(session_id)   # the MP3 exists now; never a pinned source
        return first, later

    try:
        first, later = _on_loop(scenario)
        assert first.status_code == 200 and first.headers["content-type"] == "audio/mpeg"
        assert first.content == b"ID3preview"
        assert later.content == b"ID3preview"
        assert session_store.get(session_id).playback_audio.suffix == ".mp3"
        assert calls == [session_id]
    finally:
        session_store.remove(session_id)


def test_a_pinned_source_is_ignored(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    session_store.get(session_id).playback_audio = session_store.get(session_id).source_audio
    api.get_session_path(session_id, "full.mp3").write_bytes(b"ID3preview")
    try:
        response = _on_loop(lambda: _get(session_id))
        assert response.content == b"ID3preview"
        assert session_store.get(session_id).playback_audio.suffix == ".mp3"
    finally:
        session_store.remove(session_id)


def test_failed_encode_is_503_and_never_serves_the_source(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    _fake_encoder(monkeypatch, [], succeed=False)
    try:
        response = _on_loop(lambda: _get(session_id))
        assert response.status_code == 503
        assert "could not be made" in response.json()["detail"]
        assert session_store.get(session_id).playback_audio is None
    finally:
        session_store.remove(session_id)


def test_wait_timeout_is_503_and_keeps_the_encode_running(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.3)
    monkeypatch.setattr(api, "AUDIO_PREVIEW_WAIT_S", 0.02)

    async def scenario():
        response = await _get(session_id)
        task_survived = not api._preconvert_tasks[session_id].done()
        await api._preconvert_tasks[session_id]
        return response, task_survived

    try:
        response, task_survived = _on_loop(scenario)
        assert response.status_code == 503
        assert response.json()["detail"] == "The audio preview is still being prepared. Try again in a moment."
        assert task_survived, "a timed-out wait must not cancel the shared encode"
        assert api.get_session_path(session_id, "full.mp3").exists()
    finally:
        session_store.remove(session_id)


def test_prep_and_on_demand_request_share_one_encode(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.1)

    async def fake_envelope(file_id, file_path):
        return None
    monkeypatch.setattr(api, "_run_envelope", fake_envelope)

    async def scenario():
        prep = asyncio.create_task(api._prepare_source(session_id, session_store.get(session_id).source_audio))
        await asyncio.sleep(0.02)   # the encode is running
        response = await _get(session_id)
        await prep
        return response

    try:
        response = _on_loop(scenario)
        assert response.content == b"ID3preview"
        assert calls == [session_id], "one ffmpeg run for one file"
    finally:
        session_store.remove(session_id)


def test_cleanup_cancels_the_running_encode(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    _fake_encoder(monkeypatch, [], delay=5)

    async def scenario():
        task = api.start_preconvert(session_id, session_store.get(session_id).source_audio)
        await asyncio.sleep(0.01)
        api._forget_envelope(session_id)
        await asyncio.sleep(0.01)
        return task.cancelled(), session_id in api._preconvert_tasks

    try:
        cancelled, registered = _on_loop(scenario)
        assert cancelled and not registered
    finally:
        session_store.remove(session_id)


# ---- failed encodes are remembered; the probe form never waits ----


def test_failing_encode_runs_once_for_concurrent_and_later_requests(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.05, succeed=False)

    async def scenario():
        responses = await asyncio.gather(*[_get(session_id) for _ in range(5)])
        later = await _get(session_id)
        return responses, later

    try:
        responses, later = _on_loop(scenario)
        assert all(r.status_code == 503 for r in responses) and later.status_code == 503
        assert "could not be made" in later.json()["detail"]
        assert calls == [session_id], "one ffmpeg run; a later request must not start a second"
    finally:
        api.cleanup_session(session_id)
    assert session_id not in api._preconvert_failed, "forgotten with the session"


def test_probe_form_never_waits_and_starts_the_encode_once(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.3)

    async def scenario():
        started = asyncio.get_running_loop().time()
        first = await _get(session_id, params={"wait": "0"}, headers={"Range": "bytes=0-1"})
        second = await _get(session_id, params={"wait": "0"}, headers={"Range": "bytes=0-1"})
        quick = asyncio.get_running_loop().time() - started
        await api._preconvert_tasks[session_id]
        ready = await _get(session_id, params={"wait": "0"}, headers={"Range": "bytes=0-1"})
        return first, second, quick, ready

    try:
        first, second, quick, ready = _on_loop(scenario)
        assert quick < 0.2, "the probe must not wait for the encode"
        for response in (first, second):
            assert response.status_code == 503
            assert response.json()["detail"] == "The audio preview is still being prepared. Try again in a moment."
            assert response.headers["retry-after"]
        assert calls == [session_id], "started on demand, once"
        assert ready.status_code == 206 and ready.content == b"ID"
    finally:
        session_store.remove(session_id)


def test_probe_form_reports_a_failed_encode_at_once(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.01, succeed=False)

    async def scenario():
        await api.start_preconvert(session_id, session_store.get(session_id).source_audio)
        await asyncio.sleep(0)
        return await _get(session_id, params={"wait": "0"})

    try:
        response = _on_loop(scenario)
        assert response.status_code == 503 and "could not be made" in response.json()["detail"]
        assert "retry-after" not in response.headers
        assert calls == [session_id]
    finally:
        session_store.remove(session_id)


# ---- mutants the suites above must kill ----


def test_cancelled_prep_does_not_cancel_the_encode_a_request_waits_on(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.15)

    async def fake_envelope(file_id, file_path):
        return None
    monkeypatch.setattr(api, "_run_envelope", fake_envelope)

    async def scenario():
        prep = asyncio.create_task(api._prepare_source(session_id, session_store.get(session_id).source_audio))
        await asyncio.sleep(0.02)
        request = asyncio.create_task(_get(session_id))
        await asyncio.sleep(0.02)
        prep.cancel()
        return await request

    try:
        response = _on_loop(scenario)
        assert response.status_code == 200 and response.content == b"ID3preview"
        assert calls == [session_id]
    finally:
        session_store.remove(session_id)


def test_finished_encode_leaves_the_registry(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    _fake_encoder(monkeypatch, [], delay=0.01)

    async def scenario():
        task = api.start_preconvert(session_id, session_store.get(session_id).source_audio)
        await task
        await asyncio.sleep(0)   # done callbacks run on the next iteration
        return session_id in api._preconvert_tasks

    try:
        assert _on_loop(scenario) is False
    finally:
        session_store.remove(session_id)


def test_recording_removed_while_a_request_waits_is_404(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    _fake_encoder(monkeypatch, [], delay=5)

    async def scenario():
        request = asyncio.create_task(_get(session_id))
        await asyncio.sleep(0.05)
        api._forget_envelope(session_id)
        return await asyncio.wait_for(request, 2)

    try:
        assert _on_loop(scenario).status_code == 404
    finally:
        session_store.remove(session_id)


def test_a_pinned_preview_that_vanished_is_replaced(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    session_store.get(session_id).playback_audio = tmp_path / "gone.mp3"
    api.get_session_path(session_id, "full.mp3").write_bytes(b"ID3preview")
    try:
        response = _on_loop(lambda: _get(session_id))
        assert response.status_code == 200 and response.content == b"ID3preview"
        assert session_store.get(session_id).playback_audio == api.get_session_path(session_id, "full.mp3")
    finally:
        session_store.remove(session_id)


def test_cancelled_encode_removes_its_part_file(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)

    def slow_ffmpeg(args, **kwargs):
        api.Path(args[-1]).write_bytes(b"half")
        import time
        time.sleep(0.3)
    monkeypatch.setattr(api, "run_ffmpeg", slow_ffmpeg)

    async def scenario():
        task = asyncio.create_task(api.preconvert_to_mp3(session_id, session_store.get(session_id).source_audio))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return list(api.get_session_path(session_id).glob("*.part"))

    try:
        assert _on_loop(scenario) == []
    finally:
        session_store.remove(session_id)


def test_prep_after_a_failed_on_demand_encode_does_not_run_ffmpeg_again(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    calls = []
    _fake_encoder(monkeypatch, calls, delay=0.01, succeed=False)
    envelope_calls = []

    async def fake_envelope(file_id, file_path):
        envelope_calls.append(file_id)
    monkeypatch.setattr(api, "_run_envelope", fake_envelope)

    async def scenario():
        first = await _get(session_id)   # on demand: fails
        await api._prepare_source(session_id, session_store.get(session_id).source_audio)   # prep gets its slot
        return first

    try:
        assert _on_loop(scenario).status_code == 503
        assert calls == [session_id], "one encoder call"
        assert envelope_calls == [session_id], "prep still runs the envelope"
    finally:
        session_store.remove(session_id)


def test_failed_detail_names_the_remedy(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    api._preconvert_failed.add(session_id)
    try:
        detail = _on_loop(lambda: _get(session_id)).json()["detail"]
        assert detail == ("The audio preview could not be made for this recording. "
                          "Remove it from the queue and add it again to retry.")
    finally:
        api._preconvert_failed.discard(session_id)
        session_store.remove(session_id)


def test_a_source_that_vanished_before_the_stat_is_410(tmp_path, monkeypatch):
    session_id = _session(tmp_path, monkeypatch)
    # _require_source (pathlib's own stat) passed, then the file goes away before the endpoint's
    # stat, which goes through os.stat
    real_stat = api.os.stat

    def vanishing_stat(path, *args, **kwargs):
        if str(path).endswith("source.wav"):
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(api.os, "stat", vanishing_stat)
    try:
        response = _precise(TestClient(app), session_id)
        assert response.status_code == 410
    finally:
        session_store.remove(session_id)


def test_the_stat_runs_off_the_event_loop(tmp_path, monkeypatch):
    import threading
    session_id = _session(tmp_path, monkeypatch)
    real_stat = api.os.stat
    threads = []

    def recording_stat(path, *args, **kwargs):
        if str(path).endswith("source.wav"):
            threads.append(threading.current_thread())
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(api.os, "stat", recording_stat)

    async def scenario():
        loop_thread = threading.current_thread()
        response = await _get(session_id, params={"precise": "true"})
        return loop_thread, response

    try:
        loop_thread, response = _on_loop(scenario)
        assert response.status_code == 200
        assert threads and loop_thread not in threads[-1:], "the endpoint's stat must run in a worker thread"
    finally:
        session_store.remove(session_id)
