"""Filter edge effects at cut boundaries: filtered cuts are decoded with pre/post-roll
and trimmed sample-exactly afterwards.  Real ffmpeg, offline."""

import shutil
import wave

import numpy as np
import pytest

from audio_processor import AudioProcessor, padded_window, run_ffmpeg, trim_filter
from track_boundaries import Track

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# --- padded_window (pure) ----------------------------------------------------------

@pytest.mark.parametrize("sr", [44100, 96000])
def test_window_middle_exact_samples(sr):
    seek, dur, t0, t1 = padded_window(10.0, 20.0, 100.0, sr)
    assert seek == pytest.approx(9.0) and dur == pytest.approx(12.0)
    assert t0 == sr and t1 - t0 == round(20.0 * sr) - round(10.0 * sr)


@pytest.mark.parametrize("sr", [44100, 96000])
def test_window_odd_times_keep_exact_length(sr):
    start, end = 12.3456789, 77.7654321
    seek, dur, t0, t1 = padded_window(start, end, 100.0, sr)
    assert t1 - t0 == round(end * sr) - round(start * sr)
    assert round(seek * sr) + t0 == round(start * sr)   # read start is a whole sample


def test_window_at_zero():
    seek, dur, t0, t1 = padded_window(0.0, 5.0, 60.0, 44100)
    assert seek == 0.0 and t0 == 0 and t1 == 5 * 44100 and dur == pytest.approx(6.0)
    seek, dur, t0, t1 = padded_window(0.4, 5.0, 60.0, 44100)
    assert seek == 0.0 and t0 == round(0.4 * 44100)


def test_window_at_eof():
    seek, dur, t0, t1 = padded_window(50.0, 60.0, 60.0, 44100)
    assert seek == pytest.approx(49.0) and dur == pytest.approx(11.0)
    assert t1 - t0 == 10 * 44100 and t1 == round(dur * 44100)   # nothing to trim after


def test_window_short_file():
    seek, dur, t0, t1 = padded_window(0.0, 0.5, 0.5, 48000)
    assert (seek, t0, t1) == (0.0, 0, 24000) and dur == pytest.approx(0.5)


def test_trim_filter_text():
    assert trim_filter(5, 9) == "atrim=start_sample=5:end_sample=9,asetpts=PTS-STARTPTS"


# --- real ffmpeg -------------------------------------------------------------------

SR = 44100


def _write(path, x, sr=SR):
    pcm = (np.clip(x, -1, 1) * (2 ** 15 - 1)).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1]); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


@pytest.fixture(scope="module")
def tones(tmp_path_factory):
    """12 s stereo: tones + hiss, no clicks."""
    rng = np.random.default_rng(3)
    n = SR * 12
    t = np.arange(n) / SR
    base = 0.2 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 1320 * t)
    x = np.stack([base + rng.standard_normal(n) * 0.003,
                  0.8 * base + rng.standard_normal(n) * 0.003], 1)
    p = tmp_path_factory.mktemp("pad") / "tones.wav"
    _write(p, x)
    return p


def _decode(path, ch=2):
    out = run_ffmpeg(["-v", "error", "-i", str(path), "-f", "s32le", "-ac", str(ch), "pipe:1"],
                     text=False, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i4").reshape(-1, ch).astype(np.float64) / 2**31


def _export(src, out, start, end, **kw):
    kw.setdefault("output_format", "flac")
    assert AudioProcessor().extract_track(src, Track(1, start, end), out, **kw)
    return _decode(out)


def _bare_cut_export(src, out, start, end):
    """The previous behaviour: the clean-up filters see the bare cut."""
    chain = AudioProcessor()._build_restoration_filters(1)
    run_ffmpeg(["-y", "-ss", str(start), "-t", str(end - start), "-i", str(src), "-af", chain,
                "-c:a", "flac", str(out)], capture_output=True, check=True)
    return _decode(out)


@needs_ffmpeg
def test_cleanup_edges_match_unfiltered(tones, tmp_path):
    start, end = 3.0, 9.0
    raw = _export(tones, tmp_path / "raw.flac", start, end)
    new = _export(tones, tmp_path / "new.flac", start, end, restoration_level=1)
    old = _bare_cut_export(tones, tmp_path / "old.flac", start, end)
    k = int(0.2 * SR)

    def dev(a, sl):
        return float(np.sqrt(np.mean((a[sl] - raw[sl]) ** 2)))
    head, tail = slice(0, k), slice(-k, None)
    mid = slice(len(raw) // 2, len(raw) // 2 + k)
    floor = dev(new, mid)    # the filters' own steady-state deviation on hiss
    print(f"\nedge RMS deviation vs unfiltered: bare cut start {dev(old, head):.5f} end {dev(old, tail):.5f}; "
          f"padded start {dev(new, head):.5f} end {dev(new, tail):.5f}; mid-track {floor:.5f}")
    assert len(new) == len(raw)
    assert dev(new, head) < 1.5 * floor and dev(new, tail) < 1.5 * floor
    # The bare cut's start is what this fixes: measured 0.038 vs a 0.0063 floor (about 6x)
    # on ffmpeg 6.1 and 7.1, so 4x keeps the check that the test still sees the defect.
    assert dev(old, head) > 4 * floor


_MID = (4.123456, 8.654321)


@needs_ffmpeg
@pytest.mark.parametrize("fmt,span", [
    ("flac", _MID), ("aiff", _MID),
    ("flac", (0.0, 4.0)),    # padding clamped at the file start
    ("flac", (8.0, 12.0)),   # ... and at the end
])
def test_exact_length(tones, tmp_path, fmt, span):
    start, end = span
    got = _export(tones, tmp_path / f"o.{fmt}", start, end, output_format=fmt, restoration_level=1)
    assert len(got) == round(end * SR) - round(start * SR)


@needs_ffmpeg
def test_unfiltered_export_is_the_plain_cut(tones, tmp_path):
    """Without clean-up the cut is untouched, sample for sample."""
    start, end = 3.0, 9.0
    got = _export(tones, tmp_path / "plain.flac", start, end)
    ref = _decode(tones)[round(start * SR):round(end * SR)]
    assert len(got) == len(ref) and np.max(np.abs(got - ref)) == 0
