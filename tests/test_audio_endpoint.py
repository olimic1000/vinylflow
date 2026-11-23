"""Regression tests for GET /api/audio/{file_id}.

This endpoint serves the source audio back to WaveSurfer for waveform
playback (see backend/static/app.js — `this.waveform.load("/api/audio/...")`).
It previously raised ``NameError: name 'file_info' is not defined`` on every
call because the Content-Disposition header referenced an undefined variable,
turning every waveform load into an HTTP 500.
"""

import uuid

from fastapi.testclient import TestClient

from backend.api import app, session_store

client = TestClient(app)


def _register_session(tmp_path, filename="My Record - Side A.wav"):
    """Create a real temp source file and register a Session for it."""
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF....WAVEfake-audio-bytes")
    sid = str(uuid.uuid4())
    session_store.create(
        session_id=sid,
        source_audio=source,
        source_filename=filename,
        source_duration=60.0,
        source_size=source.stat().st_size,
    )
    return sid


def test_get_audio_returns_file_with_disposition(tmp_path):
    sid = _register_session(tmp_path)
    try:
        resp = client.get(f"/api/audio/{sid}?precise=true")
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("inline;")
        assert "My" in disposition and "Side" in disposition
        assert resp.content == b"RIFF....WAVEfake-audio-bytes"
    finally:
        session_store.remove(sid)


def test_get_audio_non_latin1_filename(tmp_path):
    """Non-latin-1 filenames (en-dash, umlauts) must not 500 — Starlette
    RFC-5987-encodes them when FileResponse builds the header."""
    sid = _register_session(tmp_path, filename="Björk – Debut Side A.wav")
    try:
        resp = client.get(f"/api/audio/{sid}?precise=true")
        assert resp.status_code == 200
        assert "utf-8''" in resp.headers["content-disposition"]
    finally:
        session_store.remove(sid)


def test_get_audio_unknown_id_returns_404():
    resp = client.get(f"/api/audio/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_precise_wav_playback_uses_source_even_when_mp3_is_pinned(tmp_path, monkeypatch):
    """Cut-point playback can bypass the lossy preview without changing its URL's bytes."""
    import backend.api as api

    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    sid = _register_session(tmp_path)
    preview = api.get_session_path(sid, "full.mp3")
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"ID3preview")
    try:
        assert client.get(f"/api/audio/{sid}").content == b"ID3preview"
        response = client.get(f"/api/audio/{sid}?precise=true")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content == b"RIFF....WAVEfake-audio-bytes"
        part = client.get(f"/api/audio/{sid}?precise=true", headers={"Range": "bytes=8-11"})
        assert part.status_code == 206 and part.content == b"WAVE"
        assert part.headers["content-range"] == "bytes 8-11/28"
        assert client.get(f"/api/audio/{sid}").content == b"ID3preview"
    finally:
        session_store.remove(sid)


def test_precise_aiff_stays_on_the_preview(tmp_path, monkeypatch):
    """Browsers can't play AIFF, so ?precise=true must still serve the MP3 preview."""
    import backend.api as api

    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    source = tmp_path / "source.aiff"
    source.write_bytes(b"FORM....AIFFfake")
    sid = str(uuid.uuid4())
    session_store.create(session_id=sid, source_audio=source, source_filename="rec.aiff",
                         source_duration=60.0, source_size=source.stat().st_size)
    preview = api.get_session_path(sid, "full.mp3")
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"ID3preview")
    try:
        response = client.get(f"/api/audio/{sid}?precise=true")
        assert response.content == b"ID3preview"
        assert response.headers["content-type"] == "audio/mpeg"
    finally:
        session_store.remove(sid)


def test_preview_with_explicit_range_needs_no_stored_boundary(tmp_path, monkeypatch):
    """Manual splits create tracks the backend has no boundary for; a
    preview with explicit start/end must still render instead of 404."""
    import backend.api as api

    def fake_ffmpeg(args, **kwargs):
        out = api.Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ID3fake-mp3")

    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "run_ffmpeg", fake_ffmpeg)
    sid = _register_session(tmp_path)
    try:
        resp = client.get(f"/api/preview/{sid}/5", params={"start": 10.0, "end": 20.0})
        assert resp.status_code == 200
        assert resp.content == b"ID3fake-mp3"
        # Without a range, an unknown track number is still a 404.
        assert client.get(f"/api/preview/{sid}/5").status_code == 404
    finally:
        session_store.remove(sid)
