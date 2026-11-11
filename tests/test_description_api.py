"""``GET /api/release/{id}/description`` and the per-export album-description choice.
Offline: the release cache and the Wikipedia lookup are both fakes."""

import pytest
from fastapi.testclient import TestClient

from album_description import DescriptionResult
from backend import api

client = TestClient(api.app)


class FakeRelease:
    id = 42
    master_id = 7


class FakeDescriptions:
    def __init__(self, result):
        self.result = result
        self.lookups = []

    def lookup(self, release_id, master_id):
        self.lookups.append((release_id, master_id))
        return self.result


class FakeHandler:
    def __init__(self, release, descriptions):
        self.release = release
        self.album_descriptions = descriptions

    def get_release_by_id(self, release_id):
        return self.release


class FakeConfig:
    def __init__(self, enabled):
        self.album_description = enabled


def install(monkeypatch, enabled, result=None, release=FakeRelease()):
    descriptions = FakeDescriptions(result)
    monkeypatch.setattr(api, "metadata_handler", FakeHandler(release, descriptions))
    monkeypatch.setattr(api, "config", FakeConfig(enabled))
    return descriptions


def test_disabled_returns_status_without_lookup(monkeypatch):
    descriptions = install(monkeypatch, False, DescriptionResult("found", "x", "t", "u"))
    assert client.get("/api/release/42/description").json() == {"status": "disabled"}
    assert descriptions.lookups == []


def test_found_none_and_error_shapes(monkeypatch):
    descriptions = install(monkeypatch, True, DescriptionResult("found", "Text", "Title", "https://en.wikipedia.org/wiki/Title"))
    assert client.get("/api/release/42/description").json() == {
        "status": "found", "text": "Text", "title": "Title", "url": "https://en.wikipedia.org/wiki/Title"}
    assert descriptions.lookups == [(42, 7)]
    for status in ("none", "error"):
        descriptions.result = DescriptionResult(status, None, None, None)
        assert client.get("/api/release/42/description").json() == {
            "status": status, "text": None, "title": None, "url": None}


def test_release_without_master_id_looks_up_with_none(monkeypatch):
    class Bare:
        id = 5

    descriptions = install(monkeypatch, True, DescriptionResult("none", None, None, None), release=Bare())
    client.get("/api/release/5/description")
    assert descriptions.lookups == [(5, None)]


def test_unknown_release_is_404(monkeypatch):
    install(monkeypatch, True, DescriptionResult("none", None, None, None), release=None)
    assert client.get("/api/release/42/description").status_code == 404


def process_request(**extra):
    return api.ProcessRequest(file_id="f", release_id=1, track_mapping=[], **extra)


@pytest.mark.parametrize("config_value,request_value,expected", [
    (True, None, True), (False, None, False), (False, True, True), (True, False, False)])
def test_request_resolves_album_description(monkeypatch, config_value, request_value, expected):
    monkeypatch.setattr(api.config, "album_description", config_value)
    assert api._wants_album_description(process_request(album_description=request_value)) is expected
