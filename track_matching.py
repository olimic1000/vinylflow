"""VinylFlow — Track matching across pressings (pure functions).

Used by "borrow track lengths from another pressing": a target release's
track titles are matched against another pressing's tracklist so the donor's
durations can be copied over.  No I/O, no Discogs types -- donor tracks are
any objects (or dicts) with a ``title`` and a ``duration_str``/``duration``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence

# Matching is exact on purpose.  A wrong match gives a track another recording's length, which
# then moves where the audio is cut; a missed match only leaves the length empty.  Fuzzy matching
# kept producing wrong matches (tempo markings, a movement vs its work, one different word in a
# long title), so titles are only made *comparable* here (case, punctuation, script, number and
# part spellings) and then compared for equality.
#
# A title is read as a *base* (text before the first bracket group, " - " or "/" suffix) plus
# *qualifiers* (every later bracket group / suffix).  Qualifiers name the recording ("Live",
# "Extended", "Part 2"), so they must agree exactly.

# Qualifiers (squashed text) that do not change which recording a title names: remaster tags,
# a bare year, album/LP version, stereo/mono, a single-track "original".
_NEUTRAL_QUALIFIER_RE = re.compile(
    r"(?:\d{4} )?(?:digital(?:ly)? )?remaster(?:ed)?(?: (?:version|edition))?(?: \d{4})?"
    r"|\d{4}"
    r"|(?:album|lp) version"
    r"|(?:stereo|mono)(?: version)?"
    r"|original"
)
_FEAT_WORDS = frozenset({"feat", "ft", "featuring"})
# Unbracketed clause starters.  "ft" is deliberately absent ("Six Ft. Deep") and "feat" needs
# its dot ("No Mean Feat"): an unstripped credit is a safe missed match, a stripped title word
# is a wrong length.
_BASE_FEAT_WORDS = frozenset({"feat.", "featuring"})

# Letters NFKD does not decompose, folded to their usual ASCII spelling.
_LETTER_FOLDS = str.maketrans({
    "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "þ": "th", "ð": "d", "ı": "i",
})
# A "#" directly before digits is a number sign ("Song #1"), unless a letter precedes it ("C#").
_NUMBER_SIGN_RE = re.compile(r"(?<!\w)#(?=\d)")
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_UNITS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_HYPHENATED_NUMBER_RE = re.compile(
    r"\b(" + "|".join(_TENS) + r")-(" + "|".join(_UNITS) + r")\b"
)
# The only words dropped from a qualifier: "(Dub Mix)" = "(Dub)", never "(Mix)" or "(Radio Edit)".
_QUALIFIER_TAIL_WORDS = frozenset({"version", "mix"})

_SYNONYMS = {"pt": "part", "number": "no", "nr": "no", "n": "and"}
_SPELLED_NUMBERS = {
    word: str(value)
    for value, word in enumerate(
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split(), start=1
    )
}
_SPELLED_NUMBERS.update(
    (word, str(value))
    for value, word in enumerate(
        "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
        "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth "
        "twentieth".split(), start=1
    )
)
_SPELLED_NUMBERS.update((word, str(value)) for word, value in _TENS.items())
_ROMAN_VALUES = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
    "xix": 19, "xx": 20,
}
_ORDINAL_DIGITS_RE = re.compile(r"(\d+)(?:st|nd|rd|th)")
_DIGIT_LETTER_RE = re.compile(r"\d+|[^\W\d_]+")


def _squash(text: str) -> str:
    """Case-folded, Latin/Greek/Cyrillic accents removed, apostrophes dropped ("Don't" ->
    "dont"), "&" -> "and", "C#" -> "c sharp", "#1" -> "1", other punctuation a space.

    Letters of any script are kept (a Cyrillic or Japanese title must not squash to nothing and
    then equal every other one).  Only the U+0300 block of combining marks is removed, so kana
    voicing marks and Indic vowel signs survive.
    """
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not "\u0300" <= c <= "\u036f")
    s = unicodedata.normalize("NFC", s).casefold().translate(_LETTER_FOLDS)
    s = _NUMBER_SIGN_RE.sub(" ", s)
    s = s.replace("&", " and ").replace("#", " sharp ").replace("♯", " sharp ").replace("♭", " flat ")
    for apostrophe in "'’‘ʼ`":
        s = s.replace(apostrophe, "")
    s = _HYPHENATED_NUMBER_RE.sub(lambda m: str(_TENS[m.group(1)] + _UNITS.index(m.group(2)) + 1), s)
    return "".join(c if c.isalnum() else " " for c in s).strip()


def _canonical_tokens(words: Sequence[str], is_qualifier: bool) -> List[str]:
    """Squashed words -> comparable tokens: "pt" -> "part", spelled numbers / ordinals / roman
    numerals -> integers, "v2" -> "v", "2".

    A roman numeral is read as a number only when it is not the first token ("I'm", "V-2",
    "X-Ray" start with a letter that is no numeral); a qualifier that is just "II" counts.
    """
    tokens: List[str] = []
    for word in words:
        ordinal = _ORDINAL_DIGITS_RE.fullmatch(word)
        tokens.extend([ordinal.group(1)] if ordinal else _DIGIT_LETTER_RE.findall(word))
    result: List[str] = []
    for index, token in enumerate(tokens):
        token = _SYNONYMS.get(token, token)
        token = _SPELLED_NUMBERS.get(token, token)
        if token in _ROMAN_VALUES and (index > 0 or (is_qualifier and len(tokens) == 1)):
            token = str(_ROMAN_VALUES[token])
        result.append(token)
    return result


def _split_title(text: str):
    """Split a title into (base text, qualifier texts) in one linear pass.

    Bracket groups and " - " / "/" suffixes after the base are qualifiers.  A bracket group that opens
    the title is part of the base ("(I Can't Get No) Satisfaction", "(Untitled)").  Unbalanced
    brackets are tolerated: an unclosed group runs to the end, a stray closer is ignored.
    """
    base_pieces: List[str] = []
    qualifiers: List[str] = []
    after_break = False

    def emit_plain(piece: str):
        if piece.strip():
            (qualifiers if after_break else base_pieces).append(piece)

    def emit_group(piece: str):
        nonlocal after_break
        if not base_pieces and not after_break:
            base_pieces.append(piece)
        else:
            qualifiers.append(piece)
        after_break = True

    depth = 0
    plain_start = group_start = 0
    length = len(text)
    i = 0
    while i < length:
        char = text[i]
        if depth == 0:
            if char in "([":
                emit_plain(text[plain_start:i])
                depth, group_start = 1, i + 1
            elif char == "/":
                # Between the titled parts of a medley: the same as " - "
                emit_plain(text[plain_start:i])
                after_break = True
                plain_start = i + 1
            elif char == " " and i + 2 < length and text[i + 1] in "-–—" and text[i + 2] == " ":
                emit_plain(text[plain_start:i])
                after_break = True
                plain_start = i = i + 3
                continue
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
            if depth == 0:
                emit_group(text[group_start:i])
                plain_start = i + 1
        i += 1
    if depth > 0:
        emit_group(text[group_start:])
    else:
        emit_plain(text[plain_start:])
    return " ".join(base_pieces), qualifiers


def _strip_base_feat(base_text: str) -> str:
    """Drop an unbracketed "feat. X" clause (never from the first word, never a bare "feat")."""
    words = base_text.split()
    for index in range(1, len(words) - 1):
        if words[index].lower().rstrip(",:") in _BASE_FEAT_WORDS:
            return " ".join(words[:index])
    return base_text


@dataclass(frozen=True)
class _ParsedTitle:
    base: tuple  # canonical base tokens
    identity_qualifiers: frozenset  # canonical qualifiers that name the recording
    all_qualifiers: tuple  # every non-feat qualifier in order, neutral ones included


def _canonical_qualifier(words: Sequence[str]) -> str:
    """Canonical text of a qualifier; "Dub Mix" = "Dub" (one other word must remain)."""
    tokens = _canonical_tokens(words, True)
    if len(tokens) == 2 and tokens[1] in _QUALIFIER_TAIL_WORDS:
        tokens = tokens[:1]
    return " ".join(tokens)


def _parse_title(title: Optional[str]) -> _ParsedTitle:
    text = (title or "").strip()
    base_text, qualifier_texts = _split_title(text)
    base_words = _squash(_strip_base_feat(base_text)).split()
    # A leading "The" is dropped (before numerals are read, so "The I Walk" keeps its "I"), unless
    # what follows is another "the": "The The" must not become the one-word title "The"
    if len(base_words) > 1 and base_words[0] == "the" and base_words[1] != "the":
        base_words = base_words[1:]
    base = _canonical_tokens(base_words, False)
    identity, everything = set(), []

    def add_qualifier(canonical: str, neutral: bool):
        everything.append(canonical)
        if not neutral:
            identity.add(canonical)

    if len(base) > 2 and base[-2] == "part" and base[-1].isdigit():
        # "Song, Part 1" / "Song Pt. 1" name the same part as "Song (Part 1)"
        add_qualifier(" ".join(base[-2:]), False)
        base = base[:-2]
    for qualifier_text in qualifier_texts:
        words = _squash(qualifier_text).split()
        if not words or words[0] in _FEAT_WORDS:
            continue  # empty, or only a featured-artist credit
        add_qualifier(_canonical_qualifier(words), bool(_NEUTRAL_QUALIFIER_RE.fullmatch(" ".join(words))))
    if not base:
        # Nothing but qualifiers (or credits): compare the whole text as the base.
        whole = _canonical_tokens(_squash(text).split(), False)
        return _ParsedTitle(tuple(whole), frozenset(), ())
    return _ParsedTitle(tuple(base), frozenset(identity), tuple(everything))


def full_title_key(title: Optional[str]) -> str:
    """Comparable form that keeps every qualifier ("(Part 1)", "(Live)", "(Remastered)" ...), in
    sorted order; only featured-artist credits are dropped."""
    parsed = _parse_title(title)
    return " ".join(parsed.base + tuple(sorted(parsed.all_qualifiers)))


def normalize_title(title: Optional[str]) -> str:
    """Comparable form of a track title: the base plus its identity-bearing qualifiers (sorted).

    Featured-artist credits and identity-neutral qualifiers ("(Remastered 2011)", "- 2011
    Remaster", "(Album Version)", "(Stereo)", a bare year, "(Original)") are dropped;
    "(Part 1)", "[Live]", mixes and edits stay.  Numbers are canonical ("Pt. 2" = "Part Two").
    """
    parsed = _parse_title(title)
    return " ".join(parsed.base + tuple(sorted(parsed.identity_qualifiers)))


def parse_duration(value) -> Optional[int]:
    """"m:ss" / "h:mm:ss" -> whole seconds, or None when unparseable/zero."""
    if not value or not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    seconds = nums[-1] + nums[-2] * 60 + (nums[0] * 3600 if len(nums) == 3 else 0)
    return seconds or None


def format_duration(seconds: int) -> str:
    """Whole seconds -> "m:ss" (minutes may exceed 59)."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def _field(track, *names):
    for n in names:
        v = track.get(n) if isinstance(track, dict) else getattr(track, n, None)
        if v is not None:
            return v
    return None


@dataclass(frozen=True)
class TrackMatch:
    target_index: int
    donor_index: int
    ratio: float  # always 1.0 (exact matching only); kept for compatibility
    length: Optional[str]  # normalized "m:ss"; None when the donor track has no parseable length


def match_tracklists(target_titles: Sequence[str], donor_tracks: Sequence) -> List[TrackMatch]:
    """One-to-one match of target titles to donor tracks by exact comparable title, two passes.

    (0) ``full_title_key`` equal: every qualifier kept, so "(Stereo)" / "(Mono)" or "(Remastered)"
    pair with their own kind even when the donor lists them in another order.  (1) the canonical
    key with identity-neutral qualifiers (remaster tags, a bare year, album version, stereo/mono)
    stripped, for the targets and donors pass 0 left unclaimed.  There is no fuzzy pass (see the
    module comment).  Each track is used at most once; equal candidates prefer the donor index
    nearest the target's own index.  Targets without a match are absent; every match has
    ``ratio`` 1.0.
    """
    donors = [_parse_title(_field(t, "title") or "") for t in donor_tracks]
    targets = [_parse_title(t) for t in target_titles]
    claimed_donors = set()
    matched_targets = {}  # target index -> donor index

    def run_pass(same):
        for i in range(len(target_titles)):
            if i in matched_targets or not targets[i].base:
                continue
            best = None
            for j in range(len(donor_tracks)):
                if j in claimed_donors or not same(targets[i], donors[j]):
                    continue
                key = (-abs(j - i), -j)
                if best is None or key > best[0]:
                    best = (key, j)
            if best is not None:
                claimed_donors.add(best[1])
                matched_targets[i] = best[1]

    def same_full_key(a, b):
        return a.base + tuple(sorted(a.all_qualifiers)) == b.base + tuple(sorted(b.all_qualifiers))

    def same_identity(a, b):
        return a.base == b.base and a.identity_qualifiers == b.identity_qualifiers

    run_pass(same_full_key)
    run_pass(same_identity)

    matches: List[TrackMatch] = []
    for i in sorted(matched_targets):
        j = matched_targets[i]
        secs = parse_duration(_field(donor_tracks[j], "duration_str", "duration"))
        matches.append(TrackMatch(i, j, 1.0, format_duration(secs) if secs else None))
    return matches


def coverage(matches: Sequence[TrackMatch]) -> int:
    """Number of matches that carry a parseable length."""
    return sum(1 for m in matches if m.length)
