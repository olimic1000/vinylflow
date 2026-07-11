"""
VinylFlow - Metadata Handling Module

Handles Discogs API integration, metadata tagging, and cover art embedding.
Manages release searches, track mapping, and file tagging for FLAC, MP3, and AIFF.
"""

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional
from io import BytesIO

import requests
import discogs_client
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TPOS, TPUB, COMM, APIC, TXXX
from mutagen.aiff import AIFF
from PIL import Image


@dataclass(frozen=True)
class TagSet:
    """Format-neutral logical tags for one Track of a Release.

    Computed once per file via ``MetadataHandler._build_tag_set``; written
    by format-specific applicators (``_apply_vorbis`` for FLAC,
    ``_apply_id3`` for MP3/AIFF).  Adding a new field touches the
    builder + both applicators — no per-format tagger to keep in sync.

    ``disc_number`` is only populated for the "sequential_disc_per_lp" scheme.
    When set, ``track_number`` is the sequential position within that LP and
    ``vinyl_position`` preserves the original "A1"/"B2" label.
    When ``disc_number`` is None the "vinyl" scheme is active and
    ``track_number`` carries the raw vinyl position (e.g. "A1").
    """

    artist: str
    album: str
    title: str
    track_number: str
    year: Optional[int]
    label: Optional[str]
    release_id: int
    comment: str = "Digitized from vinyl"
    cover_data: Optional[bytes] = None
    disc_number: Optional[str] = None
    vinyl_position: Optional[str] = None


class DiscogsTrack:
    """Represents a track from Discogs release."""

    def __init__(self, position: str, title: str, duration: str = ""):
        """
        Initialize Discogs track.

        Args:
            position: Vinyl position (e.g., "A1", "B2")
            title: Track title
            duration: Track duration (e.g., "5:24")
        """
        self.position = position
        self.title = title
        self.duration_str = duration
        self.duration_seconds = self._parse_duration(duration)

    def _parse_duration(self, duration_str: str) -> Optional[float]:
        """Parse duration string to seconds."""
        if not duration_str:
            return None

        try:
            # Handle formats like "5:24" or "1:05:24"
            parts = duration_str.split(":")
            if len(parts) == 2:
                minutes, seconds = parts
                return int(minutes) * 60 + int(seconds)
            elif len(parts) == 3:
                hours, minutes, seconds = parts
                return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        except:
            pass

        return None

    def __repr__(self):
        duration = f" ({self.duration_str})" if self.duration_str else ""
        return f"{self.position}. {self.title}{duration}"


class DiscogsRelease:
    """Represents a Discogs release."""

    def __init__(self, release):
        """
        Initialize from discogs_client Release object.

        Args:
            release: discogs_client Release object
        """
        self.id = release.id
        self.title = release.title
        self.year = getattr(release, "year", "")

        # Get URI for Discogs link - construct from release ID
        self.uri = f"/release/{release.id}"

        # Get artists
        artists = getattr(release, "artists", [])
        self.artist = artists[0].name if artists else "Unknown Artist"

        # Handle various artists
        if self.artist.lower() in ["various", "various artists"]:
            self.various_artists = True
        else:
            self.various_artists = False

        # Get label
        labels = getattr(release, "labels", [])
        self.label = labels[0].name if labels else ""

        # Get format
        formats = getattr(release, "formats", [])
        self.format = formats[0]["name"] if formats else ""

        # Get images
        self.images = getattr(release, "images", [])
        self.cover_url = self.images[0]["uri"] if self.images else None

        # Parse tracklist
        self.tracks = self._parse_tracklist(getattr(release, "tracklist", []), debug=False)

    def _parse_tracklist(self, tracklist, debug=False) -> List[DiscogsTrack]:
        """Parse Discogs tracklist to DiscogsTrack objects."""
        tracks = []
        sequential_tracks = []

        for track in tracklist:
            position = getattr(track, "position", "")
            title = getattr(track, "title", "Unknown")
            duration = getattr(track, "duration", "")

            # Skip Discogs side-heading entries (type_='heading').
            if getattr(track, "type_", "") == "heading":
                continue

            # Handle vinyl positions — letter(s) followed by digits, with an optional
            # sub-track suffix such as ".1" or "-2" (e.g. "B6.1", "B6.2").
            if position and re.match(r"^[A-Z]+\d+", position):
                tracks.append(DiscogsTrack(position, title, duration))
            # Handle repeated letters (A, AA, AAA -> A1, A2, A3)
            elif position and re.match(r"^([A-Z])\1*$", position):
                letter = position[0]
                count = len(position)
                vinyl_pos = f"{letter}{count}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration))
            # Handle sequential numbers (1, 2, 3, 4)
            elif position and re.match(r"^\d+$", position):
                sequential_tracks.append((int(position), title, duration))
            # Empty position — only treat as sequential when NO standard-position tracks
            # have been found yet.  If vinyl-position tracks already exist, an empty
            # position almost certainly means a side-heading (e.g. "Shaolin Sword"),
            # not an actual track.
            elif not position and title and title.lower() not in ["tracklist", "notes"]:
                sequential_tracks.append((len(sequential_tracks) + 1, title, duration))

        # Only convert sequential-number tracks to vinyl positions when the tracklist
        # has NO standard letter-position tracks at all.  This prevents side headings
        # (which Discogs stores with empty positions alongside real A1/B1 tracks) from
        # being synthesised into duplicate "A1"/"B1" entries.
        if sequential_tracks and not tracks:
            sequential_tracks.sort(key=lambda x: x[0])
            total = len(sequential_tracks)
            half = (total + 1) // 2
            for idx, (num, title, duration) in enumerate(sequential_tracks, 1):
                vinyl_pos = f"A{idx}" if idx <= half else f"B{idx - half}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration))

        # Sort by letter prefix then by leading integer, so positions like "B6.1" and
        # "B6.2" land after "B5" rather than before "B1".
        if tracks:
            def _pos_sort_key(t):
                m = re.match(r'^([A-Z]+)(\d+)', t.position)
                if not m:
                    return (t.position, 0, 0)
                remainder = t.position[m.end():]
                sub_m = re.match(r'[.\-](\d+)', remainder)
                return (m.group(1), int(m.group(2)), int(sub_m.group(1)) if sub_m else 0)
            tracks.sort(key=_pos_sort_key)

        return tracks

    def display_summary(self) -> str:
        """Get formatted summary for display."""
        track_list = ", ".join([t.position for t in self.tracks])
        return (
            f"{self.artist} - {self.title} ({self.year}) [{self.format}] - {self.label}\n"
            f"Tracks: {track_list}"
        )

    def __repr__(self):
        return f"DiscogsRelease({self.artist} - {self.title}, {len(self.tracks)} tracks)"


class MetadataHandler:
    """Handles Discogs integration and metadata tagging."""

    def __init__(self, discogs_token: str, user_agent: str):
        """
        Initialize metadata handler.

        Args:
            discogs_token: Discogs API token
            user_agent: User agent string
        """
        self.discogs_token = discogs_token
        self.discogs_user_agent = user_agent
        self.client = discogs_client.Client(user_agent, user_token=discogs_token)
        self.last_request_time = 0
        self.min_request_interval = 1.0  # Rate limiting: max 1 req/sec
        self._rate_limit_lock = threading.Lock()

    def reinitialize(self, discogs_token: str, user_agent: str):
        """
        Reinitialize Discogs client with new credentials.

        Args:
            discogs_token: New Discogs API token
            user_agent: New user agent string
        """
        self.discogs_token = discogs_token
        self.discogs_user_agent = user_agent
        self.client = discogs_client.Client(user_agent, user_token=discogs_token)
        self.last_request_time = 0
        print(f"MetadataHandler reinitialized with new token")

    def _rate_limit(self):
        """Enforce rate limiting between requests.

        Lock-guarded: search runs on a worker thread (asyncio.to_thread) and
        can race the processing pipeline's Discogs fetches — an unguarded
        read-sleep-write would let bursts through.
        """
        with self._rate_limit_lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.min_request_interval:
                time.sleep(self.min_request_interval - elapsed)
            self.last_request_time = time.time()

    def clean_filename(self, filename: str) -> str:
        """
        Clean filename to use as search query.

        Args:
            filename: Input filename

        Returns:
            Cleaned search query
        """
        # Remove extension
        name = Path(filename).stem

        # Replace common separators with spaces
        name = re.sub(r"[-_]+", " ", name)

        # Remove extra spaces
        name = re.sub(r"\s+", " ", name).strip()

        return name

    def search_releases(self, query: str, max_results=5) -> List[Tuple[int, DiscogsRelease]]:
        """
        Search Discogs for releases.

        Args:
            query: Search query
            max_results: Maximum number of results to return

        Returns:
            List of (index, DiscogsRelease) tuples
        """
        self._rate_limit()

        try:
            results = self.client.search(query, type="release")
            releases = []

            for i, result in enumerate(results, 1):
                if i > max_results:
                    break

                try:
                    self._rate_limit()
                    release = self.client.release(result.id)
                    releases.append((i, DiscogsRelease(release)))
                except Exception as e:
                    print(f"Warning: Failed to fetch release {result.id}: {e}")
                    continue

            return releases

        except Exception as e:
            print(f"Search failed: {e}")
            return []

    def get_release_by_id(self, release_id: int) -> Optional[DiscogsRelease]:
        """
        Get release by Discogs ID.

        Args:
            release_id: Discogs release ID

        Returns:
            DiscogsRelease or None
        """
        try:
            self._rate_limit()
            release = self.client.release(release_id)
            return DiscogsRelease(release)
        except Exception as e:
            print(f"Failed to fetch release {release_id}: {e}")
            return None

    def download_cover_art(self, url: str, output_path: Path, max_size=1400) -> bool:
        """
        Download and save cover art.

        Args:
            url: Image URL
            output_path: Where to save the image
            max_size: Maximum dimension for embedding (resize if larger)

        Returns:
            True if successful
        """
        try:
            headers = {"User-Agent": self.client.user_agent}
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            img = Image.open(BytesIO(response.content))

            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

            img.save(output_path, "JPEG", quality=95)
            return True

        except Exception as e:
            print(f"Failed to download cover art: {e}")
            return False

    def prepare_cover_for_embedding(self, image_path: Path, max_size=1400) -> Optional[bytes]:
        """
        Prepare cover art for embedding in audio files.

        Args:
            image_path: Path to image file
            max_size: Maximum dimension

        Returns:
            Image bytes (JPEG), or None if error
        """
        try:
            img = Image.open(image_path)

            if img.mode != "RGB":
                img = img.convert("RGB")

            if max(img.size) > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            buffer = BytesIO()
            img.save(buffer, "JPEG", quality=90)
            return buffer.getvalue()

        except Exception as e:
            print(f"Failed to prepare cover art: {e}")
            return None

    def tag_file(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
        output_format: str = "flac",
        track_numbering: str = "vinyl",
    ) -> bool:
        """
        Write metadata tags to an audio file.
        Dispatches to the correct tagger based on output format.

        Args:
            file_path: Path to audio file
            track: Track object with vinyl_number set
            release: DiscogsRelease object
            cover_data: Optional cover art bytes to embed
            output_format: One of 'flac', 'mp3', 'aiff'
            track_numbering: "sequential_disc_per_lp" or "vinyl"

        Returns:
            True if successful
        """
        if output_format == "flac":
            return self._tag_flac(file_path, track, release, cover_data, track_numbering)
        elif output_format == "mp3":
            return self._tag_mp3(file_path, track, release, cover_data, track_numbering)
        elif output_format == "aiff":
            return self._tag_aiff(file_path, track, release, cover_data, track_numbering)
        else:
            print(f"Unsupported output format for tagging: {output_format}")
            return False

    def _find_discogs_track(self, track, release):
        """Find the Discogs track matching a vinyl_number."""
        for dt in release.tracks:
            if dt.position == track.vinyl_number:
                return dt
        print(f"Warning: No Discogs track found for {track.vinyl_number}")
        return None

    @staticmethod
    def _sequential_disc_per_lp(vinyl_number: str, processed_positions: List[str]):
        """Compute (disc_str, track_str) for the sequential_disc_per_lp scheme.

        Sides A+B share disc 1, C+D share disc 2, E+F share disc 3, etc.
        TRACKNUMBER is the sequential position across both sides of that LP
        (e.g. A1–A4 become 1–4, then B1–B3 continue as 5–7, all on disc 1).

        ``processed_positions`` must be the sorted list of vinyl positions
        actually being exported — NOT the full Discogs tracklist.  Using the
        Discogs list causes gaps when the release has extra entries (intros,
        bonus tracks, etc.) that were not mapped to output files.

        Returns (None, None) if the position doesn't match the expected pattern.
        """
        # Drop the $ anchor so sub-track positions like "B6.1" or "B6.2" match.
        m = re.match(r'^([A-Z]+)(\d+)', vinyl_number or "")
        if not m:
            return None, None
        side_index = ord(m.group(1)[0]) - ord('A') + 1
        disc_num = (side_index + 1) // 2  # A,B→1  C,D→2  E,F→3 …

        def _disc_for_pos(pos):
            pm = re.match(r'^([A-Z]+)\d+', pos or "")
            if not pm:
                return None
            si = ord(pm.group(1)[0]) - ord('A') + 1
            return (si + 1) // 2

        disc_positions = [p for p in processed_positions if _disc_for_pos(p) == disc_num]
        seq = next((i + 1 for i, p in enumerate(disc_positions) if p == vinyl_number), None)
        if seq is None:
            return None, None
        return str(disc_num), str(seq)

    def _build_tag_set(
        self,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes],
        track_numbering: str = "vinyl",
        processed_positions: Optional[List[str]] = None,
    ) -> Optional[TagSet]:
        """Compute the TagSet for one track.  Returns None if the Discogs
        track lookup fails — caller treats that as a tag-write failure."""
        discogs_track = self._find_discogs_track(track, release)
        if discogs_track is None:
            return None

        if track_numbering == "sequential_disc_per_lp":
            positions = processed_positions or [t.position for t in release.tracks]
            disc_str, track_str = self._sequential_disc_per_lp(track.vinyl_number, positions)
            if disc_str is None:
                # Unrecognised position format — fall back to vinyl scheme
                disc_str, track_str = None, track.vinyl_number
        else:
            disc_str, track_str = None, track.vinyl_number

        return TagSet(
            artist=release.artist,
            album=release.title,
            title=discogs_track.title,
            track_number=track_str,
            year=release.year,
            label=release.label or None,
            release_id=release.id,
            cover_data=cover_data,
            disc_number=disc_str,
            vinyl_position=track.vinyl_number if disc_str is not None else None,
        )

    def _apply_vorbis(self, audio: FLAC, tags: TagSet) -> None:
        """Write a TagSet to a FLAC file using Vorbis comments."""
        audio["ARTIST"] = tags.artist
        audio["ALBUM"] = tags.album
        audio["TITLE"] = tags.title
        audio["TRACKNUMBER"] = tags.track_number
        if tags.disc_number is not None:
            audio["DISCNUMBER"] = tags.disc_number
        if tags.vinyl_position is not None:
            audio["VINYLPOSITION"] = tags.vinyl_position
        audio["DATE"] = str(tags.year) if tags.year else ""
        if tags.label:
            audio["LABEL"] = tags.label
        audio["DISCOGS_RELEASE_ID"] = str(tags.release_id)
        audio["COMMENT"] = tags.comment
        if tags.cover_data:
            picture = Picture()
            picture.type = 3  # Front cover
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            picture.data = tags.cover_data
            audio.add_picture(picture)

    def _apply_id3(self, audio, tags: TagSet) -> None:
        """Write a TagSet to an ID3v2-tagged file (MP3 or AIFF)."""
        audio.tags["TIT2"] = TIT2(encoding=3, text=tags.title)
        audio.tags["TPE1"] = TPE1(encoding=3, text=tags.artist)
        audio.tags["TALB"] = TALB(encoding=3, text=tags.album)
        audio.tags["TRCK"] = TRCK(encoding=3, text=tags.track_number)
        if tags.disc_number is not None:
            audio.tags["TPOS"] = TPOS(encoding=3, text=tags.disc_number)
        if tags.vinyl_position is not None:
            audio.tags["TXXX:VINYLPOSITION"] = TXXX(
                encoding=3, desc="VINYLPOSITION", text=tags.vinyl_position,
            )
        if tags.year:
            audio.tags["TDRC"] = TDRC(encoding=3, text=str(tags.year))
        if tags.label:
            audio.tags["TPUB"] = TPUB(encoding=3, text=tags.label)
        audio.tags["TXXX:DISCOGS_RELEASE_ID"] = TXXX(
            encoding=3, desc="DISCOGS_RELEASE_ID", text=str(tags.release_id),
        )
        audio.tags["COMM"] = COMM(
            encoding=3, lang="eng", desc="", text=tags.comment,
        )
        if tags.cover_data:
            audio.tags["APIC"] = APIC(
                encoding=3, mime="image/jpeg", type=3, desc="Cover", data=tags.cover_data,
            )

    def _tag_flac(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
        track_numbering: str = "vinyl",
    ) -> bool:
        tags = self._build_tag_set(track, release, cover_data, track_numbering)
        if tags is None:
            return False
        try:
            audio = FLAC(file_path)
            audio.clear_pictures()
            audio.delete()
            self._apply_vorbis(audio, tags)
            audio.save()
            return True
        except Exception as e:
            print(f"Failed to tag {file_path}: {e}")
            return False

    def _tag_id3_format(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes],
        open_audio,
        track_numbering: str = "vinyl",
    ) -> bool:
        """Shared ID3 tagger used by ``_tag_mp3`` and ``_tag_aiff``.

        ``open_audio(path)`` returns the Mutagen wrapper instance; the
        only difference between the MP3 and AIFF paths.
        """
        tags = self._build_tag_set(track, release, cover_data, track_numbering)
        if tags is None:
            return False
        try:
            audio = open_audio(file_path)
            try:
                audio.add_tags()
            except Exception:
                pass  # Tags already exist
            self._apply_id3(audio, tags)
            audio.save()
            return True
        except Exception as e:
            print(f"Failed to tag {file_path}: {e}")
            return False

    def _tag_mp3(self, file_path, track, release, cover_data=None, track_numbering="vinyl"):
        return self._tag_id3_format(
            file_path, track, release, cover_data, lambda p: MP3(p, ID3=ID3), track_numbering,
        )

    def _tag_aiff(self, file_path, track, release, cover_data=None, track_numbering="vinyl"):
        return self._tag_id3_format(file_path, track, release, cover_data, AIFF, track_numbering)

    def sanitize_filename(self, name: str) -> str:
        """Sanitize string for use in filename."""
        name = re.sub(r'[/\\:*?"<>|]', "-", name)
        name = name.strip(" .")
        name = re.sub(r"\s+", " ", name)
        return name

    def create_album_folder_name(self, release: DiscogsRelease) -> str:
        """Create folder name for album."""
        artist = self.sanitize_filename(release.artist)
        title = self.sanitize_filename(release.title)
        return f"{artist} - {title}"

    def create_track_filename(
        self, track: "Track", release: DiscogsRelease, output_format: str = "flac"
    ) -> str:
        """
        Create filename for track.

        Args:
            track: Track object with vinyl_number set
            release: DiscogsRelease object
            output_format: One of 'flac', 'mp3', 'aiff'

        Returns:
            Filename (e.g., "A1-Groove La Chord.flac")
        """
        from audio_processor import OUTPUT_FORMATS

        format_config = OUTPUT_FORMATS.get(output_format, OUTPUT_FORMATS["flac"])
        ext = format_config["extension"]

        discogs_track = self._find_discogs_track(track, release)
        if not discogs_track:
            return f"{track.vinyl_number}-Unknown{ext}"

        title = self.sanitize_filename(discogs_track.title)
        return f"{track.vinyl_number}-{title}{ext}"
