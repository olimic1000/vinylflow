"""Pure helpers for Discogs artist names (no network, no Mutagen).

Discogs artist entries (``release.artists``, ``track.artists``) are plain dicts under
``.data``: ``{"id", "name", "anv", "join", ...}``.  ``join`` is the string that follows
the name when several artists share a credit ("&", ",", "feat.").  Names carry a
disambiguation suffix when Discogs has several artists of that name ("Nirvana (2)").
"""

import re
from typing import List, Sequence

DEFAULT_ARTIST_JOIN = ", "
VARIOUS_ARTISTS = "Various Artists"
_VARIOUS_NAMES = ("various", "various artists")

_DISAMBIGUATION_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def clean_artist_name(name: str) -> str:
    """Strip Discogs' disambiguation suffix: "Nirvana (2)" -> "Nirvana"."""
    return _DISAMBIGUATION_SUFFIX.sub("", str(name or "")).strip()


def is_various(name: str) -> bool:
    """True for the compilation pseudo-artist ("Various", "Various Artists")."""
    return clean_artist_name(name).lower() in _VARIOUS_NAMES


def _fields(entry) -> dict:
    """Raw field dict of a discogs_client Artist (its ``.data``) or a plain object."""
    data = getattr(entry, "data", None)
    if isinstance(data, dict):
        return data
    return {key: getattr(entry, key, "") for key in ("name", "join")}


def join_artist_names(entries: Sequence) -> str:
    """"Name1 <join> Name2 ..." using Discogs' join strings (fallback ", ")."""
    parts: List[str] = []
    entries = [e for e in entries if clean_artist_name(_fields(e).get("name"))]
    for index, entry in enumerate(entries):
        fields = _fields(entry)
        parts.append(clean_artist_name(fields.get("name")))
        if index == len(entries) - 1:
            break
        join = str(fields.get("join") or "").strip()
        if not join or join == ",":
            parts.append(DEFAULT_ARTIST_JOIN)
        else:
            parts.append(f" {join} ")
    return "".join(parts)
