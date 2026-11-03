"""Tests for ``audacity_labels`` and the /api/import-labels + /api/export-labels endpoints.

Label files here are written the way Audacity writes them: tab-separated
``start\\tend\\tname`` lines with six decimal places, region labels with
``end > start`` and point labels with ``end == start``.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from audacity_labels import LabelFileError, labels_from_tracks, parse_labels, tracks_from_labels
from backend.api import app, session_store

client = TestClient(app)


def _spans(tracks):
    return [(round(t.start, 3), round(t.end, 3)) for t in tracks]


# --- parsing ---------------------------------------------------------------


def test_parse_skips_blank_and_spectral_lines():
    text = (
        "1.000000\t5.000000\tIntro\n"
        "\\\t100.0\t2000.0\n"  # spectral range of the label above
        "\n"
        "5.000000\t9.000000\t\n"
    )
    assert parse_labels(text) == [(1.0, 5.0, "Intro"), (5.0, 9.0, "")]


def test_parse_tolerates_crlf_bom_and_comma_decimals():
    text = "﻿1,5\t4,25\tA1 Song\r\n"
    assert parse_labels(text) == [(1.5, 4.25, "A1 Song")]


def test_parse_rejects_non_label_text():
    with pytest.raises(LabelFileError):
        parse_labels("this is not a label file")
    with pytest.raises(LabelFileError):
        parse_labels("abc\t1.0\tname")


# --- region labels ---------------------------------------------------------


def test_region_labels_become_tracks_with_names():
    text = (
        "2.000000\t180.000000\tA1 First\n"
        "182.000000\t400.000000\tA2 Second\n"
    )
    tracks, names = tracks_from_labels(text, duration=410.0)
    assert _spans(tracks) == [(2.0, 180.0), (182.0, 400.0)]
    assert [t.number for t in tracks] == [1, 2]
    assert names == ["A1 First", "A2 Second"]


def test_region_labels_are_sorted_clamped_and_overlap_trimmed():
    text = (
        "100.0\t250.0\tB\n"
        "-1.0\t110.0\tA\n"  # starts before 0, overlaps B
        "240.0\t999.0\tC\n"  # overlaps B, runs past EOF
    )
    tracks, names = tracks_from_labels(text, duration=300.0)
    assert _spans(tracks) == [(0.0, 110.0), (110.0, 250.0), (250.0, 300.0)]
    assert names == ["A", "B", "C"]


def test_regions_win_over_points_in_a_mixed_file():
    text = "50.0\t50.0\tpoint\n10.0\t100.0\tregion\n"
    tracks, names = tracks_from_labels(text, duration=200.0)
    assert _spans(tracks) == [(10.0, 100.0)]
    assert names == ["region"]


# --- point labels ----------------------------------------------------------


def test_point_labels_mark_track_starts_and_last_runs_to_eof():
    text = "2.5\t2.5\tA1\n181.0\t181.0\tA2\n330.0\t330.0\tA3\n"
    tracks, names = tracks_from_labels(text, duration=500.0, min_track_length=30.0)
    # Short lead-in before the first point is needle drop, not a track.
    assert _spans(tracks) == [(2.5, 181.0), (181.0, 330.0), (330.0, 500.0)]
    assert names == ["A1", "A2", "A3"]


def test_long_audio_before_first_point_is_kept_as_a_track():
    text = "200.0\t200.0\tA2\n"
    tracks, names = tracks_from_labels(text, duration=400.0, min_track_length=30.0)
    assert _spans(tracks) == [(0.0, 200.0), (200.0, 400.0)]
    assert names == ["", "A2"]


def test_duplicate_points_and_point_at_eof_are_ignored():
    text = "0.0\t0.0\t\n100.0\t100.0\tx\n100.004\t100.004\tdup\n300.0\t300.0\tend\n"
    tracks, _ = tracks_from_labels(text, duration=300.0)
    assert _spans(tracks) == [(0.0, 100.0), (100.0, 300.0)]


def test_no_usable_labels_raises():
    with pytest.raises(LabelFileError):
        tracks_from_labels("", duration=100.0)
    with pytest.raises(LabelFileError):
        tracks_from_labels("500.0\t600.0\tpast the end\n", duration=100.0)


# --- export ----------------------------------------------------------------


def test_export_matches_audacity_layout_and_round_trips():
    text = labels_from_tracks([(181.0, 330.5, "A2 Second"), (2.0, 181.0, "A1\tFirst\n")])
    assert text == "2.000000\t181.000000\tA1 First\n181.000000\t330.500000\tA2 Second\n"

    tracks, names = tracks_from_labels(text, duration=400.0)
    assert _spans(tracks) == [(2.0, 181.0), (181.0, 330.5)]
    assert names == ["A1 First", "A2 Second"]


# --- API -------------------------------------------------------------------


def _register_session(tmp_path, duration=400.0):
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF....WAVEfake-audio-bytes")
    sid = str(uuid.uuid4())
    session_store.create(
        session_id=sid,
        source_audio=source,
        source_filename="Side A.wav",
        source_duration=duration,
        source_size=source.stat().st_size,
    )
    return sid


def test_import_labels_sets_session_boundaries(tmp_path):
    sid = _register_session(tmp_path)
    try:
        resp = client.post(
            "/api/import-labels",
            json={"file_id": sid, "labels": "2.0\t180.0\tA1\n182.0\t390.0\tA2\n"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [(t["number"], t["start"], t["end"], t["label"]) for t in body["tracks"]] == [
            (1, 2.0, 180.0, "A1"),
            (2, 182.0, 390.0, "A2"),
        ]
        assert body["tracks"][0]["duration"] == pytest.approx(178.0)
        assert _spans(session_store.get(sid).boundaries) == [(2.0, 180.0), (182.0, 390.0)]
    finally:
        session_store.remove(sid)


def test_import_labels_bad_file_is_422(tmp_path):
    sid = _register_session(tmp_path)
    try:
        resp = client.post("/api/import-labels", json={"file_id": sid, "labels": "hello"})
        assert resp.status_code == 422
        assert "not an Audacity label" in resp.json()["detail"]
    finally:
        session_store.remove(sid)


def test_import_labels_unknown_file_is_404():
    resp = client.post("/api/import-labels", json={"file_id": "nope", "labels": "1\t2\tx"})
    assert resp.status_code == 404


def test_export_labels_endpoint():
    resp = client.post(
        "/api/export-labels",
        json={"tracks": [{"start": 0, "end": 120.5, "name": "A1 Song"}]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"labels": "0.000000\t120.500000\tA1 Song\n"}

    assert client.post("/api/export-labels", json={"tracks": []}).status_code == 422
