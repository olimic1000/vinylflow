"""
VinylFlow - Audio Processing Module

Handles silence detection, track splitting, and audio format conversion.
Uses FFmpeg for audio analysis and format conversion.
Supports FLAC, MP3, and AIFF output formats.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional

# Track Boundary calculation lives in its own pure module.  Re-export Track
# so existing callers that do ``from audio_processor import Track`` keep
# working — the dependency arrow stays one-way (audio_processor ->
# track_boundaries, no import cycle).
from track_boundaries import (
    Track,
    boundaries_for_track_count,
    boundaries_from_gaps,
    envelope_from_pcm,
    gaps_from_envelope,
    parse_silence_log,
    refine_cuts,
)

# Energy envelope: one mono decode at a low sample rate, RMS per 10 ms frame.
ENVELOPE_SAMPLE_RATE = 8000
ENVELOPE_FRAME_S = 0.01


def _ffmpeg() -> str:
    """Return the ffmpeg executable to use.

    In a bundled (PyInstaller) build the launcher sets VINYLFLOW_FFMPEG_PATH
    to the absolute path of the bundled binary.  Fall back to bare 'ffmpeg'
    so plain source-code runs still work.
    """
    return os.environ.get("VINYLFLOW_FFMPEG_PATH") or "ffmpeg"


def run_ffmpeg(args: list, *, text: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run ffmpeg with the bundled binary path + correct text-mode defaults.

    All ffmpeg invocations across the project must go through this seam,
    not call ``subprocess.run(["ffmpeg", ...])`` directly — bundled
    builds depend on ``VINYLFLOW_FFMPEG_PATH`` and Windows requires
    utf-8/replace decoding so non-ASCII filenames don't crash the
    subprocess module.

    ``text=True`` (default) captures stdout/stderr as utf-8 strings with
    error replacement — correct for parsing ffmpeg's stderr (silence
    detection, duration probing).  Pass ``text=False`` when you need
    raw bytes (e.g. piping PCM through stdout for waveform peaks).

    All other ``subprocess.run`` kwargs flow through unchanged.
    """
    cmd = [_ffmpeg(), *args]
    if text:
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)


_DURATION_HEADER_RE = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})")


def parse_duration_header(ffmpeg_stderr: str) -> Optional[float]:
    """Parse total duration in seconds from ffmpeg's stderr header, or None."""
    match = _DURATION_HEADER_RE.search(ffmpeg_stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


_STREAM_AUDIO_RE = re.compile(r"Stream #\S+.*?Audio:\s*([^\n]*)")
_PCM_CODEC_RE = re.compile(r"\bpcm_([suf])(\d+)(?:le|be)\b")
_BIT_ANNOTATION_RE = re.compile(r"\((\d+) bit\)")


def parse_source_bit_depth(ffmpeg_stderr: str) -> Optional[int]:
    """Parse the first audio stream's bit depth from ffmpeg's stderr header.

    Reads an explicit ``(24 bit)`` annotation first (ffmpeg uses it for 24-bit
    audio held in 32-bit words), then the ``pcm_*`` codec name; float counts as
    32.  Returns None if the stream line can't be parsed.
    """
    match = _STREAM_AUDIO_RE.search(ffmpeg_stderr or "")
    if not match:
        return None
    line = match.group(1)
    annotation = _BIT_ANNOTATION_RE.search(line)
    if annotation:
        return int(annotation.group(1))
    codec = _PCM_CODEC_RE.search(line)
    if codec:
        return 32 if codec.group(1) == "f" else int(codec.group(2))
    return None


_SAMPLE_RATE_RE = re.compile(r"Audio:.*?,\s*(\d+) Hz")


def parse_sample_rate(ffmpeg_stderr: str) -> Optional[int]:
    """Sample rate of the first audio stream from ffmpeg's header dump."""
    match = _SAMPLE_RATE_RE.search(ffmpeg_stderr or "")
    return int(match.group(1)) if match else None


# Filtered cuts are decoded with this much extra audio on each side so the
# filters' start-up transients (highpass settling, adeclick's first window) land
# outside the part that is kept.
FILTER_PAD_S = 1.0


def padded_window(
    start: float,
    end: float,
    total_duration: float,
    sample_rate: int,
    pad_s: float = FILTER_PAD_S,
) -> Tuple[float, float, int, int]:
    """Plan a padded read of ``[start, end]``.

    Returns ``(seek_s, read_dur_s, trim_start_sample, trim_end_sample)``: read
    ``read_dur_s`` seconds from ``seek_s`` (``max(0, start-pad)`` to
    ``min(total, end+pad)``), filter, then keep samples
    ``[trim_start_sample, trim_end_sample)`` of the read.  The read start is
    snapped to a whole sample, so the kept length is exactly
    ``round(end*sr) - round(start*sr)`` and sample-aligned with an unpadded cut.
    """
    s0 = int(round(start * sample_rate))
    s1 = int(round(end * sample_rate))
    r0 = max(0, s0 - int(round(pad_s * sample_rate)))
    seek_s = r0 / sample_rate
    read_end = min(float(total_duration), end + pad_s)
    read_dur = max(read_end - seek_s, 0.0)
    return seek_s, read_dur, s0 - r0, s1 - r0


def trim_filter(trim_start: int, trim_end: int) -> str:
    """Keep samples ``[trim_start, trim_end)`` and restart timestamps at 0."""
    return f"atrim=start_sample={trim_start}:end_sample={trim_end},asetpts=PTS-STARTPTS"


def seek_args(seek_s: float, read_dur: float) -> List[str]:
    return ["-ss", f"{seek_s:.9f}", "-t", f"{read_dur:.9f}"]


# --- Export filter chain -----------------------------------------------------

# highpass=f=15: removes inaudible turntable motor rumble (below 15 Hz) without touching bass.
RUMBLE_FILTER = "highpass=f=15"

# Click & pop removal strengths -> adeclick settings. Chosen by measurement
# (tests/test_declick_strength.py: synthetic music + hiss + drum-like transients +
# 120 known clicks, 0.03-0.6 amplitude). Findings: the burst-fusion parameter ``b``
# and the AR window ``w`` (ms) drive transient damage far more than ``t``; a short
# window (w=30) keeps drum hits intact while a lower threshold ``t`` finds fainter
# clicks, but t<=1.2 becomes ~20x slower (false positives), so Strong stops at 1.5.
#   light  (t=2.5, b=3, w=30)       ~88% removed, damage 0.020
#   medium (t=1.8, b=3, w=30)       ~96% removed, damage 0.043, as fast as light
#   strong (t=1.5, b=3, w=30)       ~98% removed, damage 0.063, ~5x slower than light
# LEGACY_LIGHT (documentation only, not used): the original Clean Up setting was
# "adeclick=t=3:b=3" (86% removed, damage 0.073).  It removed fewer clicks *and*
# damaged transients more than medium, so Light was retuned to a shorter AR window:
#   candidate            removed  damage
#   t=3:b=3 (old)         0.858   0.073
#   t=3:b=3:w=30          0.850   0.012
#   t=3:b=2:w=30          0.758   0.007
#   t=3:b=3:w=40          0.858   0.032
#   t=2.5:b=3:w=30        0.883   0.020   <- chosen
#   t=2.2:b=3:w=30        0.908   0.028
# Exports made with "light" therefore sound slightly different from before.
DECLICK_PRESETS = {
    "light": "adeclick=t=2.5:b=3:w=30",
    "medium": "adeclick=t=1.8:b=3:w=30",
    "strong": "adeclick=t=1.5:b=3:w=30",
}
DECLICK_LEVELS = ("off",) + tuple(DECLICK_PRESETS)

# Legacy ``restoration_level`` 1 (rumble + clicks) -> (rumble, declick).
LEGACY_RESTORE = (True, "light")

# Output depth setting: "source" keeps a 16-bit recording at 16 bits, "24" always writes 24.
BIT_DEPTH_SETTINGS = ("source", "24")

# Processed audio runs in double precision (planar), so filters never round to 16 bits.
PROCESSING_FORMAT = "aformat=sample_fmts=dblp"

DITHER_TO_16 = "aresample=osf=s16:dither_method=triangular"


def target_bit_depth(setting: str, source_depth: Optional[int]) -> int:
    """Resolve the ``bit_depth`` setting ("source" | "24") to 16 or 24.

    Only a source known to be 16 bits or fewer exports at 16; an unknown depth
    exports at 24 so a 24-bit source can never be truncated.
    """
    if setting == "24":
        return 24
    return 16 if source_depth is not None and source_depth <= 16 else 24


def split_export_filters(
    rumble: bool,
    declick: str = "off",
    dither_to_16: bool = False,
) -> Optional[Tuple[str, Optional[str]]]:
    """``(main_chain, dither)`` pieces of the export chain, or None when there is
    no processing.  ``main_chain`` is a double-precision conversion, then rumble ->
    declick; ``dither`` is the trailing 16-bit conversion (None unless ``dither_to_16``).
    Kept apart so padded cuts can trim between the two.

    The ``aformat`` comes first because ``highpass`` accepts 16-bit integer samples: with
    rumble alone ffmpeg would stay in s16, making the dither a plain repack and leaving
    the low 8 bits of a 24-bit export empty (``adeclick`` already forces doubles)."""
    filters = [RUMBLE_FILTER] if rumble else []
    if declick and declick != "off":
        if declick not in DECLICK_PRESETS:
            raise ValueError(f"unknown declick strength: {declick!r}")
        filters.append(DECLICK_PRESETS[declick])
    if not filters:
        return None
    filters.insert(0, PROCESSING_FORMAT)
    return ",".join(filters), (DITHER_TO_16 if dither_to_16 else None)


def build_export_filters(
    rumble: bool,
    declick: str = "off",
    dither_to_16: bool = False,
) -> Optional[str]:
    """Build the ffmpeg ``-af`` chain used for export.

    Order: double-precision conversion -> rumble filter -> click/pop removal (``declick`` = off|light|medium|strong).
    When processing runs and ``dither_to_16`` is set, the chain ends with a
    triangular-dithered 16-bit conversion.  Returns None when there is no
    processing (the chain is never just a dither, so unprocessed 16->16 stays
    bit-exact).  No loudnorm: it levelled every track to the same loudness and
    resampled the output to 192 kHz.
    """
    parts = split_export_filters(rumble, declick, dither_to_16)
    if parts is None:
        return None
    main, dither = parts
    return f"{main},{dither}" if dither else main


# Supported input formats
SUPPORTED_INPUT_EXTENSIONS = {".wav", ".aiff", ".aif"}

# Output format configurations: codec flags for FFmpeg + file extension
OUTPUT_FORMATS = {
    "flac": {
        "extension": ".flac",
        "codec_args": ["-c:a", "flac"],
        "label": "FLAC (Lossless)",
    },
    "mp3": {
        "extension": ".mp3",
        "codec_args": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "label": "MP3 (320kbps)",
    },
    "aiff": {
        "extension": ".aiff",
        "codec_args": [],  # the codec is pcm_s16be or pcm_s24be by depth, see depth_codec_args
        "label": "AIFF (Lossless)",
    },
}


def depth_codec_args(output_format: str, bit_depth: int) -> List[str]:
    """Extra codec args pinning the output sample depth (FLAC/AIFF only)."""
    if output_format == "flac":
        if bit_depth == 24:
            return ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        return ["-sample_fmt", "s16"]
    if output_format == "aiff":
        return ["-c:a", "pcm_s24be" if bit_depth == 24 else "pcm_s16be"]
    return []


class AudioProcessor:
    """Handles audio processing operations."""

    def __init__(
        self,
        silence_threshold=-40,
        min_silence_duration=1.5,
        min_track_length=30,
        flac_compression=8,
    ):
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
        # (path, mtime_ns, size) -> (duration, sample rate)
        self._probe_cache = {}

    def probe_info(self, file_path: Path) -> Tuple[Optional[float], Optional[int]]:
        """(duration seconds, sample rate Hz), cached per source file."""
        try:
            st = Path(file_path).stat()
            key = (str(file_path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None and key in self._probe_cache:
            return self._probe_cache[key]
        try:
            result = run_ffmpeg(["-i", str(file_path)], capture_output=True, timeout=30)
            info = (parse_duration_header(result.stderr), parse_sample_rate(result.stderr))
        except Exception as e:
            print(f"Error probing audio: {e}")
            return None, None
        if key is not None and info[0] is not None and info[1]:
            self._probe_cache[key] = info
        return info

    def padded_cut(self, file_path: Path, start: float, end: float):
        """``padded_window`` for a cut of ``file_path`` or None if the source
        can't be probed (callers then fall back to an unpadded cut)."""
        duration, rate = self.probe_info(file_path)
        if not duration or not rate:
            return None
        return padded_window(start, end, duration, rate)

    def get_audio_duration(self, file_path: Path) -> Optional[float]:
        """
        Get total duration of audio file in seconds.

        Args:
            file_path: Path to audio file

        Returns:
            Duration in seconds, or None if error
        """
        try:
            # No output target: ffmpeg prints the Duration header and exits
            # nonzero without decoding the file, so this is O(1) in file size.
            result = run_ffmpeg(
                ["-i", str(file_path)],
                capture_output=True,
                timeout=30,
            )

            return parse_duration_header(result.stderr)
        except Exception as e:
            print(f"Error getting audio duration: {e}")
            return None

    def get_source_bit_depth(self, file_path: Path) -> Optional[int]:
        """Bit depth of the source's first audio stream, or None if unknown."""
        try:
            result = run_ffmpeg(["-i", str(file_path)], capture_output=True, timeout=30)
            return parse_source_bit_depth(result.stderr)
        except Exception as e:
            print(f"Error getting source bit depth: {e}")
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
            print(
                f"Threshold: {self.silence_threshold}dB, Min duration: {self.min_silence_duration}s"
            )

        try:
            gaps, total_duration = self._scan_gaps(
                file_path, self.silence_threshold, self.min_silence_duration
            )

            tracks = boundaries_from_gaps(gaps, total_duration, self.min_track_length)

            if verbose:
                print(f"\nDetected {len(tracks)} tracks:")
                for track in tracks:
                    print(f"  {track}")

            return tracks

        except subprocess.TimeoutExpired:
            raise RuntimeError("Silence detection timed out (>5 minutes)")
        except Exception as e:
            raise RuntimeError(f"Silence detection failed: {e}")

    def _scan_gaps(self, file_path: Path, threshold: float, min_duration: float):
        """Run one ffmpeg silencedetect pass and return (gaps, total_duration).

        Owns the ffmpeg subprocess; parsing Gaps and computing Track
        Boundaries stay in the pure ``track_boundaries`` module.
        """
        args = [
            "-i",
            str(file_path),
            "-af",
            f"silencedetect=noise={threshold}dB:duration={min_duration}",
            "-f",
            "null",
            "-",
        ]

        result = run_ffmpeg(args, capture_output=True, timeout=300)

        gaps = parse_silence_log(result.stderr)

        # The silencedetect run's stderr already contains the Duration
        # header, so avoid a second ffmpeg probe of the same file.
        total_duration = parse_duration_header(result.stderr)
        if total_duration is None:
            total_duration = self.get_audio_duration(file_path)
        if total_duration is None:
            raise ValueError("Could not determine audio duration")

        return gaps, total_duration

    # Smart Rescan scan ladder, run over the cached envelope (no decode per
    # pass).  The first pass is permissive enough (-30dB, 0.3s) to surface
    # nearly every real inter-track gap as a candidate; picking the right
    # ones happens in boundaries_for_track_count.  Later passes escalate for
    # very noisy records whose surface noise sits above the previous
    # threshold.
    SMART_RESCAN_LADDER = [(-30.0, 0.3), (-25.0, 0.25), (-20.0, 0.2)]

    # Frames per bounded block when reducing the spooled decode (peaks bins
    # and envelope frames alike): ~80k samples, about 160 KB of PCM.
    DECODE_BLOCK_FRAMES = 1000

    def compute_envelope_and_peaks(self, file_path: Path, block_frames: int = None):
        """Decode ``file_path`` once; return ``(envelope_db, peaks_dict)``.

        One mono, 8 kHz, s16le decode is spooled to a temporary file (never
        held in memory) and reduced in blocks of ``block_frames`` frames to
        both arrays, so a full-album recording costs one decode and bounded RAM.

        * Envelope: per-frame RMS dBFS (10 ms frames, ``ENVELOPE_FRAME_S``),
          exactly ``envelope_from_pcm`` on the whole signal: whole frames
          only, a trailing partial frame is dropped, float32.  Every silence
          question (gaps, music span) is answered in memory from it.
        * Peaks: max |sample| per equal-width bin, one bin per started 10 ms
          frame.  Keeps 10 ms transient timing at any album length; bin edges
          spread the partial tail across all bins so WaveSurfer's even
          spacing stays true to the source times.
        """
        import numpy as np

        block_frames = block_frames or self.DECODE_BLOCK_FRAMES
        frame_samples = int(ENVELOPE_SAMPLE_RATE * ENVELOPE_FRAME_S)
        peaks = []
        envelope_blocks = []
        with tempfile.TemporaryFile() as pcm:
            run_ffmpeg(
                ["-v", "error", "-i", str(file_path), "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ac", "1", "-ar", str(ENVELOPE_SAMPLE_RATE), "-"],
                text=False, stdout=pcm, stderr=subprocess.PIPE, check=True,
            )
            pcm.flush()   # buffered writes by a caller-side fake would not show in the size yet
            sample_count = os.fstat(pcm.fileno()).st_size // 2
            peak_count = (sample_count + frame_samples - 1) // frame_samples
            for first in range(0, peak_count, block_frames):
                last = min(first + block_frames, peak_count)
                # Bin edges are not frame-aligned (the tail is spread), so the
                # peaks block reads its own sample range.
                edges = np.arange(first, last + 1, dtype=np.int64) * sample_count // peak_count
                pcm.seek(int(edges[0]) * 2)
                block = pcm.read(int(edges[-1] - edges[0]) * 2)
                # Promote before abs: abs(-32768) overflows in int16.
                samples = np.frombuffer(block, dtype="<i2").astype(np.int32)
                values = np.maximum.reduceat(np.abs(samples), edges[:-1] - edges[0]) / 32768.0
                peaks.extend(np.round(values, 6).tolist())
            # Envelope: whole frames only, read in frame-aligned blocks.  Frames
            # are independent, so blockwise equals one pass over everything.
            whole_frames = sample_count // frame_samples
            pcm.seek(0)
            for first in range(0, whole_frames, block_frames):
                frames_here = min(block_frames, whole_frames - first)
                block = pcm.read(frames_here * frame_samples * 2)
                envelope_blocks.append(
                    envelope_from_pcm(np.frombuffer(block, dtype="<i2"), frame_samples)
                )
        envelope = (np.concatenate(envelope_blocks) if envelope_blocks
                    else np.empty(0, dtype=np.float32))
        peaks_dict = {"peaks": peaks or [0.0], "length": len(peaks) or 1,
                      "duration": sample_count / ENVELOPE_SAMPLE_RATE}
        return envelope, peaks_dict

    def compute_envelope(self, file_path: Path):
        """Per-frame RMS dBFS envelope of ``file_path`` (see
        ``compute_envelope_and_peaks``, which this wraps)."""
        return self.compute_envelope_and_peaks(file_path)[0]

    def compute_waveform_peaks(self, file_path: Path) -> dict:
        """Waveform peaks of ``file_path`` (see ``compute_envelope_and_peaks``)."""
        return self.compute_envelope_and_peaks(file_path)[1]

    def refine_tracks(self, db, tracks):
        """Widen envelope-derived tracks over audio still above the user's
        silence threshold, so quiet fades aren't clipped by permissive
        detection thresholds."""
        return refine_cuts(db, ENVELOPE_FRAME_S, tracks, self.silence_threshold)

    def tracks_for_count_from_envelope(self, db, target_count: int):
        """Smart Rescan over a cached envelope: find the split that yields
        exactly ``target_count`` tracks.

        Tries each ``SMART_RESCAN_LADDER`` rung (threshold, min gap) in turn
        and stops at the first whose track count matches.  Returns
        ``(tracks, scan_info)``: ``scan_info`` holds that rung's
        ``silence_threshold`` and ``min_silence_duration`` and ``matched``;
        when no rung matches, the closest result across all rungs is returned
        with ``matched`` False.  Tracks are widened by ``refine_tracks``.
        """
        total_duration = len(db) * ENVELOPE_FRAME_S
        best_tracks = None
        best_info = None

        for threshold, min_duration in self.SMART_RESCAN_LADDER:
            gaps = gaps_from_envelope(db, ENVELOPE_FRAME_S, threshold, min_duration)
            tracks = boundaries_for_track_count(
                gaps, total_duration, self.min_track_length, target_count
            )
            info = {
                "silence_threshold": threshold,
                "min_silence_duration": min_duration,
                "matched": len(tracks) == target_count,
            }
            if info["matched"]:
                return self.refine_tracks(db, tracks), info
            if best_tracks is None or abs(len(tracks) - target_count) < abs(
                len(best_tracks) - target_count
            ):
                best_tracks, best_info = tracks, info

        return self.refine_tracks(db, best_tracks), best_info

    def extract_track(
        self,
        input_file: Path,
        track: Track,
        output_file: Path,
        output_format: str = "flac",
        verbose: bool = False,
        rumble: bool = False,
        declick: str = "off",
        bit_depth: str = "source",
    ) -> bool:
        """
        Extract a single track and convert to the specified format.

        Args:
            input_file: Source audio file
            track: Track object with start/end times
            output_file: Output file path
            output_format: One of 'flac', 'mp3', 'aiff'
            verbose: Print detailed output
            rumble: apply the 15 Hz rumble high-pass
            declick: click/pop removal strength (off|light|medium|strong)
            bit_depth: "source" (16-bit sources stay 16-bit) or "24"; FLAC/AIFF only

        Returns:
            True if successful
        """
        if verbose:
            print(f"Extracting {track.vinyl_number or f'Track {track.number}'}: {output_file.name}")

        format_config = OUTPUT_FORMATS.get(output_format, OUTPUT_FORMATS["flac"])

        depth = 16
        if output_format in ("flac", "aiff"):
            source_depth = self.get_source_bit_depth(input_file) if bit_depth == "source" else None
            depth = target_bit_depth(bit_depth, source_depth)
        # Dither only when processing runs; an unprocessed 16 -> 16 cut stays bit-exact.
        parts = split_export_filters(
            rumble,
            declick,
            dither_to_16=(depth == 16 and output_format in ("flac", "aiff")),
        )
        # Filtered cuts are read with pre/post-roll and trimmed sample-exactly
        # after the filters (dither last); unfiltered cuts stay untouched.
        window = self.padded_cut(input_file, track.start, track.end) if parts else None
        # -ss/-t before -i: input-side seek, so ffmpeg jumps straight to the
        # track instead of decoding everything before it. Sample-accurate for
        # PCM WAV/AIFF input.
        if window:
            seek_s, read_dur, trim_start, trim_end = window
            args = seek_args(seek_s, read_dur) + ["-i", str(input_file)]
        else:
            args = [
                "-ss",
                str(track.start),
                "-t",
                str(track.duration),
                "-i",
                str(input_file),
            ]

        # Add codec-specific args
        args.extend(format_config["codec_args"])
        args.extend(depth_codec_args(output_format, depth))

        # Add FLAC compression level if applicable
        if output_format == "flac":
            args.extend(["-compression_level", str(self.flac_compression)])

        # Add the filter chain if processing is on
        if parts:
            main, dither = parts
            if window:
                main = f"{main},{trim_filter(trim_start, trim_end)}"
            args.extend(["-af", f"{main},{dither}" if dither else main])

        args.extend([
            "-y",  # Overwrite output file
            str(output_file),
        ])

        try:
            result = run_ffmpeg(args, capture_output=True, timeout=600)

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
                print(
                    f"Warning: Duration mismatch for {output_file}: "
                    f"expected {track.duration:.1f}s, got {actual_duration:.1f}s"
                )

            return True

        except subprocess.TimeoutExpired:
            print(f"Error: Track extraction timed out")
            return False
        except Exception as e:
            print(f"Error extracting track: {e}")
            return False

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

        # Check extension
        if file_path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS:
            return False, (
                f"Unsupported format: {file_path.suffix}. "
                f"Supported: {', '.join(SUPPORTED_INPUT_EXTENSIONS)}"
            )

        # Try to get duration (validates it's a readable audio file)
        duration = self.get_audio_duration(file_path)
        if duration is None:
            return False, f"Not a valid audio file: {file_path}"

        if duration < 60:
            return False, f"Audio file too short ({duration:.1f}s): {file_path}"

        return True, f"Valid audio file ({duration / 60:.1f} minutes)"
