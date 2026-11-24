"""Bit depth handling: AIFF export must not truncate 24-bit sources."""

import json
import shutil

import numpy as np

import pytest
from fastapi.testclient import TestClient
from mutagen.aiff import AIFF
from mutagen.flac import FLAC

from backend import api
from config import Config

from audio_processor import (
    AudioProcessor,
    Track,
    build_export_filters,
    parse_source_bit_depth,
    run_ffmpeg,
    target_bit_depth,
)

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


# --- bit depth setting: "source" (default) or "24", FLAC and AIFF ---------------

def test_target_bit_depth():
    assert target_bit_depth("source", 16) == 16
    assert target_bit_depth("source", 8) == 16
    assert target_bit_depth("source", 24) == 24
    assert target_bit_depth("source", 32) == 24
    assert target_bit_depth("source", None) == 24, "an unknown depth is never truncated"
    assert target_bit_depth("24", 16) == 24


def test_dither_only_when_processing_runs():
    assert build_export_filters(False, "off", dither_to_16=True) is None
    chain = build_export_filters(True, "light", dither_to_16=True)
    assert chain.endswith("aresample=osf=s16:dither_method=triangular")
    assert "dither" not in build_export_filters(True, "light")
    # the one shared chain: rumble, then click removal, then the dither last
    assert build_export_filters(True, "medium", dither_to_16=True) == (
        "aformat=sample_fmts=dblp,highpass=f=15,adeclick=t=1.8:b=3:w=30,"
        "aresample=osf=s16:dither_method=triangular"
    )
    assert build_export_filters(False, "light") == "aformat=sample_fmts=dblp,adeclick=t=2.5:b=3:w=30"
    # rumble alone must also run in double precision, or the dither is a mere repack of s16
    assert build_export_filters(True, "off") == "aformat=sample_fmts=dblp,highpass=f=15"
    with pytest.raises(ValueError):
        build_export_filters(False, "brutal")
    # rumble alone or click removal alone is also processing
    assert "dither" in build_export_filters(False, "strong", dither_to_16=True)
    assert "dither" in build_export_filters(True, "off", dither_to_16=True)


def _write_wav(path, codec, seconds=4):
    run_ffmpeg(
        ["-y", "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=44100:a=0.3:seed=7",
         "-ac", "2", "-c:a", codec, str(path)],
        capture_output=True, check=True,
    )
    return path


def _pcm_md5(path, start=None, duration=None):
    """md5 of the decoded PCM in 32-bit words, so 16- and 24-bit files compare losslessly."""
    args = ["-ss", str(start), "-t", str(duration)] if start is not None else []
    args += ["-i", str(path), "-f", "md5", "-c:a", "pcm_s32le", "-"]
    result = run_ffmpeg(args, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _depth_of(path):
    info = FLAC(str(path)).info if path.suffix == ".flac" else AIFF(str(path)).info
    return info.bits_per_sample


def _export(src, out, fmt, setting="source", **processing):
    ok = AudioProcessor().extract_track(
        src, Track(number=1, start=1.0, end=3.0), out, fmt, bit_depth=setting, **processing
    )
    assert ok
    return out


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["flac", "aiff"])
def test_16_bit_source_stays_16_and_bit_exact(tmp_path, fmt):
    src = _write_wav(tmp_path / "s16.wav", "pcm_s16le")
    out = _export(src, tmp_path / f"o.{fmt}", fmt)
    assert _depth_of(out) == 16
    assert _pcm_md5(out) == _pcm_md5(src, 1.0, 2.0), "unprocessed 16 -> 16 is not dithered"


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["flac", "aiff"])
def test_24_bit_source_stays_24_and_bit_exact(tmp_path, fmt):
    src = _write_wav(tmp_path / "s24.wav", "pcm_s24le")
    out = _export(src, tmp_path / f"o.{fmt}", fmt)
    assert _depth_of(out) == 24
    assert _pcm_md5(out) == _pcm_md5(src, 1.0, 2.0)


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["flac", "aiff"])
def test_processed_16_bit_source_stays_16_and_is_dithered(tmp_path, fmt):
    src = _write_wav(tmp_path / "s16.wav", "pcm_s16le")
    out = _export(src, tmp_path / f"o.{fmt}", fmt, rumble=True)
    assert _depth_of(out) == 16


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["flac", "aiff"])
def test_24_setting_forces_24_bit_from_a_16_bit_source(tmp_path, fmt):
    src = _write_wav(tmp_path / "s16.wav", "pcm_s16le")
    assert _depth_of(_export(src, tmp_path / f"o.{fmt}", fmt, setting="24")) == 24
    assert _depth_of(_export(src, tmp_path / f"p.{fmt}", fmt, setting="24", declick="light")) == 24


@needs_ffmpeg
def test_processing_a_24_bit_flac_keeps_24_bits(tmp_path):
    src = _write_wav(tmp_path / "s24.wav", "pcm_s24le")
    assert _depth_of(_export(src, tmp_path / "o.flac", "flac", rumble=True, declick="medium")) == 24


@needs_ffmpeg
def test_mp3_ignores_the_setting(tmp_path):
    src = _write_wav(tmp_path / "s16.wav", "pcm_s16le")
    out = tmp_path / "o.mp3"
    assert AudioProcessor().extract_track(src, Track(1, 1.0, 3.0), out, "mp3", bit_depth="24", rumble=True)
    assert out.stat().st_size > 1000


# --- the setting through /api/config ---------------------------------------------

client = TestClient(api.app)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    cfg = Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")
    monkeypatch.setattr(api, "config", cfg)
    return cfg


def test_bit_depth_default_persist_validate(tmp_config, tmp_path):
    assert client.get("/api/config").json()["bit_depth"] == "source"
    response = client.put("/api/config", json={"bit_depth": "24"})
    assert response.status_code == 200 and response.json()["bit_depth"] == "24"
    assert json.loads((tmp_path / "settings.json").read_text())["BIT_DEPTH"] == "24"
    reloaded = Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")
    assert reloaded.bit_depth == "24"
    assert client.put("/api/config", json={"bit_depth": "16"}).status_code == 422
    assert client.get("/api/config").json()["bit_depth"] == "24", "a rejected value changes nothing"


def test_invalid_stored_depth_falls_back_to_source(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"BIT_DEPTH": "32"}))
    cfg = Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")
    assert cfg.bit_depth == "source"


def _decode_s32(path, start=None, duration=None):
    args = ["-ss", str(start), "-t", str(duration)] if start is not None else []
    args += ["-v", "error", "-i", str(path), "-f", "s32le", "-ac", "2", "pipe:1"]
    out = run_ffmpeg(args, text=False, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i4")


@needs_ffmpeg
def test_rumble_only_16_bit_export_is_really_dithered(tmp_path):
    """highpass alone would keep the graph in s16, making the dither a plain repack (byte-identical).

    The reference is the export's own chain without the dither (double-precision highpass, plain
    rounding to s16), so this fails if the dither is dropped from the chain or from its assembly.
    """
    src = _write_wav(tmp_path / "s16.wav", "pcm_s16le", seconds=5)
    out = _export(src, tmp_path / "o.flac", "flac", rumble=True)
    undithered = tmp_path / "plain.flac"
    run_ffmpeg(["-y", "-i", str(src), "-af", build_export_filters(True, "off"), "-c:a", "flac",
                "-sample_fmt", "s16", str(undithered)], capture_output=True, check=True)
    # Same filter state from sample 0 (the export reads 1 s of pre-roll), so the interiors line up.
    exported = _decode_s32(out).astype(np.int64)
    reference = _decode_s32(undithered, 1.0, 2.0).astype(np.int64)
    count = min(len(exported), len(reference))
    lsb_diff = np.abs(exported[:count] - reference[:count]) // 65536   # one 16-bit step in s32 words
    assert lsb_diff.any(), "the export equals the undithered chain: no dither was applied"
    assert lsb_diff.max() <= 2, "dither moves a sample by a step or two at most"


@needs_ffmpeg
def test_processed_16_bit_source_at_24_uses_the_low_bits(tmp_path):
    """At 24-bit the processed output must not be a 16-bit signal padded with zeros."""
    src = _write_wav(tmp_path / "s16.wav", "pcm_s16le")
    samples = _decode_s32(_export(src, tmp_path / "o.flac", "flac", setting="24", rumble=True))
    # s32 words hold the 24-bit value in their top bytes, so bits 8-15 are the value's low byte
    assert np.any((samples >> 8) & 0xFF), "the low 8 bits of the 24-bit output are all zero"

