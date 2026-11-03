"""Tests for ``track_boundaries`` — pure Track Boundary calculation.

These exercise the three pure functions that turn audio analysis into
Track Boundaries, with no ffmpeg subprocess and no mocks:

- ``parse_silence_log``      stderr text  -> list[Gap]
- ``boundaries_from_gaps``   gaps + duration -> list[Track]
- ``boundaries_from_durations``  Discogs durations -> list[Track]

The headline case is the fade-to-EOF bug: when a side fades into silence
at end-of-file, ffmpeg emits a final ``silence_start`` with no matching
``silence_end``.  That open Gap must end the last real Track at the gap's
start and emit no trailing Track over the fade-out silence.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from audio_processor import AudioProcessor
from track_boundaries import (
    Gap,
    boundaries_for_track_count,
    boundaries_from_durations,
    boundaries_from_gaps,
    parse_silence_log,
)


# A realistic-looking ffmpeg silencedetect stderr fragment helper.
def _start(t: float) -> str:
    return f"[silencedetect @ 0x7f8b] silence_start: {t}"


def _end(t: float) -> str:
    return f"[silencedetect @ 0x7f8b] silence_end: {t} | silence_duration: 2.0"


def test_open_final_gap_ends_last_track_at_gap_start():
    """Fade-to-EOF: the open final Gap yields no trailing Track, and the
    last real Track ends at the gap's start (not extended to EOF)."""
    gaps = [Gap(180.0, 182.0), Gap(360.0, None)]
    tracks = boundaries_from_gaps(gaps, total_duration=400.0, min_track_length=30.0)

    assert [(t.start, t.end) for t in tracks] == [(0.0, 180.0), (182.0, 360.0)]
    assert [t.number for t in tracks] == [1, 2]
    # No Track spans the fade-out silence (360 -> 400).
    assert all(t.end <= 360.0 for t in tracks)


def test_no_gaps_long_enough_is_one_track():
    """No silence + duration >= min: the whole file is a single Track."""
    tracks = boundaries_from_gaps([], total_duration=200.0, min_track_length=30.0)

    assert len(tracks) == 1
    assert (tracks[0].number, tracks[0].start, tracks[0].end) == (1, 0.0, 200.0)


def test_no_gaps_too_short_is_no_tracks():
    """No silence + duration < min: nothing long enough to be a Track."""
    tracks = boundaries_from_gaps([], total_duration=20.0, min_track_length=30.0)

    assert tracks == []


def test_two_closed_gaps_make_three_tracks():
    """Two closed Gaps split the file into three sequentially-numbered Tracks."""
    gaps = [Gap(180.0, 182.0), Gap(360.0, 362.0)]
    tracks = boundaries_from_gaps(gaps, total_duration=600.0, min_track_length=30.0)

    assert [(t.number, t.start, t.end) for t in tracks] == [
        (1, 0.0, 180.0),
        (2, 182.0, 360.0),
        (3, 362.0, 600.0),
    ]


def test_lead_in_silence_drops_first_track():
    """A side that opens with silence yields no lead-in Track; numbering
    starts at the first real Track."""
    gaps = [Gap(2.0, 5.0), Gap(360.0, 363.0)]
    tracks = boundaries_from_gaps(gaps, total_duration=600.0, min_track_length=30.0)

    assert [(t.number, t.start, t.end) for t in tracks] == [
        (1, 5.0, 360.0),
        (2, 363.0, 600.0),
    ]


def test_short_middle_span_dropped_numbering_stays_sequential():
    """A between-Gaps span shorter than min is dropped, and the surviving
    Tracks keep sequential numbers (the dropped span doesn't consume a number)."""
    # Between gap[0].end (182) and gap[1].start (195) is only 13s < 30s -> dropped.
    gaps = [Gap(180.0, 182.0), Gap(195.0, 200.0), Gap(360.0, 363.0)]
    tracks = boundaries_from_gaps(gaps, total_duration=600.0, min_track_length=30.0)

    assert [(t.number, t.start, t.end) for t in tracks] == [
        (1, 0.0, 180.0),
        (2, 200.0, 360.0),
        (3, 363.0, 600.0),
    ]


# --- parse_silence_log -------------------------------------------------------


def test_parse_normal_pairs():
    """Each silence_start pairs with the following silence_end into a Gap."""
    stderr = "\n".join([_start(180.1), _end(182.4), _start(360.2), _end(363.5)])
    gaps = parse_silence_log(stderr)

    assert gaps == [Gap(180.1, 182.4), Gap(360.2, 363.5)]


def test_parse_leading_silence():
    """A side opening with silence is just a Gap whose start is near zero."""
    stderr = "\n".join([_start(0.0), _end(3.2), _start(360.0), _end(363.0)])
    gaps = parse_silence_log(stderr)

    assert gaps == [Gap(0.0, 3.2), Gap(360.0, 363.0)]


def test_parse_open_final_gap_on_fade_to_eof():
    """A trailing silence_start with no matching silence_end becomes an open
    Gap (end=None) — the fade-to-EOF case."""
    stderr = "\n".join([_start(180.0), _end(182.0), _start(360.0)])
    gaps = parse_silence_log(stderr)

    assert gaps == [Gap(180.0, 182.0), Gap(360.0, None)]


def test_parse_no_silence_is_empty():
    """No silencedetect lines at all -> no Gaps."""
    stderr = "Input #0, wav, from 'side.wav':\n  Duration: 00:20:00.00\n"
    assert parse_silence_log(stderr) == []


def test_parse_ignores_garbage_lines():
    """Non-silence lines (and an unmatched stray silence_end) are ignored."""
    stderr = "\n".join(
        [
            "frame= 100 fps=0.0 q=-1.0 size=N/A",
            _end(99.9),  # stray end with no preceding start -> ignored
            "[silencedetect @ 0x] silence_start: not_a_number",  # unparseable
            _start(180.0),
            "garbage in the middle",
            _end(182.0),
        ]
    )
    gaps = parse_silence_log(stderr)

    assert gaps == [Gap(180.0, 182.0)]


# --- boundaries_from_durations ----------------------------------------------


def test_durations_lay_tracks_back_to_back():
    """Each duration becomes a Track laid end-to-end from t=0, numbered
    sequentially, with each Track's length equal to its duration."""
    tracks = boundaries_from_durations([180.0, 200.0, 240.0])

    assert [(t.number, t.start, t.end) for t in tracks] == [
        (1, 0.0, 180.0),
        (2, 180.0, 380.0),
        (3, 380.0, 620.0),
    ]
    # The last Track ends at the sum of all durations.
    assert tracks[-1].end == 180.0 + 200.0 + 240.0


def test_durations_empty_is_no_tracks():
    assert boundaries_from_durations([]) == []


# --- boundaries_for_track_count (Smart Rescan) -------------------------------


def test_target_count_picks_longest_gaps():
    """A permissive scan surfaces short false-positive Gaps (quiet passages)
    alongside the real inter-track gaps; the longest Gaps win the separator
    slots and the false positives are ignored."""
    gaps = [
        Gap(150.0, 150.5),  # quiet passage, 0.5s — not a real gap
        Gap(300.0, 303.0),  # real gap, 3s
        Gap(450.0, 450.4),  # quiet passage, 0.4s
        Gap(600.0, 603.0),  # real gap, 3s
        Gap(900.0, 903.0),  # real gap, 3s
    ]
    tracks = boundaries_for_track_count(
        gaps, total_duration=1200.0, min_track_length=30.0, target_count=4
    )

    assert [(t.start, t.end) for t in tracks] == [
        (0.0, 300.0),
        (303.0, 600.0),
        (603.0, 900.0),
        (903.0, 1200.0),
    ]


def test_target_count_skips_gap_too_close_to_chosen_one():
    """A long Gap that would leave a span shorter than min_track_length next
    to an already-chosen Gap is skipped in favor of the next-longest."""
    gaps = [
        Gap(200.0, 205.0),  # longest, chosen first
        Gap(215.0, 219.0),  # 2nd longest, but only 10s after the first -> skipped
        Gap(400.0, 402.0),  # chosen instead
    ]
    tracks = boundaries_for_track_count(
        gaps, total_duration=600.0, min_track_length=30.0, target_count=3
    )

    assert [(t.start, t.end) for t in tracks] == [
        (0.0, 200.0),
        (205.0, 400.0),
        (402.0, 600.0),
    ]


def test_target_count_edge_gaps_trim_without_costing_separators():
    """Leading silence and an open fade-to-EOF Gap trim the lead-in/lead-out
    but don't count toward the separator budget."""
    gaps = [Gap(0.0, 5.0), Gap(300.0, 303.0), Gap(580.0, None)]
    tracks = boundaries_for_track_count(
        gaps, total_duration=600.0, min_track_length=30.0, target_count=2
    )

    assert [(t.start, t.end) for t in tracks] == [(5.0, 300.0), (303.0, 580.0)]


def test_target_count_best_effort_when_not_enough_gaps():
    """Fewer feasible separators than needed -> return what's achievable,
    never invent splits."""
    gaps = [Gap(300.0, 303.0)]
    tracks = boundaries_for_track_count(
        gaps, total_duration=600.0, min_track_length=30.0, target_count=4
    )

    assert len(tracks) == 2


def test_target_count_never_exceeds_target():
    """More real gaps than needed (e.g. the side has bonus silences): only
    target_count - 1 separators are used, so the result never overshoots."""
    gaps = [Gap(100.0, 102.0), Gap(300.0, 305.0), Gap(500.0, 502.0)]
    tracks = boundaries_for_track_count(
        gaps, total_duration=600.0, min_track_length=30.0, target_count=2
    )

    # The single separator slot goes to the longest Gap (300-305).
    assert [(t.start, t.end) for t in tracks] == [(0.0, 300.0), (305.0, 600.0)]


# --- detect_silence shell wiring --------------------------------------------


def test_detect_silence_wires_parse_and_boundaries():
    """The detect_silence shell threads ffmpeg stderr through parse_silence_log
    and boundaries_from_gaps — proving the fade-to-EOF fix flows end-to-end
    through the public method (ffmpeg + duration probe mocked)."""
    stderr = "\n".join([_start(180.0), _end(182.0), _start(360.0)])  # fade to EOF
    proc = AudioProcessor(min_track_length=30)

    with patch(
        "audio_processor.run_ffmpeg", return_value=SimpleNamespace(stderr=stderr)
    ), patch.object(AudioProcessor, "get_audio_duration", return_value=400.0):
        tracks = proc.detect_silence(Path("side.wav"))

    assert [(t.start, t.end) for t in tracks] == [(0.0, 180.0), (182.0, 360.0)]
    # The old _calculate_tracks would have produced (182.0, 400.0) here.
    assert all(t.end <= 360.0 for t in tracks)


# ---------------------------------------------------------------------------
# Envelope-based detection (synthetic numpy envelopes, 10 ms frames)
# ---------------------------------------------------------------------------

import numpy as np

from track_boundaries import (
    envelope_from_pcm,
    gaps_from_envelope,
    music_span,
)

FRAME = 0.01
LOUD = -15.0
QUIET = -60.0


def make_env(segments):
    """Build an envelope from [(seconds, level_db), ...]."""
    return np.concatenate(
        [np.full(int(round(sec / FRAME)), level, dtype=np.float32) for sec, level in segments]
    )


def test_envelope_from_pcm_levels():
    full = np.full(800, 16384, dtype=np.int16)  # half scale -> -6.02 dBFS
    silent = np.zeros(800, dtype=np.int16)
    db = envelope_from_pcm(np.concatenate([full, silent]), 80)
    assert len(db) == 20
    assert abs(db[0] - (-6.02)) < 0.05
    assert db[-1] == -120.0


def test_gaps_from_envelope_basic_and_min_duration():
    env = make_env([(5, LOUD), (2, QUIET), (5, LOUD), (0.2, QUIET), (5, LOUD)])
    gaps = gaps_from_envelope(env, FRAME, -40.0, 0.5)
    assert len(gaps) == 1
    assert abs(gaps[0].start - 5.0) < 0.02 and abs(gaps[0].end - 7.0) < 0.02


def test_gaps_from_envelope_open_gap_at_eof_and_leading_silence():
    env = make_env([(3, QUIET), (5, LOUD), (4, QUIET)])
    gaps = gaps_from_envelope(env, FRAME, -40.0, 1.0)
    assert gaps[0].start == 0.0 and abs(gaps[0].end - 3.0) < 0.02
    assert gaps[1].end is None and abs(gaps[1].start - 8.0) < 0.02


def test_gaps_from_envelope_short_trailing_run_ignored():
    env = make_env([(5, LOUD), (0.3, QUIET)])
    assert gaps_from_envelope(env, FRAME, -40.0, 1.0) == []


def test_music_span_trims_lead_in_and_run_out():
    env = make_env([(8, QUIET), (30, LOUD), (10, QUIET)])
    start, end = music_span(env, FRAME)
    assert abs(start - 8.0) < 0.05
    assert abs(end - 38.0) < 0.05


def test_music_span_ignores_click_in_lead_in():
    env = make_env([(3, QUIET), (0.08, -5.0), (5, QUIET), (30, LOUD), (6, QUIET)])
    start, end = music_span(env, FRAME)
    assert abs(start - 8.08) < 0.05
    assert abs(end - 38.08) < 0.05


def test_music_span_survives_brief_dips_in_music():
    env = make_env([(2, QUIET), (10, LOUD), (0.05, QUIET), (10, LOUD), (2, QUIET)])
    start, end = music_span(env, FRAME)
    assert abs(start - 2.0) < 0.05
    assert abs(end - 22.05) < 0.05


def test_music_span_no_music_returns_whole_file():
    env = make_env([(20, QUIET)])
    assert music_span(env, FRAME) == (0.0, 20.0)


def test_music_span_all_music():
    env = make_env([(20, LOUD)])
    start, end = music_span(env, FRAME)
    assert start == 0.0 and abs(end - 20.0) < 0.02




# --- refine_cuts -----------------------------------------------------------

from track_boundaries import REFINE_FLOOR_MARGIN_DB, Track, refine_cuts

FADE = -35.0  # between -40 and -30


def test_refine_keeps_fade_out_tail():
    env = make_env([(10, LOUD), (2, FADE), (3, QUIET), (10, LOUD), (3, QUIET)])
    out = refine_cuts(env, FRAME, [Track(1, 0.0, 10.0), Track(2, 15.0, 25.0)], -40.0)
    assert abs(out[0].end - 12.0) < 0.02
    assert out[1].start == 15.0 and abs(out[1].end - 25.0) < 0.02
    assert abs(out[0].duration - 12.0) < 0.02


def test_refine_keeps_fade_in():
    env = make_env([(3, QUIET), (2, FADE), (10, LOUD), (3, QUIET)])
    out = refine_cuts(env, FRAME, [Track(1, 5.0, 15.0)], -40.0)
    assert abs(out[0].start - 3.0) < 0.02
    assert abs(out[0].end - 15.0) < 0.02


def test_refine_neighbours_never_overlap():
    # Gap region stays above the threshold the whole way but is flat: that is a
    # noise floor, not a fade, so neither neighbour walks into it (it used to
    # meet in the middle at 12.0, the bug in review #82).
    env = make_env([(10, LOUD), (4, FADE), (10, LOUD)])
    out = refine_cuts(env, FRAME, [Track(1, 0.0, 10.0), Track(2, 14.0, 24.0)], -40.0)
    assert out[0].end <= out[1].start
    assert out[0].end == 10.0 and out[1].start == 14.0
    assert out[0].start == 0.0 and abs(out[1].end - 24.0) < 0.02


def test_refine_fades_on_both_sides_widen_without_overlap():
    # Fade-out then fade-in through a short gap: both walks follow their fade
    # and stop at the gap's own floor + margin, never crossing.
    fade_out = np.linspace(-15.0, -38.0, 200, dtype=np.float32)
    env = np.concatenate([make_env([(10, LOUD)]), fade_out, fade_out[::-1], make_env([(10, LOUD)])])
    out = refine_cuts(env, FRAME, [Track(1, 0.0, 10.5), Track(2, 13.5, 24.0)], -40.0)
    assert out[0].end > 10.5 and out[1].start < 13.5
    assert out[0].end <= out[1].start


def test_refine_isolated_click_not_absorbed():
    env = make_env([(2, QUIET), (0.05, -5.0), (3, QUIET), (10, LOUD), (3, QUIET), (0.05, -5.0), (2, QUIET)])
    start = 5.05
    out = refine_cuts(env, FRAME, [Track(1, start, start + 10.0)], -40.0)
    assert abs(out[0].start - start) < 0.02
    assert abs(out[0].end - (start + 10.0)) < 0.02


def test_refine_respects_file_edges_and_count():
    # A file that is flat above the threshold everywhere: lead-in and run-out
    # are its noise floor, so nothing is widened (it used to walk to 0 and the
    # end of the file, the bug in review #82); count and numbering are kept.
    env = make_env([(10, FADE)])
    tracks = [Track(1, 2.0, 5.0), Track(2, 6.0, 8.0)]
    out = refine_cuts(env, FRAME, tracks, -40.0)
    assert [t.number for t in out] == [1, 2]
    assert [(t.start, t.end) for t in out] == [(2.0, 5.0), (6.0, 8.0)]
    assert 0.0 <= out[0].start and out[-1].end <= 10.0
    assert out[0].end <= out[1].start


def test_refine_empty():
    assert refine_cuts(make_env([(5, LOUD)]), FRAME, [], -40.0) == []


# Noisy records (review #82): surface noise above the user's threshold.  The walk must stop
# at the gap's own floor + REFINE_FLOOR_MARGIN_DB instead of crossing the whole gap.
NOISE = -35.0
REVIEW_TRACKS = [Track(1, 5.0, 65.0), Track(2, 68.0, 128.0), Track(3, 131.0, 191.0)]


def _review_layout(noise):
    """The review's repro: music at 5-65 / 68-128 / 131-191 s, ``noise(seconds)`` in between."""
    loud = lambda sec: make_env([(sec, LOUD)])
    return np.concatenate([noise(5), loud(60), noise(3), loud(60), noise(3), loud(60), noise(8)])


def _cuts(tracks):
    return [(t.start, t.end) for t in tracks]


def test_refine_does_not_walk_across_a_noise_floor():
    env = _review_layout(lambda sec: make_env([(sec, NOISE)]))
    out = refine_cuts(env, FRAME, REVIEW_TRACKS, -40.0)
    for (s, e), (es, ee) in zip(_cuts(out), _cuts(REVIEW_TRACKS)):
        assert abs(s - es) <= FRAME and abs(e - ee) <= FRAME


def test_refine_follows_a_fade_down_to_the_noise_floor():
    # Fade -15 -> -35 dB over 65-68 s, then noise; the cut was placed at 65.75 (-20 dB).
    fade = np.linspace(LOUD, NOISE, 300, dtype=np.float32)
    env = np.concatenate([make_env([(5, NOISE), (60, LOUD)]), fade,
                          make_env([(3, NOISE), (60, LOUD), (8, NOISE)])])
    out = refine_cuts(env, FRAME, [Track(1, 5.0, 65.75), Track(2, 71.0, 131.0)], -40.0)
    # The fade drops 20 dB over 2.99 s; the walk stops where it reaches noise + margin.
    stop_db = NOISE + REFINE_FLOOR_MARGIN_DB
    expected_end = 65.0 + (LOUD - stop_db) / 20.0 * 2.99
    assert abs(out[0].end - expected_end) <= 2 * FRAME
    assert out[1].start == 71.0  # not the gap's midpoint (69.5), which the old walk reached


def test_refine_ignores_jitter_in_the_noise_floor():
    rng = np.random.default_rng(1)
    noise = lambda sec: (NOISE + rng.uniform(-2.0, 2.0, int(round(sec / FRAME)))).astype(np.float32)
    out = refine_cuts(_review_layout(noise), FRAME, REVIEW_TRACKS, -40.0)
    for (s, e), (es, ee) in zip(_cuts(out), _cuts(REVIEW_TRACKS)):
        assert abs(s - es) < 0.5 and abs(e - ee) < 0.5


def test_refine_short_fade_dominated_gap_stops_early():
    # Accepted trade-off: in a 1 s gap that is 0.7 s fade (-20 -> -35) + 0.3 s noise, the
    # median sits inside the fade (-30.8 dB), so the end stops at 65.36, ~0.35 s before the
    # fade meets the noise (its last part is already > 12 dB down).
    fade = np.linspace(-20.0, NOISE, 70, dtype=np.float32)
    env = np.concatenate([make_env([(5, NOISE), (60, LOUD)]), fade,
                          make_env([(0.3, NOISE), (60, LOUD), (8, NOISE)])])
    out = refine_cuts(env, FRAME, [Track(1, 5.0, 65.0), Track(2, 66.0, 126.0)], -40.0)
    assert abs(out[0].end - 65.36) <= FRAME
    assert out[1].start == 66.0


def test_refine_crackly_lead_in_keeps_the_first_start():
    # Lead-in crackle below the threshold on the whole (median ~-45, p90 ~-41) with clicks
    # above it: the threshold still governs, and the start does not move into the crackle.
    rng = np.random.default_rng(1)
    crackle = (-45.0 + rng.laplace(0, 2.5, 500)).clip(-60, -20).astype(np.float32)
    assert np.median(crackle) < -44 and -42 < np.percentile(crackle, 90) < -40
    env = np.concatenate([crackle, make_env([(60, LOUD), (8, -45.0)])])
    out = refine_cuts(env, FRAME, [Track(1, 5.0, 65.0)], -40.0)
    assert out[0].start == 5.0
