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
    (True, None, True), (False, None, False), (False, True, False), (True, False, False), (True, True, True)])
def test_request_may_only_turn_the_saved_setting_off(monkeypatch, config_value, request_value, expected):
    monkeypatch.setattr(api.config, "album_description", config_value)
    assert api._wants_album_description(process_request(album_description=request_value)) is expected


# --- the master id of a real DiscogsRelease reaches the lookup ---------------------------------

def real_release(master_id="unset"):
    """A real ``DiscogsRelease`` over real discogs_client objects (no network: ``refresh`` fails
    the test).  ``master_id`` "unset" keeps the fixture's own master id; None removes it."""
    import json
    from pathlib import Path

    import discogs_client
    from metadata_handler import DiscogsRelease

    data = json.loads((Path(__file__).parent / "fixtures" / "discogs_release_848848.json").read_text())
    if master_id is None:
        del data["master_id"]
    elif master_id != "unset":
        data["master_id"] = master_id
    raw = discogs_client.Release(discogs_client.Client("VinylFlow-tests"), data)

    def no_fetch(*_):
        raise AssertionError("must not fetch")
    raw.refresh = no_fetch
    return DiscogsRelease(raw)


def test_discogs_release_carries_the_master_id_of_its_payload():
    assert real_release(master_id=30521).master_id == 30521
    assert real_release(master_id=None).master_id is None


def test_description_endpoint_searches_wikidata_by_the_master_id_of_a_real_release(monkeypatch):
    from album_description import AlbumDescriptions
    from metadata_handler import MetadataHandler

    calls = []

    def recording_get(url, params=None, headers=None, timeout=None):
        calls.append(params)

        class Reply:
            status_code = 200

            @staticmethod
            def json():
                return {"query": {"search": []}}
        return Reply()

    handler = MetadataHandler("token", "VinylFlow-tests")
    release = real_release(master_id=30521)
    handler._release_cache[release.id] = release
    handler.album_descriptions = AlbumDescriptions(http_get=recording_get)
    monkeypatch.setattr(api, "metadata_handler", handler)
    monkeypatch.setattr(api, "config", FakeConfig(True))

    assert client.get(f"/api/release/{release.id}/description").json()["status"] == "none"
    assert calls[0]["srsearch"] == "haswbstatement:P1954=30521"


def test_a_process_request_cannot_switch_the_lookup_on_for_the_pipeline(monkeypatch):
    import asyncio
    from backend.sessions import OutputSpec  # noqa: F401  (spec type captured below)

    class StubSession:
        last_run = None

    captured = []
    monkeypatch.setattr(api.session_store, "get", lambda file_id: StubSession())
    monkeypatch.setattr(api.pipeline, "run", lambda session, spec, *rest: captured.append(spec))
    monkeypatch.setattr(api.config, "album_description", False)
    asyncio.run(api.process_file_background(process_request(album_description=True)))
    assert [spec.album_description for spec in captured] == [False]
