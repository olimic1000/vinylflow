"""Measure guided placement on real records (not collected by pytest).

Ground truth: every exported FLAC in ``output/<Album>/`` is located inside its
source recording in ``records/`` by envelope cross-correlation (the export
was checked by the user, so its start is the true onset of that song).  A
boundary is *right* when the cut (midpoint between the previous track's end
and the next track's start) lies in the true quiet region between the two
songs +-0.25 s.  Simulated Discogs lengths (jitter, outliers, missing) are
seeded, so runs are comparable.  Any album exported later is picked up
automatically (its first track is matched to a record; unmatched folders are
skipped).

    docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/src:ro" \\
      -v "$PWD/records:/records:ro" -v "$PWD/output:/output:ro" -v /some/scratch:/work \\
      --tmpfs /src/config -w /src vinylflow-test python tests/e2e/real_records_eval.py \\
      [--trials 30] [--set WEAK_PENALTY=0.6 ...] [--grid PRE_ROLL_S=0.2,0.3,0.5 ...]

The onset columns compare each new cut (start of the next track) with the end of the true
quiet region (where the smoothed level leaves the floor); the exported start is shown too but
is not a clean onset (older exports began up to ~1.5 s into the silence).
Envelopes and ground truth are cached in /work (first run decodes each WAV, ~10 s).
``--set`` overrides a ``track_boundaries`` constant; ``--grid`` sweeps one
constant at a time and prints the mean % right per scenario.
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import track_boundaries as tb  # noqa: E402
from audio_processor import AudioProcessor, ENVELOPE_FRAME_S as F  # noqa: E402

# (name, jitter s, outliers, outlier size s, missing)
SCENARIOS = [
    ("exact", 0, 0, 0, 0),
    ("+-3 s", 3, 0, 0, 0),
    ("+-3 s + 2 outliers 25 s", 3, 2, 25, 0),
    ("2 lengths missing", 0, 0, 0, 2),
    ("+-5 s + 1 outlier 40 s + 1 missing", 5, 1, 40, 1),
]
REGION_MARGIN_DB = 12.0
RIGHT_TOLERANCE_S = 0.25
CLIP_TOLERANCE_S = 0.05  # a track starting this far after the true onset counts as clipped


def ncc(signal, template):
    """Normalised cross-correlation of ``template`` against every offset of ``signal`` (FFT)."""
    n, m = len(signal), len(template)
    size = 1 << (n + m).bit_length()
    t = (template - template.mean()) / (template.std() + 1e-12)
    raw = np.fft.irfft(np.fft.rfft(signal, size) * np.conj(np.fft.rfft(t, size)), size)[: n - m + 1]
    c1 = np.concatenate(([0.0], np.cumsum(signal, dtype=np.float64)))
    c2 = np.concatenate(([0.0], np.cumsum(signal.astype(np.float64) ** 2)))
    mean = (c1[m:] - c1[:-m]) / m
    var = np.maximum((c2[m:] - c2[:-m]) / m - mean ** 2, 1e-24)
    return raw / (np.sqrt(var) * m)


def position_key(path):
    m = re.match(r"([A-Za-z]+)(\d+)", path.name)
    return (m.group(1).upper(), int(m.group(2))) if m else (path.name, 0)


def load_envelope(ap, path, work):
    cache = work / (path.stem + ".npy")
    if cache.exists():
        return np.load(cache)
    db = ap.compute_envelope(path)
    np.save(cache, db)
    return db


def collect_cases(records_dir, output_dir, work):
    ap = AudioProcessor()
    envs = {}
    for rec in sorted(records_dir.glob("*.wav")):
        envs[rec.name] = load_envelope(ap, rec, work)
    # Records processed and removed since still count: their cached envelopes are enough.
    for cached in sorted(work.glob("*.npy")):
        envs.setdefault(cached.stem + ".wav", np.load(cached))
    cases = []
    for folder in sorted(p for p in output_dir.iterdir() if p.is_dir()):
        flacs = sorted(folder.glob("*.flac"), key=position_key)
        if len(flacs) < 3:
            continue
        cache = work / f"truth_{re.sub(r'[^A-Za-z0-9]+', '_', folder.name)}.json"
        if cache.exists():
            data = json.loads(cache.read_text())
            cases.append((folder.name, data["record"], data["tracks"]))
            continue
        envelopes = [ap.compute_envelope(f) for f in flacs]
        probe = (10 ** (envelopes[0][:3000] / 20))
        best = (0.0, None)
        for name, db in envs.items():
            if len(db) > len(probe):
                c = ncc(10 ** (db / 20), probe).max()
                best = max(best, (float(c), name), key=lambda x: x[0])
        if best[0] < 0.9:
            print(f"skip {folder.name}: no matching record (best corr {best[0]:.2f})")
            continue
        lin = 10 ** (envs[best[1]] / 20)
        tracks, pos = [], 0
        for f, tdb in zip(flacs, envelopes):
            tl = 10 ** (tdb / 20)
            lo, hi = max(0, pos - 3000), min(len(lin), pos + 60000 + len(tl))
            c = ncc(lin[lo:hi], tl[:3000])
            k = int(c.argmax())
            start = (lo + k) * F
            tracks.append({"name": f.name, "start": start, "end": start + len(tdb) * F, "corr": float(c[k])})
            pos = int(tracks[-1]["end"] / F)
        cache.write_text(json.dumps({"record": best[1], "tracks": tracks}))
        cases.append((folder.name, best[1], tracks))
    return cases, envs


def true_regions(sm, tracks):
    """Quiet stretch (smoothed level near the floor) between each pair of exported songs."""
    floor = np.percentile(sm, 3)
    out = []
    for k in range(1, len(tracks)):
        a, b = int(tracks[k - 1]["end"] / F), int(tracks[k]["start"] / F)
        while a > 0 and sm[a - 1] < floor + REGION_MARGIN_DB:
            a -= 1
        while b < len(sm) - 1 and sm[b] < floor + REGION_MARGIN_DB:
            b += 1
        out.append((a * F, b * F))
    return out


def run_scenario(case, scen, trials, seed):
    name, jit, nout, osz, nmiss = scen
    rnd = random.Random(seed)
    db, sm, ms, me, tracks, regions, sb = case
    n = len(tracks)
    lengths = [t["end"] - t["start"] for t in tracks]
    gaps = tb.gaps_from_envelope(db, F, -30.0, 0.3)
    acc = {"old": [], "new": []}
    onset = []
    for _ in range(trials):
        d = [round(x + rnd.uniform(-jit, jit)) for x in lengths]
        for o in rnd.sample(range(n), nout):
            d[o] += rnd.choice([-1, 1]) * osz
        known = [float(x) for x in d]
        for o in rnd.sample(range(n), nmiss):
            known[o] = None
        if nmiss == 0:
            g = tb.boundaries_from_expected_durations(gaps, ms, me, known, side_breaks=sb)
            cuts = [(g.tracks[k].end + g.tracks[k + 1].start) / 2 for k in range(n - 1)]
            acc["old"].append([r[0] - RIGHT_TOLERANCE_S <= c <= r[1] + RIGHT_TOLERANCE_S for c, r in zip(cuts, regions)])
        g = tb.guided_by_alignment(db, F, ms, me, known, side_breaks=sb, sm=sm)
        if g is None:
            acc["new"].append([False] * (n - 1))
            continue
        cuts = [(g.tracks[k].end + g.tracks[k + 1].start) / 2 for k in range(n - 1)]
        right = [r[0] - RIGHT_TOLERANCE_S <= c <= r[1] + RIGHT_TOLERANCE_S for c, r in zip(cuts, regions)]
        acc["new"].append(right)
        onset += [(g.tracks[k + 1].start - regions[k][1], g.tracks[k + 1].start - tracks[k + 1]["start"])
                  for k in range(n - 1) if right[k]]
    return acc, onset


def pct(rows):
    return 100.0 * np.mean(rows) if rows else float("nan")


def build_cases(cases, envs):
    built = []
    for name, rec, tracks in cases:
        db = envs[rec]
        sm = tb.smooth_envelope(db, F)
        ms, me = tb.music_span(db, F)
        sb = [i for i in range(1, len(tracks)) if tracks[i]["name"][0].upper() != tracks[i - 1]["name"][0].upper()]
        built.append((name, (db, sm, ms, me, tracks, true_regions(sm, tracks), sb)))
    return built


def apply(overrides):
    for key, val in overrides.items():
        if not hasattr(tb, key):
            raise SystemExit(f"unknown constant {key}")
        setattr(tb, key, float(val))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default="/records")
    ap.add_argument("--output", default="/output")
    ap.add_argument("--work", default="/work")
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--set", nargs="*", default=[], metavar="NAME=VALUE")
    ap.add_argument("--grid", nargs="*", default=[], metavar="NAME=V1,V2,..")
    args = ap.parse_args()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    apply(dict(s.split("=") for s in args.set))
    cases, envs = collect_cases(Path(args.records), Path(args.output), work)
    if not cases:
        raise SystemExit("no albums with matching records")

    def evaluate():
        built = build_cases(cases, envs)
        res = {}
        for name, case in built:
            for scen in SCENARIOS:
                res[(name, scen[0])] = run_scenario(case, scen, args.trials, args.seed)
        return res

    if args.grid:
        for spec in args.grid:
            key, vals = spec.split("=")
            base = getattr(tb, key)
            print(f"\n{key} (default {base})")
            for v in vals.split(","):
                setattr(tb, key, float(v))
                res = evaluate()
                line = " | ".join(
                    f"{s[0][:14]} {np.mean([pct(res[(a, s[0])][0]['new']) for a, *_ in cases]):5.1f}%" for s in SCENARIOS
                )
                print(f"  {v:>6}: {line}")
            setattr(tb, key, base)
        return

    res = evaluate()
    print(f"\n{args.trials} trials per scenario, seed {args.seed}; right = cut inside the true quiet region +-{RIGHT_TOLERANCE_S}s")
    print(f"{'album':16} {'scenario':36} {'old':>7} {'new':>7}   new cut vs end of the true quiet region (right ones): median signed / max abs")
    for name, *_ in cases:
        for scen in SCENARIOS:
            acc, onset = res[(name, scen[0])]
            old = f"{pct(acc['old']):6.1f}%" if acc["old"] else "   n/a "
            dist = (f"{np.median([o[0] for o in onset]):+.2f} s / {max(abs(o[0]) for o in onset):.2f} s"
                    f"   (vs export start {np.median([o[1] for o in onset]):+.2f} s)") if onset else "-"
            # A track starting after the true onset loses music; "inside the gap" alone can't see that.
            clipped = [o[0] for o in onset if o[0] > CLIP_TOLERANCE_S]
            clip = f"   clipped {100 * len(clipped) / len(onset):4.1f}% (max {max(clipped, default=0):.2f} s)" if onset else ""
            print(f"{name[:16]:16} {scen[0]:36} {old} {pct(acc['new']):6.1f}%   {dist}{clip}")
    for name, rec, tracks in cases:
        print(f"\n{name} <- {rec}: {len(tracks)} tracks, min corr {min(t['corr'] for t in tracks):.3f}")


if __name__ == "__main__":
    main()
