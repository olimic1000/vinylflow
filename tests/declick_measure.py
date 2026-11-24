"""Shared synthetic-signal helpers for measuring adeclick strength.

Music-like bed (tones + slow amplitude envelope) + drum-like transient train
(short decaying noise bursts) + N known 1-3 sample full-scale clicks.
"""
import numpy as np

from audio_processor import run_ffmpeg

SR = 44100
SECONDS = 24
N_CLICKS = 120
DRUM_PERIOD = 0.4   # seconds between drum hits
DRUM_LEN = 0.03     # 30 ms analysis window per hit


def make_signals(seed=7):
    rng = np.random.default_rng(seed)
    n = SR * SECONDS
    t = np.arange(n) / SR
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 0.7 * t)
    music = sum(a * np.sin(2 * np.pi * f * t) for f, a in
                ((110, 0.12), (220, 0.10), (330, 0.08), (440, 0.08), (660, 0.05), (1320, 0.03)))
    music = music * env
    drums = np.zeros(n)
    drum_starts = []
    for k in range(int(SECONDS / DRUM_PERIOD) - 1):
        s = int((0.2 + k * DRUM_PERIOD) * SR)
        m = int(DRUM_LEN * SR)
        k_ = np.arange(m)
        # snare/kick-like: decaying tone body + band-limited noise crack
        noise = rng.standard_normal(m)
        noise = np.convolve(noise, np.ones(4) / 4, mode="same")  # ~lowpass, not white
        body = 0.45 * np.sin(2 * np.pi * 180 * k_ / SR) * np.exp(-k_ / (0.012 * SR))
        crack = 0.35 * noise * np.exp(-k_ / (0.005 * SR))
        drums[s:s + m] += body + crack
        drum_starts.append(s)
    hiss = rng.standard_normal(n) * 0.01  # surface noise: makes faint clicks genuinely hard
    clean = np.clip(music + drums + hiss, -0.9, 0.9).astype(np.float32)

    # click positions: away from drum windows, spaced out
    forbidden = np.zeros(n, bool)
    for s in drum_starts:
        forbidden[max(0, s - 400):s + int(DRUM_LEN * SR) + 400] = True
    positions = []
    while len(positions) < N_CLICKS:
        p = int(rng.integers(SR // 2, n - SR // 2))
        if forbidden[p - 50:p + 50].any() or any(abs(p - q) < 2000 for q in positions):
            continue
        positions.append(p)
    positions.sort()
    amps = []
    noisy = clean.copy()
    for p in positions:
        width = int(rng.integers(1, 4))
        sign = 1.0 if rng.random() < 0.5 else -1.0
        amp = float(np.exp(rng.uniform(np.log(0.03), np.log(0.6))))  # faint ticks to loud pops
        noisy[p:p + width] += sign * amp
        amps.append((width, amp))
    return clean, noisy, positions, drum_starts, amps


def run_chain(sig, chain):
    """Run mono float32 samples through an ffmpeg -af chain; return float32 samples."""
    res = run_ffmpeg(
        ["-hide_banner", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1",
         "-i", "pipe:0", "-af", chain, "-f", "f32le", "-ar", str(SR), "-ac", "1", "pipe:1"],
        text=False, input=sig.astype("<f4").tobytes(), capture_output=True, timeout=300,
    )
    assert res.returncode == 0, res.stderr[-400:]
    out = np.frombuffer(res.stdout, dtype="<f4")
    if len(out) < len(sig):
        out = np.pad(out, (0, len(sig) - len(out)))
    return out[: len(sig)]


def _energy_windows(x, centres, half):
    return sum(float(np.sum(x[max(0, c - half):c + half] ** 2)) for c in centres)


def measure(chain, signals=None):
    """Return (clicks_removed, transient_damage) for an adeclick chain.

    clicks_removed: fraction of the known clicks (amplitudes 0.03-0.6 over a
      hissy music bed) whose largest remaining deviation from the clean signal
      at the click samples is under half the injected amplitude.
    transient_damage: energy of (processed - clean) inside the drum windows when
      the chain runs on the *clean* signal, relative to the clean drum energy.
    """
    clean, noisy, positions, drum_starts, amps = signals or make_signals()
    out_noisy = run_chain(noisy, chain)
    removed = 0
    for p, (width, amp) in zip(positions, amps):
        residual = float(np.max(np.abs(out_noisy[p:p + width] - clean[p:p + width])))
        if residual < 0.5 * amp:
            removed += 1

    out_clean = run_chain(clean, chain)
    half = int(DRUM_LEN * SR)
    centres = [s + half // 2 for s in drum_starts]
    damage = _energy_windows(out_clean - clean, centres, half // 2) / _energy_windows(clean, centres, half // 2)
    return removed / len(positions), damage
