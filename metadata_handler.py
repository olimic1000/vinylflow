"""
VinylFlow - Metadata Handling Module

Handles Discogs API integration, metadata tagging, and cover art embedding.
Manages release searches, track mapping, and file tagging for FLAC, MP3, and AIFF.
"""

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional
from io import BytesIO

import requests
import discogs_client
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, TIT2, TPE1, TPE2, TPE3, TALB, TCMP, TCOM, TDRC, TMCL, TRCK, TPOS, TPUB, COMM, APIC, TXXX,
)
from mutagen.aiff import AIFF
from PIL import Image
from config import DEFAULT_TRACK_NUMBERING
from album_description import DIGITIZED_COMMENT, AlbumDescriptions

from release_credits import (
    VARIOUS_ARTISTS,
    Credit,
    artist_from_credits,
    artist_names,
    classify_credits,
    credits_for_track,
    is_various,
    join_artist_names,
    lone_artist_composer,
    parse_credits,
    performer_text,
)


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
    ``track_total`` / ``disc_total`` are likewise only set for that scheme:
    tracks on this LP, and LPs in the release.
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
    comment: str = DIGITIZED_COMMENT
    cover_data: Optional[bytes] = None
    disc_number: Optional[str] = None
    vinyl_position: Optional[str] = None
    track_total: Optional[str] = None
    disc_total: Optional[str] = None
    album_artist: Optional[str] = None
    compilation: bool = False
    # Classical releases only (see ``release_credits``); empty otherwise.
    composers: Tuple[str, ...] = ()
    conductors: Tuple[str, ...] = ()
    ensembles: Tuple[str, ...] = ()
    performers: Tuple[Tuple[str, str], ...] = ()  # (name, instrument or empty)


class DiscogsTrack:
    """Represents a track from Discogs release."""

    def __init__(self, position: str, title: str, duration: str = "", parts: Optional[List[dict]] = None, artist: str = "",
                 credits: Optional[List[Credit]] = None, raw_position: str = "",
                 artist_names: Tuple[str, ...] = ()):
        """
        Initialize Discogs track.

        Args:
            position: Vinyl position (e.g., "A1", "B2")
            title: Track title
            duration: Track duration (e.g., "5:24")
            parts: For an umbrella track (a Discogs index track such as a suite),
                its sub-tracks as ``{position, title, duration}``
            artist: The track's own artist(s) when Discogs lists them (compilations)
            credits: Track-level credits (Discogs ``extraartists``)
            raw_position: Position as Discogs wrote it (credit track ranges use it)
            artist_names: The track's own artists, one name each (``artist`` joins them)
        """
        self.position = position
        self.artist = artist
        self.credits = list(credits or [])
        self.raw_position = raw_position or position
        self.artist_names = tuple(artist_names)
        self.parts = list(parts or [])
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
        self.artist = join_artist_names(artists) or "Unknown Artist"
        self.artist_names = artist_names(artists)

        # Handle various artists
        self.various_artists = is_various(self.artist)

        # Genres and release-level credits (Discogs ``extraartists``); classical tags use them.
        # Read from the raw payload, which the fields above have loaded: discogs_client refreshes
        # on any key the payload lacks, one more Discogs request. Plain fakes set attributes.
        payload = getattr(release, "data", None)
        if not isinstance(payload, dict):
            payload = {"genres": getattr(release, "genres", None), "extraartists": getattr(release, "credits", None)}
        self.genres = list(payload.get("genres") or [])
        self.credits = parse_credits(payload.get("extraartists"))

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
            # type_ and sub_tracks exist only in the raw payload (discogs_client's
            # Track has no attributes for them); plain fakes may set attributes.
            raw = getattr(track, "data", None)
            raw = raw if isinstance(raw, dict) else {}
            kind = raw.get("type_", getattr(track, "type_", "")) or ""
            position = getattr(track, "position", "")
            title = getattr(track, "title", "Unknown")
            duration = getattr(track, "duration", "")
            track_artist = join_artist_names(getattr(track, "artists", None) or [])
            track_credits = parse_credits(getattr(track, "credits", None))
            extras = {
                "artist": track_artist, "credits": track_credits, "raw_position": position,
                "artist_names": artist_names(getattr(track, "artists", None) or []),
            }
            parts = None

            # Skip Discogs side-heading entries (type_='heading').
            if kind == "heading":
                continue

            # An index track (a suite, e.g. "Feel The Benefit" over B4-B6) has no
            # position itself; its parts are sub_tracks. Keep it as one umbrella
            # track at its first part's position, lasting as long as all parts.
            if kind == "index":
                umbrella = self._umbrella(raw, getattr(track, "sub_tracks", None), title, duration)
                if umbrella is None:
                    continue
                position, duration, parts = umbrella
                extras["raw_position"] = position

            # Handle vinyl positions — letter(s) followed by digits, with an optional
            # sub-track suffix such as ".1" or "-2" (e.g. "B6.1", "B6.2").
            if position and re.match(r"^[A-Z]+\d+", position):
                tracks.append(DiscogsTrack(position, title, duration, parts, **extras))
            # Handle repeated letters (A, AA, AAA -> A1, A2, A3)
            elif position and re.match(r"^([A-Z])\1*$", position):
                letter = position[0]
                count = len(position)
                vinyl_pos = f"{letter}{count}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration, parts, **extras))
            # Handle sequential numbers (1, 2, 3, 4)
            elif position and re.match(r"^\d+$", position):
                sequential_tracks.append((int(position), title, duration, parts, extras))
            # Empty position — only treat as sequential when NO standard-position tracks
            # have been found yet.  If vinyl-position tracks already exist, an empty
            # position almost certainly means a side-heading (e.g. "Shaolin Sword"),
            # not an actual track.
            elif not position and title and title.lower() not in ["tracklist", "notes"]:
                sequential_tracks.append((len(sequential_tracks) + 1, title, duration, parts, extras))

        # Only convert sequential-number tracks to vinyl positions when the tracklist
        # has NO standard letter-position tracks at all.  This prevents side headings
        # (which Discogs stores with empty positions alongside real A1/B1 tracks) from
        # being synthesised into duplicate "A1"/"B1" entries.
        if sequential_tracks and not tracks:
            sequential_tracks.sort(key=lambda x: x[0])
            total = len(sequential_tracks)
            half = (total + 1) // 2
            for idx, (num, title, duration, parts, extras) in enumerate(sequential_tracks, 1):
                vinyl_pos = f"A{idx}" if idx <= half else f"B{idx - half}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration, parts, **extras))

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

    @staticmethod
    def _umbrella(raw: dict, sub_attr, title: str, duration: str):
        """(position, duration, parts) for an index track, or None without usable parts."""
        subs = raw.get("sub_tracks") if raw else None
        if subs is None and sub_attr:
            subs = [getattr(t, "data", None) or {"position": getattr(t, "position", ""),
                    "title": getattr(t, "title", ""), "duration": getattr(t, "duration", "")} for t in sub_attr]
        parts = [{"position": str(t.get("position") or ""), "title": str(t.get("title") or ""),
                  "duration": str(t.get("duration") or "")} for t in subs or [] if isinstance(t, dict)]
        positioned = [p for p in parts if p["position"]]
        if not positioned:
            return None
        # "B4a" / "B4.1" parts share the base "B4"; "B4", "B5", "B6" start at "B4".
        m = re.match(r"^([A-Z]+\d+|\d+)", positioned[0]["position"])
        position = m.group(1) if m else positioned[0]["position"]
        if not duration:
            secs = [DiscogsTrack._parse_duration(None, p["duration"]) for p in parts]
            if secs and all(secs):
                total = int(sum(secs))
                duration = f"{total // 60}:{total % 60:02d}" if total < 3600 else \
                    f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
        return position, duration, parts

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
        self._release_cache: "OrderedDict[int, DiscogsRelease]" = OrderedDict()
        self._release_cache_lock = threading.Lock()
        self.album_descriptions = AlbumDescriptions()

    RELEASE_CACHE_SIZE = 128

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
        with self._release_cache_lock:
            self._release_cache.clear()
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

    def search_basic(self, query: str, max_results: int = 5) -> List[dict]:
        """
        Search Discogs with ONE request and build basic results from the raw
        search payloads (no per-release fetch).

        Only each result's plain ``.data`` dict is read: attribute access on
        discogs_client objects can lazily fetch the full release.  Tracklists
        come later from get_release_by_id (GET /api/release/{id}).

        Args:
            query: Search query
            max_results: Maximum number of results to return

        Returns:
            List of dicts (id, artist, title, year, label, format, country,
            cover_url, uri, master_id)
        """
        self._rate_limit()
        results = self.client.search(query, type="release")
        # Page size goes on the list, not in the query: the library appends its own
        # per_page (default 50) to each page URL, so a per_page search field would
        # send Discogs two page sizes.
        results.per_page = max(1, int(max_results))
        hits = results.page(1)

        basic = []
        for result in hits[:max_results]:
            data = result.data
            rid = data.get("id")
            if rid is None:
                continue
            artist, title = self._split_search_title(data.get("title") or "")
            basic.append(
                {
                    "id": rid,
                    "artist": artist,
                    "title": title,
                    "year": data.get("year") or "",
                    "label": self._first(data.get("label")),
                    "format": self._first(data.get("format")),
                    "country": data.get("country") or "",
                    "cover_url": data.get("cover_image") or data.get("thumb") or None,
                    "uri": f"/release/{rid}",
                    "master_id": data.get("master_id") or None,
                }
            )
        return basic

    @staticmethod
    def _first(value) -> str:
        """First element of a list-valued search field (or the string itself)."""
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else ""
        return str(value) if value else ""

    @staticmethod
    def _split_search_title(full: str) -> Tuple[str, str]:
        """Split Discogs' "Artist - Title" search title (first " - " wins)."""
        if " - " in full:
            artist, title = full.split(" - ", 1)
            return artist.strip(), title.strip()
        return "Unknown Artist", full.strip()

    def get_release_by_id(self, release_id: int) -> Optional[DiscogsRelease]:
        """
        Get release by Discogs ID (served from a bounded in-memory cache).

        Args:
            release_id: Discogs release ID

        Returns:
            DiscogsRelease or None
        """
        key = int(release_id)
        with self._release_cache_lock:
            cached = self._release_cache.get(key)
            if cached is not None:
                self._release_cache.move_to_end(key)
                return cached

        try:
            self._rate_limit()
            release = DiscogsRelease(self.client.release(key))
        except Exception as e:
            print(f"Failed to fetch release {release_id}: {e}")
            return None

        with self._release_cache_lock:
            self._release_cache[key] = release
            self._release_cache.move_to_end(key)
            while len(self._release_cache) > self.RELEASE_CACHE_SIZE:
                self._release_cache.popitem(last=False)
        return release

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
        track_numbering: str = DEFAULT_TRACK_NUMBERING,
        comment: Optional[str] = None,
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
            comment: Comment tag text; None keeps the default ("Digitized from vinyl").

        Returns:
            True if successful
        """
        if output_format == "flac":
            return self._tag_flac(file_path, track, release, cover_data, track_numbering, comment)
        elif output_format == "mp3":
            return self._tag_mp3(file_path, track, release, cover_data, track_numbering, comment)
        elif output_format == "aiff":
            return self._tag_aiff(file_path, track, release, cover_data, track_numbering, comment)
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
    def _disc_of(position: str) -> Optional[int]:
        """LP number of a vinyl position: A,B→1  C,D→2  E,F→3 …; None if unrecognised.

        A side is one letter, possibly repeated ("A1", "AA1"), so "CD1" or "DVD1" (a disc
        sold with the record) is no side C or D. No end anchor, so sub-track positions like
        "B6.1" or "B6.2" match too."""
        m = re.match(r'^([A-Z])\1*\d+', position or "")
        if not m:
            return None
        return (ord(m.group(1)) - ord('A') + 2) // 2

    @staticmethod
    def _sequential_disc_per_lp(vinyl_number: str, release_positions: List[str]):
        """Compute (disc_str, track_str) for the sequential_disc_per_lp scheme.

        Sides A+B share disc 1, C+D share disc 2, E+F share disc 3, etc.
        TRACKNUMBER is the sequential position across both sides of that LP
        (e.g. A1–A4 become 1–4, then B1–B3 continue as 5–7, all on disc 1).

        ``release_positions`` is the release's tracklist in order, not just the
        tracks being exported: a side exported on its own continues after side A,
        and a track left out leaves a gap.

        Returns (None, None) if the position doesn't match the expected pattern.
        """
        disc_num = MetadataHandler._disc_of(vinyl_number)
        if disc_num is None:
            return None, None
        disc_positions = [p for p in release_positions if MetadataHandler._disc_of(p) == disc_num]
        seq = next((i + 1 for i, p in enumerate(disc_positions) if p == vinyl_number), None)
        if seq is None:
            return None, None
        return str(disc_num), str(seq)

    def _build_tag_set(
        self,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes],
        track_numbering: str = DEFAULT_TRACK_NUMBERING,
        comment: Optional[str] = None,
    ) -> Optional[TagSet]:
        """Compute the TagSet for one track.  Returns None if the Discogs
        track lookup fails — caller treats that as a tag-write failure."""
        discogs_track = self._find_discogs_track(track, release)
        if discogs_track is None:
            return None

        track_total = disc_total = None
        if track_numbering == "sequential_disc_per_lp":
            positions = [t.position for t in release.tracks]
            disc_str, track_str = self._sequential_disc_per_lp(track.vinyl_number, positions)
            if disc_str is None:
                # Unrecognised position format — fall back to vinyl scheme
                disc_str, track_str = None, track.vinyl_number
            else:
                disc_num = int(disc_str)
                track_total = str(sum(self._disc_of(p) == disc_num for p in positions))
                # The highest LP the release reaches, so every file of a release agrees even
                # when Discogs skips side letters (A, B, E, F -> 3).
                release_discs = {self._disc_of(t.position) for t in release.tracks} - {None}
                disc_total = str(max(release_discs | {disc_num}))
        else:
            disc_str, track_str = None, track.vinyl_number

        # Compilations: each track carries its own artist; the release artist
        # ("Various") is only the fallback and ALBUMARTIST says "Various Artists".
        various = getattr(release, "various_artists", False)
        artist = release.artist
        if various:
            artist = getattr(discogs_track, "artist", "") or release.artist

        classical = {}
        if any(str(genre).lower() == "classical" for genre in getattr(release, "genres", None) or []):
            classical = self._classical_tags(discogs_track, release, various)
            performer_names = classical.pop("performer_names")
            if performer_names:
                artist = ", ".join(performer_names)

        return TagSet(
            artist=artist,
            album=release.title,
            title=discogs_track.title,
            track_number=track_str,
            year=release.year,
            label=release.label or None,
            release_id=release.id,
            cover_data=cover_data,
            disc_number=disc_str,
            vinyl_position=track.vinyl_number if disc_str is not None else None,
            track_total=track_total,
            disc_total=disc_total,
            album_artist=VARIOUS_ARTISTS if various else release.artist,
            compilation=various,
            comment=comment or DIGITIZED_COMMENT,
            **classical,
        )

    def _classical_tags(self, discogs_track, release, various: bool) -> dict:
        """Composer / conductor / ensemble / performers for one track of a classical
        release (see ``release_credits``); ``performer_names`` feeds ARTIST."""
        # (raw Discogs position, exported position) pairs: credit track ranges are written
        # against what Discogs shows ("1 - 3", "B1a to B1b"), not our A1/B1 labels. An
        # index track's movements map to the umbrella, so their credits reach it.
        positions = []
        for release_track in release.tracks:
            positions.append((getattr(release_track, "raw_position", release_track.position), release_track.position))
            positions += [
                (part["position"], release_track.position) for part in getattr(release_track, "parts", None) or []
                if part.get("position") and part["position"] != release_track.position
            ]
        credits = credits_for_track(
            getattr(release, "credits", []), getattr(discogs_track, "credits", []),
            discogs_track.position, positions,
        )
        found = classify_credits(credits)
        # Every credit on the release: who a name is doesn't depend on which tracks it covers.
        all_credits = list(getattr(release, "credits", [])) + [
            credit for release_track in release.tracks for credit in getattr(release_track, "credits", [])
        ]
        # A compilation's track artist is as often the singer as the composer: no guess there.
        fallback_composer = "" if various else lone_artist_composer(getattr(release, "artist_names", ()), all_credits)
        performer_names = artist_from_credits(found)
        # Credits often name only part of the line up, so they replace ARTIST only when no
        # one would be lost from it; the composer isn't lost, ARTIST is for the performers.
        main_artists = getattr(discogs_track, "artist_names", ()) if various else getattr(release, "artist_names", ())
        accounted_for = {*performer_names, *classify_credits(all_credits).composers, fallback_composer}
        if not accounted_for.issuperset(main_artists):
            performer_names = []
        return {
            "composers": found.composers or ((fallback_composer,) if fallback_composer else ()),
            "conductors": found.conductors,
            "ensembles": found.ensembles,
            "performers": found.performers,
            "performer_names": performer_names,
        }

    def _apply_vorbis(self, audio: FLAC, tags: TagSet) -> None:
        """Write a TagSet to a FLAC file using Vorbis comments."""
        audio["ARTIST"] = tags.artist
        if tags.album_artist:
            audio["ALBUMARTIST"] = tags.album_artist
        audio["ALBUM"] = tags.album
        audio["TITLE"] = tags.title
        audio["TRACKNUMBER"] = tags.track_number
        if tags.track_total:
            audio["TRACKTOTAL"] = tags.track_total
        if tags.disc_number is not None:
            audio["DISCNUMBER"] = tags.disc_number
        if tags.disc_total:
            audio["DISCTOTAL"] = tags.disc_total
        if tags.vinyl_position is not None:
            audio["VINYLPOSITION"] = tags.vinyl_position
        if tags.compilation:
            audio["COMPILATION"] = "1"
        if tags.composers:
            audio["COMPOSER"] = list(tags.composers)
        if tags.conductors:
            audio["CONDUCTOR"] = list(tags.conductors)
        if tags.ensembles:
            audio["ENSEMBLE"] = list(tags.ensembles)
        if tags.performers:
            audio["PERFORMER"] = [performer_text(name, role) for name, role in tags.performers]
        if tags.year:
            audio["DATE"] = str(tags.year)
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
        if tags.album_artist:
            audio.tags["TPE2"] = TPE2(encoding=3, text=tags.album_artist)
        audio.tags["TRCK"] = TRCK(
            encoding=3,
            text=f"{tags.track_number}/{tags.track_total}" if tags.track_total else tags.track_number,
        )
        if tags.compilation:
            audio.tags["TCMP"] = TCMP(encoding=3, text="1")
        if tags.composers:
            audio.tags["TCOM"] = TCOM(encoding=3, text=list(tags.composers))
        if tags.conductors:
            audio.tags["TPE3"] = TPE3(encoding=3, text=list(tags.conductors))
        if tags.ensembles:
            audio.tags["TXXX:ENSEMBLE"] = TXXX(encoding=3, desc="ENSEMBLE", text=list(tags.ensembles))
        if tags.performers:
            audio.tags["TMCL"] = TMCL(
                encoding=3, people=[[role or "performer", name] for name, role in tags.performers],
            )
        if tags.disc_number is not None:
            audio.tags["TPOS"] = TPOS(
                encoding=3,
                text=f"{tags.disc_number}/{tags.disc_total}" if tags.disc_total else tags.disc_number,
            )
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

    @staticmethod
    def _comment_kwargs(comment: Optional[str]) -> dict:
        """``comment=`` only when set, so the default call to ``_build_tag_set`` stays unchanged
        (existing test doubles of it have a fixed positional signature)."""
        return {"comment": comment} if comment else {}

    def _tag_flac(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
        track_numbering: str = DEFAULT_TRACK_NUMBERING,
        comment: Optional[str] = None,
    ) -> bool:
        tags = self._build_tag_set(track, release, cover_data, track_numbering, **self._comment_kwargs(comment))
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
        track_numbering: str = DEFAULT_TRACK_NUMBERING,
        comment: Optional[str] = None,
    ) -> bool:
        """Shared ID3 tagger used by ``_tag_mp3`` and ``_tag_aiff``.

        ``open_audio(path)`` returns the Mutagen wrapper instance; the
        only difference between the MP3 and AIFF paths.
        """
        tags = self._build_tag_set(track, release, cover_data, track_numbering, **self._comment_kwargs(comment))
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

    def _tag_mp3(self, file_path, track, release, cover_data=None, track_numbering=DEFAULT_TRACK_NUMBERING,
                 comment=None):
        return self._tag_id3_format(
            file_path, track, release, cover_data, lambda p: MP3(p, ID3=ID3), track_numbering, comment,
        )

    def _tag_aiff(self, file_path, track, release, cover_data=None, track_numbering=DEFAULT_TRACK_NUMBERING,
                  comment=None):
        return self._tag_id3_format(file_path, track, release, cover_data, AIFF, track_numbering, comment)

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
