"""
VinylFlow - Metadata Handling Module

Handles Discogs API integration, metadata tagging, and cover art embedding.
Manages release searches, track mapping, and file tagging for FLAC, MP3, and AIFF.
"""

import re
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from io import BytesIO

import requests
import discogs_client
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TPUB, COMM, APIC, TXXX
from mutagen.aiff import AIFF
from PIL import Image


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

            # Handle vinyl positions (A1, B2, etc.)
            if position and re.match(r"^[A-Z]\d+", position):
                tracks.append(DiscogsTrack(position, title, duration))
            # Handle repeated letters (A, AA, AAA -> A1, A2, A3 / B, BB, BBB -> B1, B2, B3)
            elif position and re.match(r"^([A-Z])\1*$", position):
                letter = position[0]
                count = len(position)
                vinyl_pos = f"{letter}{count}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration))
            # Handle sequential numbers (1, 2, 3, 4)
            elif position and re.match(r"^\d+$", position):
                sequential_tracks.append((int(position), title, duration))
            # Handle empty position - assume sequential
            elif not position and title and title.lower() not in ["tracklist", "notes"]:
                sequential_tracks.append((len(sequential_tracks) + 1, title, duration))

        # Handle sequential tracks (convert numeric positions to vinyl format)
        if sequential_tracks:
            sequential_tracks.sort(key=lambda x: x[0])

            total = len(sequential_tracks)
            half = (total + 1) // 2

            for idx, (num, title, duration) in enumerate(sequential_tracks, 1):
                if idx <= half:
                    vinyl_pos = f"A{idx}"
                else:
                    vinyl_pos = f"B{idx - half}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration))

        # Sort all tracks by position for proper display
        if tracks:
            tracks.sort(
                key=lambda t: (
                    t.position[0],
                    int(t.position[1:]) if t.position[1:].isdigit() else 0,
                )
            )

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
        self.client = discogs_client.Client(user_agent, user_token=discogs_token)
        self.last_request_time = 0
        self.min_request_interval = 1.0  # Rate limiting: max 1 req/sec

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
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

        Returns:
            True if successful
        """
        if output_format == "flac":
            return self._tag_flac(file_path, track, release, cover_data)
        elif output_format == "mp3":
            return self._tag_mp3(file_path, track, release, cover_data)
        elif output_format == "aiff":
            return self._tag_aiff(file_path, track, release, cover_data)
        else:
            print(f"Unsupported output format for tagging: {output_format}")
            return False

    # Keep the old name as an alias for backwards compatibility (used by CLI)
    def tag_flac_file(self, file_path, track, release, cover_data=None):
        """Backwards-compatible alias for tag_file with FLAC format."""
        return self._tag_flac(file_path, track, release, cover_data)

    def _find_discogs_track(self, track, release):
        """Find the Discogs track matching a vinyl_number."""
        for dt in release.tracks:
            if dt.position == track.vinyl_number:
                return dt
        print(f"Warning: No Discogs track found for {track.vinyl_number}")
        return None

    def _tag_flac(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
    ) -> bool:
        """Write Vorbis comment tags to FLAC file."""
        try:
            audio = FLAC(file_path)
            audio.clear_pictures()
            audio.delete()

            discogs_track = self._find_discogs_track(track, release)
            if not discogs_track:
                return False

            audio["ARTIST"] = release.artist
            audio["ALBUM"] = release.title
            audio["TITLE"] = discogs_track.title
            audio["TRACKNUMBER"] = track.vinyl_number
            audio["DATE"] = str(release.year) if release.year else ""

            if release.label:
                audio["LABEL"] = release.label

            audio["DISCOGS_RELEASE_ID"] = str(release.id)
            audio["COMMENT"] = "Digitized from vinyl"

            if cover_data:
                picture = Picture()
                picture.type = 3  # Front cover
                picture.mime = "image/jpeg"
                picture.desc = "Cover"
                picture.data = cover_data
                audio.add_picture(picture)

            audio.save()
            return True

        except Exception as e:
            print(f"Failed to tag {file_path}: {e}")
            return False

    def _tag_mp3(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
    ) -> bool:
        """Write ID3v2 tags to MP3 file."""
        try:
            audio = MP3(file_path, ID3=ID3)

            try:
                audio.add_tags()
            except Exception:
                pass  # Tags already exist

            discogs_track = self._find_discogs_track(track, release)
            if not discogs_track:
                return False

            audio.tags["TIT2"] = TIT2(encoding=3, text=discogs_track.title)
            audio.tags["TPE1"] = TPE1(encoding=3, text=release.artist)
            audio.tags["TALB"] = TALB(encoding=3, text=release.title)
            audio.tags["TRCK"] = TRCK(encoding=3, text=track.vinyl_number)

            if release.year:
                audio.tags["TDRC"] = TDRC(encoding=3, text=str(release.year))

            if release.label:
                audio.tags["TPUB"] = TPUB(encoding=3, text=release.label)

            audio.tags["TXXX:DISCOGS_RELEASE_ID"] = TXXX(
                encoding=3, desc="DISCOGS_RELEASE_ID", text=str(release.id)
            )
            audio.tags["COMM"] = COMM(
                encoding=3, lang="eng", desc="", text="Digitized from vinyl"
            )

            if cover_data:
                audio.tags["APIC"] = APIC(
                    encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data,
                )

            audio.save()
            return True

        except Exception as e:
            print(f"Failed to tag {file_path}: {e}")
            return False

    def _tag_aiff(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
    ) -> bool:
        """Write ID3v2 tags to AIFF file (AIFF uses ID3 tags like MP3)."""
        try:
            audio = AIFF(file_path)

            try:
                audio.add_tags()
            except Exception:
                pass  # Tags already exist

            discogs_track = self._find_discogs_track(track, release)
            if not discogs_track:
                return False

            audio.tags["TIT2"] = TIT2(encoding=3, text=discogs_track.title)
            audio.tags["TPE1"] = TPE1(encoding=3, text=release.artist)
            audio.tags["TALB"] = TALB(encoding=3, text=release.title)
            audio.tags["TRCK"] = TRCK(encoding=3, text=track.vinyl_number)

            if release.year:
                audio.tags["TDRC"] = TDRC(encoding=3, text=str(release.year))

            if release.label:
                audio.tags["TPUB"] = TPUB(encoding=3, text=release.label)

            audio.tags["TXXX:DISCOGS_RELEASE_ID"] = TXXX(
                encoding=3, desc="DISCOGS_RELEASE_ID", text=str(release.id)
            )
            audio.tags["COMM"] = COMM(
                encoding=3, lang="eng", desc="", text="Digitized from vinyl"
            )

            if cover_data:
                audio.tags["APIC"] = APIC(
                    encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data,
                )

            audio.save()
            return True

        except Exception as e:
            print(f"Failed to tag {file_path}: {e}")
            return False

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


def compare_track_durations(
    detected_tracks: List["Track"], discogs_tracks: List[DiscogsTrack], tolerance=5.0
) -> Dict:
    """
    Compare detected tracks with Discogs tracks to validate matching.

    Args:
        detected_tracks: List of detected Track objects
        discogs_tracks: List of DiscogsTrack objects from Discogs
        tolerance: Tolerance in seconds for duration mismatch

    Returns:
        Dict with 'matches', 'warnings', and 'errors' keys
    """
    result = {
        "matches": [],
        "warnings": [],
        "errors": [],
        "total_detected": len(detected_tracks),
        "total_discogs": len(discogs_tracks),
    }

    if len(detected_tracks) != len(discogs_tracks):
        result["errors"].append(
            f"Track count mismatch: detected {len(detected_tracks)}, "
            f"Discogs has {len(discogs_tracks)}"
        )

    for i, det_track in enumerate(detected_tracks):
        if i < len(discogs_tracks):
            discogs_duration = discogs_tracks[i].duration_seconds

            if discogs_duration:
                diff = abs(det_track.duration - discogs_duration)

                if diff < tolerance:
                    result["matches"].append(
                        f"Track {i+1}: Duration match ({det_track.duration:.0f}s)"
                    )
                elif diff > tolerance * 2:
                    if i + 1 < len(discogs_tracks):
                        next_duration = discogs_tracks[i + 1].duration_seconds
                        if next_duration:
                            combined = discogs_duration + next_duration
                            if abs(det_track.duration - combined) < tolerance:
                                result["warnings"].append(
                                    f"Track {i+1} ({det_track.duration:.0f}s) appears to contain "
                                    f"2 tracks: {discogs_tracks[i].position} + {discogs_tracks[i+1].position} "
                                    f"(combined: {combined:.0f}s)"
                                )

    return result
