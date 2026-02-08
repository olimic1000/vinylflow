"""
VinylFlow - Metadata Handling Module

Handles Discogs API integration, metadata tagging, and cover art embedding.
Manages release searches, track mapping, and FLAC file tagging.
"""

import re
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from io import BytesIO

import requests
import discogs_client
from mutagen.flac import FLAC, Picture
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
            # Handle repeated letters (A, AA, AAA → A1, A2, A3 / B, BB, BBB → B1, B2, B3)
            elif position and re.match(r"^([A-Z])\1*$", position):
                # Count how many times the letter repeats
                letter = position[0]
                count = len(position)
                vinyl_pos = f"{letter}{count}"
                tracks.append(DiscogsTrack(vinyl_pos, title, duration))
            # Handle sequential numbers (1, 2, 3, 4) - we'll convert these to vinyl positions
            elif position and re.match(r"^\d+$", position):
                sequential_tracks.append((int(position), title, duration))
            # Handle empty position - assume sequential
            elif not position and title and title.lower() not in ["tracklist", "notes"]:
                # No position, but has a title - treat as sequential
                sequential_tracks.append((len(sequential_tracks) + 1, title, duration))

        # Handle sequential tracks (convert numeric positions to vinyl format)
        if sequential_tracks:
            sequential_tracks.sort(key=lambda x: x[0])  # Sort by track number

            # No vinyl positions found, convert all sequential to vinyl format
            total = len(sequential_tracks)
            half = (total + 1) // 2  # Round up for odd numbers

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

            # Iterate through results with manual counter (Discogs results don't support slicing)
            for i, result in enumerate(results, 1):
                if i > max_results:
                    break

                try:
                    # Fetch full release data
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
            # Include User-Agent header to avoid 403 errors from Discogs image server
            headers = {"User-Agent": self.client.user_agent}
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            # Open image
            img = Image.open(BytesIO(response.content))

            # Convert to RGB if needed (for JPEG compatibility)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

            # Save original size as folder.jpg
            img.save(output_path, "JPEG", quality=95)

            return True

        except Exception as e:
            print(f"Failed to download cover art: {e}")
            return False

    def prepare_cover_for_embedding(self, image_path: Path, max_size=1400) -> Optional[bytes]:
        """
        Prepare cover art for embedding in FLAC.

        Args:
            image_path: Path to image file
            max_size: Maximum dimension

        Returns:
            Image bytes (JPEG), or None if error
        """
        try:
            img = Image.open(image_path)

            # Convert to RGB
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Resize if too large
            if max(img.size) > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            # Save to bytes
            buffer = BytesIO()
            img.save(buffer, "JPEG", quality=90)
            return buffer.getvalue()

        except Exception as e:
            print(f"Failed to prepare cover art: {e}")
            return None

    def tag_flac_file(
        self,
        file_path: Path,
        track: "Track",
        release: DiscogsRelease,
        cover_data: Optional[bytes] = None,
    ) -> bool:
        """
        Write metadata tags to FLAC file.

        Args:
            file_path: Path to FLAC file
            track: Track object with vinyl_number set
            release: DiscogsRelease object
            cover_data: Optional cover art bytes to embed

        Returns:
            True if successful
        """
        try:
            audio = FLAC(file_path)

            # Clear existing tags
            audio.clear_pictures()
            audio.delete()

            # Find the Discogs track that matches this track
            discogs_track = None
            for dt in release.tracks:
                if dt.position == track.vinyl_number:
                    discogs_track = dt
                    break

            if not discogs_track:
                print(f"Warning: No Discogs track found for {track.vinyl_number}")
                return False

            # Write Vorbis comments
            audio["ARTIST"] = release.artist
            audio["ALBUM"] = release.title
            audio["TITLE"] = discogs_track.title
            audio["TRACKNUMBER"] = track.vinyl_number
            audio["DATE"] = str(release.year) if release.year else ""

            # Optional fields
            if release.label:
                audio["LABEL"] = release.label

            # Add Discogs reference
            audio["DISCOGS_RELEASE_ID"] = str(release.id)
            audio["COMMENT"] = "Digitized from vinyl"

            # Embed cover art if provided
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

    def sanitize_filename(self, name: str) -> str:
        """
        Sanitize string for use in filename.

        Args:
            name: Input string

        Returns:
            Safe filename string
        """
        # Replace problematic characters
        name = re.sub(r'[/\\:*?"<>|]', "-", name)

        # Remove leading/trailing spaces and dots
        name = name.strip(" .")

        # Replace multiple spaces with single space
        name = re.sub(r"\s+", " ", name)

        return name

    def create_album_folder_name(self, release: DiscogsRelease) -> str:
        """
        Create folder name for album.

        Args:
            release: DiscogsRelease object

        Returns:
            Folder name (e.g., "Aril Brikha - Departure")
        """
        artist = self.sanitize_filename(release.artist)
        title = self.sanitize_filename(release.title)
        return f"{artist} - {title}"

    def create_track_filename(self, track: "Track", release: DiscogsRelease) -> str:
        """
        Create filename for track.

        Args:
            track: Track object with vinyl_number set
            release: DiscogsRelease object

        Returns:
            Filename (e.g., "A1-Groove La Chord.flac")
        """
        # Find Discogs track
        discogs_track = None
        for dt in release.tracks:
            if dt.position == track.vinyl_number:
                discogs_track = dt
                break

        if not discogs_track:
            return f"{track.vinyl_number}-Unknown.flac"

        title = self.sanitize_filename(discogs_track.title)
        return f"{track.vinyl_number}-{title}.flac"


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

    # Check count mismatch
    if len(detected_tracks) != len(discogs_tracks):
        result["errors"].append(
            f"Track count mismatch: detected {len(detected_tracks)}, "
            f"Discogs has {len(discogs_tracks)}"
        )

    # Check for duration-based issues (merged tracks)
    for i, det_track in enumerate(detected_tracks):
        # Check if this track's duration matches sum of multiple Discogs tracks
        if i < len(discogs_tracks):
            discogs_duration = discogs_tracks[i].duration_seconds

            if discogs_duration:
                diff = abs(det_track.duration - discogs_duration)

                if diff < tolerance:
                    result["matches"].append(
                        f"Track {i+1}: Duration match ({det_track.duration:.0f}s)"
                    )
                elif diff > tolerance * 2:
                    # Check if it matches sum of this and next track
                    if i + 1 < len(discogs_tracks):
                        next_duration = discogs_tracks[i + 1].duration_seconds
                        if next_duration:
                            combined = discogs_duration + next_duration
                            if abs(det_track.duration - combined) < tolerance:
                                result["warnings"].append(
                                    f"⚠️ Track {i+1} ({det_track.duration:.0f}s) appears to contain "
                                    f"2 tracks: {discogs_tracks[i].position} + {discogs_tracks[i+1].position} "
                                    f"(combined: {combined:.0f}s)"
                                )

    return result
