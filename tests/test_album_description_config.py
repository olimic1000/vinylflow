"""Persistence of the album_description setting via /api/config (off by default)."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import api
from config import Config

client = TestClient(api.app)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ALBUM_DESCRIPTION", raising=False)
    cfg = Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")
    monkeypatch.setattr(api, "config", cfg)
    return cfg


def test_album_description_defaults_off_and_persists(tmp_config, tmp_path):
    assert tmp_config.album_description is False
    assert client.get("/api/config").json()["album_description"] is False
    assert client.put("/api/config", json={"album_description": True}).json()["album_description"] is True
    assert json.loads((tmp_path / "settings.json").read_text())["ALBUM_DESCRIPTION"] is True
    assert client.get("/api/config").json()["album_description"] is True
    reloaded = Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")
    assert reloaded.album_description is True
    # Turning it back off must persist too (False is not "absent").
    assert client.put("/api/config", json={"album_description": False}).json()["album_description"] is False
    assert json.loads((tmp_path / "settings.json").read_text())["ALBUM_DESCRIPTION"] is False


def test_album_description_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ALBUM_DESCRIPTION", "true")
    cfg = Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json")
    assert cfg.album_description is True


def test_unrelated_update_leaves_album_description_alone(tmp_config):
    client.put("/api/config", json={"album_description": True})
    client.put("/api/config", json={"min_track_length": 20})
    assert tmp_config.album_description is True


@pytest.mark.parametrize("value, expected", [
    ("false", False), ("0", False), ("no", False), ("", False),
    ("yes", True), ("1", True), (" ON ", True), (True, True), (False, False),
])
def test_album_description_string_values_from_settings_json(tmp_path, monkeypatch, value, expected):
    """settings.json written by hand (or an env value) may hold a string; only true-ish strings enable it."""
    monkeypatch.delenv("ALBUM_DESCRIPTION", raising=False)
    (tmp_path / "settings.json").write_text(json.dumps({"ALBUM_DESCRIPTION": value}))
    assert Config(env_path=tmp_path / ".env", settings_path=tmp_path / "settings.json").album_description is expected
