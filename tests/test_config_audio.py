"""Rumble filter / click removal settings: defaults, persistence, validation, the
``restoration_level`` an old open page still sends, and how a process request becomes an OutputSpec."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from audio_processor import DECLICK_LEVELS, DECLICK_PRESETS, LEGACY_RESTORE
from backend import api
from backend.sessions import SessionStore
from config import Config

client = TestClient(api.app)


def _config(tmp_path):
    return Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.setattr(api, "config", cfg)
    return cfg


def test_levels_and_presets_agree():
    assert DECLICK_LEVELS == ("off", "light", "medium", "strong")
    assert set(DECLICK_PRESETS) == {"light", "medium", "strong"}
    assert LEGACY_RESTORE == (True, "light")


def test_defaults_are_off(tmp_config):
    body = client.get("/api/config").json()
    assert (body["rumble_filter"], body["declick"]) == (False, "off")
    assert "restoration_level" not in body


def test_put_persists_and_reloads(tmp_config, tmp_path):
    body = client.put("/api/config", json={"rumble_filter": True, "declick": "strong"}).json()
    assert body["rumble_filter"] is True and body["declick"] == "strong"
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["RUMBLE_FILTER"] is True and saved["DECLICK"] == "strong"
    reloaded = _config(tmp_path)
    assert reloaded.rumble_filter is True and reloaded.declick == "strong"
    # Turning things off must persist too (False / "off" are values, not "absent").
    client.put("/api/config", json={"rumble_filter": False, "declick": "off"})
    reloaded = _config(tmp_path)
    assert reloaded.rumble_filter is False and reloaded.declick == "off"


def test_one_field_leaves_the_other_alone(tmp_config):
    client.put("/api/config", json={"rumble_filter": True})
    body = client.put("/api/config", json={"declick": "medium"}).json()
    assert body["rumble_filter"] is True and body["declick"] == "medium"


@pytest.mark.parametrize("payload", [{"declick": "brutal"}, {"declick": 3}, {"rumble_filter": "maybe"}])
def test_invalid_values_are_422_and_change_nothing(tmp_config, payload):
    assert client.put("/api/config", json=payload).status_code == 422
    body = client.get("/api/config").json()
    assert (body["rumble_filter"], body["declick"]) == (False, "off")


# --- process request -> OutputSpec ------------------------------------------------

def _spec_for(tmp_path, monkeypatch, **fields):
    """Run the request through process_file_background with the pipeline stubbed; return its OutputSpec."""
    store = SessionStore()
    monkeypatch.setattr(api, "session_store", store)
    source = tmp_path / "recording.wav"
    source.write_bytes(b"fake audio; the pipeline is stubbed")
    store.create("job", source, source.name, 120, source.stat().st_size)
    specs = []
    monkeypatch.setattr(api.pipeline, "run", lambda session, spec, *args: specs.append(spec))
    request = api.ProcessRequest(file_id="job", release_id=7, track_mapping=[], **fields)
    asyncio.run(api.process_file_background(request))
    return specs[0]


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, (False, "off")),
        ({"restoration_level": 1}, (True, "light")),        # old clients
        ({"restoration_level": 0}, (False, "off")),
        ({"rumble_filter": True}, (True, "off")),
        ({"declick": "medium"}, (False, "medium")),
        ({"rumble_filter": True, "declick": "strong"}, (True, "strong")),
        ({"rumble_filter": False, "declick": "off", "restoration_level": 1}, (False, "off")),  # new fields win
        # one new field sent: the other half still follows the old level
        ({"restoration_level": 1, "rumble_filter": False}, (False, "light")),
        ({"restoration_level": 1, "declick": "off"}, (True, "off")),
        ({"restoration_level": 1, "declick": "strong"}, (True, "strong")),
        ({"restoration_level": 0, "rumble_filter": True}, (True, "off")),
    ],
)
def test_request_to_spec(tmp_config, tmp_path, monkeypatch, fields, expected):
    spec = _spec_for(tmp_path, monkeypatch, **fields)
    assert (spec.rumble_filter, spec.declick) == expected


def test_spec_carries_the_saved_bit_depth(tmp_config, tmp_path, monkeypatch):
    tmp_config.bit_depth = "24"
    assert _spec_for(tmp_path, monkeypatch).bit_depth == "24"


def test_old_page_request_is_accepted(tmp_config, tmp_path, monkeypatch):
    """A page opened before the upgrade still sends restoration_level and hum_freq."""
    request = api.ProcessRequest(
        file_id="job", release_id=7, track_mapping=[], restoration_level=1, hum_freq=50
    )
    assert api._resolve_cleanup(request) == (True, "light")
    spec = _spec_for(tmp_path, monkeypatch, restoration_level=1, hum_freq=50)
    assert (spec.rumble_filter, spec.declick) == (True, "light")


def test_request_rejects_unknown_strength(tmp_config):
    response = client.post("/api/process", json={
        "file_id": "x", "release_id": 1, "track_mapping": [], "declick": "brutal"})
    assert response.status_code == 422

