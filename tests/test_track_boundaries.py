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
