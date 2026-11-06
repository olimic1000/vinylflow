"""VinylFlow — Track Boundary calculation.

Owns the Track Boundary concept end-to-end, independent of ffmpeg.  Three
pure functions over plain data turn audio analysis into Track Boundaries:

- ``parse_silence_log``       ffmpeg silencedetect stderr -> list[Gap]
- ``boundaries_from_gaps``    Gaps + total duration       -> list[Track]
- ``boundaries_from_durations``  Discogs track durations  -> list[Track]
- ``boundaries_for_track_count``  permissive-scan Gaps + a target Track
  count -> list[Track] (Smart Rescan: pick the likeliest separator Gaps)

Envelope-based helpers (one decode, then everything in memory):

- ``envelope_from_pcm``       int16 PCM -> per-frame RMS dBFS array
- ``gaps_from_envelope``      envelope + threshold -> list[Gap]
- ``music_span``              envelope -> (music_start, music_end)
- ``boundaries_from_expected_durations``  Gaps + music span + Discogs
  durations -> GuidedBoundaries (Tracks snapped to nearby Gaps)
- ``guided_by_alignment``     envelope + Discogs durations (any may be
  unknown) -> GuidedBoundaries by best-match alignment of every track
  length to the gaps (``smooth_envelope``, ``gap_candidates``,
  ``align_durations_to_gaps``, ``onset_cut``)
- ``refine_cuts``             widen Tracks over audio still above the
  user's threshold (quiet fades found at a permissive threshold)

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
from typing import List, Optional, Sequence, Tuple

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


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


# ---------------------------------------------------------------------------
# Envelope-based detection
# ---------------------------------------------------------------------------

ENVELOPE_FLOOR_DB = -120.0


def envelope_from_pcm(samples: np.ndarray, frame_samples: int) -> np.ndarray:
    """Per-frame RMS level in dBFS (float32) from mono int16 PCM.

    A trailing partial frame is dropped.  Digital silence maps to
    ``ENVELOPE_FLOOR_DB``.  Works in chunks so a 90-minute side does not
    need a float copy of the whole signal.
    """
    n_frames = len(samples) // frame_samples
    out = np.empty(n_frames, dtype=np.float32)
    chunk_frames = max(1, (1 << 20) // frame_samples)
    for i in range(0, n_frames, chunk_frames):
        j = min(n_frames, i + chunk_frames)
        block = samples[i * frame_samples : j * frame_samples].astype(np.float32) / 32768.0
        block = block.reshape(-1, frame_samples)
        mean_sq = np.mean(block * block, axis=1)
        out[i:j] = 10.0 * np.log10(np.maximum(mean_sq, 1e-12))
    return np.maximum(out, ENVELOPE_FLOOR_DB)


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Half-open index runs ``[start, end)`` where ``mask`` is True."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask.astype(bool), [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[0::2], edges[1::2])]


def gaps_from_envelope(
    db: np.ndarray, frame_s: float, threshold_db: float, min_silence_s: float
) -> List[Gap]:
    """Gaps from an envelope, mirroring ffmpeg ``silencedetect``.

    A run of frames below ``threshold_db`` lasting at least
    ``min_silence_s`` is a Gap.  A run that reaches the end of the
    envelope is an open Gap (``end=None``).
    """
    gaps: List[Gap] = []
    n = len(db)
    for a, b in _runs(db < threshold_db):
        if (b - a) * frame_s + 1e-9 < min_silence_s:
            continue
        gaps.append(Gap(a * frame_s, None if b >= n else b * frame_s))
    return gaps


# Vinyl surface noise and an empty lead-in/run-out groove normally sit
# around -50..-60 dBFS; music (even quiet passages) spends its time above
# -35.  -38 dB is just above the noise and below quiet music, and matches
# the app's default silence threshold of -40 dB.  Requiring ~1 s of
# sustained level (90% of frames, so brief dips between notes don't break
# it) rejects needle-drop thumps and clicks, which last tens to a few
# hundred milliseconds.
MUSIC_FLOOR_DB = -38.0
MUSIC_SUSTAIN_S = 1.0
MUSIC_SUSTAIN_FRACTION = 0.9


def music_span(
    db: np.ndarray,
    frame_s: float,
    floor_db: float = MUSIC_FLOOR_DB,
    sustain_s: float = MUSIC_SUSTAIN_S,
    sustain_fraction: float = MUSIC_SUSTAIN_FRACTION,
) -> Tuple[float, float]:
    """``(music_start, music_end)`` with lead-in and run-out trimmed.

    Music starts where the level first stays above ``floor_db`` for
    ``sustain_s`` (at least ``sustain_fraction`` of the frames in that
    window) and ends at the last such point.  Returns ``(0, total)`` when
    no sustained music is found.
    """
    total = len(db) * frame_s
    n = len(db)
    win = max(1, int(round(sustain_s / frame_s)))
    if n < win:
        return 0.0, total

    above = (db > floor_db).astype(np.int32)
    csum = np.concatenate(([0], np.cumsum(above)))
    counts = csum[win:] - csum[:-win]  # above-count of window starting at i
    ok = np.flatnonzero(counts >= sustain_fraction * win)
    if ok.size == 0:
        return 0.0, total

    first_win, last_win = int(ok[0]), int(ok[-1])
    # Tighten to the actual first/last above-floor frame inside the windows.
    start_idx = first_win + int(np.argmax(above[first_win : first_win + win]))
    tail = above[last_win : last_win + win]
    end_idx = last_win + (len(tail) - int(np.argmax(tail[::-1])))
    return start_idx * frame_s, min(end_idx, n) * frame_s


@dataclass(frozen=True)
class BoundaryInfo:
    """How one internal Track Boundary was placed."""

    index: int  # 1 = boundary between track 1 and 2
    expected: float  # where the Discogs durations put it ('align': this track's start + its own length)
    snapped: bool
    gap: Optional[Gap]  # the chosen Gap when snapped
    position: float  # where the cut landed: gap midpoint if snapped; 'align': midway between the tracks


@dataclass
class GuidedBoundaries:
    tracks: List[Track]
    boundaries: List[BoundaryInfo]
    scale: float  # scale actually applied to the Discogs durations
    scale_clamped: bool  # True when span/total was outside the clamp (scale=1.0)
    raw_scale: float
    model: str = "scale"  # 'align' (best match), 'gap', 'scale' (proportional), or 'sides' (anchored flip)
    gap_s: float = 0.0  # per-gap silence assumed by 'gap'; 0.0 for the other models


# Snap scoring: a Gap's length (capped) minus a penalty per second of
# distance from the expected time.  1 s away costs 0.25 s of gap length, so
# a clearly longer gap beats a slightly closer short one, but a long gap at
# the edge of the window does not beat a decent gap right on target.
SNAP_GAP_CAP_S = 6.0
SNAP_DISTANCE_PENALTY = 0.25
SNAP_MIN_SEPARATION_S = 1.0

# Largest per-gap residual (seconds) still read as "Discogs lengths exclude the
# silent gaps between tracks"; beyond it the lengths are assumed wrong/partial.
MAX_GAP_S = 8.0
# A side hint alone must not turn an ordinary inter-song gap into an anchor.
SIDE_FLIP_MIN_GAP_S = 10.0


def boundaries_from_expected_durations(
    gaps: List[Gap],
    music_start: float,
    music_end: float,
    durations: List[float],
    tolerance_s: float = 10.0,
    scale_clamp: Tuple[float, float] = (0.9, 1.1),
    side_breaks: Optional[List[int]] = None,
) -> GuidedBoundaries:
    """Place Track Boundaries from Discogs durations, snapped to Gaps.

    Discogs lengths usually exclude the 2-4 s silent gaps between vinyl
    tracks.  With ``n`` tracks, ``residual = span - sum(durations)`` and
    ``0 <= residual / (n - 1) <= MAX_GAP_S``, the **gap model** applies:
    ``gap = residual / (n - 1)``, scale 1.0, and expected boundary ``k`` is
    the middle of gap ``k``: ``music_start + cum_k + (k-1)*gap + gap/2``.
    Otherwise (negative residual, or a per-gap residual above MAX_GAP_S) the
    **scale model** applies: cumulative durations are scaled onto the span
    (scale clamped to ``scale_clamp``; outside it the scale is 1.0 and
    ``scale_clamped`` is reported).  ``model`` / ``gap_s`` report which.

    Each internal boundary is snapped to the best closed Gap whose distance
    from the expected time is within ``tolerance_s`` (also limited to 40% of
    the shorter neighbouring track), scored by length minus a distance
    penalty, and kept strictly increasing.  With no usable Gap the cut falls
    on the expected time.

    Always returns exactly ``len(durations)`` Tracks.

    ``side_breaks`` are the track counts before known Discogs side changes.
    A long silence near such a boundary anchors the two sides independently,
    so flip time is not distributed over the other expected markers. Without
    both pieces of evidence the ordinary gap/scale model is preserved.
    """
    if not durations:
        raise ValueError("durations must not be empty")

    total = float(sum(durations))
    span = max(0.0, music_end - music_start)
    raw_scale = span / total if total > 0 else 1.0
    lo, hi = scale_clamp
    clamped = not (lo <= raw_scale <= hi)
    scale = 1.0 if clamped else raw_scale

    n = len(durations)
    model = "scale"
    gap_s = 0.0
    if n > 1:
        per_gap = (span - total) / (n - 1)
        if 0.0 <= per_gap <= MAX_GAP_S:
            model, gap_s, scale = "gap", per_gap, 1.0

    closed = [g for g in gaps if g.end is not None]
    infos: List[BoundaryInfo] = []
    track_starts = [music_start]
    track_ends: List[float] = []
    prev_pos = music_start  # end of the previous chosen cut (monotonic floor)

    cum = 0.0
    for i in range(len(durations) - 1):
        cum += durations[i]
        if model == "gap":
            expected = music_start + cum + i * gap_s + gap_s / 2
        else:
            expected = music_start + cum * scale
        tol = min(
            tolerance_s,
            0.4 * min(durations[i], durations[i + 1]) * scale,
        )

        best: Optional[Gap] = None
        best_score = float("-inf")
        for g in closed:
            if g.start < music_start or g.end > music_end:
                continue
            if g.start < prev_pos + SNAP_MIN_SEPARATION_S:
                continue
            if g.start <= expected <= g.end:
                dist = 0.0
            else:
                dist = min(abs(g.start - expected), abs(g.end - expected))
            if dist > tol:
                continue
            score = min(g.end - g.start, SNAP_GAP_CAP_S) - SNAP_DISTANCE_PENALTY * dist
            if score > best_score:
                best, best_score = g, score

        if best is not None:
            track_ends.append(best.start)
            track_starts.append(best.end)
            prev_pos = best.end
            infos.append(
                BoundaryInfo(i + 1, expected, True, best, (best.start + best.end) / 2)
            )
        else:
            cut = min(max(expected, prev_pos), music_end)
            track_ends.append(cut)
            track_starts.append(cut)
            prev_pos = cut
            infos.append(BoundaryInfo(i + 1, expected, False, None, cut))

    track_ends.append(music_end)
    tracks = [
        Track(n, s, max(s, e)) for n, (s, e) in enumerate(zip(track_starts, track_ends), start=1)
    ]
    for split in side_breaks or []:
        if not 1 <= split < n:
            raise ValueError("side breaks must be internal track indices")
        expected = infos[split - 1].expected
        tolerance = min(tolerance_s, 0.4 * min(durations[split - 1:split + 1]) * scale)
        candidates = [
            g for g in closed
            if music_start < g.start < g.end < music_end
            and g.end - g.start >= SIDE_FLIP_MIN_GAP_S
            and max(g.start - expected, expected - g.end, 0) <= tolerance
            # A long quiet passage can contain the expected time yet leave
            # too little room for the advertised songs on one side.
            and g.start - music_start >= lo * sum(durations[:split])
            and music_end - g.end >= lo * sum(durations[split:])
        ]
        if not candidates:
            continue
        anchor = min(candidates, key=lambda g: abs((g.start + g.end) / 2 - expected))
        # Re-use the established placement model within each anchored span;
        # recurse so recordings containing several sides handle every flip.
        left = boundaries_from_expected_durations(
            gaps, music_start, anchor.start, durations[:split], tolerance_s, scale_clamp,
            [b for b in side_breaks if b < split],
        )
        right = boundaries_from_expected_durations(
            gaps, anchor.end, music_end, durations[split:], tolerance_s, scale_clamp,
            [b - split for b in side_breaks if b > split],
        )
        joined_tracks = [Track(i, t.start, t.end) for i, t in
                         enumerate(left.tracks + right.tracks, start=1)]
        midpoint = (anchor.start + anchor.end) / 2
        joined_infos = left.boundaries + [BoundaryInfo(split, midpoint, True, anchor, midpoint)] + [
            BoundaryInfo(b.index + split, b.expected, b.snapped, b.gap, b.position)
            for b in right.boundaries
        ]
        return GuidedBoundaries(joined_tracks, joined_infos, 1.0,
                                left.scale_clamped or right.scale_clamped, raw_scale, "sides", 0.0)
    return GuidedBoundaries(
        tracks, infos, scale, clamped and model == "scale", raw_scale, model, gap_s
    )


# ---------------------------------------------------------------------------
# Best-match alignment (guided placement)
# ---------------------------------------------------------------------------
#
# Every Discogs length is compared with the music between two *chosen* gaps,
# so one wrong length costs only its own track (cumulative expected positions
# let it shift every later cut).  Gaps are read from a click-smoothed
# envelope: a vinyl gap is surface noise broken up by clicks and run-out
# thumps, which the raw -30 dB test fragments.  Tuned on real records by
# ``tests/e2e/real_records_eval.py``.

SMOOTH_S = 0.31  # rolling-median width: longer than a click, shorter than a gap
ASSUMED_FLOOR_DB = 25.0  # floor below the median level when the span has no quiet 3 %
NO_FLOOR_WITHIN_DB = 8.0  # ...i.e. its 3rd percentile is within this of the median
QUIET_MARGIN_DB = 10.0  # candidate = smoothed level below (3rd percentile) + this
MIN_CANDIDATE_S = 0.4
MERGE_BUMP_S = 1.0  # quiet runs closer than this are one gap (thump inside a flip)
CONTRAST_AROUND_S = 8.0  # music level is read this far either side of a gap
WEAK_WINDOW_S = 5.0  # no-gap (segue) candidates: quietest 0.3 s in each window
WEAK_WIDTH_S = 0.3
WEAK_PENALTY = 0.6  # strength lost by a no-gap candidate (in units of GAP_STRENGTH_W)
GAP_STRENGTH_W = 2.0  # real gaps outweigh length error: Discogs lengths are often off by seconds
LENGTH_SIGMA_S = 3.0  # Discogs length tolerance: sigma = 3 s + 3 % of the length
LENGTH_SIGMA_FRAC = 0.03
MIN_ALIGNED_TRACK_S = 5.0  # two chosen gaps closer than this cannot hold a track
MAX_ALIGN_CANDIDATES = 2000  # bounds the alignment: ~130 MB, ~2 s at 99 tracks; ~2.8 h of 5 s weak windows
PRE_ROLL_S = 0.8  # cut this long before the next song's onset (user: >= 0.5 s more than the 0.3 s it was)
POST_ROLL_S = 0.5  # silence the previous track keeps at least, when the gap allows both
EDGE_PAD_S = 0.5  # lead-in / run-out silence kept before the first and after the last track
MAX_TAIL_S = 4.0  # silence kept after the previous song's fade before dropping the rest
ONSET_RISE_DB = 6.0  # onset = level clearly above the gap level, sustained
ONSET_SUSTAIN_S = 0.05  # ...for this long on the raw envelope (a click is shorter)
ONSET_LONG_QUIET_S = 1.0  # the song starts after the gap's last quiet run this long (rests in a soft intro are shorter)
ONSET_FLIP_RISE_DB = 10.0  # rise needed inside a side-flip-length gap (needle-drop noise over near-digital silence)
# Needle drop after a side flip: arm-up silence, a thump, then lead-in groove crackle before the music.
ARM_UP_DB = -68.0  # quieter than any surface noise: the needle was lifted (observed -75..-83 dB)
LEAD_IN_SETTLE_S = 0.75  # the thump is over by then
LEAD_IN_MAX_S = 20.0  # longest lead-in searched
LEAD_IN_AFTER_S = 5.0  # the music is judged on this much audio after the step
LEAD_IN_MIN_SCORE_DB = 5.0  # music median over the loudest (p90) of the crackle before it
LEAD_IN_MIN_STEP_DB = 8.0  # music median over the crackle median
DIGITAL_SILENCE_DB = -90.0  # gaps this quiet hold nothing worth keeping: no tail


@dataclass(frozen=True)
class GapCandidate:
    start: float
    end: float
    strength: float  # higher = likelier a real inter-track gap (may be negative)
    strong: bool  # a real quiet run; False = quietest spot of a window (segue)


def smooth_envelope(db: np.ndarray, frame_s: float, window_s: float = SMOOTH_S) -> np.ndarray:
    """Rolling median of the envelope (float32), chunked so memory stays small."""
    size = max(1, int(round(window_s / frame_s))) | 1
    half = size // 2
    padded = np.pad(np.asarray(db, dtype=np.float32), half, mode="edge")
    out = np.empty(len(db), dtype=np.float32)
    step = 1 << 16
    for i in range(0, len(db), step):
        j = min(len(db), i + step)
        out[i:j] = np.median(sliding_window_view(padded[i : j + size - 1], size), axis=1)
    return out


def gap_candidates(
    sm: np.ndarray, frame_s: float, start: float, end: float
) -> List[GapCandidate]:
    """Places a track boundary could be, inside ``[start, end]`` of a smoothed envelope.

    Strong: quiet runs (below the span's floor, its 3rd percentile, + ``QUIET_MARGIN_DB``,
    at least ``MIN_CANDIDATE_S``, bumps under ``MERGE_BUMP_S`` merged) scored by
    length and by contrast with the music around them.  Weak: the quietest
    0.3 s of every 5 s window not already inside a strong one, scored by
    contrast alone and penalised, so a segue can still be cut near where its
    length says while a real gap wins whenever one is close.
    """
    a, b = int(start / frame_s), min(len(sm), int(end / frame_s))
    seg = sm[a:b]
    if len(seg) < 2:
        return []
    p3, p50 = np.percentile(seg, [3, 50])
    # Almost no gap in the span (the 3rd percentile is music): assume a floor.
    floor = float(p50 - ASSUMED_FLOOR_DB if p3 > p50 - NO_FLOOR_WITHIN_DB else p3)
    runs: List[List[int]] = []
    for i, j in _runs(seg < floor + QUIET_MARGIN_DB):
        if runs and (i - runs[-1][1]) * frame_s < MERGE_BUMP_S:
            runs[-1][1] = j
        else:
            runs.append([i, j])

    reach = int(CONTRAST_AROUND_S / frame_s)

    def contrast(i: int, j: int) -> float:
        around = np.concatenate([seg[max(0, i - reach) : i], seg[j : j + reach]])
        music = float(np.percentile(around, 75)) if len(around) else floor
        return music - float(np.median(seg[i:j]))

    out: List[GapCandidate] = []
    for i, j in runs:
        if (j - i) * frame_s < MIN_CANDIDATE_S:
            continue
        strength = min((j - i) * frame_s, 4.0) / 4.0 + float(np.clip(contrast(i, j), 0, 30)) / 30.0
        out.append(GapCandidate(start + i * frame_s, start + j * frame_s, GAP_STRENGTH_W * strength, True))

    strong = [(c.start, c.end) for c in out]
    w = max(1, int(WEAK_WIDTH_S / frame_s))
    win = int(WEAK_WINDOW_S / frame_s)
    csum = np.concatenate(([0.0], np.cumsum(seg, dtype=np.float64)))
    for lo in range(0, len(seg) - win, win):
        roll = (csum[lo + w : lo + win + 1] - csum[lo : lo + win - w + 1]) / w
        k = lo + int(np.argmin(roll))
        t0 = start + k * frame_s
        if any(s - 1.0 <= t0 <= e + 1.0 for s, e in strong):
            continue
        strength = 0.5 * float(np.clip(contrast(k, k + w), 0, 30)) / 30.0 - WEAK_PENALTY
        out.append(GapCandidate(t0, t0 + w * frame_s, GAP_STRENGTH_W * strength, False))
    out.sort(key=lambda c: (c.start, c.end))
    return out


def _length_cost(length: np.ndarray, expected: Optional[float]) -> np.ndarray:
    """Robust (Cauchy-like) cost of a track length against its Discogs length."""
    if expected is None:
        return np.zeros_like(length)
    sigma = LENGTH_SIGMA_S + LENGTH_SIGMA_FRAC * expected
    return np.log1p(((length - expected) / sigma) ** 2)


def align_durations_to_gaps(
    cands: Sequence[GapCandidate],
    start: float,
    end: float,
    durations: Sequence[Optional[float]],
    side_breaks: Sequence[int] = (),
) -> Optional[List[GapCandidate]]:
    """Choose ``len(durations) - 1`` candidates, in order, that best explain the lengths.

    Dynamic programme minimising the sum over tracks of a robust length error
    (the music between the chosen gaps vs that track's own Discogs length;
    ``None`` = unknown, costs nothing) minus the strength of every chosen gap.
    Side-change boundaries count their gap's strength twice.  Returns ``None``
    when there are too few candidates to place the tracks.

    Each step holds m x m matrices, so a very long capture keeps only its
    ``MAX_ALIGN_CANDIDATES`` strongest candidates; strong ones always outrank weak ones.
    """
    n = len(durations)
    pool = [c for c in cands if start < c.start and c.end < end]
    if len(pool) > MAX_ALIGN_CANDIDATES:
        strongest = sorted(pool, key=lambda c: c.strength)[-MAX_ALIGN_CANDIDATES:]
        pool = sorted(strongest, key=lambda c: (c.start, c.end))
    m = len(pool)
    if n < 2 or m < n - 1:
        return None
    gs = np.array([c.start for c in pool])
    ge = np.array([c.end for c in pool])
    strength = np.array([c.strength for c in pool])
    flips = set(side_breaks)

    def bonus(k: int) -> np.ndarray:  # boundary k (0-based) ends track k
        return strength * (2.0 if (k + 1) in flips else 1.0)

    inf = 1e12
    cost = _length_cost(gs - start, durations[0]) - bonus(0)
    cost = np.where(gs - start >= MIN_ALIGNED_TRACK_S, cost, inf)
    back: List[np.ndarray] = []
    length = gs[None, :] - ge[:, None]  # [previous gap, next gap]
    feasible = length >= MIN_ALIGNED_TRACK_S
    for k in range(1, n - 1):
        total = cost[:, None] + _length_cost(length, durations[k])
        total = np.where(feasible, total, inf)
        prev = np.argmin(total, axis=0)
        cost = total[prev, np.arange(m)] - bonus(k)
        back.append(prev)
    tail = end - ge
    final = cost + np.where(tail >= MIN_ALIGNED_TRACK_S, _length_cost(tail, durations[n - 1]), inf)
    j = int(np.argmin(final))
    if final[j] >= inf / 2:
        return None
    picks = [j]
    for prev in reversed(back):
        j = int(prev[j])
        picks.append(j)
    return [pool[i] for i in reversed(picks)]


def _onset(
    db: np.ndarray, sm: np.ndarray, frame_s: float, gap: GapCandidate, next_gap_start: Optional[float] = None
) -> float:
    """Where the music after ``gap`` starts.

    A candidate can be several quiet runs merged across short bumps, and those bumps
    can be the first notes of a soft intro (or run-out thumps before a side flip).
    So the music is taken to start after the candidate's *last* quiet run of at
    least ``ONSET_LONG_QUIET_S`` (rests inside an intro are shorter; the silence
    before a song is not): the first point where the smoothed level rises
    ``ONSET_RISE_DB`` above that run's own level (``ONSET_FLIP_RISE_DB`` in a
    side-flip-length gap, whose needle-drop noise sits above near-digital silence),
    refined on the raw envelope to a sustained rise (a lone click is shorter than
    ``ONSET_SUSTAIN_S``).  Without such a run: the first clear rise after the candidate.
    ``next_gap_start`` is where this track's music must end (see ``_after_needle_drop``).
    """
    n = len(sm)
    a, b = int(gap.start / frame_s), min(n - 1, int(gap.end / frame_s))
    seg = sm[a : b + 1]
    level = float(np.median(seg))
    thr = level + ONSET_RISE_DB
    limit = min(n - 1, b + int(3.0 / frame_s))
    long_runs = [r for r in _runs(seg < thr) if (r[1] - r[0]) * frame_s >= ONSET_LONG_QUIET_S]
    if long_runs:
        r0, r1 = long_runs[-1]
        rise = ONSET_FLIP_RISE_DB if gap.end - gap.start >= SIDE_FLIP_MIN_GAP_S else ONSET_RISE_DB
        thr = float(np.median(seg[r0:r1])) + rise
        first = i = a + r1
    else:
        first, i = a, b
    while i < limit and sm[i] < thr:  # the quiet run can end short of a clear rise
        i += 1
    while i > first and sm[i - 1] >= thr:  # ...or after the smoothed level crossed it
        i -= 1
    sustain = max(1, int(round(ONSET_SUSTAIN_S / frame_s)))
    lo, hi = max(a, i - int(0.4 / frame_s)), min(n - sustain, i + int(0.4 / frame_s))
    onset = next((j for j in range(lo, hi + 1) if db[j : j + sustain].min() > thr), i)
    return _after_needle_drop(db, sm, frame_s, onset, next_gap_start) * frame_s


def _after_needle_drop(
    db: np.ndarray, sm: np.ndarray, frame_s: float, i: int, next_gap_start: Optional[float] = None
) -> int:
    """If the sound at frame ``i`` is a needle drop (arm-up silence before it, then lead-in
    crackle and a clear step up into the music), the frame where the music starts;
    otherwise ``i``.  Side A's lead-in is already outside the music span; this is side B's.

    The step is the change point maximising (music median) - (90th percentile of the crackle
    before it), so a lead-in whose crackle wanders or swells still gives way to the music,
    while a song that starts right after the thump (no clear step) is left untouched, and so
    is a very quiet opening that barely rises above the crackle.  The step is only looked for
    far enough before ``next_gap_start`` to leave a track: a quiet short song after the flip
    can pass for crackle, with the next song as its music.
    """
    n = len(sm)
    before = sm[max(0, i - int(1.0 / frame_s)) : i]
    if not len(before) or float(np.median(before)) > ARM_UP_DB:
        return i
    a = i + int(LEAD_IN_SETTLE_S / frame_s)
    after = int(LEAD_IN_AFTER_S / frame_s)
    stop = min(n - after, i + int(LEAD_IN_MAX_S / frame_s))
    if next_gap_start is not None:
        stop = min(stop, int((next_gap_start - MIN_ALIGNED_TRACK_S) / frame_s))
    best, best_t = -np.inf, None
    for t in range(a + int(0.5 / frame_s), stop, int(0.1 / frame_s)):
        score = float(np.median(sm[t : t + after]) - np.percentile(sm[a:t], 90))
        if score > best:
            best, best_t = score, t
    if best_t is None or best < LEAD_IN_MIN_SCORE_DB:
        return i
    crackle = float(np.median(sm[a:best_t]))
    if float(np.median(sm[best_t : best_t + after])) - crackle < LEAD_IN_MIN_STEP_DB:
        return i
    # The music's first sound: a raw rise over the crackle that lasts longer than a click.
    level = crackle + ONSET_RISE_DB
    sustain = max(1, int(round(ONSET_SUSTAIN_S / frame_s)))
    return next((k for k in range(max(a, best_t - int(1.0 / frame_s)), best_t + int(0.5 / frame_s))
                 if db[k : k + sustain].min() > level), best_t)

def onset_cut(
    db: np.ndarray, sm: np.ndarray, frame_s: float, gap: GapCandidate, pre_roll_s: Optional[float] = None,
    onset: Optional[float] = None,
) -> Tuple[float, float]:
    """``(previous_end, next_start)`` for the boundary at a chosen candidate.

    A real gap: the next track starts ``pre_roll_s`` before the next song's
    onset (never before the gap begins) and the previous track keeps the
    silence up to there, so the cut sits right by the beginning of the next
    song.  Silence longer than ``MAX_TAIL_S`` after the fade is not glued on
    (side flips, run-outs): the previous track ends ``MAX_TAIL_S`` after the
    fade and the rest is dropped; a digitally silent gap keeps no tail.
    A weak (no-gap) candidate is cut contiguously at its quietest point.
    ``onset`` saves finding the song's onset again when the caller already has it.
    """
    if not gap.strong:
        mid = (gap.start + gap.end) / 2
        return mid, mid
    if onset is None:
        onset = _onset(db, sm, frame_s, gap)
    pre = PRE_ROLL_S if pre_roll_s is None else pre_roll_s
    cut = onset - pre
    if cut < gap.start + POST_ROLL_S:
        # Too short for both paddings: share the silence in proportion, so
        # neither song's audio crosses into the other track.
        quiet = max(0.0, onset - gap.start)
        cut = gap.start + quiet * POST_ROLL_S / (pre + POST_ROLL_S)
    a, b = int(gap.start / frame_s), max(int(gap.start / frame_s) + 1, int(gap.end / frame_s))
    if float(np.median(sm[a:b])) < DIGITAL_SILENCE_DB:
        return gap.start, cut
    return min(cut, gap.start + MAX_TAIL_S), cut


def _assemble(
    db: np.ndarray,
    sm: np.ndarray,
    frame_s: float,
    music_start: float,
    music_end: float,
    chosen: Sequence[GapCandidate],
    durations: Sequence[Optional[float]],
    model: str,
) -> GuidedBoundaries:
    """Tracks and boundary info from the chosen candidates (one per internal boundary)."""
    starts, ends, infos = [music_start], [], []
    music_from = music_start  # where this track's music begins (guides ignore the padding)
    for k, cand in enumerate(chosen):
        next_gap_start = chosen[k + 1].start if k + 1 < len(chosen) else music_end
        onset = _onset(db, sm, frame_s, cand, next_gap_start) if cand.strong else None
        prev_end, next_start = onset_cut(db, sm, frame_s, cand, onset=onset)
        prev_end = max(prev_end, starts[-1])
        d = durations[k]
        expected = music_from + d if d is not None else (prev_end + next_start) / 2
        ends.append(prev_end)
        starts.append(max(next_start, prev_end))
        music_from = max(starts[-1], onset) if cand.strong else starts[-1]
        infos.append(BoundaryInfo(
            k + 1, expected, cand.strong,
            Gap(cand.start, cand.end) if cand.strong else None, (prev_end + next_start) / 2,
        ))
    ends.append(music_end)
    # Breathing room from the lead-in / run-out silence (never past the recording).
    starts[0] = max(0.0, music_start - EDGE_PAD_S)
    ends[-1] = min(len(db) * frame_s, music_end + EDGE_PAD_S)
    tracks = [Track(i, s, max(s, e)) for i, (s, e) in enumerate(zip(starts, ends), start=1)]
    known = sum(d for d in durations if d is not None)
    raw = (music_end - music_start) / known if known > 0 else 1.0
    return GuidedBoundaries(tracks, infos, 1.0, False, raw, model, 0.0)


def guided_by_alignment(
    db: np.ndarray,
    frame_s: float,
    music_start: float,
    music_end: float,
    durations: Sequence[Optional[float]],
    side_breaks: Optional[Sequence[int]] = None,
    sm: Optional[np.ndarray] = None,
) -> Optional[GuidedBoundaries]:
    """Guided placement by best-match alignment; ``None`` if it cannot place the tracks.

    Always ``len(durations)`` Tracks when it returns.  ``expected`` is where a
    track's own Discogs length puts its end given where it was started (the
    chosen position when that length is unknown); ``snapped`` means a real gap
    was used, not a segue spot.  With no known length at all there is nothing to
    align, so ``None`` (the caller falls back to count-based detection).
    """
    n = len(durations)
    if n < 2:
        return None
    if all(d is None for d in durations):
        return None
    sm = smooth_envelope(db, frame_s) if sm is None else sm
    chosen = align_durations_to_gaps(
        gap_candidates(sm, frame_s, music_start, music_end),
        music_start, music_end, durations, side_breaks or (),
    )
    if chosen is None:
        return None
    return _assemble(db, sm, frame_s, music_start, music_end, chosen, durations, "align")


# refine_cuts stops this far above the median level of the gap it walks into.  Measured on
# synthetic gaps (tests/test_track_boundaries.py, refine section): 2 dB drifts up to 0.17 s
# into correlated ±3 dB surface noise, 3 dB stays within one frame, and every extra dB ends
# a 3 s fade ~0.15 s earlier.
REFINE_FLOOR_MARGIN_DB = 3.0


def refine_cuts(
    db: np.ndarray, frame_s: float, tracks: List[Track], threshold_db: float
) -> List[Track]:
    """Widen each Track over an adjacent fade, never over a gap's noise floor.

    Gap-based cuts sit at the edge of the gap as found at a (possibly more
    permissive) threshold, which clips quiet fade-ins and fade-outs.  Each
    start walks backwards, and each end forwards, while the neighbouring
    frame is above ``threshold_db`` *and* above the median level of the
    stretch it walks into (the gap to the neighbouring Track, or the
    lead-in / run-out) plus ``REFINE_FLOOR_MARGIN_DB``.  So a fade is kept,
    and surface noise that sits above the threshold is not: on a quiet
    record the floor is far below the threshold and only the threshold
    counts.  The walk stops at the first quiet frame, so a click separated
    from the music by quiet frames is not absorbed.  Walks never cross the
    neighbouring Track's original edge, 0, or the end of the envelope; if
    two walks still meet they share the gap at its middle.  Track numbers
    and count are kept; durations are recomputed.

    Known trade-off: in a short gap that is mostly fade the median lies in
    the fade, so the walk stops a little early (~0.35 s of a 0.7 s fade that
    is already > 12 dB down); a lower percentile would follow it further but
    reads jittered noise too low.
    """
    n = len(db)
    if not tracks:
        return []

    def idx(t: float) -> int:
        return max(0, min(n, int(round(t / frame_s))))

    starts = [idx(t.start) for t in tracks]
    ends = [idx(t.end) for t in tracks]
    new_starts = list(starts)
    new_ends = list(ends)

    def stop_level(lo: int, hi: int) -> float:
        # Callers skip empty stretches: np.median([]) is nan, and max() with nan
        # depends on argument order.
        return max(threshold_db, float(np.median(db[lo:hi])) + REFINE_FLOOR_MARGIN_DB)

    for k in range(len(tracks)):
        lo = ends[k - 1] if k > 0 else 0
        hi = starts[k + 1] if k + 1 < len(tracks) else n
        i = starts[k]
        if lo < i:  # touching cuts have no stretch to walk into
            stop = stop_level(lo, i)
            while i > lo and db[i - 1] > stop:
                i -= 1
        new_starts[k] = i
        j = ends[k]
        if j < hi:
            stop = stop_level(j, hi)
            while j < hi and db[j] > stop:
                j += 1
        new_ends[k] = j

    for k in range(len(tracks) - 1):
        if new_ends[k] > new_starts[k + 1]:
            mid = (new_ends[k] + new_starts[k + 1]) // 2
            new_ends[k] = new_starts[k + 1] = mid

    refined = []
    for t, s, e in zip(tracks, new_starts, new_ends):
        # Keep the original time when the walk didn't move it (no float drift).
        start = t.start if s == idx(t.start) else s * frame_s
        end = t.end if e == idx(t.end) else e * frame_s
        refined.append(Track(t.number, start, max(start, end)))
    return refined
