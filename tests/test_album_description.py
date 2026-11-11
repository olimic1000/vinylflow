"""Album description lookup (Wikidata -> English Wikipedia).  Fully offline: every HTTP call
goes to a fake ``http_get``."""

import urllib.parse

import pytest
import requests

import album_description as ad
from album_description import (
    DESCRIPTION_CACHE_SIZE,
    DESCRIPTION_TOTAL_BUDGET_S,
    DESCRIPTION_USER_AGENT,
    DIGITIZED_COMMENT,
    AlbumDescriptions,
    compose_comment,
)

WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
SUMMARY_PREFIX = "https://en.wikipedia.org/api/rest_v1/page/summary/"


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def search_payload(*qids):
    return {"query": {"search": [{"title": qid} for qid in qids]}}


def entities_payload(**titles):
    """``Q1="Title"`` -> an entity with an enwiki sitelink; ``Q2=None`` -> an entity without."""
    entities = {}
    for qid, title in titles.items():
        entities[qid] = {"sitelinks": {"enwiki": {"title": title}}} if title else {"id": qid}
    return {"entities": entities}


def summary_payload(extract="Lead  paragraph.\n", kind="standard"):
    return {"type": kind, "title": "plain", "displaytitle": "<i>html</i>", "extract": extract}


class FakeWeb:
    """Stands in for ``requests.get`` so no real HTTP happens.  It mirrors the response shapes of
    the Wikidata search / wbgetentities API and the Wikipedia REST summary endpoint, answers from
    canned data keyed by the request, and records every call (url, params, headers, timeout).

    ``search`` maps a ``haswbstatement:`` string to a response; ``entities`` is the
    wbgetentities response; ``summaries`` maps a URL to a response.  A value may be an exception
    instance, which is raised instead.
    """

    def __init__(self, search=None, entities=None, summaries=None):
        self.search = search or {}
        self.entities = entities
        self.summaries = summaries or {}
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        if url == WIKIDATA_URL and params.get("action") == "query":
            answer = self.search.get(params["srsearch"], FakeResponse(200, search_payload()))
        elif url == WIKIDATA_URL and params.get("action") == "wbgetentities":
            answer = self.entities
        elif url.startswith(SUMMARY_PREFIX):
            answer = self.summaries.get(url, FakeResponse(404, {}))
        else:
            raise AssertionError(f"unexpected request {url} {params}")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def searched(self):
        return [c[1]["srsearch"] for c in self.calls if c[1] and c[1].get("action") == "query"]


def happy_web(title="Some Album"):
    quoted = urllib.parse.quote(title.replace(" ", "_"), safe="")
    return FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1"))},
        entities=FakeResponse(200, entities_payload(Q1=title)),
        summaries={SUMMARY_PREFIX + quoted: FakeResponse(200, summary_payload())},
    )


def test_compose_comment():
    assert compose_comment(None) == DIGITIZED_COMMENT == "Digitized from vinyl"
    assert compose_comment("") == "Digitized from vinyl"
    assert compose_comment("Text") == "Text\nDigitized from vinyl"


def test_master_hit_found_with_built_url_and_normalised_text():
    web = happy_web()
    result = AlbumDescriptions(http_get=web).lookup(1, 10)
    assert result.status == "found"
    assert result.text == "Lead paragraph."
    assert result.title == "Some Album"
    assert result.url == "https://en.wikipedia.org/wiki/Some_Album"
    assert web.searched() == ["haswbstatement:P1954=10"]


def test_release_id_fallback_only_after_zero_master_hits():
    web = FakeWeb(
        search={"haswbstatement:P2206=1": FakeResponse(200, search_payload("Q1"))},
        entities=FakeResponse(200, entities_payload(Q1="Some Album")),
        summaries={SUMMARY_PREFIX + "Some_Album": FakeResponse(200, summary_payload())},
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "found"
    assert web.searched() == ["haswbstatement:P1954=10", "haswbstatement:P2206=1"]


def test_no_master_id_never_queries_p1954():
    web = FakeWeb(
        search={"haswbstatement:P2206=1": FakeResponse(200, search_payload("Q1"))},
        entities=FakeResponse(200, entities_payload(Q1="Some Album")),
        summaries={SUMMARY_PREFIX + "Some_Album": FakeResponse(200, summary_payload())},
    )
    assert AlbumDescriptions(http_get=web).lookup(1, None).status == "found"
    assert web.searched() == ["haswbstatement:P2206=1"]


def test_non_positive_ids_are_not_queried():
    web = FakeWeb()
    assert AlbumDescriptions(http_get=web).lookup(0, -5).status == "none"
    assert web.calls == []


def test_master_hit_without_enwiki_is_none_and_skips_release_fallback():
    web = FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1"))},
        entities=FakeResponse(200, entities_payload(Q1=None)),
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "none"
    assert web.searched() == ["haswbstatement:P1954=10"]


@pytest.mark.parametrize("sitelinks", [[], {}], ids=["empty-list", "empty-object"])
def test_an_item_whose_sitelinks_are_empty_is_none_not_error(sitelinks):
    """wbgetentities can send an empty sitelinks as a JSON list; that is "no article", not a
    malformed reply (an error is never cached, so the preview would offer Retry forever)."""
    web = FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1", "Q2"))},
        entities=FakeResponse(200, {"entities": {"Q1": {"id": "Q1", "sitelinks": sitelinks},
                                                 "Q2": {"sitelinks": {"enwiki": {"title": "Only"}}}}}),
        summaries={SUMMARY_PREFIX + "Only": FakeResponse(200, summary_payload())},
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "found"
    web = FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1"))},
        entities=FakeResponse(200, {"entities": {"Q1": {"id": "Q1", "sitelinks": sitelinks}}}),
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "none"


def test_two_qids_with_enwiki_is_ambiguous_none():
    web = FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1", "Q2"))},
        entities=FakeResponse(200, entities_payload(Q1="A", Q2="B")),
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "none"


def test_two_qids_one_enwiki_is_found_and_missing_entity_tolerated():
    entities = entities_payload(Q1=None, Q2="Some Album")
    entities["entities"]["Q3"] = {"missing": ""}
    web = FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1", "Q2", "Q3"))},
        entities=FakeResponse(200, entities),
        summaries={SUMMARY_PREFIX + "Some_Album": FakeResponse(200, summary_payload())},
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "found"


def test_non_qid_search_title_is_ignored():
    web = FakeWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Property:P1", "Q1"))},
        entities=FakeResponse(200, entities_payload(Q1="Some Album")),
        summaries={SUMMARY_PREFIX + "Some_Album": FakeResponse(200, summary_payload())},
    )
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "found"
    ids = [c[1]["ids"] for c in web.calls if c[1] and c[1].get("action") == "wbgetentities"]
    assert ids == ["Q1"]


@pytest.mark.parametrize("response", [
    FakeResponse(200, summary_payload(kind="disambiguation")),
    FakeResponse(200, summary_payload(kind="no-extract")),
    FakeResponse(200, summary_payload(kind="mainpage")),
    FakeResponse(200, summary_payload(extract="   ")),
    FakeResponse(404, {}),
])
def test_unusable_summary_is_none(response):
    web = happy_web()
    web.summaries = {SUMMARY_PREFIX + "Some_Album": response}
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "none"


def failing_steps():
    """(label, builder of a FakeWeb whose failure lands at that step)."""
    def web_with(search=None, entities=None, summary=None):
        base = happy_web()
        if search is not None:
            base.search = {"haswbstatement:P1954=10": search}
        if entities is not None:
            base.entities = entities
        if summary is not None:
            base.summaries = {SUMMARY_PREFIX + "Some_Album": summary}
        return base

    for name, failure in [
        ("conn", requests.ConnectionError("down")),
        ("timeout", requests.Timeout("slow")),
        ("500", FakeResponse(500, {})),
        ("badjson", FakeResponse(200, bad_json=True)),
        ("badshape", FakeResponse(200, {"unexpected": 1})),
    ]:
        yield f"search-{name}", web_with(search=failure)
        yield f"entities-{name}", web_with(entities=failure)
        # A summary that is JSON but not an object is malformed.
        yield f"summary-{name}", web_with(summary=FakeResponse(200, []) if name == "badshape" else failure)


@pytest.mark.parametrize("label,web", list(failing_steps()), ids=[l for l, _ in failing_steps()])
def test_failures_are_error_not_exceptions(label, web):
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "error"


def test_chain_stops_at_first_connection_error():
    web = happy_web()
    web.search = {"haswbstatement:P1954=10": requests.ConnectionError("down")}
    assert AlbumDescriptions(http_get=web).lookup(1, 10).status == "error"
    assert len(web.calls) == 1  # no P2206 fallback, no later steps


def test_time_budget_shrinks_timeouts_and_stops_chain():
    now = [0.0]

    def clock():
        return now[0]

    class SlowWeb(FakeWeb):
        def __call__(self, url, params=None, headers=None, timeout=None):
            result = super().__call__(url, params, headers, timeout)
            now[0] += 5.0  # every request "takes" 5 s
            return result

    web = SlowWeb(
        search={"haswbstatement:P1954=10": FakeResponse(200, search_payload("Q1"))},
        entities=FakeResponse(200, entities_payload(Q1="Some Album")),
        summaries={SUMMARY_PREFIX + "Some_Album": FakeResponse(200, summary_payload())},
    )
    result = AlbumDescriptions(http_get=web, clock=clock).lookup(1, 10)
    timeouts = [c[3] for c in web.calls]
    assert timeouts[0] == ad.DESCRIPTION_HTTP_TIMEOUT_S
    assert timeouts[1] == ad.DESCRIPTION_HTTP_TIMEOUT_S
    assert timeouts[2] == pytest.approx(DESCRIPTION_TOTAL_BUDGET_S - 10.0)
    assert result.status == "found"

    now[0] = 0.0

    class Jump(SlowWeb):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            now[0] += DESCRIPTION_TOTAL_BUDGET_S  # first request eats the whole budget
            return result

    jump = Jump(search=web.search, entities=web.entities, summaries=web.summaries)
    assert AlbumDescriptions(http_get=jump, clock=clock).lookup(2, 10).status == "error"
    assert len(jump.calls) == 1


def test_cache_found_and_none_but_not_error():
    web = happy_web()
    descriptions = AlbumDescriptions(http_get=web)
    descriptions.lookup(1, 10)
    count = len(web.calls)
    assert descriptions.lookup(1, 10).status == "found"
    assert len(web.calls) == count

    none_web = FakeWeb()
    none_descriptions = AlbumDescriptions(http_get=none_web)
    assert none_descriptions.lookup(5, None).status == "none"
    count = len(none_web.calls)
    assert none_descriptions.lookup(5, None).status == "none"
    assert len(none_web.calls) == count

    error_web = FakeWeb(search={"haswbstatement:P1954=10": requests.ConnectionError("x")})
    error_descriptions = AlbumDescriptions(http_get=error_web)
    assert error_descriptions.lookup(7, 10).status == "error"
    assert error_descriptions.lookup(7, 10).status == "error"
    assert len(error_web.calls) == 2  # retried, not cached


def test_lru_evicts_past_cache_size():
    web = FakeWeb()
    descriptions = AlbumDescriptions(http_get=web)
    for release_id in range(1, DESCRIPTION_CACHE_SIZE + 2):
        descriptions.lookup(release_id, None)
    web.calls.clear()
    descriptions.lookup(1, None)  # oldest was evicted
    assert web.calls
    web.calls.clear()
    descriptions.lookup(DESCRIPTION_CACHE_SIZE + 1, None)  # newest still cached
    assert not web.calls


def test_title_is_fully_quoted_in_summary_path_and_url():
    web = happy_web("AC/DC Live")
    result = AlbumDescriptions(http_get=web).lookup(1, 10)
    assert result.url == "https://en.wikipedia.org/wiki/AC%2FDC_Live"
    assert web.calls[-1][0] == SUMMARY_PREFIX + "AC%2FDC_Live"


def test_non_ascii_title_round_trips():
    title = "Björk – Homogénic"
    web = happy_web(title)
    result = AlbumDescriptions(http_get=web).lookup(1, 10)
    assert result.status == "found" and result.title == title
    assert result.url == "https://en.wikipedia.org/wiki/Bj%C3%B6rk_%E2%80%93_Homog%C3%A9nic"


def test_every_request_has_user_agent_and_timeout():
    web = happy_web()
    AlbumDescriptions(http_get=web).lookup(1, 10)
    assert len(web.calls) == 3
    for _url, _params, headers, timeout in web.calls:
        assert headers["User-Agent"] == DESCRIPTION_USER_AGENT
        assert timeout and timeout > 0
