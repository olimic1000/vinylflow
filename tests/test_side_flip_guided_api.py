"""Discogs side hints reach guided placement; no Discogs calls are needed."""
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.api as api


@pytest.fixture
def recording(tmp_path, monkeypatch):
    sid = str(uuid.uuid4())
    source = tmp_path / 'album.wav'
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename='album.wav',
                             source_duration=466, source_size=0)
    envelope = np.full(46600, -12.)
    for start, end in [(100, 103), (203, 263), (363, 366)]:
        envelope[start * 100:end * 100] = -90

    async def cached_envelope(session):
        return envelope

    monkeypatch.setattr(api, 'get_envelope', cached_envelope)
    yield sid
    api.session_store.remove(sid)


def test_guided_endpoint_uses_side_hint_for_guides_and_cuts(recording):
    response = TestClient(api.app).post('/api/analyze-guided', json={
        'file_id': recording, 'expected_tracks': 4, 'durations': [100] * 4, 'side_breaks': [2],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['model'] == 'align'
    # Best-match alignment: each guide is the track's own length from where it started.
    assert [b['expected'] for b in body['boundaries']] == pytest.approx([100, 203, 363], abs=0.1)  # from each song's onset
    assert [b['snapped'] for b in body['boundaries']] == [True, True, True]
    # Contiguous cuts just before the next song; the 60 s flip is not glued on.
    assert body['tracks'][0]['end'] == pytest.approx(body['tracks'][1]['start'], abs=0.01)
    assert body['tracks'][0]['end'] == pytest.approx(103 - 0.8, abs=0.1)  # PRE_ROLL_S before the onset
    assert body['tracks'][1]['end'] == pytest.approx(207, abs=0.1)
    assert body['tracks'][2]['start'] == pytest.approx(263 - 0.8, abs=0.1)


@pytest.mark.parametrize('breaks', [[0], [4], [2, 2], [3, 1]])
def test_invalid_side_hints_are_rejected(recording, breaks):
    response = TestClient(api.app).post('/api/analyze-guided', json={
        'file_id': recording, 'expected_tracks': 4, 'durations': [100] * 4, 'side_breaks': breaks,
    })
    assert response.status_code == 422


def test_guided_endpoint_with_no_known_length_falls_back_to_count_detection(recording):
    """With every length unknown there is nothing to align: the endpoint places the tracks by
    count (Smart Rescan ladder) and reports no guided model."""
    response = TestClient(api.app).post('/api/analyze-guided', json={
        'file_id': recording, 'expected_tracks': 4, 'durations': [None] * 4, 'side_breaks': [2],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['method'] == 'count'
    assert body['model'] is None and body['boundaries'] == []
    assert len(body['tracks']) == 4


def test_guided_endpoint_treats_an_infinite_length_as_unknown(recording):
    """JSON 1e400 parses as float('inf'); it must not reach the response (inf is not valid JSON)."""
    body = '{"file_id": "%s", "expected_tracks": 4, "durations": [100, 1e400, 100, 100]}' % recording
    response = TestClient(api.app).post(
        '/api/analyze-guided', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 200, response.text
    assert len(response.json()['tracks']) == 4
