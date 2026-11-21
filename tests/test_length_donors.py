"""MetadataHandler.find_length_donors and GET /api/release/{id}/length-donors (fakes only)."""

from __future__ import annotations

import threading
from collections import OrderedDict

import pytest
from fastapi.testclient import TestClient

from metadata_handler import MetadataHandler, ReleaseNotFound


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class StrictVersion:
    """Master version hit: only ``.data`` may be touched."""

    def __init__(self, data):
        object.__setattr__(self, "data", data)

    def __getattr__(self, name):
        raise AssertionError(f"touched .{name} on a version (lazy fetch)")


class FakeVersions:
    def __init__(self, items, log):
        self.items = items
        self.per_page = 50
        self.log = log

    @property
    def pages(self):
        self.log.append("versions.pages")
        return 1

    def page(self, n):
        return [StrictVersion(d) for d in self.items]


class FakeMaster:
    def __init__(self, mid, items, log):
        self.data = {"id": mid, "resource_url": f"https://x/masters/{mid}"}
        self.versions = FakeVersions(items, log)


def raw_release(rid, tracks, master_id=None, fmt="Vinyl", year=2000):
    return _Obj(
        id=rid,
        title=f"Album {rid}",
        year=year,
        data={"master_id": master_id} if master_id else {},
        artists=[_Obj(name="Someone")],
        labels=[_Obj(name=f"Lab{rid}")],
        formats=[{"name": fmt}],
        images=[],
        tracklist=[
            _Obj(position=f"A{i + 1}", title=t, duration=d) for i, (t, d) in enumerate(tracks)
        ],
    )


class FakeClient:
    def __init__(self, releases, versions):
        self.releases = releases
        self.versions = versions
        self.release_calls = []
        self.log = []

    def release(self, rid):
        self.release_calls.append(rid)
        return self.releases[rid]

    def master(self, mid):
        return FakeMaster(mid, self.versions, self.log)


def ver(rid, major="Vinyl", year="2001", label="L", country="UK", fmt=None):
    return {
        "id": rid,
        "label": label,
        "country": country,
        "released": year,
        "format": fmt or f"{major}, LP",
        "major_formats": [major],
    }


FULL = [("One", "3:00"), ("Two", "4:00"), ("Three", "5:00")]
NONE = [("One", ""), ("Two", ""), ("Three", "")]


def make(client):
    h = MetadataHandler.__new__(MetadataHandler)
    h.client = client
    h.min_request_interval = 0
    h.last_request_time = 0
    h._rate_limit_lock = threading.Lock()
    h._release_cache = OrderedDict()
    h._release_cache_lock = threading.Lock()
    h._donor_cache = {}
    h._donor_cache_lock = threading.Lock()
    h._donor_inflight = {}
    h.rate_calls = 0
    real = h._rate_limit

    def spy():
        h.rate_calls += 1
        real()

    h._rate_limit = spy
    return h


def scenario(extra_versions=(), extra_releases=None):
    releases = {1: raw_release(1, NONE, master_id=77)}
    versions = [ver(1)]
    releases.update(extra_releases or {})
    versions.extend(extra_versions)
    return FakeClient(releases, versions)


def test_no_master_returns_empty_without_version_fetch():
    c = FakeClient({1: raw_release(1, NONE)}, [])
    h = make(c)
    assert h.find_length_donors(1) == []
    assert c.log == []


def test_basic_donor_skips_self_and_formats_result():
    c = scenario(
        [ver(2, year="2005", label="Reissue", country="US")],
        {2: raw_release(2, FULL + [("Bonus", "9:00")])},
    )
    h = make(c)
    out = h.find_length_donors(1)
    assert c.release_calls == [1, 2]  # 1 once (target), never as its own donor
    assert out == [
        {
            "release_id": 2,
            "label": "Reissue",
            "year": 2005,
            "country": "US",
            "format": "Vinyl, LP",
            "matched": 3,
            "missing": 3,
            "lengths": {"0": "3:00", "1": "4:00", "2": "5:00"},
        }
    ]


def test_same_format_first_and_sorting_by_coverage():
    partial = [("One", "3:00"), ("Two", ""), ("Three", "")]
    c = scenario(
        [ver(2, major="CD"), ver(3, major="Vinyl"), ver(4, major="CD")],
        {2: raw_release(2, FULL), 3: raw_release(3, FULL), 4: raw_release(4, partial)},
    )
    h = make(c)
    out = h.find_length_donors(1, want=10)
    assert c.release_calls[:2] == [1, 3]  # vinyl candidate fetched before the CDs
    assert [d["release_id"] for d in out] == [3, 2, 4]
    assert out[-1]["matched"] == 1


def test_budget_respected_and_rate_limited_per_fetch():
    rels = {i: raw_release(i, NONE) for i in range(2, 12)}
    c = scenario([ver(i) for i in range(2, 12)], rels)
    h = make(c)
    progress = []
    out = h.find_length_donors(1, max_fetches=4, on_progress=lambda *a: progress.append(a))
    assert out == []
    donor_fetches = [r for r in c.release_calls if r != 1]
    assert len(donor_fetches) == 4
    # 1 target fetch + 4 donor fetches + 1 versions page
    assert h.rate_calls == 1 + 4 + 1
    assert progress[0] == (0, 4, 0) and progress[-1][0] == 4


def test_stops_early_after_want_full_donors():
    rels = {i: raw_release(i, FULL) for i in range(2, 10)}
    c = scenario([ver(i) for i in range(2, 10)], rels)
    h = make(c)
    out = h.find_length_donors(1, want=3)
    assert len(out) == 3
    assert len([r for r in c.release_calls if r != 1]) == 3


def test_cache_means_no_new_fetches_second_time():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    h = make(c)
    first = h.find_length_donors(1)
    calls, rates, log = list(c.release_calls), h.rate_calls, list(c.log)
    again = h.find_length_donors(1)
    assert again == first
    assert (c.release_calls, h.rate_calls, c.log) == (calls, rates, log)


def test_donors_without_lengths_are_dropped():
    c = scenario([ver(2)], {2: raw_release(2, NONE)})
    assert make(c).find_length_donors(1) == []


class FakeHTTPError(Exception):
    """Like discogs_client's HTTPError: carries the response status."""

    def __init__(self, status_code):
        super().__init__(f"{status_code} error")
        self.status_code = status_code


def test_missing_release_raises_release_not_found():
    h = make(FakeClient({}, []))
    h.client.release = lambda rid: (_ for _ in ()).throw(FakeHTTPError(404))
    with pytest.raises(ReleaseNotFound):
        h.find_length_donors(5)


def test_discogs_error_on_target_is_not_reported_as_not_found():
    # Changed from "any failure -> LookupError": a Discogs outage must not read as "no such release".
    h = make(FakeClient({}, []))
    h.client.release = lambda rid: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(Exception) as caught:
        h.find_length_donors(5)
    assert not isinstance(caught.value, ReleaseNotFound)


def failing_client(client, failing_ids):
    """Make `client.release` fail (like a network error) for the given ids."""
    real = client.release

    def release(rid):
        if rid in failing_ids:
            client.release_calls.append(rid)
            raise ConnectionError("discogs down")
        return real(rid)

    client.release = release
    return client


def test_failed_donor_fetch_is_not_cached_as_no_donors():
    c = failing_client(scenario([ver(2), ver(3)], {2: raw_release(2, FULL), 3: raw_release(3, FULL)}), {2})
    h = make(c)
    first = h.find_length_donors(1)
    assert [d["release_id"] for d in first] == [3]
    assert h._donor_cache == {}  # incomplete lookup: nothing remembered
    c.release = FakeClient(c.releases, c.versions).release  # Discogs is back
    h.find_length_donors(1, want=10)
    assert 2 in c.release_calls and c.release_calls.count(2) == 1
    assert {d["release_id"] for d in h._donor_cache[1]} == {2, 3}


def test_all_donor_fetches_failing_raises_and_caches_nothing():
    c = failing_client(scenario([ver(2), ver(3)], {}), {2, 3})
    h = make(c)
    with pytest.raises(Exception) as caught:
        h.find_length_donors(1)
    assert not isinstance(caught.value, ReleaseNotFound)
    assert h._donor_cache == {}


def test_concurrent_lookups_for_one_release_share_one_run():
    gate = threading.Event()
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    real_master = c.master

    def slow_master(mid):
        gate.wait(5)
        return real_master(mid)

    c.master = slow_master
    h = make(c)
    results = []
    threads = [threading.Thread(target=lambda: results.append(h.find_length_donors(1))) for _ in range(2)]
    threads[0].start()
    import time
    time.sleep(0.2)  # the first caller is now inside the version listing
    threads[1].start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(results) == 2 and results[0] == results[1] != []
    assert c.log.count("versions.pages") == 1
    assert c.release_calls.count(2) == 1


def test_second_caller_runs_its_own_lookup_when_the_first_failed():
    c = failing_client(scenario([ver(2)], {2: raw_release(2, FULL)}), {2})
    h = make(c)
    with pytest.raises(Exception):
        h.find_length_donors(1)
    assert h._donor_inflight == {}  # the guard is released on failure
    c.release = FakeClient(c.releases, c.versions).release
    assert [d["release_id"] for d in h.find_length_donors(1)] == [2]


def test_donor_that_fills_nothing_is_dropped_and_ranking_counts_fills():
    # Release 1 already knows One's length; only Two and Three are missing.
    releases = {1: raw_release(1, [("One", "3:00"), ("Two", ""), ("Three", "")], master_id=77)}
    only_known = [("One", "3:10"), ("Two", ""), ("Three", "")]  # fills nothing
    one_fill = [("One", "3:10"), ("Two", "4:00"), ("Three", "")]
    two_fills = [("One", "3:10"), ("Two", "4:00"), ("Three", "5:00")]
    releases.update({2: raw_release(2, only_known), 3: raw_release(3, one_fill), 4: raw_release(4, two_fills)})
    c = FakeClient(releases, [ver(1), ver(2), ver(3), ver(4)])
    out = make(c).find_length_donors(1, want=10)
    assert [d["release_id"] for d in out] == [4, 3]
    assert out[0]["lengths"] == {"1": "4:00", "2": "5:00"}  # no length for the track that already has one
    assert (out[0]["matched"], out[0]["missing"]) == (2, 2)
    assert (out[1]["matched"], out[1]["missing"]) == (1, 2)


# --- endpoint -------------------------------------------------------------

def test_endpoint_returns_donors_and_broadcasts_progress(monkeypatch):
    from backend import api

    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)

    class H:
        def find_length_donors(self, rid, on_progress=None, **kw):
            on_progress(2, 12, 1)
            return [{"release_id": 9, "matched": 1, "missing": 2, "lengths": {"0": "1:00"}}]

    monkeypatch.setattr(api, "metadata_handler", H())
    monkeypatch.setattr(api, "broadcast_message", fake_broadcast)
    with TestClient(api.app) as client:
        r = client.get("/api/release/5/length-donors")
    assert r.status_code == 200
    assert r.json()["donors"][0]["release_id"] == 9
    assert {"type": "donor_progress", "release_id": 5, "checked": 2, "budget": 12, "found": 1} in sent


def test_endpoint_discogs_error_is_clean_502(monkeypatch):
    from backend import api

    class H:
        def find_length_donors(self, *a, **kw):
            raise RuntimeError("secret internal detail")

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        r = client.get("/api/release/5/length-donors")
    assert r.status_code == 502
    assert "secret" not in r.json()["detail"]


def test_endpoint_unknown_release_404(monkeypatch):
    # Changed from LookupError: only the dedicated ReleaseNotFound means 404.
    from backend import api

    class H:
        def find_length_donors(self, *a, **kw):
            raise ReleaseNotFound("Release not found")

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        assert client.get("/api/release/5/length-donors").status_code == 404


@pytest.mark.parametrize("error", [KeyError("tracklist"), IndexError("x"), LookupError("odd")])
def test_endpoint_malformed_payload_is_502_not_404(monkeypatch, error):
    from backend import api

    class H:
        def find_length_donors(self, *a, **kw):
            raise error

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        r = client.get("/api/release/5/length-donors")
    assert r.status_code == 502
    assert "tracklist" not in r.json()["detail"]
