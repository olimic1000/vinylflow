"""Restore exports must keep the source sample rate (loudnorm outputs 192 kHz)."""

import shutil

import pytest

from audio_processor import AudioProcessor, Track, parse_sample_rate, run_ffmpeg

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_parse_sample_rate():
    line = "Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 48000 Hz, stereo, s16, 1536 kb/s"
    assert parse_sample_rate(line) == 48000
    assert parse_sample_rate("") is None
    assert parse_sample_rate(None) is None


def _tone(path, rate):
    run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", f"sine=f=440:d=6:sample_rate={rate}", "-c:a", "pcm_s16le", str(path)],
        capture_output=True,
        check=True,
    )


def _rate(path):
    return parse_sample_rate(run_ffmpeg(["-i", str(path)], capture_output=True).stderr)


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


@needs_ffmpeg
def test_no_restore_rate_unchanged(tmp_path):
    src = tmp_path / "source.wav"
    out = tmp_path / "out.flac"
    _tone(src, 48000)
    assert AudioProcessor().extract_track(src, Track(1, 1.0, 5.0), out, output_format="flac")
    assert _rate(out) == 48000
