"""MetadataHandler.search_basic and POST /api/search (basic cards, no tracklists).

Discogs is faked; no network."""

from __future__ import annotations

import threading
from collections import OrderedDict

import pytest
from discogs_client.exceptions import HTTPError
from fastapi.testclient import TestClient

import backend.api as api
from metadata_handler import MetadataHandler


class StrictResult:
    """Search hit that only exposes ``.data`` -- anything else would trigger
    a lazy per-release fetch in discogs_client, so it must never be touched."""

    def __init__(self, data):
        object.__setattr__(self, "data", data)

    def __getattr__(self, name):
        raise AssertionError(f"touched .{name} on a search result (lazy fetch)")


def _hit(i, **over):
    data = {
        "id": i,
        "type": "release",
        "title": f"Artist {i} - Album {i}",
        "year": "1999",
        "label": [f"Label {i}", "Other"],
        "format": ["Vinyl", "LP", "Album"],
        "country": "UK",
        "thumb": f"https://img/thumb{i}.jpg",
        "cover_image": f"https://img/cover{i}.jpg",
        "uri": f"/release/{i}-slug",
        "master_id": 500 + i,
        "resource_url": f"https://api.discogs.com/releases/{i}",
    }
    data.update(over)
    return StrictResult(data)


class FakePages:
    """discogs_client's paginated search list: lazy until ``page()``, which is one
    request using the list's ``per_page`` (50 unless the caller sets it); a page
    past the end is a 404, as Discogs answers."""

    def __init__(self, client):
        self.client = client
        self.per_page = 50

    def page(self, index):
        self.client.page_calls.append((index, self.per_page))
        start = (index - 1) * self.per_page
        if index > 1 and start >= len(self.client.hits):
            raise HTTPError("Page not found", 404)
        return self.client.hits[start:start + self.per_page]


class FakeClient:
    def __init__(self, hits=()):
        self.hits = list(hits)
        self.search_calls = []
        self.page_calls = []
        self.release_calls = []

    def search(self, query, **kw):
        self.search_calls.append((query, kw))
        return FakePages(self)

    def release(self, rid):
        self.release_calls.append(rid)
        raise AssertionError("search must not fetch full releases")


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


def test_search_basic_uses_only_data_and_one_request():
    client = FakeClient([_hit(1), _hit(2), _hit(3)])
    out = make(client).search_basic("q", 2)
    assert len(client.search_calls) == 1
    assert client.page_calls == [(1, 2)], "one request, page size set on the list"
    assert client.release_calls == []
    assert [r["id"] for r in out] == [1, 2]
    assert out[0] == {
        "id": 1,
        "artist": "Artist 1",
        "title": "Album 1",
        "year": "1999",
        "label": "Label 1",
        "format": "Vinyl",
        "country": "UK",
        "cover_url": "https://img/cover1.jpg",
        "uri": "/release/1",
        "master_id": 501,
    }


def test_search_basic_fallbacks():
    handler = make(FakeClient([
        _hit(1, title="Just A Title", cover_image="", label=[], format=None, master_id=0),
        _hit(2, title="A - B - C", cover_image=None),
    ]))
    first, second = handler.search_basic("q", 5)
    assert (first["artist"], first["title"]) == ("Unknown Artist", "Just A Title")
    assert first["cover_url"] == "https://img/thumb1.jpg"
    assert first["label"] == "" and first["format"] == "" and first["master_id"] is None
    assert (second["artist"], second["title"]) == ("A", "B - C")
    assert second["cover_url"] == "https://img/thumb2.jpg"


def test_search_page_size_is_not_sent_as_a_search_field():
    # The library appends its own per_page to every page URL, so a per_page search
    # field would send Discogs two page sizes and later pages would start at 51.
    client = FakeClient([_hit(i) for i in range(1, 30)])
    make(client).search_basic("q", 12)
    assert "per_page" not in client.search_calls[0][1]
    assert client.page_calls == [(1, 12)]


def test_search_basic_skips_hits_without_an_id():
    handler = make(FakeClient([_hit(1, id=None), _hit(2)]))
    assert [r["id"] for r in handler.search_basic("q", 5)] == [2]


def test_search_later_page_is_one_request_for_the_next_results():
    client = FakeClient([_hit(i) for i in range(1, 30)])
    out = make(client).search_basic("q", 12, page=2)
    assert [r["id"] for r in out] == list(range(13, 25))
    assert client.page_calls == [(2, 12)]


def test_search_past_the_last_page_is_empty():
    client = FakeClient([_hit(i) for i in range(1, 13)])
    assert make(client).search_basic("q", 12, page=2) == []


def test_search_endpoint_returns_cards_without_tracklists(monkeypatch):
    calls = []

    class Handler:
        def search_basic(self, query, max_results=5, page=1):
            calls.append((query, max_results, page))
            return [{"id": 1, "artist": "A", "title": "T"}]

    monkeypatch.setattr(api, "metadata_handler", Handler())
    with TestClient(api.app) as client:
        response = client.post("/api/search", json={"query": "q", "max_results": 12})
    assert response.status_code == 200
    assert calls == [("q", 12, 1)]
    assert response.json() == {
        "results": [{"id": 1, "artist": "A", "title": "T", "tracks": None}],
        "has_more": False,
    }


def _search_endpoint(monkeypatch, found):
    calls = []

    class Handler:
        def search_basic(self, query, max_results=5, page=1):
            calls.append((query, max_results, page))
            return [{"id": page * 100 + i} for i in range(found)]

    monkeypatch.setattr(api, "metadata_handler", Handler())
    return calls


@pytest.mark.parametrize("page, found, has_more", [
    (1, 12, True), (2, 12, True),
    (api.SEARCH_MAX_PAGES, 12, False),   # the last page we fetch, even when Discogs has more
    (1, 7, False),                       # a short page is Discogs' last
])
def test_search_endpoint_pages(monkeypatch, page, found, has_more):
    calls = _search_endpoint(monkeypatch, found)
    with TestClient(api.app) as client:
        response = client.post("/api/search", json={"query": "q", "max_results": 12, "page": page})
    assert response.status_code == 200
    assert calls == [("q", 12, page)]
    body = response.json()
    assert len(body["results"]) == found and body["has_more"] is has_more


def test_search_endpoint_refuses_pages_beyond_the_limit(monkeypatch):
    calls = _search_endpoint(monkeypatch, 12)
    with TestClient(api.app) as client:
        for page in (0, api.SEARCH_MAX_PAGES + 1):
            response = client.post("/api/search", json={"query": "q", "max_results": 12, "page": page})
            assert response.status_code == 422
    assert calls == [], "never reaches Discogs"
