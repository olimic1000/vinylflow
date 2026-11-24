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
# a bare year, album/LP version, a single-track "original".  Stereo/mono tags are neutral too, but
# only against an untagged title (see ``_ParsedTitle.channel``), so they are not listed here.
_NEUTRAL_QUALIFIER_RE = re.compile(
    r"(?:\d{4} )?(?:digital(?:ly)? )?remaster(?:ed)?(?: (?:version|edition))?(?: \d{4})?"
    r"|\d{4}"
    r"|(?:album|lp) version"
    r"|original"
)
_CHANNEL_TAGS = frozenset({"mono", "stereo"})
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
    channel: str = ""  # "mono" / "stereo" when tagged: compatible with no tag, never the opposite tag


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
    channel = ""

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
        canonical = _canonical_qualifier(words)
        # Decided from the raw words: "(Mono Mix)" / "(Stereo Mix)" may be a different mix, so they
        # stay identity qualifiers (a safe miss); only "mono"/"stereo" [+ "version"] are channel tags
        if words[0] in _CHANNEL_TAGS and words[1:] in ([], ["version"]):
            channel = words[0]
            add_qualifier(canonical, True)
        else:
            add_qualifier(canonical, bool(_NEUTRAL_QUALIFIER_RE.fullmatch(" ".join(words))))
    if not base:
        # Nothing but qualifiers (or credits): compare the whole text as the base.
        whole = _canonical_tokens(_squash(text).split(), False)
        return _ParsedTitle(tuple(whole), frozenset(), ())
    return _ParsedTitle(tuple(base), frozenset(identity), tuple(everything), channel)


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
    length: Optional[str]  # normalized "m:ss"; None when the donor track has no parseable length


_OPPOSITE_CHANNEL = {"mono": "stereo", "stereo": "mono"}


def _effective_channels(parsed_titles: Sequence[_ParsedTitle]) -> List[str]:
    """Per title: its channel tag, or the channel IMPLIED by the same list.

    When a list holds a base title (same identity key) exactly twice, once tagged and once not,
    the untagged entry is the opposite channel ("Song (Mono)" next to "Song" means "Song" is the
    stereo one).  Anything more crowded (a title tagged twice, or two untagged copies) or a title
    nobody tags implies nothing and stays "" (no channel information): with more entries the
    reading is a guess, and a wrong pairing moves audio cuts.
    """
    members = {}
    for index, parsed in enumerate(parsed_titles):
        members.setdefault((parsed.base, parsed.identity_qualifiers), []).append(index)
    channels = [parsed.channel for parsed in parsed_titles]
    for indexes in members.values():
        if len(indexes) == 2:
            first, second = indexes
            for tagged, untagged in ((first, second), (second, first)):
                if channels[tagged] and not parsed_titles[untagged].channel:
                    channels[untagged] = _OPPOSITE_CHANNEL[channels[tagged]]
    return channels


def match_tracklists(target_titles: Sequence[str], donor_tracks: Sequence) -> List[TrackMatch]:
    """One-to-one match of target titles to donor tracks by exact comparable title.

    Tracks are first grouped by *identity key* (base plus identity-bearing qualifiers; remaster
    tags, a bare year, album version and mono/stereo tags are neutral).  A group whose size differs
    between the two lists is skipped entirely: a title repeated ("Interlude" twice, several
    "Untitled") is only paired when both lists repeat it equally often, so a vinyl that left one
    out never gets another track's length.  Unique keys (1 against 1) are the common case of this.

    Inside an equal-sized group, two passes in list order:
    (0) equal ``full_title_key`` (every qualifier kept, so "(Stereo)" / "(Remastered)" pair with
    their own kind in any order), the k-th target with the k-th donor, only when both have the
    same number of that full key; (1) the remainder, k-th with k-th, which is refused (the whole
    remainder stays unmatched) if any pair would cross a pass-0 pair of the group (order must be
    preserved) or join opposite mono/stereo channels (mono/stereo is neutral only against an
    untagged title).  A channel is a tag or, for an untagged title next to a tagged one of the
    same base in its own list, the implied opposite (``_effective_channels``): ``[Song (Mono),
    Song]`` against ``[Song, Song (Stereo)]`` pairs Mono with the first and Song with the second.
    Pairs of opposite channels, implied ones included, are dropped from pass 0 as well.  There is
    no fuzzy pass (see the module comment).  Targets without a match are absent.
    """
    donors = [_parse_title(_field(t, "title") or "") for t in donor_tracks]
    targets = [_parse_title(t) for t in target_titles]
    donor_channels = _effective_channels(donors)
    target_channels = _effective_channels(targets)

    def channels_clash(target_index, donor_index):
        return bool(target_channels[target_index] and donor_channels[donor_index]
                    and target_channels[target_index] != donor_channels[donor_index])

    def identity_key(parsed):
        return parsed.base, parsed.identity_qualifiers

    def full_key(parsed):
        return parsed.base + tuple(sorted(parsed.all_qualifiers))

    def group_by(indexes, parsed_list, key_of):
        groups = {}
        for index in indexes:
            if parsed_list[index].base:
                groups.setdefault(key_of(parsed_list[index]), []).append(index)
        return groups

    matched_targets = {}  # target index -> donor index
    donor_groups = group_by(range(len(donors)), donors, identity_key)
    for key, target_indexes in group_by(range(len(targets)), targets, identity_key).items():
        donor_indexes = donor_groups.get(key, [])
        if len(target_indexes) != len(donor_indexes):
            continue
        pairs = {}  # this group's pairs: target index -> donor index
        donors_by_full_key = group_by(donor_indexes, donors, full_key)
        for full, full_targets in group_by(target_indexes, targets, full_key).items():
            full_donors = donors_by_full_key.get(full, [])
            if len(full_targets) == len(full_donors):
                pairs.update((i, j) for i, j in zip(full_targets, full_donors)
                             if not channels_clash(i, j))
        rest_targets = [i for i in target_indexes if i not in pairs]
        claimed_donors = set(pairs.values())
        rest_donors = [j for j in donor_indexes if j not in claimed_donors]
        rest = list(zip(rest_targets, rest_donors))
        compatible = not any(channels_clash(i, j) for i, j in rest)
        crossing = any((i - i2) * (j - j2) < 0 for i, j in rest for i2, j2 in pairs.items())
        if compatible and not crossing:
            pairs.update(rest)
        matched_targets.update(pairs)

    matches: List[TrackMatch] = []
    for i in sorted(matched_targets):
        j = matched_targets[i]
        secs = parse_duration(_field(donor_tracks[j], "duration_str", "duration"))
        matches.append(TrackMatch(i, j, format_duration(secs) if secs else None))
    return matches
