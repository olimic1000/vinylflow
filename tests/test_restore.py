"""Restore cleans up clicks and rumble but must leave volume and sample rate alone."""

import re
import shutil

import pytest

from audio_processor import AudioProcessor, Track, run_ffmpeg

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _tone(path, rate=44100):
    run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", f"sine=f=440:d=6:sample_rate={rate}", "-c:a", "pcm_s16le", str(path)],
        capture_output=True,
        check=True,
    )


def _mean_volume(path):
    stderr = run_ffmpeg(
        ["-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True
    ).stderr
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", stderr).group(1))


def _rate(path):
    stderr = run_ffmpeg(["-i", str(path)], capture_output=True).stderr
    return int(re.search(r"Audio:.*?,\s*(\d+) Hz", stderr).group(1))


@needs_ffmpeg
def test_restore_keeps_volume(tmp_path):
    src = tmp_path / "source.wav"
    plain = tmp_path / "plain.flac"
    restored = tmp_path / "restored.flac"
    _tone(src)
    processor = AudioProcessor()
    assert processor.extract_track(src, Track(1, 1.0, 5.0), plain)
    assert processor.extract_track(src, Track(1, 1.0, 5.0), restored, restoration_level=1)
    assert abs(_mean_volume(restored) - _mean_volume(plain)) < 0.5


@needs_ffmpeg
@pytest.mark.parametrize(
    "fmt, rate",
    # MP3 tops out at 48 kHz, so 96 kHz is only checked for FLAC and AIFF.
    [("flac", 44100), ("mp3", 44100), ("aiff", 44100), ("flac", 96000), ("aiff", 96000)],
)
def test_restore_keeps_source_sample_rate(tmp_path, fmt, rate):
    src = tmp_path / "source.wav"
    out = tmp_path / f"out.{fmt}"
    _tone(src, rate)
    assert AudioProcessor().extract_track(src, Track(1, 1.0, 5.0), out, output_format=fmt, restoration_level=1)
    assert _rate(out) == rate
