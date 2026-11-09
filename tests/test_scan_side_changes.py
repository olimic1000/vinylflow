"""GET /api/scan/{id}?side_changes=N reports the longest non-music stretches (side flips)."""
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.api as api


@pytest.fixture
def recording(tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    source = tmp_path / "album.wav"
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename="album.wav",
                             source_duration=466, source_size=0)
    envelope = np.full(46600, -12.)
    envelope[:300] = -90
    envelope[20300:26300] = -90  # a 60 s side flip
    envelope[46300:] = -90
    monkeypatch.setattr(api, "_envelope_tasks", {})
    monkeypatch.setattr(api, "get_session_path", lambda file_id, name: tmp_path / name)
    (tmp_path / "envelope.npy").touch()

    async def cached_envelope(session):
        return envelope

    monkeypatch.setattr(api, "get_envelope", cached_envelope)
    yield sid
    api.session_store.remove(sid)


def test_side_changes_lists_the_long_silence(recording):
    body = TestClient(api.app).get(f"/api/scan/{recording}?side_changes=1").json()
    assert body["ready"] is True
    (start, end), = body["side_changes"]
    assert start == pytest.approx(203, abs=1.5) and end == pytest.approx(263, abs=1.5)
    assert round(start, 2) == start


def test_default_has_no_side_changes_key(recording):
    assert "side_changes" not in TestClient(api.app).get(f"/api/scan/{recording}").json()


@pytest.mark.parametrize("value", [-1, 9])
def test_side_changes_out_of_range_is_422(recording, value):
    assert TestClient(api.app).get(f"/api/scan/{recording}?side_changes={value}").status_code == 422
