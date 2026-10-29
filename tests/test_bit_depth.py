"""Bit depth handling: AIFF export must not truncate 24-bit sources."""

import shutil

import pytest
from mutagen.aiff import AIFF

from audio_processor import AudioProcessor, Track, parse_source_bit_depth, run_ffmpeg

S16 = "Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 44100 Hz, stereo, s16, 1411 kb/s"
S24 = "Stream #0:0: Audio: pcm_s24le ([1][0][0][0] / 0x0001), 44100 Hz, stereo, s32 (24 bit), 2116 kb/s"
S24_AIFF = "Stream #0:0: Audio: pcm_s24be, 48000 Hz, stereo, s32 (24 bit), 2304 kb/s"
S32 = "Stream #0:0: Audio: pcm_s32le ([1][0][0][0] / 0x0001), 44100 Hz, stereo, s32, 2822 kb/s"
F32 = "Stream #0:0: Audio: pcm_f32le ([3][0][0][0] / 0x0003), 44100 Hz, stereo, flt, 2822 kb/s"


@pytest.mark.parametrize(
    "line, expected",
    [(S16, 16), (S24, 24), (S24_AIFF, 24), (S32, 32), (F32, 32)],
)
def test_parse_source_bit_depth(line, expected):
    assert parse_source_bit_depth(f"  Duration: 00:00:03.00\n  {line}\n") == expected


def test_parse_source_bit_depth_unknown():
    assert parse_source_bit_depth("") is None
    assert parse_source_bit_depth(None) is None
    assert parse_source_bit_depth("Stream #0:0: Audio: mp3, 44100 Hz, stereo, fltp") is None


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _make_wav(path, codec):
    run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", "sine=f=440:d=3:sample_rate=44100", "-c:a", codec, str(path)],
        capture_output=True,
        check=True,
    )


@needs_ffmpeg
@pytest.mark.parametrize("codec, expected_bits", [("pcm_s16le", 16), ("pcm_s24le", 24)])
def test_aiff_export_keeps_source_bit_depth(tmp_path, codec, expected_bits):
    src = tmp_path / "source.wav"
    out = tmp_path / "out.aiff"
    _make_wav(src, codec)
    assert AudioProcessor().extract_track(src, Track(1, 0.0, 2.0), out, output_format="aiff")
    assert AIFF(out).info.bits_per_sample == expected_bits
