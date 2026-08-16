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


# --- detect_tracks_for_count shell wiring ------------------------------------


def _stderr_with_duration(*lines: str) -> str:
    return "\n".join(["  Duration: 00:10:00.00, start: 0.000000", *lines])


def test_detect_tracks_for_count_escalates_until_match():
    """The first (permissive) pass misses a gap; the second pass finds both,
    matches the target, and reports the settings that worked."""
    first_pass = _stderr_with_duration(_start(180.0), _end(182.0))
    second_pass = _stderr_with_duration(_start(180.0), _end(182.0), _start(400.0), _end(403.0))
    proc = AudioProcessor(min_track_length=30)

    with patch(
        "audio_processor.run_ffmpeg",
        side_effect=[SimpleNamespace(stderr=first_pass), SimpleNamespace(stderr=second_pass)],
    ):
        tracks, info = proc.detect_tracks_for_count(Path("side.wav"), target_count=3)

    assert [(t.start, t.end) for t in tracks] == [
        (0.0, 180.0),
        (182.0, 400.0),
        (403.0, 600.0),
    ]
    assert info["matched"] is True
    assert info["silence_threshold"] == -25.0


def test_detect_tracks_for_count_returns_closest_when_no_match():
    """No pass reaches the target: the closest result is returned with
    matched=False so the UI can say so honestly."""
    one_gap = _stderr_with_duration(_start(180.0), _end(182.0))
    proc = AudioProcessor(min_track_length=30)

    with patch(
        "audio_processor.run_ffmpeg",
        side_effect=[SimpleNamespace(stderr=one_gap)] * 3,
    ):
        tracks, info = proc.detect_tracks_for_count(Path("side.wav"), target_count=5)

    assert len(tracks) == 2
    assert info["matched"] is False


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
