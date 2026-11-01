"""Discogs release cache, GET /api/release/{id} and the shared release serializer.

Discogs is faked: ``FakeClient`` records every request, so the tests can count
how often Discogs would have been called.  No network."""

import threading
from collections import OrderedDict

import pytest
from fastapi.testclient import TestClient

import backend.api as api
from backend.api import _serialize_release, app
from metadata_handler import DiscogsRelease, MetadataHandler


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _raw_release(rid):
    return _Obj(
        id=rid,
        title=f"Album {rid}",
        year=2001,
        artists=[_Obj(name="Someone")],
        labels=[_Obj(name="Lab")],
        formats=[{"name": "Vinyl"}],
        images=[{"uri": "https://img/full.jpg"}],
        tracklist=[
            _Obj(position="A1", title="One", duration="3:00"),
            _Obj(position="B1", title="Two", duration=""),
        ],
    )


class FakeClient:
    """Stands in for discogs_client.Client: search hits expose only ``.id``;
    ``release`` raises for unknown ids, as a 404 from Discogs would."""

    def __init__(self, releases=None, hits=()):
        self.releases = releases or {}
        self.hits = [_Obj(id=i) for i in hits]
        self.release_calls = []

    def search(self, query, **kw):
        return list(self.hits)

    def release(self, rid):
        self.release_calls.append(rid)
        if rid not in self.releases:
            raise LookupError(f"release {rid} not found")
        return self.releases[rid]


def make(client):
    """A MetadataHandler without a real discogs_client.Client or rate-limit sleeps."""
    handler = MetadataHandler.__new__(MetadataHandler)
    handler.client = client
    handler.min_request_interval = 0
    handler.last_request_time = 0
    handler._rate_limit_lock = threading.Lock()
    handler._release_cache = OrderedDict()
    handler._release_cache_lock = threading.Lock()
    return handler


def test_release_cache_hit_and_bound():
    client = FakeClient(releases={i: _raw_release(i) for i in range(1, 200)})
    handler = make(client)
    first = handler.get_release_by_id(1)
    assert handler.get_release_by_id(1) is first
    assert client.release_calls == [1]

    for i in range(2, 2 + MetadataHandler.RELEASE_CACHE_SIZE + 5):
        handler.get_release_by_id(i)
    assert len(handler._release_cache) == MetadataHandler.RELEASE_CACHE_SIZE
    assert 1 not in handler._release_cache  # oldest evicted
    handler.get_release_by_id(1)
    assert client.release_calls.count(1) == 2


def test_failed_fetch_not_cached():
    handler = make(FakeClient())
    assert handler.get_release_by_id(9) is None
    assert len(handler._release_cache) == 0


def test_search_fills_the_cache_so_export_does_not_refetch():
    client = FakeClient(releases={1: _raw_release(1), 2: _raw_release(2)}, hits=[1, 2])
    handler = make(client)
    found = handler.search_releases("q", max_results=5)
    assert [release.id for _, release in found] == [1, 2]
    handler.get_release_by_id(2)   # what the export pipeline does
    assert client.release_calls == [1, 2]


def test_search_skips_a_release_that_fails_to_load():
    client = FakeClient(releases={2: _raw_release(2)}, hits=[1, 2])
    found = make(client).search_releases("q", max_results=5)
    assert [(index, release.id) for index, release in found] == [(2, 2)]


def test_reinitialize_clears_the_cache():
    handler = MetadataHandler("token", "VinylFlow-tests")
    handler._release_cache[1] = object()
    handler.reinitialize("other", "VinylFlow-tests")
    assert len(handler._release_cache) == 0


def test_serialize_release_shape():
    release = DiscogsRelease(_raw_release(7))
    assert _serialize_release(release) == {
        "id": 7,
        "artist": "Someone",
        "title": "Album 7",
        "year": 2001,
        "label": "Lab",
        "format": "Vinyl",
        "cover_url": "https://img/full.jpg",
        "uri": "/release/7",
        "tracks": [
            {"position": "A1", "title": "One", "duration": "3:00"},
            {"position": "B1", "title": "Two", "duration": ""},
        ],
    }


@pytest.mark.parametrize("known, status", [(True, 200), (False, 404)])
def test_release_endpoint(monkeypatch, known, status):
    client = FakeClient(releases={7: _raw_release(7)} if known else {})
    monkeypatch.setattr(api, "metadata_handler", make(client))
    response = TestClient(app).get("/api/release/7")
    assert response.status_code == status
    if known:
        assert response.json()["tracks"][0] == {"position": "A1", "title": "One", "duration": "3:00"}
