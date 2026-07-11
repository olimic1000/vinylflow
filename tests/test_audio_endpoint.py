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
        resp = client.get(f"/api/audio/{sid}")
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
        resp = client.get(f"/api/audio/{sid}")
        assert resp.status_code == 200
        assert "utf-8''" in resp.headers["content-disposition"]
    finally:
        session_store.remove(sid)


def test_get_audio_unknown_id_returns_404():
    resp = client.get(f"/api/audio/{uuid.uuid4()}")
    assert resp.status_code == 404
