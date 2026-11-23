"""MetadataHandler.find_length_donors and GET /api/release/{id}/length-donors (fakes only)."""

from __future__ import annotations

import threading
import time
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
    def __init__(self, items, log, count=None):
        self.items = items
        self.per_page = 50
        self.log = log
        self.count = len(items) if count is None else count  # Discogs' total, free once pages is known

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
    h._donor_cache = OrderedDict()
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
            "compared": 0,
            "disagree": 0,
            "compared_indexes": [],
            "disagree_indexes": [],
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
    assert {d["release_id"] for d in h._donor_cache[1]["donors"]} == {2, 3}


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
        def find_length_donor_report(self, rid, on_progress=None, **kw):
            on_progress(2, 12, 1)
            return {
                "donors": [{"release_id": 9, "matched": 1, "missing": 2, "lengths": {"0": "1:00"}}],
                "checked": 2,
                "versions": 340,
            }

    monkeypatch.setattr(api, "metadata_handler", H())
    monkeypatch.setattr(api, "broadcast_message", fake_broadcast)
    with TestClient(api.app) as client:
        r = client.get("/api/release/5/length-donors")
    assert r.status_code == 200
    assert r.json()["donors"][0]["release_id"] == 9
    assert (r.json()["checked"], r.json()["versions"]) == (2, 340)
    assert {"type": "donor_progress", "release_id": 5, "checked": 2, "budget": 12, "found": 1} in sent


def test_endpoint_discogs_error_is_clean_502(monkeypatch):
    from backend import api

    class H:
        def find_length_donor_report(self, *a, **kw):
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
        def find_length_donor_report(self, *a, **kw):
            raise ReleaseNotFound("Release not found")

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        assert client.get("/api/release/5/length-donors").status_code == 404


@pytest.mark.parametrize("error", [KeyError("tracklist"), IndexError("x"), LookupError("odd")])
def test_endpoint_malformed_payload_is_502_not_404(monkeypatch, error):
    from backend import api

    class H:
        def find_length_donor_report(self, *a, **kw):
            raise error

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        r = client.get("/api/release/5/length-donors")
    assert r.status_code == 502
    assert "tracklist" not in r.json()["detail"]


# --- disagreement with the release's own lengths (demote, never drop) ----------------------------

def partial_scenario(donor_tracks_by_id, own=None):
    own = own or [("One", "3:00"), ("Two", ""), ("Three", "")]
    releases = {1: raw_release(1, own, master_id=77)}
    releases.update({rid: raw_release(rid, t) for rid, t in donor_tracks_by_id.items()})
    return FakeClient(releases, [ver(1)] + [ver(rid) for rid in donor_tracks_by_id])


def test_disagreement_threshold_is_ten_seconds_on_a_short_track():
    c = partial_scenario({
        2: [("One", "3:09"), ("Two", "4:00"), ("Three", "5:00")],  # 9 s off a 3:00 track: agrees
        3: [("One", "3:11"), ("Two", "4:00"), ("Three", "5:00")],  # 11 s off: disagrees
    })
    out = {d["release_id"]: d for d in make(c).find_length_donors(1, want=10)}
    assert (out[2]["compared"], out[2]["disagree"]) == (1, 0)
    assert (out[3]["compared"], out[3]["disagree"]) == (1, 1)


def test_disagreement_threshold_is_five_percent_on_a_long_track():
    own = [("One", "20:00"), ("Two", ""), ("Three", "")]  # 5 % = 60 s
    c = partial_scenario({
        2: [("One", "20:59"), ("Two", "4:00")],
        3: [("One", "21:01"), ("Two", "4:00")],
    }, own)
    out = {d["release_id"]: d for d in make(c).find_length_donors(1, want=10)}
    assert out[2]["disagree"] == 0 and out[3]["disagree"] == 1


def test_disagreeing_donor_is_demoted_not_dropped_and_listed_after_agreeing_ones():
    c = partial_scenario({
        2: [("One", "3:30"), ("Two", "4:00"), ("Three", "5:00")],  # fills most but disagrees
        3: [("One", "3:00"), ("Two", "4:00")],  # fills less, agrees
    })
    out = make(c).find_length_donors(1, want=10)
    assert [d["release_id"] for d in out] == [3, 2]
    assert out[1]["disagree"] == 1 and out[1]["lengths"] == {"1": "4:00", "2": "5:00"}


def test_release_without_own_lengths_compares_nothing():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    (donor,) = make(c).find_length_donors(1)
    assert (donor["compared"], donor["disagree"]) == (0, 0)


def test_disagreeing_donors_do_not_trigger_the_early_stop():
    own = [("One", "3:00"), ("Two", ""), ("Three", "")]
    bad = [("One", "5:00"), ("Two", "4:00"), ("Three", "5:00")]
    c = partial_scenario({2: bad, 3: bad, 4: bad, 5: [("One", "3:00"), ("Two", "4:00"), ("Three", "5:00")]}, own)
    out = make(c).find_length_donors(1, want=1)
    assert out[0]["release_id"] == 5 and len(out) == 4


# --- report: how far the lookup got ------------------------------------------------------------

def test_report_says_how_many_versions_were_checked_of_how_many_exist():
    rels = {i: raw_release(i, NONE) for i in range(2, 12)}
    c = scenario([ver(i) for i in range(2, 12)], rels)
    c.master = lambda mid, real=c.master: _with_total(real(mid), 340)
    report = make(c).find_length_donor_report(1, max_fetches=4)
    assert report["donors"] == [] and report["checked"] == 4 and report["versions"] == 339


def _with_total(master, total):
    master.versions.count = total
    return master


def test_report_cached_and_cache_hits_count_as_checked():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    h = make(c)
    first = h.find_length_donor_report(1)
    assert (first["checked"], first["versions"]) == (1, 1)
    assert h.find_length_donor_report(1) == first


# --- bounded cache and wait ---------------------------------------------------------------------

def test_donor_cache_evicts_the_oldest_report():
    h = make(FakeClient({}, []))
    h.DONOR_CACHE_SIZE = 2
    for key in (1, 2, 3):
        h._remember_donor_report(key, {"donors": [], "checked": 0, "versions": None})
    assert list(h._donor_cache) == [2, 3]


def _call_in_thread(fn, join_s=5):
    """Run fn in a thread; fail (instead of hanging) if it does not return within join_s."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("value", fn()), daemon=True)
    thread.start()
    thread.join(join_s)
    assert not thread.is_alive(), "call did not return: it is waiting on the stuck lookup"
    return box["value"]


def test_waiting_caller_runs_its_own_lookup_after_the_timeout():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    h = make(c)
    h.DONOR_WAIT_TIMEOUT_S = 0.2
    h.DONOR_WAIT_SLICE_S = 0.05
    h._donor_inflight[1] = threading.Event()  # a lookup that never finishes
    out = _call_in_thread(lambda: h.find_length_donors(1))
    assert [d["release_id"] for d in out] == [2]


def test_waiter_picks_up_a_result_cached_while_the_event_never_fires():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    h = make(c)
    h.DONOR_WAIT_TIMEOUT_S = 60  # far beyond the join below: only the slice re-check can end the wait
    h.DONOR_WAIT_SLICE_S = 0.05
    h._donor_inflight[1] = threading.Event()
    threading.Timer(0.2, lambda: h._remember_donor_report(
        1, {"donors": [{"release_id": 5}], "checked": 1, "versions": 1})).start()
    out = _call_in_thread(lambda: h.find_length_donors(1))
    assert out == [{"release_id": 5}]
    assert c.release_calls == []  # it did not run a lookup of its own


def test_returned_results_are_copies_on_every_path():
    # Early-return path (nothing missing): the cache's own list must not be handed out
    h = make(FakeClient({1: raw_release(1, FULL, master_id=77)}, []))
    h.find_length_donors(1).append("junk")
    assert h.find_length_donors(1) == []
    # Normal path: donors, their lengths and index lists are copies too, fresh and cached
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    h = make(c)
    for _ in range(2):  # first call computes, second reads the cache
        out = h.find_length_donors(1)
        out[0]["lengths"]["0"] = "99:99"
        out[0]["compared_indexes"].append(42)
        out.append("junk")
    again = h.find_length_donors(1)
    assert len(again) == 1 and again[0]["lengths"]["0"] == "3:00" and again[0]["compared_indexes"] == []


def test_disagreement_is_exactly_ten_seconds_not_inclusive():
    c = partial_scenario({2: [("One", "3:10"), ("Two", "4:00")], 3: [("One", "2:50"), ("Two", "4:00")]})
    out = {d["release_id"]: d for d in make(c).find_length_donors(1, want=10)}
    assert out[2]["disagree"] == 0 and out[3]["disagree"] == 0  # exactly 10 s off agrees


def test_cache_hit_refreshes_recency_so_eviction_is_lru_not_fifo():
    h = make(FakeClient({}, []))
    h.DONOR_CACHE_SIZE = 2
    report = {"donors": [], "checked": 0, "versions": None}
    h._remember_donor_report(1, report)
    h._remember_donor_report(2, report)
    h.find_length_donors(1)  # reads 1: now 2 is the least recently used
    h._remember_donor_report(3, report)
    assert list(h._donor_cache) == [1, 3]


def test_disagreement_is_reported_per_track_index_for_side_scoped_recounts():
    own = [("One", "3:00"), ("Two", ""), ("Three", "9:00")]
    c = partial_scenario({2: [("One", "3:00"), ("Two", "4:00"), ("Three", "5:00")]}, own)
    (donor,) = make(c).find_length_donors(1)
    assert (donor["compared"], donor["disagree"]) == (2, 1)
    assert donor["compared_indexes"] == [0, 2] and donor["disagree_indexes"] == [2]


def test_checked_excludes_failed_fetches():
    c = failing_client(scenario([ver(2), ver(3)], {2: raw_release(2, FULL), 3: raw_release(3, FULL)}), {2})
    report = make(c).find_length_donor_report(1, want=10)
    assert [d["release_id"] for d in report["donors"]] == [3] and report["checked"] == 1


def test_versions_is_the_masters_total_minus_the_release_itself():
    # Only some version pages are read, so the release may not be in the list: still one of the total
    c = scenario([ver(2)], {2: raw_release(2, NONE)})
    c.master = lambda mid, real=c.master: _with_total(real(mid), 340)
    assert make(c).find_length_donor_report(1)["versions"] == 339
    c = FakeClient({1: raw_release(1, NONE, master_id=77), 2: raw_release(2, NONE)}, [ver(2)])  # self not listed
    c.master = lambda mid, real=c.master: _with_total(real(mid), 340)
    assert make(c).find_length_donor_report(1)["versions"] == 339
    assert make(scenario([ver(2)], {2: raw_release(2, NONE)})).find_length_donor_report(1)["versions"] == 1


def _gated_master(c, behaviours):
    """Replace c.master: the n-th call waits for behaviours[n]["gate"], then raises behaviours[n]["error"] if set."""
    real = c.master
    calls = []

    def master(mid):
        n = len(calls)
        calls.append(mid)
        step = behaviours[min(n, len(behaviours) - 1)]
        if "entered" in step:
            step["entered"].set()
        step["gate"].wait(5)
        if step.get("error"):
            raise step["error"]
        return real(mid)

    return calls, master


def _start(fn, results, key):
    def run():
        try:
            results[key] = ("ok", fn())
        except Exception as error:  # noqa: BLE001 - reported to the test
            results[key] = ("error", error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_timed_out_waiter_takes_over_and_later_callers_wait_on_the_new_owner():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    gate = threading.Event()
    entered = threading.Event()
    gate_open = threading.Event()
    gate_open.set()
    # First lookup (the taker-over) fails once it is released; the second one succeeds at once
    calls, c.master = _gated_master(c, [
        {"gate": gate, "entered": entered, "error": RuntimeError("transient")}, {"gate": gate_open}])
    h = make(c)
    h.DONOR_WAIT_TIMEOUT_S = 60
    h.DONOR_WAIT_SLICE_S = 0.5
    stuck = threading.Event()
    h._donor_inflight[1] = stuck  # the original owner, hung on the network
    results = {}
    h.DONOR_WAIT_TIMEOUT_S = 0.2
    taker = _start(lambda: h.find_length_donors(1), results, "taker")
    assert entered.wait(5), "the waiter never took over"
    assert h._donor_inflight[1] is not stuck, "the taker-over registers its own Event"
    h.DONOR_WAIT_TIMEOUT_S = 60
    third = _start(lambda: h.find_length_donors(1), results, "third")
    gate.set()  # the new owner fails: the third caller must be woken by ITS Event, not wait on the stuck one
    taker.join(5)
    third.join(5)
    assert not third.is_alive(), "the third caller kept waiting on the stuck Event"
    assert results["taker"][0] == "error"
    assert [d["release_id"] for d in results["third"][1]] == [2]
    assert h._donor_inflight == {}


def test_old_owner_finishing_late_does_not_remove_the_new_owners_entry():
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    old_gate, new_gate = threading.Event(), threading.Event()
    old_in, new_in = threading.Event(), threading.Event()
    calls, c.master = _gated_master(c, [
        {"gate": old_gate, "entered": old_in}, {"gate": new_gate, "entered": new_in}])
    h = make(c)
    h.DONOR_WAIT_SLICE_S = 0.05
    results = {}
    old = _start(lambda: h.find_length_donors(1), results, "old")
    assert old_in.wait(5)
    old_event = h._donor_inflight[1]
    h.DONOR_WAIT_TIMEOUT_S = 0.2
    new = _start(lambda: h.find_length_donors(1), results, "new")
    assert new_in.wait(5), "the waiter never took over"
    new_event = h._donor_inflight[1]
    old_gate.set()
    old.join(5)
    assert not old.is_alive()
    assert h._donor_inflight.get(1) is new_event and not new_event.is_set(), "the old owner removed or set the new owner's Event"
    assert old_event.is_set(), "an owner always releases its own waiters, even after being replaced"
    new_gate.set()
    new.join(5)
    assert not new.is_alive() and h._donor_inflight == {} and new_event.is_set()


@pytest.mark.parametrize("bad_id", [0, 10_000_000_000, -3])
def test_endpoint_rejects_release_ids_outside_one_to_ten_digits(monkeypatch, bad_id):
    from backend import api

    class H:
        def find_length_donor_report(self, *a, **kw):
            raise AssertionError("must not be called")

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        assert client.get(f"/api/release/{bad_id}/length-donors").status_code == 422


def test_endpoint_logs_a_traceback_for_a_502(monkeypatch, caplog):
    from backend import api

    class H:
        def find_length_donor_report(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(api, "metadata_handler", H())
    with TestClient(api.app) as client:
        assert client.get("/api/release/5/length-donors").status_code == 502
    assert any(r.exc_info for r in caplog.records if "length-donors" in r.getMessage())


def test_waiters_timing_out_together_start_one_lookup_not_one_each():
    # The deadline belongs to the Event being waited on: once one waiter has taken over from a
    # stuck owner, the others wait on the new owner instead of each taking over in turn.
    c = scenario([ver(2)], {2: raw_release(2, FULL)})
    gate, entered = threading.Event(), threading.Event()
    calls, c.master = _gated_master(c, [{"gate": gate, "entered": entered}])
    h = make(c)
    h.DONOR_WAIT_SLICE_S = 0.05
    h.DONOR_WAIT_TIMEOUT_S = 1.0
    h._donor_inflight[1] = threading.Event()  # the original owner, hung on the network
    results = {}
    threads = [_start(lambda: h.find_length_donors(1), results, n) for n in range(4)]
    assert entered.wait(5), "no waiter took over from the stuck owner"
    # A few slices: with one deadline per waiter the others would take over within one slice;
    # the new owner itself is only due for replacement a full second later
    time.sleep(3 * h.DONOR_WAIT_SLICE_S)
    lookups_running = len(calls)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert lookups_running == 1, f"{lookups_running} lookups ran at once"
