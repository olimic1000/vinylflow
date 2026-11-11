"""Album description for the Comment tag: the lead paragraph of the album's English Wikipedia
article, found through Wikidata by Discogs master / release id (exact ID links, no title guessing).
"""

import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

import requests

DIGITIZED_COMMENT = "Digitized from vinyl"

# Wikimedia policy asks for a unique User-Agent with contact info; deliberately not the
# user-editable Discogs one.
DESCRIPTION_USER_AGENT = "VinylFlow/1.0 (https://github.com/olimic1000/vinylflow)"
DESCRIPTION_HTTP_TIMEOUT_S = 6.0
DESCRIPTION_TOTAL_BUDGET_S = 12.0
DESCRIPTION_CACHE_SIZE = 128

WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKIPEDIA_ARTICLE_URL = "https://en.wikipedia.org/wiki/"
WIKIDATA_DISCOGS_MASTER_PROPERTY = "P1954"
WIKIDATA_DISCOGS_RELEASE_PROPERTY = "P2206"
WIKIDATA_SEARCH_LIMIT = 5
WIKIDATA_ITEM_ID = re.compile(r"^Q\d+$")

STATUS_FOUND = "found"
STATUS_NONE = "none"
STATUS_ERROR = "error"


def compose_comment(description: Optional[str]) -> str:
    """The Comment tag: description, an LF, then the digitized note; the note alone without text."""
    if not description:
        return DIGITIZED_COMMENT
    return f"{description}\n{DIGITIZED_COMMENT}"


@dataclass(frozen=True)
class DescriptionResult:
    """``status`` is found / none (no linked article) / error (network or malformed response)."""

    status: str
    text: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None


class _LookupFailed(Exception):
    """A connection error, timeout, non-2xx status or malformed response: the result is ``error``."""


class AlbumDescriptions:
    """Cached Wikidata -> Wikipedia lookup.

    ``http_get`` is ``requests.get`` unless a test injects a fake; ``clock`` is injectable for
    the time-budget test.  Results are cached per Discogs release id in an in-memory LRU.
    """

    def __init__(self, http_get: Callable = requests.get, clock: Callable[[], float] = time.monotonic):
        self._http_get = http_get
        self._clock = clock
        self._cache: "OrderedDict[int, DescriptionResult]" = OrderedDict()
        # Held only around cache get/put, never across HTTP; two concurrent lookups of the
        # same release are harmless (same value, last writer wins).
        self._cache_lock = threading.Lock()

    def lookup(self, release_id: int, master_id: Optional[int]) -> DescriptionResult:
        """Find the album's Wikipedia lead paragraph.

        ``found`` and ``none`` are cached.  ``error`` is not, so the next request retries
        (the network may be back).  The whole chain shares one time budget and ends at the first
        connection error, so a silently dropping network costs at most
        ``DESCRIPTION_TOTAL_BUDGET_S`` per call.  Never raises.
        """
        with self._cache_lock:
            cached = self._cache.get(release_id)
            if cached is not None:
                self._cache.move_to_end(release_id)
                return cached
        try:
            result = self._fetch(release_id, master_id)
        except _LookupFailed:
            return DescriptionResult(STATUS_ERROR)
        with self._cache_lock:
            self._cache[release_id] = result
            self._cache.move_to_end(release_id)
            while len(self._cache) > DESCRIPTION_CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    # -- chain ----------------------------------------------------------------------------

    def _fetch(self, release_id, master_id) -> DescriptionResult:
        deadline = self._clock() + DESCRIPTION_TOTAL_BUDGET_S
        master_id = self._positive_int(master_id)
        release_id = self._positive_int(release_id)
        item_ids: list = []
        if master_id:
            item_ids = self._search_items(WIKIDATA_DISCOGS_MASTER_PROPERTY, master_id, deadline)
        if not item_ids and release_id:
            # Only when the master search found nothing; a hit without an article is final.
            item_ids = self._search_items(WIKIDATA_DISCOGS_RELEASE_PROPERTY, release_id, deadline)
        if not item_ids:
            return DescriptionResult(STATUS_NONE)
        title = self._english_article_title(item_ids, deadline)
        if title is None:
            return DescriptionResult(STATUS_NONE)
        return self._summary(title, deadline)

    @staticmethod
    def _positive_int(value) -> Optional[int]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _search_items(self, wikidata_property: str, discogs_id: int, deadline: float) -> list:
        data = self._get_json(WIKIDATA_API_URL, {
            "action": "query", "list": "search",
            "srsearch": f"haswbstatement:{wikidata_property}={discogs_id}",
            "srlimit": WIKIDATA_SEARCH_LIMIT, "format": "json",
        }, deadline)
        try:
            titles = [hit["title"] for hit in data["query"]["search"]]
        except (KeyError, TypeError):
            raise _LookupFailed("malformed Wikidata search response")
        return [t for t in titles if isinstance(t, str) and WIKIDATA_ITEM_ID.match(t)]

    def _english_article_title(self, item_ids: list, deadline: float) -> Optional[str]:
        """The enwiki title when exactly one item has an article.

        Several candidates with articles are ambiguous: picking one could attach the wrong
        album's text, so that is ``None`` like no article at all.
        """
        data = self._get_json(WIKIDATA_API_URL, {
            "action": "wbgetentities", "ids": "|".join(item_ids),
            "props": "sitelinks", "sitefilter": "enwiki", "format": "json",
        }, deadline)
        try:
            titles = []
            for item_id in item_ids:
                entity = data["entities"].get(item_id) or {}
                sitelinks = entity.get("sitelinks")
                # An item without sitelinks may send them as an empty JSON list: no article.
                title = sitelinks.get("enwiki", {}).get("title") if isinstance(sitelinks, dict) else None
                if title:
                    titles.append(title)
        except (KeyError, TypeError, AttributeError):
            raise _LookupFailed("malformed Wikidata entities response")
        return titles[0] if len(titles) == 1 else None

    def _summary(self, title: str, deadline: float) -> DescriptionResult:
        quoted = urllib.parse.quote(title.replace(" ", "_"), safe="")
        response = self._request(WIKIPEDIA_SUMMARY_URL + quoted, None, deadline)
        if response.status_code == 404:
            return DescriptionResult(STATUS_NONE)
        data = self._json_of(response)
        try:
            kind, extract = data.get("type"), data.get("extract")
        except AttributeError:
            raise _LookupFailed("malformed Wikipedia summary")
        text = " ".join(extract.split()) if isinstance(extract, str) else ""
        if kind != "standard" or not text:
            return DescriptionResult(STATUS_NONE)
        # Plain ``title`` (not the HTML ``displaytitle``); the URL is built here, never taken
        # from the response.
        return DescriptionResult(STATUS_FOUND, text, title, WIKIPEDIA_ARTICLE_URL + quoted)

    # -- HTTP -----------------------------------------------------------------------------

    def _request(self, url: str, params: Optional[dict], deadline: float):
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _LookupFailed("time budget spent")
        try:
            response = self._http_get(
                url, params=params, headers={"User-Agent": DESCRIPTION_USER_AGENT},
                timeout=min(DESCRIPTION_HTTP_TIMEOUT_S, remaining),
            )
        except requests.RequestException as error:
            raise _LookupFailed(str(error))
        if response.status_code != 404 and not 200 <= response.status_code < 300:
            raise _LookupFailed(f"HTTP {response.status_code}")
        return response

    def _get_json(self, url: str, params: dict, deadline: float):
        response = self._request(url, params, deadline)
        if response.status_code == 404:
            raise _LookupFailed("HTTP 404")
        return self._json_of(response)

    @staticmethod
    def _json_of(response):
        try:
            return response.json()
        except ValueError:
            raise _LookupFailed("malformed JSON")
