"""Best-match alignment of Discogs lengths to gaps (synthetic envelopes, no audio files).

Music is -20 dB (+-2 dB jitter), gaps are surface noise at -60 dB; envelopes
have 10 ms frames like the real one.  Real-record numbers: tests/e2e/real_records_eval.py.
"""
import numpy as np
import pytest

import track_boundaries as tb
from track_boundaries import (
    GapCandidate,
    align_durations_to_gaps,
    gap_candidates,
    guided_by_alignment,
    music_span,
    onset_cut,
    smooth_envelope,
)

F = 0.01


def album(lengths, gaps=3.0, lead=5.0, runout=6.0, clicks=(), seed=0, level=-60.0):
    """(db, gap spans, music_start, music_end); ``gaps`` is one value or one per boundary."""
    rng = np.random.default_rng(seed)
    gaps = [gaps] * (len(lengths) - 1) if np.isscalar(gaps) else list(gaps)

    def seg(kind, dur):
        n = round(dur / F)
        if kind == "m":
            return -20 + rng.normal(0, 2, n)
        if level <= -100:
            return np.full(n, -120.0)
        return level + rng.normal(0, 1.5, n)

    parts, spans, t = [seg("g", lead)], [], lead
    for i, d in enumerate(lengths):
        parts.append(seg("m", d))
        t += d
        if i < len(gaps):
            parts.append(seg("g", gaps[i]))
            spans.append((t, t + gaps[i]))
            t += gaps[i]
    parts.append(seg("g", runout))
    db = np.concatenate(parts)
    for c in clicks:
        db[int(c / F):int(c / F) + 2] = -25
    return db, spans, lead, lead + sum(lengths) + sum(gaps)


def place(db, music, durations, side_breaks=None):
    return guided_by_alignment(db, F, music[0], music[1], durations, side_breaks=side_breaks)


def test_clicks_inside_a_gap_do_not_split_it():
    db, spans, ms, me = album([60, 50], gaps=4.0)
    gap = spans[0]
    db[int((gap[0] + 0.5) / F):int((gap[0] + 0.5) / F) + 3] = -25
    db[int((gap[0] + 2.0) / F):int((gap[0] + 2.0) / F) + 2] = -30
    strong = [c for c in gap_candidates(smooth_envelope(db, F), F, ms, me) if c.strong]
    assert len(strong) == 1
    assert strong[0].start == pytest.approx(gap[0], abs=0.3) and strong[0].end == pytest.approx(gap[1], abs=0.3)


def test_fragmented_flip_gap_is_one_candidate_and_anchors_the_sides():
    # 60 s flip with a once-per-revolution thump (0.1 s at -35 dB every 1.8 s)
    lengths = [100, 100, 100, 100]
    db, spans, ms, me = album(lengths, gaps=[3.0, 60.0, 3.0])
    flip = spans[1]
    for t in np.arange(flip[0] + 1.0, flip[1] - 1.0, 1.8):
        db[int(t / F):int(t / F) + 10] = -35
    sm = smooth_envelope(db, F)
    strong = [c for c in gap_candidates(sm, F, ms, me) if c.strong]
    assert len([c for c in strong if flip[0] - 1 <= c.start <= flip[1]]) == 1
    # lengths wrong by 15 s on both sides of the flip: the side break still picks the flip
    g = place(db, (ms, me), [115.0, 85.0, 115.0, 85.0], side_breaks=[2])
    assert g.boundaries[1].gap.start == pytest.approx(flip[0], abs=0.3)
    assert len(g.tracks) == 4


def test_one_wrong_length_does_not_shift_the_other_cuts():
    lengths = [60, 50, 70, 40, 55, 45]
    db, spans, ms, me = album(lengths)
    exact = place(db, (ms, me), [float(x) for x in lengths])
    off = place(db, (ms, me), [60.0, 50.0, 70.0, 40.0 + 25.0, 55.0, 45.0])  # one length 25 s long
    for k, (a, b) in enumerate(zip(exact.tracks, off.tracks)):
        assert (a.start, a.end) == pytest.approx((b.start, b.end), abs=0.01), k
    for b, span in zip(exact.boundaries, spans):
        assert b.snapped and b.gap.start == pytest.approx(span[0], abs=0.3)


def test_missing_lengths_cost_nothing():
    lengths = [60, 50, 70, 40, 55]
    db, spans, ms, me = album(lengths)
    g = place(db, (ms, me), [60.0, None, 70.0, None, 55.0])
    assert [round(b.gap.start) for b in g.boundaries] == [round(s[0]) for s in spans]
    assert place(db, (ms, me), [None] * 5) is None  # nothing known: nothing to align


def test_segue_without_a_gap_is_cut_near_the_predicted_point_at_its_dip():
    lengths = [60, 50, 70, 40]
    db, spans, ms, me = album(lengths, gaps=[3.0, 0.0, 3.0])
    # the 0.0 s "gap" is a segue: only a short dip at the join
    seg_at = spans[1][0]
    db[int((seg_at - 0.15) / F):int((seg_at + 0.15) / F)] = -33
    g = place(db, (ms, me), [60.0, 50.0, 70.0, 40.0])
    b = g.boundaries[1]
    assert not b.snapped and b.gap is None
    assert b.position == pytest.approx(seg_at, abs=0.5)
    assert g.tracks[1].end == g.tracks[2].start  # contiguous


def test_contiguous_cut_lands_pre_roll_before_the_next_onset():
    lengths = [60, 50, 70]
    db, spans, ms, me = album(lengths, gaps=2.0)
    g = place(db, (ms, me), [60.0, 50.0, 70.0])
    for k, span in enumerate(spans):
        assert g.tracks[k].end == g.tracks[k + 1].start
        assert g.tracks[k + 1].start == pytest.approx(span[1] - tb.PRE_ROLL_S, abs=0.2)
    # expected = this track's own length from where its music starts (not its padded cut)
    assert g.boundaries[0].expected == pytest.approx(ms + 60.0)
    assert g.boundaries[1].expected == pytest.approx(spans[0][1] + 50.0, abs=0.2)


def test_short_gap_is_shared_so_both_tracks_keep_some_silence():
    db, spans, ms, me = album([60, 50], gaps=0.9)   # shorter than PRE_ROLL_S + POST_ROLL_S
    g = place(db, (ms, me), [60.0, 50.0])
    cut, (quiet_from, onset) = g.tracks[0].end, spans[0]
    assert g.tracks[1].start == cut
    assert quiet_from < cut < onset
    share = tb.POST_ROLL_S / (tb.PRE_ROLL_S + tb.POST_ROLL_S)
    assert cut == pytest.approx(quiet_from + (onset - quiet_from) * share, abs=0.2)


def test_a_click_just_before_the_onset_is_not_the_onset():
    db, spans, ms, me = album([60, 50], gaps=4.0)
    gap = spans[0]
    db[int((gap[1] - 1.0) / F):int((gap[1] - 1.0) / F) + 2] = -25
    sm = smooth_envelope(db, F)
    cand = GapCandidate(gap[0], gap[1], 1.0, True)
    prev_end, start = onset_cut(db, sm, F, cand)
    assert start == pytest.approx(gap[1] - tb.PRE_ROLL_S, abs=0.2)


def test_long_gap_is_not_glued_onto_the_previous_track():
    db, spans, ms, me = album([60, 50, 70], gaps=[3.0, 20.0])
    g = place(db, (ms, me), [60.0, 50.0, 70.0])
    long_gap = spans[1]
    assert g.tracks[1].end == pytest.approx(long_gap[0] + tb.MAX_TAIL_S, abs=0.3)
    assert g.tracks[2].start == pytest.approx(long_gap[1] - tb.PRE_ROLL_S, abs=0.2)
    assert g.tracks[2].start - g.tracks[1].end > 10.0  # the silence in between is dropped
    assert g.boundaries[1].position == pytest.approx((g.tracks[1].end + g.tracks[2].start) / 2)


def test_digitally_silent_gap_keeps_no_tail():
    db, spans, ms, me = album([60, 50], gaps=3.0, level=-120.0)
    g = place(db, (ms, me), [60.0, 50.0])
    assert g.tracks[0].end == pytest.approx(spans[0][0], abs=0.05)
    assert g.tracks[1].start == pytest.approx(spans[0][1] - tb.PRE_ROLL_S, abs=0.2)


@pytest.mark.parametrize("durations", [
    [60.0, 50.0, 70.0, 40.0, 55.0, 45.0],
    [10.0, 500.0, 5.0, 90.0, 55.0, 45.0],
    [60.0, None, None, None, None, 45.0],
])
def test_always_exactly_n_ordered_tracks(durations):
    db, _, ms, me = album([60, 50, 70, 40, 55, 45])
    g = place(db, (ms, me), durations)
    assert len(g.tracks) == 6 and len(g.boundaries) == 5 and g.model == "align"
    # Lead-in / run-out padding: EDGE_PAD_S of silence before the first and after the last track.
    assert g.tracks[0].start == pytest.approx(max(0.0, ms - tb.EDGE_PAD_S))
    assert g.tracks[-1].end == pytest.approx(min(len(db) * F, me + tb.EDGE_PAD_S))
    for a, b in zip(g.tracks, g.tracks[1:]):
        assert a.start < a.end <= b.start < b.end


def test_too_many_tracks_for_the_audio_returns_none():
    db, _, ms, me = album([60, 50])
    assert place(db, (ms, me), [5.0] * 60) is None
    assert align_durations_to_gaps([], ms, me, [60.0, 50.0]) is None


def test_alignment_is_fast_on_a_45_minute_recording():
    import time
    lengths = [225] * 12
    db, _, ms, me = album(lengths, gaps=3.0)
    t0 = time.time()
    g = place(db, (ms, me), [float(x) for x in lengths], side_breaks=[6])
    assert g is not None and time.time() - t0 < 5.0


def test_guided_endpoint_falls_back_to_the_gap_model(monkeypatch, tmp_path):
    import uuid
    from fastapi.testclient import TestClient
    import backend.api as api

    sid = str(uuid.uuid4())
    source = tmp_path / "album.wav"
    source.touch()
    api.session_store.create(session_id=sid, source_audio=source, source_filename="album.wav",
                             source_duration=300, source_size=0)
    db, spans, _, _ = album([60, 50, 70], gaps=3.0)

    async def cached(session):
        return db

    monkeypatch.setattr(api, "get_envelope", cached)
    monkeypatch.setattr(api, "guided_by_alignment", lambda *a, **k: None)
    try:
        r = TestClient(api.app).post("/api/analyze-guided", json={
            "file_id": sid, "expected_tracks": 3, "durations": [60, 50, 70]})
    finally:
        api.session_store.remove(sid)
    body = r.json()
    assert r.status_code == 200 and body["method"] == "durations" and body["model"] == "gap"
    assert len(body["tracks"]) == 3


def test_a_soft_intro_merged_into_the_gap_is_not_clipped():
    """Short quiet notes (rests < 1 s) after the silence merge into one gap candidate; the
    song starts at the first of them, not after the merged candidate (was clipped ~3 s)."""
    db, spans, ms, me = album([60, 50], gaps=3.0)
    intro = spans[0][1]
    for k in range(3):   # three 0.3 s notes at -35 dB, 0.6 s rests, then the song proper
        a = int((intro + k * 0.9) / F)
        db[a:a + 30] = -35.0
        db[a + 30:a + 90] = -60.0
    db[int((intro + 2.7) / F):int((intro + 50) / F)] = -20.0
    g = place(db, (ms, me), [60.0, 50.0])
    assert g.tracks[1].start == pytest.approx(intro - tb.PRE_ROLL_S, abs=0.2)


def test_run_out_thumps_in_a_side_flip_are_not_the_next_song():
    """Once-per-revolution thumps (0.9 s every 1.8 s) after side A's last song, then the
    flip's near-digital silence: side B starts at its music, not at a thump."""
    db, spans, ms, me = album([60, 60], gaps=40.0)
    flip_start, flip_end = spans[0]
    t = flip_start + 2.0
    while t < flip_start + 20.0:
        db[int(t / F):int((t + 0.9) / F)] = -48.0
        t += 1.8
    db[int((flip_start + 21.0) / F):int(flip_end / F)] = -85.0
    g = place(db, (ms, me), [60.0, 60.0], side_breaks=[1])
    assert g.tracks[1].start == pytest.approx(flip_end - tb.PRE_ROLL_S, abs=0.2)


def _side_flip(lead_in_s, music_db=-18.0, crackle_db=-35.0, seed=3):
    """Two 60 s sides; the flip: fade gap, arm-up silence, needle-drop thump, lead-in crackle."""
    db, spans, ms, me = album([60, 60], gaps=40.0, seed=seed)
    rng = np.random.default_rng(seed)
    flip_start, flip_end = spans[0]
    drop = flip_end - lead_in_s - 0.4
    db[int((flip_start + 3) / F):int(drop / F)] = -80.0                       # needle lifted
    db[int(drop / F):int((drop + 0.4) / F)] = -10.0                           # the thump
    n = int(flip_end / F) - int((drop + 0.4) / F)
    db[int((drop + 0.4) / F):int(flip_end / F)] = crackle_db + rng.normal(0, 1.5, n)
    db[int(flip_end / F):int((flip_end + 60) / F)] = music_db + rng.normal(0, 2, int(60 / F))
    return db, ms, me, drop, flip_end


def test_side_b_starts_after_the_needle_drop_and_lead_in():
    db, ms, me, drop, music = _side_flip(lead_in_s=6.0)
    g = place(db, (ms, me), [60.0, 60.0], side_breaks=[1])
    assert g.tracks[1].start == pytest.approx(music - tb.PRE_ROLL_S, abs=0.2)


def test_a_song_starting_right_after_the_thump_keeps_its_start():
    db, ms, me, drop, music = _side_flip(lead_in_s=0.0)
    g = place(db, (ms, me), [60.0, 60.0], side_breaks=[1])
    assert g.tracks[1].start == pytest.approx(drop - tb.PRE_ROLL_S, abs=0.3)


def test_a_quiet_opening_barely_above_the_crackle_is_not_skipped():
    """A 6 dB step could be soft music: keep the needle drop rather than risk clipping."""
    db, ms, me, drop, music = _side_flip(lead_in_s=6.0, music_db=-29.0)
    g = place(db, (ms, me), [60.0, 60.0], side_breaks=[1])
    assert g.tracks[1].start < music - 3.0
