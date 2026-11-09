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


def test_guided_endpoint_with_no_known_length_places_sides_and_gaps_by_count(recording):
    """With every length unknown the endpoint still places the tracks from the sides and clear
    gaps (``align-even``); ``method`` stays "count" so the UI draws no ghost markers."""
    response = TestClient(api.app).post('/api/analyze-guided', json={
        'file_id': recording, 'expected_tracks': 4, 'durations': [None] * 4, 'side_breaks': [2],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['method'] == 'count' and body['matched'] is True
    assert body['model'] == 'align-even'
    assert len(body['tracks']) == 4
    assert [round(b['gap']['start']) for b in body['boundaries']] == [100, 203, 363]


def test_guided_endpoint_treats_an_infinite_length_as_unknown(recording):
    """JSON 1e400 parses as float('inf'); it must not reach the response (inf is not valid JSON)."""
    body = '{"file_id": "%s", "expected_tracks": 4, "durations": [100, 1e400, 100, 100]}' % recording
    response = TestClient(api.app).post(
        '/api/analyze-guided', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 200, response.text
    assert len(response.json()['tracks']) == 4


def guided(payload):
    return TestClient(api.app).post('/api/analyze-guided', json=payload)


@pytest.fixture
def short_recording(tmp_path, monkeypatch):
    """70 s with one gap: far too short for 20 tracks."""
    sid = str(uuid.uuid4())
    source = tmp_path / 'short.wav'
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename='short.wav',
                             source_duration=70, source_size=0)
    envelope = np.full(7000, -12.)
    envelope[3000:3300] = -90

    async def cached_envelope(session):
        return envelope

    monkeypatch.setattr(api, 'get_envelope', cached_envelope)
    yield sid
    api.session_store.remove(sid)


def test_more_tracks_than_the_recording_can_hold_is_no_match(short_recording):
    """The gap/scale model clamps surplus cuts to the end of the music: 0 s tracks, not a split."""
    response = guided({'file_id': short_recording, 'expected_tracks': 20, 'durations': [30] * 20})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['matched'] is False and body['method'] == 'count'
    assert body['tracks'] and all(track['duration'] > 0 for track in body['tracks'])
    stored = api.session_store.get(short_recording).boundaries
    assert [track.start for track in stored] == [track['start'] for track in body['tracks']]


def test_huge_lengths_the_alignment_cannot_place_are_no_server_error(short_recording):
    """Their running total overflowed to inf in the gap/scale model, which JSON can't encode."""
    response = guided({'file_id': short_recording, 'expected_tracks': 20, 'durations': [1e308] * 20})
    assert response.status_code == 200, response.text
    assert response.json()['method'] == 'count'


@pytest.mark.parametrize('durations', [[1e308] * 4, [5000.0] * 4])
def test_lengths_beyond_the_recording_count_as_unknown(recording, durations):
    response = guided({'file_id': recording, 'expected_tracks': 4, 'durations': durations})
    assert response.status_code == 200, response.text
    body = response.json()
    # Every length unknown: placed by sides and clear gaps, not the count ladder.
    assert body['method'] == 'count' and body['model'] == 'align-even'


@pytest.mark.parametrize('bad_length', [1e308, 5000.0])
def test_one_length_beyond_the_recording_leaves_the_others_in_use(recording, bad_length):
    response = guided({'file_id': recording, 'expected_tracks': 4, 'durations': [100, bad_length, 100, 100]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['model'] == 'align' and len(body['tracks']) == 4
    assert all(boundary['expected'] < 466 for boundary in body['boundaries'])


@pytest.mark.parametrize('count', [-1, 0, 100])
def test_track_count_out_of_range_is_rejected(recording, count):
    assert guided({'file_id': recording, 'expected_tracks': count}).status_code == 422


def test_unknown_file_is_not_found():
    assert guided({'file_id': str(uuid.uuid4()), 'expected_tracks': 4}).status_code == 404


@pytest.mark.parametrize('durations', [[], [100] * 3, [100] * 5])
def test_a_wrong_number_of_lengths_is_rejected(recording, durations):
    assert guided({'file_id': recording, 'expected_tracks': 4, 'durations': durations}).status_code == 422


def test_no_lengths_at_all_uses_count_detection(recording):
    body = guided({'file_id': recording, 'expected_tracks': 4}).json()
    # No durations field at all: the count ladder (no guided model), not align-even.
    assert body['method'] == 'count' and body['model'] is None and len(body['tracks']) == 4


def test_zero_and_negative_lengths_count_as_unknown(recording):
    body = guided({'file_id': recording, 'expected_tracks': 4, 'durations': [100, 0, -5, 100]}).json()
    assert body['model'] == 'align' and len(body['tracks']) == 4
    # All of them unusable: no length known, so sides and clear gaps decide.
    body = guided({'file_id': recording, 'expected_tracks': 4, 'durations': [0, 0, -1, -1]}).json()
    assert body['method'] == 'count' and body['model'] == 'align-even'


def test_mixed_known_and_unknown_lengths_align(recording):
    body = guided({'file_id': recording, 'expected_tracks': 4, 'durations': [100, None, 100, None]}).json()
    assert body['model'] == 'align' and len(body['tracks']) == 4
    assert body['boundaries'][0]['expected'] == pytest.approx(100, abs=0.1)


@pytest.mark.parametrize('durations', [[400], [None], None])
def test_a_single_track(recording, durations):
    response = guided({'file_id': recording, 'expected_tracks': 1, 'durations': durations})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body['tracks']) == 1 and body['boundaries'] == [] and body['matched'] is True


def record_broadcasts(monkeypatch):
    """Keeps what the endpoint sends over the WebSocket (no client is connected in a test)."""
    messages = []

    async def broadcast(message):
        messages.append(message)

    monkeypatch.setattr(api, 'broadcast_message', broadcast)
    return messages


def test_progress_is_broadcast_like_the_other_detection_endpoints(recording, monkeypatch):
    messages = record_broadcasts(monkeypatch)
    assert guided({'file_id': recording, 'expected_tracks': 4, 'durations': [100] * 4}).status_code == 200
    assert [message['type'] for message in messages] == ['progress', 'step_complete']
    assert all(message['file_id'] == recording for message in messages)


def test_a_bug_in_the_alignment_is_an_error_not_a_fallback(recording, monkeypatch):
    """The alignment returns None when it can't place the tracks; an exception is a real bug."""
    messages = record_broadcasts(monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(api, 'guided_by_alignment', broken)
    response = guided({'file_id': recording, 'expected_tracks': 4, 'durations': [100] * 4})
    assert response.status_code == 500
    assert messages[-1]['type'] == 'error'

