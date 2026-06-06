#!/usr/bin/env python3
"""
retag.py — Retroactively convert VinylFlow track-numbering schemes.

Two schemes (matching VinylFlow's config options):

  vinyl
      TRACKNUMBER = "A1", "A2", "B1" …
      No DISCNUMBER or VINYLPOSITION tags are written.

  sequential_disc_per_lp
      TRACKNUMBER = "1", "2", "3" … (sequential across both sides of each LP)
      DISCNUMBER  = "1" (A+B share disc 1), "2" (C+D share disc 2) …
      VINYLPOSITION = "A1", "A2", "B1" … (original vinyl position, preserved)

Conversion notes
  vinyl → sequential   Always possible: vinyl positions encode the side and
                       track number, so disc/seq numbers can be computed.

  sequential → vinyl   Requires the VINYLPOSITION tag to be present. If a file
                       was exported with the sequential scheme it will have this
                       tag. Files exported with the legacy vinyl scheme before
                       the sequential option existed will NOT have it, and the
                       tool will skip them with a warning.

Supported formats: FLAC (.flac), MP3 (.mp3), AIFF (.aiff / .aif)

Usage
  # Auto-detect current scheme and convert to the other:
  python scripts/retag.py /path/to/album/

  # Force a specific target scheme:
  python scripts/retag.py /path/to/album/ --to vinyl
  python scripts/retag.py /path/to/album/ --to sequential_disc_per_lp

  # Preview without writing:
  python scripts/retag.py /path/to/album/ --dry-run
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

try:
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TRCK, TPOS, TXXX
    from mutagen.aiff import AIFF
except ImportError:
    sys.exit("mutagen is required.  Install it with:  pip install mutagen")


SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".aiff", ".aif"}


# ---------------------------------------------------------------------------
# Position helpers (mirrors the logic in metadata_handler.py)
# ---------------------------------------------------------------------------

def _pos_sort_key(pos: str) -> tuple:
    """Sort key for vinyl positions (A1, B6.1, B6.2, C3, …)."""
    m = re.match(r'^([A-Z]+)(\d+)', pos or "")
    if not m:
        return (pos, 0, 0)
    remainder = pos[m.end():]
    sub_m = re.match(r'[.\-](\d+)', remainder)
    return (m.group(1), int(m.group(2)), int(sub_m.group(1)) if sub_m else 0)


def _disc_and_track(vinyl_pos: str, all_positions: list[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Compute (disc_number, track_number) for a vinyl position under the
    sequential_disc_per_lp scheme, given the full sorted list of positions
    being converted.

    A+B → disc 1,  C+D → disc 2,  E+F → disc 3, …
    Track number is sequential across both sides of the disc.

    Returns (None, None) if the position doesn't match the expected pattern.
    """
    m = re.match(r'^([A-Z]+)(\d+)', vinyl_pos or "")
    if not m:
        return None, None

    side_index = ord(m.group(1)[0]) - ord('A') + 1
    disc_num = (side_index + 1) // 2  # A,B→1  C,D→2  E,F→3

    def _disc_for(pos: str) -> Optional[int]:
        pm = re.match(r'^([A-Z]+)\d+', pos or "")
        if not pm:
            return None
        si = ord(pm.group(1)[0]) - ord('A') + 1
        return (si + 1) // 2

    disc_positions = [p for p in all_positions if _disc_for(p) == disc_num]
    seq = next((i + 1 for i, p in enumerate(disc_positions) if p == vinyl_pos), None)
    if seq is None:
        return None, None
    return str(disc_num), str(seq)


# ---------------------------------------------------------------------------
# Tag reading
# ---------------------------------------------------------------------------

class FileInfo:
    """Current tag state for a single audio file."""

    def __init__(self, path: Path):
        self.path = path
        self.tracknumber: Optional[str] = None
        self.discnumber: Optional[str] = None
        self.vinylposition: Optional[str] = None
        self._load()

    def _load(self):
        ext = self.path.suffix.lower()
        try:
            if ext == ".flac":
                audio = FLAC(self.path)
                self.tracknumber = _first(audio.get("TRACKNUMBER"))
                self.discnumber  = _first(audio.get("DISCNUMBER"))
                self.vinylposition = _first(audio.get("VINYLPOSITION"))
            elif ext in {".mp3", ".aiff", ".aif"}:
                audio = MP3(self.path, ID3=ID3) if ext == ".mp3" else AIFF(self.path)
                tags = audio.tags
                if tags is None:
                    return
                trck = tags.get("TRCK")
                self.tracknumber = str(trck.text[0]).strip() if trck else None
                tpos = tags.get("TPOS")
                self.discnumber  = str(tpos.text[0]).strip() if tpos else None
                vp = tags.get("TXXX:VINYLPOSITION")
                self.vinylposition = str(vp.text[0]).strip() if vp else None
        except Exception as exc:
            print(f"  [ERROR] Could not read {self.path.name}: {exc}", file=sys.stderr)


def _first(values) -> Optional[str]:
    """Return the first element of a list/tuple, or None."""
    if values:
        return str(values[0]).strip() or None
    return None


# ---------------------------------------------------------------------------
# Scheme detection
# ---------------------------------------------------------------------------

VINYL_RE    = re.compile(r'^[A-Z]+\d+')
NUMERIC_RE  = re.compile(r'^\d+$')


def detect_scheme(files: list[FileInfo]) -> str:
    """
    Inspect TRACKNUMBER tags and return the current scheme:
      'vinyl', 'sequential_disc_per_lp', 'mixed', or 'unknown'.
    """
    vinyl_count   = 0
    numeric_count = 0
    for f in files:
        tn = (f.tracknumber or "").strip()
        if VINYL_RE.match(tn):
            vinyl_count += 1
        elif NUMERIC_RE.match(tn):
            numeric_count += 1

    if vinyl_count > 0 and numeric_count == 0:
        return "vinyl"
    if numeric_count > 0 and vinyl_count == 0:
        return "sequential_disc_per_lp"
    if vinyl_count > 0 and numeric_count > 0:
        return "mixed"
    return "unknown"


# ---------------------------------------------------------------------------
# Tag writing
# ---------------------------------------------------------------------------

def _write_tags(
    path: Path,
    tracknumber: str,
    discnumber: Optional[str],
    vinylposition: Optional[str],
):
    """Overwrite numbering-related tags, leaving all other tags intact."""
    ext = path.suffix.lower()

    if ext == ".flac":
        audio = FLAC(path)
        audio["TRACKNUMBER"] = tracknumber
        if discnumber is not None:
            audio["DISCNUMBER"] = discnumber
        else:
            audio.pop("DISCNUMBER", None)
        if vinylposition is not None:
            audio["VINYLPOSITION"] = vinylposition
        else:
            audio.pop("VINYLPOSITION", None)
        audio.save()

    elif ext in {".mp3", ".aiff", ".aif"}:
        audio = MP3(path, ID3=ID3) if ext == ".mp3" else AIFF(path)
        if audio.tags is None:
            audio.add_tags()

        audio.tags["TRCK"] = TRCK(encoding=3, text=tracknumber)

        if discnumber is not None:
            audio.tags["TPOS"] = TPOS(encoding=3, text=discnumber)
        else:
            audio.tags.delall("TPOS")

        if vinylposition is not None:
            audio.tags["TXXX:VINYLPOSITION"] = TXXX(
                encoding=3, desc="VINYLPOSITION", text=vinylposition,
            )
        else:
            try:
                del audio.tags["TXXX:VINYLPOSITION"]
            except KeyError:
                pass

        audio.save()


# ---------------------------------------------------------------------------
# Conversion logic
# ---------------------------------------------------------------------------

def convert_to_sequential(files: list[FileInfo], dry_run: bool) -> int:
    """Convert vinyl-scheme files → sequential_disc_per_lp."""
    vinyl_files = [
        f for f in files
        if f.tracknumber and VINYL_RE.match(f.tracknumber)
    ]

    if not vinyl_files:
        print("  No files with vinyl-position TRACKNUMBER found.")
        return 0

    # Build sorted position list to compute sequential numbers correctly.
    all_positions = sorted(
        (f.tracknumber for f in vinyl_files),
        key=_pos_sort_key,
    )

    changed = 0
    for f in sorted(vinyl_files, key=lambda x: _pos_sort_key(x.tracknumber or "")):
        vinyl_pos = f.tracknumber
        disc_str, track_str = _disc_and_track(vinyl_pos, all_positions)
        if disc_str is None:
            print(f"  [SKIP] {f.path.name}: cannot compute disc/track for {vinyl_pos!r}")
            continue

        print(
            f"  {'(dry run) ' if dry_run else ''}{f.path.name}\n"
            f"    TRACKNUMBER  {vinyl_pos!r} → {track_str!r}\n"
            f"    DISCNUMBER   (none) → {disc_str!r}\n"
            f"    VINYLPOSITION (none) → {vinyl_pos!r}"
        )

        if not dry_run:
            _write_tags(f.path, track_str, disc_str, vinyl_pos)
        changed += 1

    return changed


def convert_to_vinyl(files: list[FileInfo], dry_run: bool) -> int:
    """Convert sequential_disc_per_lp files → vinyl scheme."""
    seq_files = [
        f for f in files
        if f.tracknumber and NUMERIC_RE.match(f.tracknumber)
    ]

    if not seq_files:
        print("  No files with numeric TRACKNUMBER found.")
        return 0

    changed = 0
    skipped = 0
    for f in sorted(seq_files, key=lambda x: int(x.tracknumber or "0")):
        vinyl_pos = f.vinylposition
        if not vinyl_pos:
            print(
                f"  [SKIP] {f.path.name}: TRACKNUMBER={f.tracknumber!r} but no "
                f"VINYLPOSITION tag — cannot determine vinyl side/position.\n"
                f"         (File may have been tagged before the sequential scheme "
                f"was introduced.)"
            )
            skipped += 1
            continue

        print(
            f"  {'(dry run) ' if dry_run else ''}{f.path.name}\n"
            f"    TRACKNUMBER   {f.tracknumber!r} → {vinyl_pos!r}\n"
            f"    DISCNUMBER    {f.discnumber!r} → (removed)\n"
            f"    VINYLPOSITION {vinyl_pos!r} → (removed)"
        )

        if not dry_run:
            _write_tags(f.path, vinyl_pos, None, None)
        changed += 1

    if skipped:
        print(
            f"\n  {skipped} file(s) skipped — VINYLPOSITION tag is required to "
            f"reverse sequential numbering back to vinyl positions."
        )

    return changed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert VinylFlow track numbering between 'vinyl' and 'sequential_disc_per_lp' schemes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("directory", type=Path, help="Album directory to process.")
    parser.add_argument(
        "--to",
        choices=["vinyl", "sequential_disc_per_lp"],
        default=None,
        help="Target scheme. If omitted, auto-detects current scheme and converts to the other.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any files.",
    )
    args = parser.parse_args()

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        sys.exit(f"Not a directory: {directory}")

    # Collect audio files (non-recursive — one album at a time).
    audio_files = sorted(
        p for p in directory.iterdir()
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not audio_files:
        sys.exit(f"No supported audio files (.flac, .mp3, .aiff) found in {directory}")

    print(f"Found {len(audio_files)} audio file(s) in {directory}\n")

    files = [FileInfo(p) for p in audio_files]

    current_scheme = detect_scheme(files)
    print(f"Detected scheme: {current_scheme}")

    # Determine target scheme.
    if args.to:
        target = args.to
    elif current_scheme == "vinyl":
        target = "sequential_disc_per_lp"
    elif current_scheme == "sequential_disc_per_lp":
        target = "vinyl"
    else:
        sys.exit(
            f"Cannot auto-detect target scheme (current: {current_scheme!r}).\n"
            f"Use --to vinyl  or  --to sequential_disc_per_lp to specify explicitly."
        )

    if current_scheme == target:
        print(f"Files are already in the '{target}' scheme — nothing to do.")
        return

    print(f"Converting: {current_scheme} → {target}")
    if args.dry_run:
        print("(dry run — no files will be modified)\n")
    else:
        print()

    if target == "sequential_disc_per_lp":
        changed = convert_to_sequential(files, args.dry_run)
    else:
        changed = convert_to_vinyl(files, args.dry_run)

    print(f"\n{'Would update' if args.dry_run else 'Updated'} {changed} file(s).")


if __name__ == "__main__":
    main()
