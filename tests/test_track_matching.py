"""Pure title normalization / tracklist matching for borrowed lengths."""

from __future__ import annotations

from track_matching import match_tracklists, normalize_title


class T:
    def __init__(self, title, duration=""):
        self.title = title
        self.duration_str = duration


def test_normalize_basic_and_punctuation():
    # Requirement changed: apostrophes are dropped, not turned into a space, so "Don't" == "Dont"
    # and "I'm" is not read as the numeral "I" plus "m".
    assert normalize_title("  Don't Stop!  ") == "dont stop"
    assert normalize_title("Rock & Roll") == "rock and roll"


def test_normalize_diacritics():
    assert normalize_title("Café del Mar") == normalize_title("Cafe Del Mar")
    assert normalize_title("Björk") == "bjork"


def test_normalize_strips_only_identity_neutral_qualifiers():
    # Allowlisted qualifiers (remaster tags, bare year, album/LP version, stereo/mono, "original")
    # do not change which recording a title names, so they are stripped.
    assert normalize_title("Heroes (Remastered 2011)") == "heroes"
    assert normalize_title("Heroes (Album Version) [Remastered]") == "heroes"
    assert normalize_title("Heroes - 2011 Remaster") == "heroes"
    assert normalize_title("Heroes (1977)") == "heroes"
    assert normalize_title("Heroes (Stereo)") == "heroes"
    assert normalize_title("Heroes (Original)") == "heroes"
    # Requirement changed: "(Part 1)", "[Live]", mixes etc. name a different recording, so they stay
    # (this test used to assert that "(Part 1)" and "[Live]" were stripped).
    assert normalize_title("Heroes [Live]") == "heroes live"
    assert normalize_title("Heroes (Part 1) (Remastered)") == "heroes part 1"
    # Requirement changed: a trailing "version"/"mix" word inside a qualifier is dropped when one
    # other word remains ("Extended Version" = "Extended"), so this is "blue monday extended".
    assert normalize_title("Blue Monday (Extended Version)") == "blue monday extended"


def test_normalize_keeps_inner_parenthetical_and_all_paren_titles():
    assert normalize_title("(I Can't Get No) Satisfaction") == "i cant get no satisfaction"
    assert normalize_title("(Untitled)") == "untitled"


def test_normalize_feat():
    assert normalize_title("Song (feat. Someone)") == "song"
    # Requirement changed: an unbracketed "ft." is no longer stripped ("Six Ft. Deep" is a title);
    # "feat." and bracketed credits are.
    assert normalize_title("Song feat. Someone Else") == "song"
    assert normalize_title("Song [ft. Someone Else]") == "song"
    assert normalize_title("Six Ft. Deep") == "6 ft deep"
    assert normalize_title("Song featuring X & Y") == "song"
    assert normalize_title(None) == ""


def test_match_exact_with_lengths():
    m = match_tracklists(["One", "Two"], [T("One", "3:00"), T("Two", "4:05")])
    assert [(x.target_index, x.donor_index, x.length) for x in m] == [(0, 0, "3:00"), (1, 1, "4:05")]


def test_match_remaster_and_feat_suffixes():
    m = match_tracklists(["Heroes", "Fame"], [T("Heroes (Remastered 2017)", "6:07"), T("Fame feat. X", "3:30")])
    assert len(m) == 2 and all(x.length for x in m)


def test_match_diacritics():
    # Requirement changed: no fuzzy pass any more, so the "Colour"/"Color" spelling variant this
    # test used to accept now lives in MUST_NOT_MATCH; diacritics are still folded exactly.
    m = match_tracklists(["Cafe del Mar"], [T("Café del Mar", "7:00")])
    assert len(m) == 1


def test_match_extra_donor_bonus_tracks():
    donor = [T("One", "1:00"), T("Bonus", "9:00"), T("Two", "2:00"), T("Bonus 2", "3:00")]
    m = match_tracklists(["One", "Two"], donor)
    assert [(x.target_index, x.donor_index) for x in m] == [(0, 0), (1, 2)]


def test_match_swapped_order():
    m = match_tracklists(["One", "Two", "Three"], [T("Two", "2:00"), T("One", "1:00"), T("Three", "3:00")])
    got = {x.target_index: x.length for x in m}
    assert got == {0: "1:00", 1: "2:00", 2: "3:00"}


def test_match_no_match():
    assert match_tracklists(["Alpha"], [T("Completely Different", "1:00")]) == []
    assert match_tracklists([], [T("A", "1:00")]) == []
    assert match_tracklists(["Alpha"], []) == []


def test_match_is_one_to_one_and_prefers_nearest_on_ties():
    donor = [T("Intro", "0:30"), T("Mid", "3:00"), T("Intro", "0:45")]
    m = match_tracklists(["Mid", "Intro", "Intro"], donor)
    by_t = {x.target_index: x for x in m}
    assert by_t[1].donor_index == 0  # tie between 0 and 2: nearest to index 1 (both 1 away) -> lower
    assert by_t[2].donor_index == 2
    assert len({x.donor_index for x in m}) == len(m)


def test_partial_lengths_only_counted_when_parseable():
    donor = [T("One", "3:00"), T("Two", ""), T("Three", "n/a")]
    m = match_tracklists(["One", "Two", "Three"], donor)
    assert len(m) == 3
    assert sum(1 for x in m if x.length) == 1
    assert [x.length for x in m] == ["3:00", None, None]


def test_accepts_dicts_and_normalizes_length():
    m = match_tracklists(["One"], [{"title": "One", "duration": "03:05"}])
    assert m[0].length == "3:05"


def _pairs(m):
    return {x.target_index: x.donor_index for x in m}


def test_different_mix_is_not_a_match():
    # A master groups the 7" and the 12": the extended mix's length must not be borrowed.
    assert match_tracklists(["Blue Monday"], [T("Blue Monday (Extended Version)", "7:29")]) == []
    assert match_tracklists(["Song"], [T("Song (Instrumental)", "3:00")]) == []
    assert match_tracklists(["Song (Live)"], [T("Song", "3:00")]) == []


def test_parts_with_same_base_title_match_exactly_not_swapped():
    donor = [T("Song (Part 2)", "2:00"), T("Song (Part 1)", "1:00")]
    m = match_tracklists(["Song (Part 1)", "Song (Part 2)"], donor)
    assert {x.target_index: x.length for x in m} == {0: "1:00", 1: "2:00"}


def test_base_and_instrumental_not_swapped():
    donor = [T("X (Instrumental)", "2:00"), T("X", "3:00")]
    m = match_tracklists(["X", "X (Instrumental)"], donor)
    assert {x.target_index: x.length for x in m} == {0: "3:00", 1: "2:00"}


def test_missing_roman_numeral_movement_does_not_take_a_neighbour():
    donor = [T("Movement II", "2:00"), T("Movement III", "3:00")]
    m = match_tracklists(["Movement I", "Movement II", "Movement III"], donor)
    assert {x.target_index: x.length for x in m} == {1: "2:00", 2: "3:00"}


def test_numbered_works_do_not_cross_match():
    assert match_tracklists(["Sonata No. 1 in C"], [T("Sonata No. 2 in C", "9:00")]) == []


def test_full_key_pass_beats_the_neutral_stripped_pass():
    # Replaces test_exact_pass_beats_earlier_fuzzy_claim (no fuzzy pass left; same intent: the more
    # exact pairing wins a donor).  Stripped of "(Remastered)" both targets would claim donor 0 first.
    donor = [T("Glass River", "2:00"), T("Glass River (Remastered)", "4:00")]
    m = match_tracklists(["Glass River (Remastered)", "Glass River"], donor)
    assert {x.target_index: x.length for x in m} == {0: "4:00", 1: "2:00"}


def test_neutral_qualifiers_inside_one_release_stay_paired_in_any_order():
    donor = [T("Glass River (Mono)", "3:00"), T("Glass River (Stereo)", "3:01")]
    m = match_tracklists(["Glass River (Stereo)", "Glass River (Mono)"], donor)
    assert {x.target_index: x.length for x in m} == {0: "3:01", 1: "3:00"}


def test_pair_from_the_full_key_pass_is_not_reconsidered():
    donor = [T("Glass River (2011 Remaster)", "1:00"), T("Glass River", "2:00")]
    m = match_tracklists(["Glass River (Remastered)", "Glass River"], donor)
    # The bare title claims the bare donor in pass 0; the remastered one takes what is left.
    assert {x.target_index: x.length for x in m} == {0: "1:00", 1: "2:00"}


def test_qualifier_pass_matches_remaster_tags_both_ways():
    m = match_tracklists(["Heroes (Remastered)", "Fame"], [T("Heroes", "6:07"), T("Fame - 2016 Remaster", "3:30")])
    assert _pairs(m) == {0: 0, 1: 1}


# --- structural matching (base + qualifiers) ---------------------------------------------------

import time

import pytest

# (target, donor): different recordings or works; borrowing the donor's length would be wrong.
MUST_NOT_MATCH = [
    # Dropping a leading "The" must not turn "The The" into the one-word title "The"
    ("The The", "The"),
    # A lone letter naming a different piece: "Track A" / "Track B" was 0.86, above the threshold
    ("Track A", "Track B"),
    ("Theme A", "Theme B"),
    ("Untitled A", "Untitled B"),
    ("Track (feat. MC) (Extended Mix)", "Track"),
    ("Track feat. MC (Live)", "Track"),
    ("Six Ft. Deep", "Six"),
    ("No Mean Feat", "No Mean"),
    ("Everybody Wants To Rule The World", "Everybody Wants To Rule The World (Extended)"),
    ("Everybody Wants To Rule The World", "Everybody Wants To Rule The World (Remixed)"),
    ("Everybody Wants To Rule The World", "Everybody Wants To Rule The World (Edited)"),
    ("Everybody Wants To Rule The World", "Everybody Wants To Rule The World (Single)"),
    ("A Very Long Song Title Indeed Yes", "A Very Long Song Title Indeed Yes (Acapella)"),
    ("Shine On You Crazy Diamond (Part One)", "Shine On You Crazy Diamond (Part Two)"),
    ("Symphony No. 5, First Movement", "Symphony No. 5, Third Movement"),
    ("Prelude In C Major", "Prelude In C Minor"),
    ("Prelude In C Major", "Prelude In D Major"),
    ("Nocturne In C Sharp Minor", "Nocturne In C Minor"),
    ("Sonata No. 1 in C", "Sonata No. 2 in C"),
    ("Song (Part 1)", "Song (Part 2)"),
    # No fuzzy matching: near-misses the old fuzzy pass was tuned around are all misses now.
    ("Colour Me Bad", "Color Me Bad"),   # moved from MUST_MATCH: a spelling variant, not canonicalised
    ("X-Ray", "Xray"),                    # moved from MUST_MATCH: split vs joined word
    ("Glass Concerto: Allegro", "Glass Concerto: Allegretto"),
    ("Glass Concerto: Allegro", "Glass Concerto"),
    ("Glass Concerto, Allegro", "Glass Concerto"),
    ("The Long River Of Quiet Glass Lamps", "The Long River Of Quiet Glass Lights"),
    ("Glass River Dub", "Glass River"),
    ("Glass River Live", "Glass River"),
    ("Glass River", "Glass River (Live)"),
    ("Glass River (Club Mix)", "Glass River (Dub Mix)"),
    ("Glass River (Dub)", "Glass River (Extended)"),
    ("Etude in C#", "Etude in C"),
    ("Etude in C", "Etude in G"),
    ("Glass River Part 1", "Glass River Part 2"),
    ("Twenty-Two Lamps", "21 Lamps"),
    ("The Glass River", "The Glass Rivers"),
    ("Glass River / Night Signal", "Glass River / Night Lamp"),
    ("Movement XXI", "Movement 21"),
    # Letters of any script count: these used to squash to nothing and then both read as "live".
    ("Стеклянная река (Live)", "Ночной сигнал (Live)"),
    ("硝子の川 (Live)", "夜の信号 (Live)"),
    ("Γυάλινος ποταμός (Live)", "Νυχτερινό σήμα (Live)"),
    ("ガラス", "カラス"),                   # dakuten kept: glass vs crow
]

# (target, donor): same recording written differently; the length may be borrowed.
MUST_MATCH = [
    ("Song Pt. 2", "Song Part 2"),
    ("Song (Pt. 2)", "Song (Part Two)"),
    ("Symphony No. 5, First Movement", "Symphony Number 5, 1st Movement"),
    ("Symphony No. 5, Movement II", "Symphony No. 5, Movement 2"),
    ("I'm Waiting For The Man", "Im Waiting For The Man"),
    ("V-2 Schneider", "V2 Schneider"),
    ("Song (feat. MC)", "Song"),
    ("Song [ft. MC]", "Song"),
    ("Song feat. MC", "Song"),
    ("Song (feat. MC) (Live)", "Song (Live)"),
    ("Song (Live) (Part 1)", "Song (Part 1) (Live)"),
    ("Heroes", "Heroes (2011 Digital Remaster)"),
    ("Heroes (Remastered)", "Heroes - 2011 Remaster"),
    ("Rock 'N' Roll", "Rock And Roll"),
    ("Don't", "Dont"),
    ("Cafe del Mar & Co", "Café del Mar and Co"),
    ("Six Ft. Deep", "Six Ft. Deep"),
    ("No Mean Feat", "No Mean Feat"),
    ("Prelude In C Major", "Prelude in C major"),
    # Unicode-aware squashing
    ("Стеклянная река (Live)", "СТЕКЛЯННАЯ РЕКА [live]"),
    ("硝子の川 (Live)", "硝子の川 (Live)"),
    ("Γυάλινος ποταμός", "ΓΥΑΛΙΝΟΣ ΠΟΤΑΜΟΣ"),
    ("Strasse Lamp", "Straße Lamp"),
    ("Soren Lamp", "Søren Lamp"),
    ("Lodz Lamp", "Łódź Lamp"),
    ("Thor Lamp", "Þor Lamp"),
    ("Dragan Lamp", "Đragan Lamp"),
    ("Aegir Lamp", "Ægir Lamp"),
    ("Coeur Lamp", "Cœur Lamp"),
    # Cheap safe equivalences
    ("Glass River Pt.1", "Glass River Part I"),
    ("Glass River Pt. 1", "Glass River, Part 1"),
    ("Glass River, Part 1", "Glass River - Part 1"),
    ("Glass River - Part 1", "Glass River (Part 1)"),
    ("Glass River (Part 1)", "Glass River Part One"),
    ("Glass River #1", "Glass River 1"),
    ("Etude in C#", "Etude in C Sharp"),
    ("Glass River / Night Signal", "Glass River - Night Signal"),
    ("Glass River (Instrumental)", "Glass River (Instrumental Version)"),
    ("Glass River (Dub)", "Glass River (Dub Mix)"),
    ("Glass River (Extended)", "Glass River (Extended Version)"),
    ("Glass River (Demo)", "Glass River (Demo Version)"),
    ("Twenty-One Lamps", "21 Lamps"),
    ("The Glass River", "Glass River"),
    ("The I Walk Alone", "I Walk Alone"),   # "I" after a dropped "The" is still a word, not a numeral
    ("Glass River (Live) Pt. 2", "Glass River (Part 2) (Live)"),
]


@pytest.mark.parametrize("target,donor", MUST_NOT_MATCH)
def test_structural_must_not_match(target, donor):
    assert match_tracklists([target], [T(donor, "3:00")]) == []
    assert match_tracklists([donor], [T(target, "3:00")]) == []


@pytest.mark.parametrize("target,donor", MUST_MATCH)
def test_structural_must_match(target, donor):
    assert _pairs(match_tracklists([target], [T(donor, "3:00")])) == {0: 0}
    assert _pairs(match_tracklists([donor], [T(target, "3:00")])) == {0: 0}


def test_duplicate_untitled_titles_pair_in_order():
    m = match_tracklists(["Untitled", "Untitled"], [T("Untitled", "1:00"), T("Untitled", "2:00")])
    assert {x.target_index: x.length for x in m} == {0: "1:00", 1: "2:00"}


def test_normalization_is_linear_time_on_huge_titles():
    started = time.perf_counter()
    for title in ("(" * 50000, "feat. " * 8000, "a (b) - " * 6000, "x" * 50000, " " * 50000 + "-"):
        normalize_title(title)
    match_tracklists(["a (b) " * 8000], [T("a (b) " * 8000, "1:00")])
    assert time.perf_counter() - started < 2.0


# --- repeated titles pair k-th with k-th, only when both lists repeat them equally often ---------

def test_repeated_title_does_not_cross_when_donor_has_an_extra_leading_track():
    # Review example: the first Interlude must not take the donor's 1:45 one.
    donor = [T("Interlude", "0:20"), T("A", "3:00"), T("B", "3:00"), T("Interlude", "1:45"), T("C", "3:00")]
    m = match_tracklists(["A", "B", "Interlude", "Interlude", "C"], donor)
    got = {x.target_index: x.length for x in m}
    assert got[2] == "0:20" and got[3] == "1:45"


def test_unequal_repeat_counts_match_none_of_them_but_other_titles_still_match():
    donor = [T("Untitled", "1:00"), T("Untitled", "2:00"), T("Untitled", "3:00"), T("Untitled", "4:00"), T("Real", "5:00")]
    m = match_tracklists(["Untitled", "Untitled", "Untitled", "Real"], donor)
    assert _pairs(m) == {3: 4}


def test_repeat_in_only_one_list_is_ambiguous_and_skipped():
    assert match_tracklists(["Intro"], [T("Intro", "0:10"), T("Intro", "0:20")]) == []
    assert match_tracklists(["Intro", "Intro"], [T("Intro", "0:10")]) == []


def test_repeat_count_ignores_tracks_claimed_by_the_full_key_pass():
    # The plain "Song" pair is made in pass 0; the remaining remastered pair is then unique in pass 1.
    donor = [T("Song", "1:00"), T("Song (2011 Remaster)", "2:00")]
    m = match_tracklists(["Song", "Song (Remastered)"], donor)
    assert {x.target_index: x.length for x in m} == {0: "1:00", 1: "2:00"}


# --- mono and stereo are neutral only against an untagged title ---------------------------------

@pytest.mark.parametrize("target,donor", [
    ("Helter Skelter (Mono)", "Helter Skelter (Stereo)"),
    ("Helter Skelter (Stereo)", "Helter Skelter (Mono)"),
    ("Helter Skelter (Mono Version)", "Helter Skelter (Stereo Version)"),
    ("Helter Skelter - Stereo Version", "Helter Skelter (Mono)"),
])
def test_opposite_channel_tags_never_match(target, donor):
    assert match_tracklists([target], [T(donor, "4:29")]) == []


@pytest.mark.parametrize("target,donor", [
    ("Helter Skelter (Mono)", "Helter Skelter"),
    ("Helter Skelter", "Helter Skelter (Stereo)"),
    ("Helter Skelter (Mono)", "Helter Skelter (Mono Version)"),
    ("Helter Skelter (Stereo)", "Helter Skelter - Stereo"),
])
def test_channel_tag_matches_untagged_or_same_tag(target, donor):
    assert _pairs(match_tracklists([target], [T(donor, "4:29")])) == {0: 0}


# --- review round 2: mixes are not channel tags; twins; pass interplay -----------------------------

@pytest.mark.parametrize("target,donor", [
    ("Song (Mono Mix)", "Song"),
    ("Song (Stereo Mix)", "Song"),
    ("Song", "Song (Stereo Mix)"),
])
def test_mono_or_stereo_mix_is_an_identity_qualifier_not_a_channel_tag(target, donor):
    assert match_tracklists([target], [T(donor, "3:00")]) == []


def test_mono_mix_still_pairs_with_the_same_mix():
    assert _pairs(match_tracklists(["Song (Mono Mix)"], [T("Song (Mono Mix)", "3:00")])) == {0: 0}


def test_channel_incompatible_twin_voids_the_whole_repeated_group():
    donor = [T("Song (Mono) (Remastered)", "3:00"), T("Song (Stereo)", "3:01")]
    assert match_tracklists(["Song", "Song (Mono)"], donor) == []


def test_full_key_pairs_may_not_be_crossed_by_the_remainder():
    # (a) t0<->d0 and t2<->d1 are full-key pairs; the remainder t1<->d2 would cross t2<->d1
    donor = [T("Untitled", "1:00"), T("Untitled", "2:00"), T("Untitled (2011)", "3:00")]
    m = match_tracklists(["Untitled", "Untitled (Remastered)", "Untitled"], donor)
    assert _pairs(m) == {0: 0, 2: 1}


def test_identity_group_with_different_counts_is_skipped_in_every_pass():
    # (b) three bare "Untitled" targets (incl. the remastered one) against four: nothing pairs
    donor = [T("Untitled", "1:00"), T("Untitled", "2:00"), T("Untitled", "3:00"), T("Untitled (Remastered)", "4:00")]
    m = match_tracklists(["Untitled", "Untitled", "Untitled (Remastered)"], donor)
    assert m == []


def test_remainder_pairs_in_list_order_when_nothing_crosses():
    donor = [T("Untitled (2011)", "1:00"), T("Untitled", "2:00")]
    m = match_tracklists(["Untitled (Remastered)", "Untitled"], donor)
    assert _pairs(m) == {0: 0, 1: 1}


def test_full_key_pass_needs_equal_full_key_counts():
    # "S (Mono)" is 2 vs 1 and "S" 1 vs 2: pass 0 pairs neither; the remainder pairs in order
    donor = [T("S (Mono)", "1:00"), T("S", "2:00"), T("S", "3:00")]
    m = match_tracklists(["S (Mono)", "S (Mono)", "S"], donor)
    assert _pairs(m) == {0: 0, 1: 1, 2: 2}


# --- an untagged title next to a tagged one of the same list is the opposite channel --------------

def _lengths_by_target(target_titles, donor_titles_and_lengths):
    donors = [T(title, length) for title, length in donor_titles_and_lengths]
    return {m.target_index: m.length for m in match_tracklists(target_titles, donors)}


def test_untagged_title_next_to_mono_is_stereo_so_mono_gets_the_untagged_donor_length():
    # The donor's untagged "Song" sits beside "(Stereo)", so it is the mono one: it belongs to
    # the target's "(Mono)".  Before, the untagged pair formed first and the target's "Song" got
    # the mono 3:00.
    assert _lengths_by_target(["Song (Mono)", "Song"], [("Song", "3:00"), ("Song (Stereo)", "3:30")]) == {0: "3:00", 1: "3:30"}


def test_mirrored_case_untagged_target_is_the_mono_partner_of_stereo():
    assert _lengths_by_target(["Song", "Song (Stereo)"], [("Song (Mono)", "3:00"), ("Song", "3:30")]) == {0: "3:00", 1: "3:30"}


def test_a_lone_untagged_target_against_a_mono_stereo_pair_stays_unmatched():
    # Today's behaviour (kept): the group sizes differ (1 against 2), so nothing is paired.
    assert _lengths_by_target(["Song"], [("Song", "3:00"), ("Song (Stereo)", "3:30")]) == {}


def test_implied_opposite_channels_are_refused_even_for_equal_untagged_titles():
    # target: Song = stereo (beside Mono); donor: Song = mono (beside Stereo).  Nothing may pair.
    assert _lengths_by_target(["Song", "Song (Mono)"], [("Song", "3:00"), ("Song (Stereo)", "3:30")]) == {}


def test_a_crowded_title_implies_nothing_for_the_untagged_one():
    # "Song" beside two tags (or two tagged copies) has no implied channel: today's pairing stays
    assert _lengths_by_target(["Song (Mono)", "Song (Stereo)", "Song"],
                              [("Song (Mono)", "3:00"), ("Song (Stereo)", "3:30"), ("Song", "4:00")]) == {0: "3:00", 1: "3:30", 2: "4:00"}


def test_list_without_any_channel_tag_still_pairs_untagged_titles():
    assert _lengths_by_target(["Song"], [("Song", "3:00")]) == {0: "3:00"}
