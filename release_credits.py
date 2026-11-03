"""Pure helpers for Discogs artist names and credits (no network, no Mutagen).

Discogs artist entries (``release.artists``, ``track.artists``, ``release.credits``
= ``extraartists``) are plain dicts under ``.data``:
``{"id", "name", "anv", "join", "role", "tracks", ...}``.  ``join`` is the string
that follows the name when several artists share a credit ("&", ",", "feat.").
Names carry a disambiguation suffix when Discogs has several artists of that
name ("Nirvana (2)").  ``role`` is free text such as "Conductor, Liner Notes" or
"Piano [Solo]"; ``tracks`` is free text such as "A1 to A3" or "B1, B2".
"""

import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

DEFAULT_ARTIST_JOIN = ", "
VARIOUS_ARTISTS = "Various Artists"
_VARIOUS_NAMES = ("various", "various artists")

_DISAMBIGUATION_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")

# Credit roles that name the composer (lower-case, brackets removed).
COMPOSER_ROLES = frozenset({
    "composed by", "composer", "written-by", "written by", "music by",
})

CONDUCTOR_ROLES = frozenset({"conductor", "chorus master"})

# Roles whose credited name is a group (tagged ENSEMBLE).
ENSEMBLE_ROLES = frozenset({
    "orchestra", "ensemble", "choir", "chorus", "quartet", "trio", "quintet",
    "sextet", "septet", "octet", "string quartet", "consort",
})

VOICE_ROLES = frozenset({
    "vocals", "voice", "soprano", "mezzo-soprano", "alto", "contralto",
    "countertenor", "tenor", "baritone", "bass", "bass-baritone", "treble",
    "vocal", "narrator",
})

INSTRUMENT_ROLES = frozenset({
    "piano", "violin", "viola", "cello", "double bass", "harpsichord", "organ",
    "clarinet", "flute", "oboe", "bassoon", "horn", "trumpet", "trombone",
    "tuba", "harp", "guitar", "percussion", "timpani", "recorder", "lute",
    "saxophone", "cor anglais", "english horn", "piccolo", "celesta",
    "fortepiano", "continuo", "bass clarinet", "contrabassoon", "mandolin",
    "accordion", "drums", "synthesizer", "keyboards",
})

# Second word of a singer credit: Discogs writes "Soprano Vocals", "Baritone Voice".
VOICE_SUFFIXES = frozenset({"vocals", "vocal", "voice"})

# Other performing roles tagged as PERFORMER (no instrument).
OTHER_PERFORMER_ROLES = frozenset({
    "performer", "soloist", "leader", "concertmaster",
})

# Leading words that qualify an allowed role ("Electric Guitar", "Principal Oboe").
ROLE_MODIFIERS = frozenset({
    "electric", "acoustic", "classical", "solo", "principal", "first",
    "second", "lead", "french", "baroque", "alto", "tenor", "bass",
})

# Everything performing; any role not in here (Producer, Engineer, Liner Notes,
# Design, Photography, Mastered By, Lacquer Cut By, Recorded By ...) is never tagged.
PERFORMING_ROLES = (
    CONDUCTOR_ROLES | ENSEMBLE_ROLES | VOICE_ROLES | INSTRUMENT_ROLES | OTHER_PERFORMER_ROLES
)


@dataclass(frozen=True)
class Credit:
    """One Discogs credit: who, a single normalised role, and the tracks it covers."""

    name: str
    role: str
    tracks: str = ""


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
    return {key: getattr(entry, key, "") for key in ("name", "join", "role", "tracks")}


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


def sole_artist_name(entries: Sequence) -> str:
    """The cleaned name when exactly one artist is listed, else "".

    Used as the classical composer fallback: a line such as "Beethoven – Karajan,
    Berliner Philharmoniker" also names performers, so it is no composer."""
    names = [clean_artist_name(_fields(entry).get("name")) for entry in entries or []]
    names = [name for name in names if name]
    return names[0] if len(names) == 1 else ""


def _split_roles(role_text: str) -> List[str]:
    """Split "Conductor, Liner Notes" on commas outside [brackets]."""
    pieces, depth, current = [], 0, ""
    for char in str(role_text or ""):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            pieces.append(current)
            current = ""
        else:
            current += char
    pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def role_base(role: str) -> str:
    """Role without its [modifier] ("Piano [Solo]" -> "piano"), lower-case."""
    return re.sub(r"\s+", " ", re.sub(r"\[.*?\]", "", role)).strip().lower()


def _allowed(base: str, allowed: frozenset) -> bool:
    """Is a role (``role_base`` form) in ``allowed``, alone or after one modifier ("Principal Oboe")?"""
    if base in allowed:
        return True
    head, _, rest = base.partition(" ")
    return head in ROLE_MODIFIERS and rest in allowed


def _is_singer(base: str) -> bool:
    """A "<voice type> Vocals" credit ("Soprano Vocals", "Baritone Voice")."""
    head, _, rest = base.partition(" ")
    return head in VOICE_ROLES and rest in VOICE_SUFFIXES


def parse_credits(entries: Sequence) -> List[Credit]:
    """One Credit per (name, single role); names cleaned, empty names dropped."""
    credits: List[Credit] = []
    for entry in entries or []:
        fields = _fields(entry)
        name = clean_artist_name(fields.get("name"))
        if not name:
            continue
        for role in _split_roles(fields.get("role")):
            credits.append(Credit(name, role, str(fields.get("tracks") or "").strip()))
    return credits


def credit_applies(tracks_text: str, position: str, positions: Sequence[Tuple[str, str]]) -> bool:
    """Does a credit's track text cover ``position``?

    ``positions`` is the release's ordered [(raw Discogs position, normalised
    position)].  Several entries may share a normalised position: an index
    track's movements are listed under their umbrella's position, so a credit
    on any movement covers the umbrella.  Empty text covers every track.  Entries are comma/semicolon
    separated; each is a position or a range ("A1 to A3", "1-4").  Credits
    that can't be resolved cover nothing (never attribute a soloist wrongly).
    """
    text = (tracks_text or "").strip()
    if not text:
        return True
    here = {i for i, (_, normalised) in enumerate(positions) if normalised == position}
    if not here:
        return False

    def indexes(token: str) -> List[int]:
        token = token.strip().lower()
        if not token:
            return []
        # "B1" also covers sub-positions "B1a" / "B1.1", but not "B10"; a bare
        # side letter "A" must not match a repeated-letter position "AA".
        boundary = r"(?![0-9])" if token[-1].isdigit() else r"(?![0-9a-z])"
        found = []
        for i, (raw, normalised) in enumerate(positions):
            for candidate in (raw.lower(), normalised.lower()):
                if candidate == token or re.match(re.escape(token) + boundary, candidate):
                    found.append(i)
                    break
        return found

    for part in re.split(r"[,;&]", text):
        pieces = re.split(r"\s+to\s+|\s*[-–—]\s*", part.strip(), maxsplit=1)
        if len(pieces) == 2:
            first, last = indexes(pieces[0]), indexes(pieces[1])
            if first and last and any(min(first) <= i <= max(last) for i in here):
                return True
        elif here & set(indexes(part)):
            return True
    return False


def credits_for_track(
    release_credits: Sequence[Credit],
    track_credits: Sequence[Credit],
    position: str,
    positions: Sequence[Tuple[str, str]],
) -> List[Credit]:
    """Release-level credits covering the track plus the track's own credits (deduped)."""
    chosen = [c for c in release_credits if credit_applies(c.tracks, position, positions)]
    chosen += list(track_credits)
    seen, unique = set(), []
    for credit in chosen:
        key = (credit.name, role_base(credit.role))
        if key not in seen:
            seen.add(key)
            unique.append(credit)
    return unique


@dataclass(frozen=True)
class ClassicalCredits:
    """One track's taggable credits by kind; performers are (name, role label)."""

    composers: Tuple[str, ...] = ()
    conductors: Tuple[str, ...] = ()
    ensembles: Tuple[str, ...] = ()
    performers: Tuple[Tuple[str, str], ...] = ()  # (name, instrument or role)


def classify_credits(credits: Sequence[Credit]) -> ClassicalCredits:
    """Sort credits into composer / conductor / ensemble / performer (allowlist only)."""
    composers, conductors, ensembles, performers = [], [], [], []

    def add(bucket: list, value):
        if value not in bucket:
            bucket.append(value)

    for credit in credits:
        base = role_base(credit.role)
        if base in COMPOSER_ROLES:
            add(composers, credit.name)
        elif _allowed(base, CONDUCTOR_ROLES):
            add(conductors, credit.name)
        elif _allowed(base, ENSEMBLE_ROLES):
            add(ensembles, credit.name)
        elif _allowed(base, PERFORMING_ROLES) or _is_singer(base):
            label = "" if base in OTHER_PERFORMER_ROLES else role_base(credit.role).title()
            add(performers, (credit.name, label))
    return ClassicalCredits(
        tuple(composers), tuple(conductors), tuple(ensembles), tuple(performers),
    )


def performer_text(name: str, label: str) -> str:
    """FLAC PERFORMER value: "Name (Instrument)"."""
    return f"{name} ({label})" if label else name
