"""
Audio processing module for Vinyl Digitizer.
Handles silence detection, track splitting, and FLAC conversion.
"""

import re
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional


class Track:
    """Represents a detected or split track."""

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


class AudioProcessor:
    """Handles audio processing operations."""

    def __init__(self, silence_threshold=-40, min_silence_duration=1.5,
                 min_track_length=30, flac_compression=8):
        """
        Initialize audio processor.

        Args:
            silence_threshold: Silence threshold in dB (negative)
            min_silence_duration: Minimum silence duration in seconds
            min_track_length: Minimum track length in seconds
            flac_compression: FLAC compression level (0-8)
        """
        self.silence_threshold = silence_threshold
        self.min_silence_duration = min_silence_duration
        self.min_track_length = min_track_length
        self.flac_compression = flac_compression

    def get_audio_duration(self, file_path: Path) -> Optional[float]:
        """
        Get total duration of audio file in seconds.

        Args:
            file_path: Path to audio file

        Returns:
            Duration in seconds, or None if error
        """
        try:
            cmd = [
                'ffmpeg', '-i', str(file_path),
                '-f', 'null', '-'
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            # Parse duration from ffmpeg output
            match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})', result.stderr)
            if match:
                hours, minutes, seconds = match.groups()
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

            return None
        except Exception as e:
            print(f"Error getting audio duration: {e}")
            return None

    def detect_silence(self, file_path: Path, verbose=False) -> List[Track]:
        """
        Detect silence in audio file and return track boundaries.

        Args:
            file_path: Path to audio file
            verbose: Print detailed output

        Returns:
            List of Track objects
        """
        if verbose:
            print(f"Detecting silence in: {file_path.name}")
            print(f"Threshold: {self.silence_threshold}dB, Min duration: {self.min_silence_duration}s")

        # Run ffmpeg silence detection
        cmd = [
            'ffmpeg', '-i', str(file_path),
            '-af', f'silencedetect=noise={self.silence_threshold}dB:duration={self.min_silence_duration}',
            '-f', 'null', '-'
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300
            )

            # Parse silence periods from stderr
            silence_starts = []
            silence_ends = []

            for line in result.stderr.split('\n'):
                if 'silence_start' in line:
                    match = re.search(r'silence_start: ([\d.]+)', line)
                    if match:
                        silence_starts.append(float(match.group(1)))
                elif 'silence_end' in line:
                    match = re.search(r'silence_end: ([\d.]+)', line)
                    if match:
                        silence_ends.append(float(match.group(1)))

            # Get total duration
            total_duration = self.get_audio_duration(file_path)
            if total_duration is None:
                raise ValueError("Could not determine audio duration")

            # Calculate track boundaries
            tracks = self._calculate_tracks(silence_starts, silence_ends, total_duration)

            if verbose:
                print(f"\nDetected {len(tracks)} tracks:")
                for track in tracks:
                    print(f"  {track}")

            return tracks

        except subprocess.TimeoutExpired:
            raise RuntimeError("Silence detection timed out (>5 minutes)")
        except Exception as e:
            raise RuntimeError(f"Silence detection failed: {e}")

    def _calculate_tracks(self, silence_starts: List[float], silence_ends: List[float],
                          total_duration: float) -> List[Track]:
        """
        Calculate track boundaries from silence periods.

        Args:
            silence_starts: List of silence start times
            silence_ends: List of silence end times
            total_duration: Total audio duration

        Returns:
            List of Track objects
        """
        # Create track boundaries
        # Track 1 starts at 0, ends at first silence start
        # Track N starts at previous silence end, ends at next silence start
        # Last track ends at total duration

        tracks = []
        track_num = 1

        # Handle case with no silence detected
        if not silence_starts:
            if total_duration >= self.min_track_length:
                tracks.append(Track(track_num, 0, total_duration))
            return tracks

        # First track
        if silence_starts[0] >= self.min_track_length:
            tracks.append(Track(track_num, 0, silence_starts[0]))
            track_num += 1

        # Middle tracks
        for i in range(len(silence_ends) - 1):
            start = silence_ends[i]
            end = silence_starts[i + 1] if i + 1 < len(silence_starts) else total_duration
            if end - start >= self.min_track_length:
                tracks.append(Track(track_num, start, end))
                track_num += 1

        # Last track
        if silence_ends:
            last_start = silence_ends[-1]
            if total_duration - last_start >= self.min_track_length:
                tracks.append(Track(track_num, last_start, total_duration))

        return tracks

    def split_tracks_duration_based(self, file_path: Path, durations: List[float],
                                    verbose=False) -> List[Track]:
        """
        Create track splits based on provided durations (for when silence detection fails).

        Args:
            file_path: Path to audio file
            durations: List of track durations from Discogs
            verbose: Print detailed output

        Returns:
            List of Track objects
        """
        tracks = []
        current_time = 0.0
        track_num = 1

        for duration in durations:
            start = current_time
            end = current_time + duration
            tracks.append(Track(track_num, start, end))
            current_time = end
            track_num += 1

        if verbose:
            print(f"\nCreated {len(tracks)} duration-based tracks:")
            for track in tracks:
                print(f"  {track}")

        return tracks

    def extract_track(self, input_file: Path, track: Track, output_file: Path,
                      verbose=False) -> bool:
        """
        Extract a single track and convert to FLAC.

        Args:
            input_file: Source WAV file
            track: Track object with start/end times
            output_file: Output FLAC file path
            verbose: Print detailed output

        Returns:
            True if successful
        """
        if verbose:
            print(f"Extracting {track.vinyl_number or f'Track {track.number}'}: {output_file.name}")

        cmd = [
            'ffmpeg',
            '-i', str(input_file),
            '-ss', str(track.start),
            '-t', str(track.duration),
            '-c:a', 'flac',
            '-compression_level', str(self.flac_compression),
            '-y',  # Overwrite output file
            str(output_file)
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600
            )

            if result.returncode != 0:
                print(f"Error: ffmpeg failed: {result.stderr}")
                return False

            # Verify output file exists and has reasonable size
            if not output_file.exists():
                print(f"Error: Output file not created: {output_file}")
                return False

            if output_file.stat().st_size < 1000:
                print(f"Error: Output file suspiciously small: {output_file}")
                return False

            # Verify duration is close to expected
            actual_duration = self.get_audio_duration(output_file)
            if actual_duration is None:
                print(f"Warning: Could not verify duration of {output_file}")
            elif abs(actual_duration - track.duration) > 2.0:
                print(f"Warning: Duration mismatch for {output_file}: "
                      f"expected {track.duration:.1f}s, got {actual_duration:.1f}s")

            return True

        except subprocess.TimeoutExpired:
            print(f"Error: Track extraction timed out")
            return False
        except Exception as e:
            print(f"Error extracting track: {e}")
            return False

    def extract_all_tracks(self, input_file: Path, tracks: List[Track],
                          output_dir: Path, verbose=False) -> List[Path]:
        """
        Extract all tracks from input file.

        Args:
            input_file: Source WAV file
            tracks: List of Track objects
            output_dir: Directory for output files
            verbose: Print detailed output

        Returns:
            List of successfully created output file paths
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        output_files = []

        for track in tracks:
            # Use vinyl number if available, otherwise track number
            track_id = track.vinyl_number if track.vinyl_number else f"{track.number:02d}"
            output_file = output_dir / f"temp_{track_id}.flac"

            if self.extract_track(input_file, track, output_file, verbose):
                output_files.append(output_file)
            else:
                print(f"Failed to extract track {track.number}")
                # Clean up partial output
                if output_file.exists():
                    output_file.unlink()

        return output_files

    def validate_audio_file(self, file_path: Path) -> Tuple[bool, str]:
        """
        Validate that file is a valid audio file.

        Args:
            file_path: Path to file

        Returns:
            (is_valid, message)
        """
        if not file_path.exists():
            return False, f"File not found: {file_path}"

        if not file_path.is_file():
            return False, f"Not a file: {file_path}"

        if file_path.stat().st_size == 0:
            return False, f"File is empty: {file_path}"

        # Try to get duration (validates it's a readable audio file)
        duration = self.get_audio_duration(file_path)
        if duration is None:
            return False, f"Not a valid audio file: {file_path}"

        if duration < 60:
            return False, f"Audio file too short ({duration:.1f}s): {file_path}"

        return True, f"Valid audio file ({duration / 60:.1f} minutes)"
