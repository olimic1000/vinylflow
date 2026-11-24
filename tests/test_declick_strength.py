"""Measured strength of the click & pop removal presets.

Real ffmpeg on a synthetic signal (music-like tones + surface hiss + decaying
drum-like transients) with 120 injected 1-3 sample clicks at known positions.
For each preset we measure the fraction of clicks removed and the damage done
to the drum transients (energy of processed-minus-clean inside the drum windows,
relative to the drum energy).  Assertions are deliberately loose; run with
``pytest -s tests/test_declick_strength.py`` to see the table.
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from audio_processor import DECLICK_PRESETS  # noqa: E402
from declick_measure import make_signals, measure  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TRANSIENT_DAMAGE_BOUND = 0.15   # energy ratio; i.e. error stays >= ~8 dB below the hit


@pytest.fixture(scope="module")
def table():
    sig = make_signals()
    rows = {name: measure(chain, sig) for name, chain in DECLICK_PRESETS.items()}
    print("\npreset   chain                       clicks_removed  transient_damage")
    for name, (removed, damage) in rows.items():
        print(f"{name:8s} {DECLICK_PRESETS[name]:27s} {removed:14.3f}  {damage:16.4f}")
    return rows


OLD_LIGHT_DAMAGE = 0.073    # the original Light (t=3:b=3) -- see LEGACY_LIGHT note
OLD_LIGHT_REMOVED = 0.858


def test_click_removal_increases_light_to_strong(table):
    light, medium, strong = (table[k][0] for k in ("light", "medium", "strong"))
    assert light + 0.03 < medium, "light must stay clearly gentler than medium"
    assert strong >= medium - 0.005, "strong should not remove fewer clicks than medium"
    assert strong > light + 0.05


def test_retuned_light_beats_old_light(table):
    removed, damage = table["light"]
    assert damage < OLD_LIGHT_DAMAGE, f"light damage {damage:.3f} not below old {OLD_LIGHT_DAMAGE}"
    assert removed >= OLD_LIGHT_REMOVED - 0.01, f"light removes {removed:.3f}, below old light"


def test_every_preset_removes_most_clicks(table):
    assert all(removed >= 0.75 for removed, _ in table.values())


def test_transient_damage_stays_bounded(table):
    for name, (_, damage) in table.items():
        assert damage < TRANSIENT_DAMAGE_BOUND, f"{name}: transient damage {damage:.3f}"
