"""VinylFlow — Audacity label files.

Audacity's label track can be exported (File → Export → Export Labels) and
imported (File → Import → Labels) as a plain-text file, one label per line:

    <start seconds>\t<end seconds>\t<label text>

A *region* label has ``end > start`` and spans a stretch of audio.  A
*point* label has ``end == start`` and marks a single instant.  Lines that
start with a backslash carry a label's spectral (frequency) range; they are
not labels and are skipped.

Two pure functions bridge that format and VinylFlow Track Boundaries:

- ``tracks_from_labels``  label-file text + source duration -> list[Track]
- ``labels_from_tracks``  (start, end, name) rows           -> label-file text

How labels become Track Boundaries:

- Region labels: each region is one Track.  Overlaps are trimmed so the
  Tracks stay in order and never share audio.
- Point labels only: each point marks where a Track *starts* (the same
  convention as Audacity's Export Multiple).  A Track runs from one point to
  the next, and the last runs to the end of the file.  Audio before the
  first point is lead-in (needle drop) and becomes a Track only when it is
  at least ``min_track_length`` long.
- A file with both kinds uses its region labels and ignores the points.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from track_boundaries import Track

# Spans shorter than this are slivers from imprecise clicks, not tracks.
MIN_SPAN_SECONDS = 1.0
# Point labels this close together (or this close to 0 / EOF) are one point.
POINT_EPSILON = 0.01


class LabelFileError(ValueError):
    """The text is not a usable Audacity label file."""


def _parse_time(value: str) -> float:
    # Audacity writes '.' but tolerate a ',' decimal separator from
    # hand-edited files or locale-dependent tools.
    return float(value.strip().replace(",", "."))


def parse_labels(text: str) -> List[Tuple[float, float, str]]:
    """Parse label-file text into ``(start, end, name)`` rows, file order."""
    labels = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip("\r\n﻿")
        if not line.strip() or line.startswith("\\"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise LabelFileError(
                f"Line {line_no} is not an Audacity label (expected tab-separated start and end times)."
            )
        try:
            start = _parse_time(parts[0])
            end = _parse_time(parts[1])
        except ValueError:
            raise LabelFileError(f"Line {line_no} has a time that is not a number.") from None
        name = "\t".join(parts[2:]).strip()
        if end < start:
            start, end = end, start
        labels.append((start, end, name))
    return labels


def _clamp(t: float, duration: float) -> float:
    return min(max(t, 0.0), duration)


def _region_tracks(regions, duration: float) -> List[Tuple[float, float, str]]:
    spans = []
    prev_end = 0.0
    for start, end, name in sorted(regions, key=lambda r: (r[0], r[1])):
        start = max(_clamp(start, duration), prev_end)
        end = _clamp(end, duration)
        if end - start < MIN_SPAN_SECONDS:
            continue
        spans.append((start, end, name))
        prev_end = end
    return spans


def _point_tracks(points, duration: float, min_track_length: float) -> List[Tuple[float, float, str]]:
    marks: List[Tuple[float, str]] = []
    for t, _, name in sorted(points, key=lambda p: p[0]):
        t = _clamp(t, duration)
        if duration - t < POINT_EPSILON:
            continue  # a point at EOF starts nothing
        if marks and t - marks[-1][0] < POINT_EPSILON:
            continue  # duplicate click
        marks.append((t, name))

    spans = []
    if marks and marks[0][0] >= min_track_length:
        spans.append((0.0, marks[0][0], ""))
    for i, (start, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else duration
        if end - start >= MIN_SPAN_SECONDS:
            spans.append((start, end, name))
    return spans


def tracks_from_labels(
    text: str, duration: float, min_track_length: float = 30.0
) -> Tuple[List[Track], List[str]]:
    """Turn label-file text into Track Boundaries over a ``duration``-second file.

    Returns ``(tracks, names)``: numbered Tracks in time order, and each
    Track's label text (``""`` when the label had none).

    Raises ``LabelFileError`` when the text has no labels or none of them
    yields a Track.
    """
    labels = parse_labels(text)
    if not labels:
        raise LabelFileError("The file has no labels in it.")

    regions = [lab for lab in labels if lab[1] - lab[0] > POINT_EPSILON]
    if regions:
        spans = _region_tracks(regions, duration)
    else:
        spans = _point_tracks(labels, duration, min_track_length)

    if not spans:
        raise LabelFileError("None of the labels fall within this recording.")

    tracks = [Track(i + 1, start, end) for i, (start, end, _) in enumerate(spans)]
    names = [name for _, _, name in spans]
    return tracks, names


def _clean_name(name: Optional[str]) -> str:
    # A tab or newline inside a name would break the line format.
    return " ".join((name or "").split())


def labels_from_tracks(rows: Iterable[Tuple[float, float, Optional[str]]]) -> str:
    """Format ``(start, end, name)`` rows as an Audacity label file.

    Region labels, six decimal places, in time order — the same layout
    Audacity's own Export Labels writes.
    """
    lines = [
        f"{start:.6f}\t{end:.6f}\t{_clean_name(name)}\n"
        for start, end, name in sorted(rows, key=lambda r: (r[0], r[1]))
    ]
    return "".join(lines)

