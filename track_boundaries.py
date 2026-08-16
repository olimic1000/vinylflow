"""VinylFlow — Track Boundary calculation.

Owns the Track Boundary concept end-to-end, independent of ffmpeg.  Three
pure functions over plain data turn audio analysis into Track Boundaries:

- ``parse_silence_log``       ffmpeg silencedetect stderr -> list[Gap]
- ``boundaries_from_gaps``    Gaps + total duration       -> list[Track]
- ``boundaries_from_durations``  Discogs track durations  -> list[Track]
- ``boundaries_for_track_count``  permissive-scan Gaps + a target Track
  count -> list[Track] (Smart Rescan: pick the likeliest separator Gaps)

A **Gap** is a ``(start, end)`` span of detected silence.  A Track occupies
the audio *between* consecutive Gaps.  The final Gap may be **open**
(``end=None``) when the side fades to silence at EOF — an open Gap yields no
trailing Track.

The ffmpeg subprocess sits outside this module, in
``AudioProcessor.detect_silence``; everything here is pure and testable
without a subprocess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Gap:
    """A span of detected silence within a Source Audio file.

    ``end is None`` marks an *open* Gap: the silence runs to end-of-file
    (the side fades out).  Only the final Gap in a sequence may be open.
    """

    start: float
    end: Optional[float] = None


class Track:
    """Represents a detected or split track — i.e. one Track Boundary."""

    def __init__(self, number: int, start: float, end: float):
        """
        Initialize track.

        Args:
            number: Track number (1-indexed)
            start: Start time in seconds
            end: End time in seconds
        """
        self.number = number
        self.start = start
        self.end = end
        self.duration = end - start
        self.vinyl_number = None  # Will be set during mapping (e.g., "A1", "B2")
        self.title = None  # Will be set from Discogs

    def format_time(self, seconds: float) -> str:
        """Format seconds as MM:SS."""
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"

    def __repr__(self):
        duration_str = self.format_time(self.duration)
        time_range = f"{self.format_time(self.start)} - {self.format_time(self.end)}"
        vinyl = f" [{self.vinyl_number}]" if self.vinyl_number else ""
        title = f" - {self.title}" if self.title else ""
        return f"Track {self.number}{vinyl}: {time_range} ({duration_str}){title}"


# ffmpeg can report a slightly negative silence_start for leading silence
# (e.g. "silence_start: -0.00768"), so accept a sign and clamp to 0 below.
_SILENCE_START_RE = re.compile(r"silence_start: (-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end: (-?[\d.]+)")


def parse_silence_log(stderr: str) -> List[Gap]:
    """Parse ffmpeg ``silencedetect`` stderr into a list of Gaps.

    Each ``silence_start`` is paired with the next ``silence_end`` in line
    order.  A trailing ``silence_start`` with no matching ``silence_end``
    becomes an open Gap (``end=None``) — the side faded to silence at EOF.
    Lines that aren't silence markers, and any stray ``silence_end`` with no
    pending start, are ignored.
    """
    gaps: List[Gap] = []
    pending_start: Optional[float] = None

    for line in stderr.split("\n"):
        if "silence_start" in line:
            match = _SILENCE_START_RE.search(line)
            if match:
                pending_start = max(0.0, float(match.group(1)))
        elif "silence_end" in line:
            match = _SILENCE_END_RE.search(line)
            if match and pending_start is not None:
                gaps.append(Gap(pending_start, float(match.group(1))))
                pending_start = None

    if pending_start is not None:
        gaps.append(Gap(pending_start, None))

    return gaps


def boundaries_from_gaps(
    gaps: List[Gap], total_duration: float, min_track_length: float
) -> List[Track]:
    """Compute Track Boundaries from the Gaps between audio.

    A Track occupies the audio between consecutive Gaps:
    lead-in before the first Gap, the spans between Gaps, and the lead-out
    after the last Gap.  An open final Gap (``end is None``) yields no
    trailing Track.  Any span shorter than ``min_track_length`` is dropped;
    track numbers stay sequential across dropped spans.
    """
    tracks: List[Track] = []
    num = 1

    # No silence detected: the whole file is one Track if it's long enough.
    if not gaps:
        if total_duration >= min_track_length:
            tracks.append(Track(num, 0.0, total_duration))
        return tracks

    # Lead-in: the Track before the first Gap (skipped if the side opens
    # with silence, i.e. the first Gap starts within the lead-in window).
    first = gaps[0]
    if first.start >= min_track_length:
        tracks.append(Track(num, 0.0, first.start))
        num += 1

    # Between Gaps: a Track runs from one Gap's end to the next Gap's start.
    # Only the final Gap may be open, so gaps[i].end is never None here.
    for i in range(len(gaps) - 1):
        start = gaps[i].end
        end = gaps[i + 1].start
        if end - start >= min_track_length:
            tracks.append(Track(num, start, end))
            num += 1

    # Lead-out: the Track after the last Gap — only if that Gap is closed.
    last = gaps[-1]
    if last.end is not None and total_duration - last.end >= min_track_length:
        tracks.append(Track(num, last.end, total_duration))

    return tracks


def boundaries_for_track_count(
    gaps: List[Gap],
    total_duration: float,
    min_track_length: float,
    target_count: int,
) -> List[Track]:
    """Pick the Gaps most likely to be real inter-track gaps so the result
    has ``target_count`` Tracks (best effort).

    Intended for a *permissive* silence scan whose Gaps are a superset of
    the real inter-track gaps (quiet musical passages show up too).  Real
    vinyl track gaps are the longest silences on a side, so candidates are
    ranked by duration and exactly ``target_count - 1`` separators are
    chosen greedily, skipping any that would leave a span shorter than
    ``min_track_length`` next to an already-chosen Gap.

    Edge Gaps — silence at the very start of the side, silence running to
    (or near) EOF, and open Gaps — are always kept: they trim lead-in and
    lead-out rather than separate two Tracks, so they cost nothing toward
    the separator budget.

    Returns whatever ``boundaries_from_gaps`` yields for the chosen Gaps,
    which may hold fewer than ``target_count`` Tracks when not enough
    feasible separators exist; it never holds more.
    """
    if target_count < 1:
        return []

    def _is_edge(gap: Gap) -> bool:
        if gap.start < min_track_length:
            return True  # leading silence: trims the lead-in
        if gap.end is None or total_duration - gap.end < min_track_length:
            return True  # trailing / open silence: trims the lead-out
        return False

    chosen = [g for g in gaps if _is_edge(g)]
    candidates = [g for g in gaps if not _is_edge(g)]
    # Longest silences first; start time breaks ties deterministically.
    candidates.sort(key=lambda g: (-(g.end - g.start), g.start))

    separators = 0
    for gap in candidates:
        if separators == target_count - 1:
            break
        # The chosen Gap nearest on each side bounds the spans this Gap
        # would create; both spans must be long enough to survive as Tracks.
        left = max((g.end for g in chosen if g.start < gap.start and g.end is not None), default=0.0)
        right = min((g.start for g in chosen if g.start > gap.start), default=total_duration)
        if gap.start - left >= min_track_length and right - gap.end >= min_track_length:
            chosen.append(gap)
            separators += 1

    chosen.sort(key=lambda g: g.start)
    return boundaries_from_gaps(chosen, total_duration, min_track_length)


def boundaries_from_durations(durations: List[float]) -> List[Track]:
    """Lay Tracks end-to-end from Discogs durations (the fallback path).

    Used when silence detection fails: each duration becomes a Track placed
    immediately after the previous one, starting at t=0, numbered
    sequentially.
    """
    tracks: List[Track] = []
    current_time = 0.0

    for num, duration in enumerate(durations, start=1):
        start = current_time
        end = current_time + duration
        tracks.append(Track(num, start, end))
        current_time = end

    return tracks
