"""Guided placement when Discogs gives no track lengths at all (synthetic envelopes).

Music is -20 dB (+-2 dB), inter-song gaps and side changes are surface noise at -60 dB,
10 ms frames.  A real box set (60 tracks, 12 sides, 4 h) is measured in
``tests/e2e/real_records_eval.py`` / the notes in AGENTS.md.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import track_boundaries as tb
from metadata_handler import DiscogsRelease
from track_boundaries import find_side_changes, guided_by_alignment, music_span, smooth_envelope

F = 0.01


def recording(sides, gap=3.0, change=120.0, lead=5.0, seed=1, gaps=None):
    """(db, music_start, music_end, side spans, noise stretches).

    ``sides`` = song lengths per side; ``change`` = non-music seconds between sides
    (run-out noise); ``gaps`` = optional per-side list of inter-song gap seconds."""
    rng = np.random.default_rng(seed)

    def music(d):
        return -20 + rng.normal(0, 2, round(d / F))

    def noise(d):
        return -60 + rng.normal(0, 1.5, round(d / F))

    parts, t = [noise(lead)], lead
    spans, stretches = [], []
    for s, songs in enumerate(sides):
        g = gaps[s] if gaps else gap
        begin = t
        for i, d in enumerate(songs):
            parts.append(music(d))
            t += d
            if i < len(songs) - 1:
                parts.append(noise(g))
                t += g
        spans.append((begin, t))
        if s < len(sides) - 1:
            parts.append(noise(change))
            stretches.append((t, t + change))
            t += change
    parts.append(noise(6.0))
    return np.concatenate(parts), lead, t, spans, stretches


def place(db, ms, me, n, side_breaks=None):
    return guided_by_alignment(db, F, ms, me, [None] * n, side_breaks=side_breaks)


def per_side(g, spans):
    mids = [(t.start + t.end) / 2 for t in g.tracks]
    return [sum(a <= m < b for m in mids) for a, b in spans]


def in_noise(g, stretches):
    return [t.number for t in g.tracks if any(a < (t.start + t.end) / 2 < b for a, b in stretches)]


def test_multi_side_recording_gets_the_right_count_per_side_and_nothing_in_the_noise():
    sides = [[170, 200, 140, 210, 180], [150, 190, 220, 160, 200], [180, 130, 240, 170, 190]]
    db, ms, me, spans, stretches = recording(sides)
    g = place(db, ms, me, 15, [5, 10])
    assert g is not None and g.model == "align-even" and len(g.tracks) == 15
    assert per_side(g, spans) == [5, 5, 5]
    assert in_noise(g, stretches) == []
    for k in (5, 10):  # a side change: the previous song ends within the tail cap of the stretch start
        a, b = stretches[k // 5 - 1]
        assert g.tracks[k - 1].end <= a + tb.MAX_TAIL_S + 0.5 and g.tracks[k].start >= b - 1.0
    # every cut sits in a quiet stretch, not in music
    for left, right in zip(g.tracks, g.tracks[1:]):
        mid = int((left.end + right.start) / 2 / F)
        assert db[mid - 20:mid + 20].max() < -40


def test_uneven_song_lengths_still_cut_at_the_clear_gaps():
    side = [60, 400, 90, 330, 120]  # nothing like an even split
    db, ms, me, spans, _ = recording([side], gap=4.0)
    g = place(db, ms, me, 5)
    ends = np.cumsum(side[:-1]) + 4.0 * np.arange(len(side) - 1) + ms
    for b, e in zip(g.boundaries, ends):
        assert e <= b.position <= e + 4.0 and b.snapped


def test_one_side_without_side_breaks_uses_clear_gaps():
    db, ms, me, spans, _ = recording([[120, 130, 125, 118, 135, 122]], gap=2.5)
    g = place(db, ms, me, 6)
    assert len(g.tracks) == 6 and all(b.snapped for b in g.boundaries)
    lens = [t.duration for t in g.tracks]
    assert min(lens) > 115 and max(lens) < 140


def test_a_side_with_fewer_real_gaps_than_tracks_falls_back_to_weak_spots():
    # side 2 has songs 1-2 segued (no gap): still five tracks, the segue cut at its quietest point
    rng = np.random.default_rng(3)
    db, ms, me, spans, stretches = recording([[150, 160, 170, 155, 165], [200, 150, 160, 170, 150]])
    a, b = spans[1]
    # remove the gap between side 2's first two songs: replace it with music
    gap_start = a + 200
    db[int(gap_start / F):int((gap_start + 3.0) / F)] = -20 + rng.normal(0, 2, round(3.0 / F))
    g = place(db, ms, me, 10, [5])
    assert len(g.tracks) == 10 and per_side(g, spans) == [5, 5]
    assert in_noise(g, stretches) == []
    assert sum(not b.snapped for b in g.boundaries) >= 1


def test_side_change_shorter_than_found_is_placed_by_the_alignment():
    # 2 side changes but only one long stretch (the other is a 3 s gap): sides 2/3 are one span
    sides = [[150, 160, 170, 155], [180, 150, 160, 170], [150, 190, 160, 170]]
    db, ms, me, spans, stretches = recording(sides, change=100.0)
    # shrink the second stretch to an ordinary gap
    a, b = stretches[1]
    db = np.concatenate([db[:int((a + 3) / F)], db[int(b / F):]])
    me -= b - a - 3
    g = place(db, ms, me, 12, [4, 8])
    assert len(g.tracks) == 12 and in_noise(g, stretches[:1]) == []
    assert sum(1 for t in g.tracks if t.end < stretches[0][0] + 5) == 4


def test_find_side_changes_takes_the_longest_in_time_order():
    db, ms, me, spans, stretches = recording([[100, 100], [100, 100], [100, 100]], change=60.0)
    sm = smooth_envelope(db, F)
    found = find_side_changes(sm, F, ms, me, 2)
    assert len(found) == 2
    for c, (a, b) in zip(found, stretches):
        assert c.start == pytest.approx(a, abs=2.0) and c.end == pytest.approx(b, abs=2.0)
    assert find_side_changes(sm, F, ms, me, 0) == []


def test_api_places_by_count_without_ghost_markers(tmp_path, monkeypatch):
    import uuid
    from fastapi.testclient import TestClient
    import backend.api as api

    sid = str(uuid.uuid4())
    source = tmp_path / "album.wav"
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename="album.wav",
                             source_duration=300, source_size=0)
    db, ms, me, spans, _ = recording([[120, 130, 125], [118, 135, 122]], change=60.0)

    async def cached(session):
        return db

    monkeypatch.setattr(api, "get_envelope", cached)
    try:
        r = TestClient(api.app).post("/api/analyze-guided", json={
            "file_id": sid, "expected_tracks": 6, "durations": [None] * 6, "side_breaks": [3]})
    finally:
        api.session_store.remove(sid)
    body = r.json()
    assert r.status_code == 200 and body["model"] == "align-even" and body["method"] == "count"
    assert len(body["tracks"]) == 6 and len(body["boundaries"]) == 5


def test_fixture_release_parses_to_twelve_sides_of_five():
    data = json.loads((Path(__file__).parent / "fixtures" / "discogs_release_7593948.json").read_text())
    rows = [SimpleNamespace(data=t, position=t["position"], title=t["title"], duration=t.get("duration", ""),
                            artists=[], credits=[]) for t in data["tracklist"]]
    rel = DiscogsRelease(SimpleNamespace(id=7593948, title=data["title"], year=data.get("year"), artists=[],
                                         labels=[], formats=[], images=[], genres=[], credits=[], data={},
                                         tracklist=rows))
    assert len(rel.tracks) == 60 and all(t.duration_seconds is None for t in rel.tracks)
    letters = [t.position[0] for t in rel.tracks]
    breaks = [i for i in range(1, len(letters)) if letters[i] != letters[i - 1]]  # as the frontend sends them
    assert breaks == list(range(5, 60, 5))


def test_a_span_with_more_candidates_than_the_cap_gives_up_for_the_count_ladder(monkeypatch):
    """A span this crowded means no sides were found on a very long capture: past
    MAX_EVEN_CANDIDATES guided_without_lengths returns None so the endpoint uses the count ladder.
    (A quality guard: MAX_ALIGN_CANDIDATES is what bounds the memory.)"""
    db, ms, me, _spans, _stretches = recording([[180] * 4])
    assert guided_by_alignment(db, F, ms, me, [None] * 4) is not None
    monkeypatch.setattr(tb, "MAX_EVEN_CANDIDATES", 1)
    assert guided_by_alignment(db, F, ms, me, [None] * 4) is None


def test_a_side_of_one_track_still_has_to_be_long_enough_to_be_a_song():
    """A one-track span has no gap to align, so nothing else checks its length: here a stretch of
    noise before a 2 s fragment is taken for the flip, and "side B" would be a 3 s track."""
    rng = np.random.default_rng(1)
    music = lambda d: -20 + rng.normal(0, 2, round(d / F))
    noise = lambda d: -60 + rng.normal(0, 1.5, round(d / F))
    db = np.concatenate([noise(5), music(180), noise(3), music(180), noise(3), music(180),
                         noise(15), music(2), noise(5)])
    ms, me = music_span(db, F)
    assert me > 5 + 180 * 3 + 6 + 15, "the fragment keeps the music span past the stretch"
    assert guided_by_alignment(db, F, ms, me, [None] * 4, [3]) is None


def test_a_real_side_of_one_track_is_placed():
    db, ms, me, spans, _stretches = recording([[180, 180, 180], [200]])
    placed = guided_by_alignment(db, F, ms, me, [None] * 4, [3])
    assert placed.model == "align-even" and len(placed.tracks) == 4
    assert abs(placed.tracks[3].duration - 200) < 5
