# VinylFlow Domain Glossary

Authoritative names for domain concepts. When code names drift from these terms, the code is wrong, not the glossary.

## Session

The user-facing unit of work. Born when a user uploads a source audio file; lives through Release linkage, track-boundary detection, and position mapping; ends when processing completes (output written) or the Session is reaped/abandoned.

A Session owns:
- one **Source Audio** file
- at most one linked **Release**
- a set of **Track Boundaries**
- a set of **Position Mappings** (boundary → Position)
- the eventual **Output Folder** path

Lifecycle states: **Ready → Processing → Complete | Failed**. A Session is born `Ready` once its Source Audio is on disk; between Ready and Processing it accumulates attributes (boundaries, Release link, mappings) — those are mutations within Ready, not separate states. Both `Complete` and `Failed` can transition back to `Processing` when the user re-runs the Pipeline (e.g. after fixing mappings).

One Session = one Release attempt from one Source Audio file. A double LP processed as two uploads is two Sessions, even though they target the same Release.

## Release

A Discogs release: artist, album, year, cover art, ordered list of Tracks (each with a Position and a duration). Linked into a Session by user search/selection. The Release determines the Output Folder name (`{Artist} - {Album}/`) and supplies the metadata embedded in tagged output files.

## Source Audio

The WAV/AIFF recording the user uploads. Contains some-or-all of a Release's tracks. May span multiple physical vinyl sides — silence detection works across the whole file regardless. Stored at `VINYLFLOW_UPLOAD_DIR/{session_id}/source.{ext}`.

## Track Boundary

A timestamp range `(start, end)` within a Source Audio file, identifying one Track. Produced by silence detection (primary) or fallback duration-based analysis. Editable by the user via the waveform UI before processing.

## Position

The vinyl track designation: `A1`, `A2`, `B1`, etc. Comes from the Release. A Track Boundary becomes a tagged output file only after the user assigns it a Position (the Position Mapping step).

## Position Mapping

The user's assignment of `Track Boundary → Position`. May be partial (user only mapped some boundaries) or non-sequential. Drives output filename and tag content.

## Output Folder

The destination directory for processed tracks: `{DEFAULT_OUTPUT_DIR}/{Artist} - {Album}/`. One per Release. If the user processes the same Release twice (two Sessions), both write into the same folder.

## Pipeline

The mechanical sequence that turns a Session in `Ready` state into a Session in `Complete` state: download cover art → for each mapped Track Boundary, ffmpeg-extract → convert to output format → tag → rename. The Pipeline is an operation *on* a Session, not a separate domain concept.
