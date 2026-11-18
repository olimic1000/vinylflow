"""The support note after an export: shown until turned off, and the choice is saved."""

import json

from fastapi.testclient import TestClient

from backend import api
from config import Config


def load(settings_path):
    return Config(env_path=settings_path.parent / "no.env", settings_path=settings_path)


def test_shown_by_default(tmp_path):
    assert load(tmp_path / "settings.json").hide_support_ask is False


def test_choice_survives_a_restart(tmp_path):
    path = tmp_path / "settings.json"
    assert load(path).save_hide_support_ask(True)
    assert json.loads(path.read_text())["HIDE_SUPPORT_ASK"] is True
    assert load(path).hide_support_ask is True


def test_config_endpoint_reads_and_saves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "config", load(tmp_path / "settings.json"))
    client = TestClient(api.app)
    assert client.get("/api/config").json()["hide_support_ask"] is False
    reply = client.put("/api/config", json={"hide_support_ask": True})
    assert reply.json()["hide_support_ask"] is True
    assert load(tmp_path / "settings.json").hide_support_ask is True
