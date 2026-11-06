"""Classical credits: roles, credit track ranges, composer/conductor/ensemble/performer tags.

Releases are built through the real ``DiscogsRelease`` parser from fakes that mimic
discogs_client objects: an ``Artist`` keeps its raw payload in ``.data`` (``name``,
``join``, ``role``, ``tracks``); ``Track.credits`` and ``Release.credits`` are lists of
those (Discogs ``extraartists``).  No Discogs calls anywhere.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from audio_processor import OUTPUT_FORMATS, run_ffmpeg
from metadata_handler import DiscogsRelease, MetadataHandler
from release_credits import (
    Credit,
    classify_credits,
    credit_applies,
    credits_for_track,
    parse_credits,
    performer_text,
    role_base,
    _split_roles,
)


class FakeArtist:
    """discogs_client-style artist/credit: name plus the raw payload under ``.data``."""

    def __init__(self, name, join="", role="", tracks=""):
        self.name = name
        self.data = {"id": 1, "name": name, "anv": "", "join": join, "role": role, "tracks": tracks}


class FakeTrackObj:
    def __init__(self, position, title, artists=(), credits=()):
        self.position, self.title, self.duration = position, title, "3:00"
        self.artists, self.credits = list(artists), list(credits)


class FakeLabel:
    name = "Test Label"


class FakeReleaseObj:
    def __init__(self, artists, tracklist, genres=("Rock",), credits=(), year=1985):
        self.id, self.title, self.year = 7, "Test Album", year
        self.artists, self.tracklist, self.genres = list(artists), list(tracklist), list(genres)
        self.credits = list(credits)
        self.labels, self.formats, self.images = [FakeLabel()], [{"name": "Vinyl"}], []


class FakeTrack:
    def __init__(self, vinyl_number):
        self.vinyl_number = vinyl_number


def handler():
    return MetadataHandler(discogs_token="", user_agent="")


def tags_for(release, position):
    return handler()._build_tag_set(FakeTrack(position), release, None)


def classical(release_credits=(), tracks=None, artist="Beethoven"):
    return DiscogsRelease(FakeReleaseObj(
        [FakeArtist(artist)],
        tracks or [FakeTrackObj(p, f"Mvt {p}") for p in ("A1", "A2", "B1", "B2")],
        genres=["Classical"], credits=release_credits,
    ))


# --- role parsing -----------------------------------------------------------

def test_roles_split_on_commas_outside_brackets():
    assert _split_roles("Conductor, Liner Notes") == ["Conductor", "Liner Notes"]
    assert _split_roles("Piano [Steinway, Solo], Celesta") == ["Piano [Steinway, Solo]", "Celesta"]
    assert _split_roles("") == []


def test_role_base_drops_bracketed_modifier():
    assert role_base("Piano [Steinway]") == "piano"
    assert role_base("  Electric   Guitar ") == "electric guitar"


def test_parse_credits_one_credit_per_role_and_cleans_names():
    credits = parse_credits([
        FakeArtist("Karajan (2)", role="Conductor, Liner Notes", tracks="A1"),
        FakeArtist("", role="Piano"),  # empty name dropped
    ])
    assert credits == [Credit("Karajan", "Conductor", "A1"), Credit("Karajan", "Liner Notes", "A1")]
    assert parse_credits(None) == []


def test_modifiers_and_soloist_roles():
    found = classify_credits([
        Credit("Kempff", "Piano [Steinway]"),
        Credit("Perlman", "Soloist"),
        Credit("Perlman", "Violin"),
        Credit("Q", "Principal Oboe"),
    ])
    assert found.performers == (("Kempff", "Piano"), ("Perlman", "Violin"), ("Q", "Principal Oboe"))


@pytest.mark.parametrize("role", ["Soloist, Violin", "Violin, Soloist", "Violin [Solo], Leader"])
def test_a_named_instrument_replaces_the_bare_performer_entry(role):
    """One person, one PERFORMER value: "X (Violin)", not also a bare "X"."""
    credits = parse_credits([FakeArtist("X", role=role)])
    assert classify_credits(credits).performers == (("X", "Violin"),)


def test_a_bare_performer_credit_is_kept_without_an_instrument():
    assert classify_credits([Credit("X", "Leader"), Credit("Y", "Cello")]).performers == (("X", ""), ("Y", "Cello"))


@pytest.mark.parametrize("role, label", [
    ("Vocals [Soprano]", "Soprano"),
    ("Voice [Mezzo-soprano]", "Mezzo-Soprano"),
    ("Vocals [Solo, Baritone]", "Baritone"),
    ("Vocals [Solo]", "Vocals"),
    ("Piano [Steinway]", "Piano"),
    ("Flute [Alto]", "Flute"),  # an alto flute, not a singer
])
def test_a_voice_type_in_brackets_labels_the_singer(role, label):
    assert classify_credits([Credit("N", role)]).performers == (("N", label),)


@pytest.mark.parametrize("role", ["Strings", "Cembalo"])
def test_more_instruments_are_allowed(role):
    assert classify_credits([Credit("N", role)]).performers == (("N", role),)


@pytest.mark.parametrize("role", ["Chorus Master", "Choir Master"])
def test_a_chorus_master_is_a_performer_not_the_conductor(role):
    found = classify_credits([Credit("Pitz", role), Credit("Klemperer", "Conductor")])
    assert found.conductors == ("Klemperer",)
    assert found.performers == (("Pitz", role),)


@pytest.mark.parametrize("role", [
    "Soprano Vocals", "Mezzo-soprano Vocals", "Contralto Vocals", "Countertenor Vocals",
    "Tenor Vocals", "Baritone Vocals", "Bass-Baritone Vocals", "Alto Vocals", "Bass Vocals",
    "Soprano Voice", "Treble Vocals",
])
def test_voice_type_vocals_are_performers(role):
    """Discogs' standard singer credit is "<voice type> Vocals"; every voice type is kept."""
    assert classify_credits([Credit("N", role)]).performers == (("N", role.title()),)


def test_role_allowlist_rejects_production_roles():
    found = classify_credits([
        Credit(n, r) for n, r in [
            ("P", "Producer"), ("E", "Engineer"), ("L", "Liner Notes"), ("D", "Design"),
            ("Ph", "Photography By"), ("M", "Mastered By"), ("C", "Lacquer Cut By"),
            ("R", "Recorded By"), ("G", "Guitar Technician"), ("O", "Orchestra"),
            ("Q", "Electric Guitar"), ("W", "Written-By"), ("K", "Conductor"),
        ]
    ])
    assert found.performers == (("Q", "Electric Guitar"),)
    assert found.ensembles == ("O",)
    assert found.composers == ("W",) and found.conductors == ("K",)


def test_performer_text():
    assert performer_text("Kempff", "Piano") == "Kempff (Piano)"
    assert performer_text("Kempff", "") == "Kempff"


# --- credit track ranges ----------------------------------------------------

POSITIONS = [("A1", "A1"), ("A2", "A2"), ("B1", "B1"), ("B2", "B2")]


@pytest.mark.parametrize("text, covered", [
    ("", ["A1", "A2", "B1", "B2"]),          # no text: every track
    ("A1 to A2", ["A1", "A2"]),
    ("A2 to B1", ["A2", "B1"]),
    ("B1, B2", ["B1", "B2"]),
    ("A1; B2", ["A1", "B2"]),
    ("A1 - A2", ["A1", "A2"]),
    ("a1", ["A1"]),
    ("Z9", []),                              # unresolvable: covers nothing
    ("A1 to Z9", []),
    ("whole side A", []),
])
def test_credit_applies(text, covered):
    assert [p for _, p in POSITIONS if credit_applies(text, p, POSITIONS)] == covered


def test_credit_ranges_use_discogs_raw_positions():
    """Numbered Discogs positions become A1.. on export; ranges still use what Discogs wrote."""
    positions = [("1", "A1"), ("2", "A2"), ("3", "B1")]
    assert credit_applies("2 - 3", "A2", positions) and credit_applies("2 - 3", "B1", positions)
    assert not credit_applies("2 - 3", "A1", positions)
    # The exported (normalised) position matches too, so a lettered range also works.
    assert credit_applies("A1 to A2", "A2", positions) and not credit_applies("A1 to A2", "B1", positions)


def test_lettered_range_over_sub_tracks():
    positions = [("B1a", "B1"), ("B1b", "B1"), ("B2", "B2")]
    assert credit_applies("B1a to B1b", "B1", positions)
    assert not credit_applies("B1a to B1b", "B2", positions)


def test_credit_on_a_track_covers_its_lettered_sub_positions():
    """Plain (non-index) tracks B1a/B1b: a credit written "B1" covers both, never B10."""
    positions = [("B1a", "B1a"), ("B1b", "B1b"), ("B10", "B10")]
    assert credit_applies("B1", "B1a", positions) and credit_applies("B1", "B1b", positions)
    assert not credit_applies("B1", "B10", positions)
    # A bare letter still matches only itself: "A" is not "AA" (repeated-letter positions).
    letters = [("A", "A1"), ("AA", "A2")]
    assert credit_applies("A", "A1", letters) and not credit_applies("A", "A2", letters)


def test_position_unknown_to_release_is_not_covered():
    assert not credit_applies("A1", "C1", POSITIONS)


def test_release_and_track_credits_merge_and_dedupe():
    release_level = [Credit("Karajan", "Conductor"), Credit("Kempff", "Piano", "A1")]
    track_level = [Credit("Karajan", "Conductor"), Credit("Gould", "Piano")]
    chosen = credits_for_track(release_level, track_level, "A2", POSITIONS)
    assert [(c.name, c.role) for c in chosen] == [("Karajan", "Conductor"), ("Gould", "Piano")]


# --- tag set ----------------------------------------------------------------

def test_composer_falls_back_to_release_artist():
    tags = tags_for(classical(), "A1")
    assert tags.composers == ("Beethoven",)
    assert tags.artist == "Beethoven" and tags.album_artist == "Beethoven"
    assert tags.conductors == tags.ensembles == tags.performers == ()


@pytest.mark.parametrize("track_artist", ["Bach", "Maria Callas"])
def test_a_compilation_track_artist_is_never_guessed_as_composer(track_artist):
    """A compilation's track artist is as often the singer as the composer."""
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")],
        [FakeTrackObj("A1", "One", artists=[FakeArtist(track_artist)]), FakeTrackObj("A2", "Two")],
        genres=["Classical"],
    ))
    assert tags_for(release, "A1").composers == ()
    assert tags_for(release, "A2").composers == ()


@pytest.mark.parametrize("role", ["Piano", "Conductor", "Orchestra", "Soprano Vocals"])
def test_composer_fallback_skips_an_artist_with_a_performing_credit(role):
    release = classical([FakeArtist("Glenn Gould", role=role, tracks="A1")], artist="Glenn Gould")
    assert tags_for(release, "A1").composers == ()
    assert tags_for(release, "B1").composers == ()  # the credit is on A1, but it still says who they are


def test_composer_fallback_sees_a_performing_credit_on_a_track():
    release = classical(tracks=[
        FakeTrackObj("A1", "One"),
        FakeTrackObj("A2", "Two", credits=[FakeArtist("Glenn Gould", role="Piano")]),
    ], artist="Glenn Gould")
    assert tags_for(release, "A1").composers == ()


def test_no_composer_is_guessed_once_the_release_credits_one_anywhere():
    release = classical([
        FakeArtist("Johann Sebastian Bach", role="Composed By", tracks="A1 to A2"),
        FakeArtist("Glenn Gould", role="Piano"),
    ], artist="Glenn Gould")
    assert tags_for(release, "A1").composers == ("Johann Sebastian Bach",)
    assert tags_for(release, "B1").composers == ()  # unresolved stays empty
    # A composer credited on one track alone counts too.
    on_a_track = classical(tracks=[
        FakeTrackObj("A1", "One", credits=[FakeArtist("Bach", role="Composed By")]),
        FakeTrackObj("A2", "Two"),
    ], artist="Gould")
    assert tags_for(on_a_track, "A2").composers == ()


def test_composer_fallback_needs_a_single_artist():
    """"Beethoven – Karajan, Berliner Philharmoniker" names performers too: no guess then."""
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Ludwig van Beethoven", join="–"), FakeArtist("Herbert von Karajan", join=","),
         FakeArtist("Berliner Philharmoniker")],
        [FakeTrackObj("A1", "One")], genres=["Classical"],
        credits=[FakeArtist("Herbert von Karajan", role="Conductor")],
    ))
    tags = tags_for(release, "A1")
    assert tags.composers == ()
    assert tags.conductors == ("Herbert von Karajan",)
    # An explicit composer credit still wins over the artist line.
    release.credits = parse_credits([FakeArtist("Ludwig van Beethoven", role="Composed By")])
    assert tags_for(release, "A1").composers == ("Ludwig van Beethoven",)
    # Same rule for a compilation's track artists.
    compilation = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")],
        [FakeTrackObj("A1", "One", artists=[FakeArtist("Bach", join="–"), FakeArtist("Gould")])],
        genres=["Classical"],
    ))
    assert tags_for(compilation, "A1").composers == ()


def test_credits_and_track_ranges():
    release = classical([
        FakeArtist("Ludwig van Beethoven", role="Composed By"),
        FakeArtist("Herbert von Karajan", role="Conductor, Liner Notes"),
        FakeArtist("Berliner Philharmoniker", role="Orchestra"),
        FakeArtist("Maria Callas", role="Soprano Vocals", tracks="A1"),
        FakeArtist("Wilhelm Kempff", role="Piano [Solo]", tracks="A1 to A2"),
        FakeArtist("Pierre Fournier", role="Cello", tracks="B1, B2"),
        FakeArtist("Some Producer", role="Producer"),
        FakeArtist("Some Engineer", role="Recorded By", tracks="A1"),
    ], artist="Ludwig van Beethoven")
    a1, b2 = tags_for(release, "A1"), tags_for(release, "B2")
    assert a1.composers == ("Ludwig van Beethoven",)
    assert a1.conductors == ("Herbert von Karajan",)
    assert a1.ensembles == ("Berliner Philharmoniker",)
    # Ensembles are not soloists; producer and engineer are never tagged.
    assert a1.performers == (("Maria Callas", "Soprano Vocals"), ("Wilhelm Kempff", "Piano"))
    assert a1.artist == "Maria Callas, Wilhelm Kempff, Berliner Philharmoniker, Herbert von Karajan"
    assert ("Pierre Fournier", "Cello") in b2.performers
    assert ("Pierre Fournier", "Cello") not in a1.performers
    assert ("Wilhelm Kempff", "Piano") not in b2.performers
    assert b2.album_artist == "Ludwig van Beethoven"


def test_track_level_credits_and_artist_precedence():
    release = classical(
        [FakeArtist("Karajan", role="Conductor"), FakeArtist("BPO", role="Orchestra")],
        tracks=[FakeTrackObj("A1", "One", credits=[FakeArtist("Gould", role="Piano")]),
                FakeTrackObj("A2", "Two")],
    )
    assert tags_for(release, "A1").artist == "Gould, BPO, Karajan"  # soloists > ensembles > conductor
    assert tags_for(release, "A2").artist == "BPO, Karajan"  # no soloist on this track


def test_artist_is_conductor_alone_when_no_soloist_or_ensemble():
    release = classical([FakeArtist("Karajan", role="Conductor")])
    assert tags_for(release, "A1").artist == "Karajan"


def test_artist_line_stays_when_the_credits_leave_out_a_main_artist():
    """A lone soloist credit must not push the orchestra and conductor out of ARTIST."""
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Berliner Philharmoniker", join=","), FakeArtist("Herbert von Karajan")],
        [FakeTrackObj("A1", "One")], genres=["Classical"],
        credits=[FakeArtist("Michel Schwalbé", role="Violin [Solo]")],
    ))
    tags = tags_for(release, "A1")
    assert tags.artist == "Berliner Philharmoniker, Herbert von Karajan"
    assert tags.performers == (("Michel Schwalbé", "Violin"),)


def test_compilation_track_artist_stays_in_artist():
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")],
        [FakeTrackObj("A1", "One", artists=[FakeArtist("Maria Callas")]), FakeTrackObj("A2", "Two")],
        genres=["Classical"],
        credits=[FakeArtist("La Scala Orchestra", role="Orchestra"), FakeArtist("Serafin", role="Conductor")],
    ))
    assert tags_for(release, "A1").artist == "Maria Callas"
    assert tags_for(release, "A2").artist == "La Scala Orchestra, Serafin"  # no track artist to lose


def test_artist_is_reordered_when_the_credits_cover_every_main_artist():
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Karajan", join=","), FakeArtist("BPO", join=","), FakeArtist("Kempff")],
        [FakeTrackObj("A1", "One")], genres=["Classical"],
        credits=[FakeArtist("Karajan", role="Conductor"), FakeArtist("BPO", role="Orchestra"),
                 FakeArtist("Kempff", role="Piano")],
    ))
    assert tags_for(release, "A1").artist == "Kempff, BPO, Karajan"


def test_a_composer_in_the_artist_line_does_not_keep_it():
    """"Beethoven – Karajan, BPO" with a composer credit: ARTIST is the performers."""
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Beethoven", join="–"), FakeArtist("Karajan", join=","), FakeArtist("BPO")],
        [FakeTrackObj("A1", "One"), FakeTrackObj("B1", "Two")], genres=["Classical"],
        credits=[FakeArtist("Beethoven", role="Composed By", tracks="A1"),
                 FakeArtist("Karajan", role="Conductor"), FakeArtist("BPO", role="Orchestra")],
    ))
    assert tags_for(release, "A1").artist == "BPO, Karajan"
    assert tags_for(release, "B1").artist == "BPO, Karajan"  # credited as composer on A1 only


def test_a_chorus_master_stays_out_of_artist():
    release = classical([FakeArtist("Pitz", role="Chorus Master"), FakeArtist("Klemperer", role="Conductor")])
    assert tags_for(release, "A1").artist == "Klemperer"
    assert tags_for(classical([FakeArtist("Pitz", role="Chorus Master")]), "A1").artist == "Beethoven"


def test_artist_unchanged_without_performers():
    release = classical([FakeArtist("Some Producer", role="Producer")])
    assert tags_for(release, "A1").artist == "Beethoven"


def test_numeric_positions_in_credit_ranges():
    release = classical(
        [FakeArtist("Solo Violin", role="Violin", tracks="2 - 3")],
        tracks=[FakeTrackObj(str(n), f"M{n}") for n in range(1, 5)],
    )
    assert ("Solo Violin", "Violin") in tags_for(release, "A2").performers
    assert tags_for(release, "A1").performers == ()


def test_unresolvable_credit_range_covers_nothing():
    release = classical([FakeArtist("Kempff", role="Piano", tracks="movement three")])
    assert tags_for(release, "A1").performers == ()


def test_non_classical_release_has_no_classical_tags():
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Band")], [FakeTrackObj("A1", "x")],
        credits=[FakeArtist("Pianist", role="Piano"), FakeArtist("W", role="Written-By")],
    ))
    tags = tags_for(release, "A1")
    assert tags.composers == tags.conductors == tags.ensembles == tags.performers == ()
    assert tags.artist == "Band"


def test_genre_match_is_case_insensitive_and_one_of_many():
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("B")], [FakeTrackObj("A1", "x")], genres=["Stage & Screen", "classical"],
    ))
    assert tags_for(release, "A1").composers == ("B",)


# --- tracklist parsing ------------------------------------------------------

def test_parse_reads_genres_release_and_track_credits():
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("B")],
        [FakeTrackObj("A1", "x", credits=[FakeArtist("Gould", role="Piano")])],
        genres=["Classical"], credits=[FakeArtist("Karajan", role="Conductor")],
    ))
    assert release.genres == ["Classical"]
    assert release.credits == [Credit("Karajan", "Conductor")]
    assert release.tracks[0].credits == [Credit("Gould", "Piano")]
    assert release.tracks[0].raw_position == "A1"


def test_parse_without_credits_or_genres_attributes():
    release = DiscogsRelease(SimpleNamespace(
        id=1, title="T", year=1990, artists=[], labels=[], formats=[], images=[],
        tracklist=[SimpleNamespace(position="A1", title="x", duration="1:00")],
    ))
    assert release.genres == [] and release.credits == []
    assert release.tracks[0].credits == []


def test_raw_position_kept_for_every_parse_path():
    numbered = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("B")], [FakeTrackObj("1", "One", credits=[FakeArtist("G", role="Piano")]),
                            FakeTrackObj("2", "Two")],
    ))
    assert [(t.position, t.raw_position) for t in numbered.tracks] == [("A1", "1"), ("B1", "2")]
    assert numbered.tracks[0].credits == [Credit("G", "Piano")]

    repeated = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("B")], [FakeTrackObj("AA", "One", credits=[FakeArtist("G", role="Piano")])],
    ))
    assert (repeated.tracks[0].position, repeated.tracks[0].raw_position) == ("A2", "AA")
    assert repeated.tracks[0].credits == [Credit("G", "Piano")]

    blank = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("B")], [FakeTrackObj("", "Untitled", credits=[FakeArtist("G", role="Piano")])],
    ))
    assert blank.tracks[0].credits == [Credit("G", "Piano")]


def test_umbrella_track_keeps_its_credits_and_release_credits_reach_it():
    suite = SimpleNamespace(
        data={"position": "", "type_": "index", "title": "Suite", "duration": "",
              "sub_tracks": [{"position": "B1a", "title": "I", "duration": "1:00"},
                             {"position": "B1b", "title": "II", "duration": "2:00"}]},
        position="", title="Suite", duration="", artists=[],
        credits=[FakeArtist("Gould", role="Piano")],
    )
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("B")], [FakeTrackObj("A1", "One"), suite],
        genres=["Classical"], credits=[FakeArtist("Kempff", role="Piano", tracks="B1")],
    ))
    umbrella = release.tracks[-1]
    assert umbrella.position == "B1" and umbrella.raw_position == "B1" and umbrella.parts
    assert umbrella.credits == [Credit("Gould", "Piano")]
    tags = tags_for(release, "B1")
    assert ("Gould", "Piano") in tags.performers
    # A release-level credit naming the umbrella's position reaches it, and only it.
    assert ("Kempff", "Piano") in tags.performers
    assert ("Kempff", "Piano") not in tags_for(release, "A1").performers


def index_track(title, part_positions, credits=()):
    """A Discogs index track (suite) as discogs_client gives it: type and sub_tracks only in .data."""
    return SimpleNamespace(
        data={"position": "", "type_": "index", "title": title, "duration": "",
              "sub_tracks": [{"position": pos, "title": f"Mvt {pos}", "duration": "1:00"}
                             for pos in part_positions]},
        position="", title=title, duration="", artists=[], credits=list(credits),
    )


def test_credits_written_on_lettered_movements_reach_the_umbrella():
    """The usual classical layout: the work is an index track, credits name its movements."""
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Beethoven")],
        [index_track("Symphony No. 5", ["A1a", "A1b", "A1c", "A1d"]), FakeTrackObj("A2", "Encore")],
        genres=["Classical"], credits=[
            FakeArtist("Kempff", role="Piano", tracks="A1a to A1d"),
            FakeArtist("Gould", role="Piano", tracks="A1c"),
            FakeArtist("Karajan", role="Conductor", tracks="A1"),
            FakeArtist("Fournier", role="Cello", tracks="A2"),
        ],
    ))
    assert [t.position for t in release.tracks] == ["A1", "A2"]
    a1 = tags_for(release, "A1")
    assert a1.performers == (("Kempff", "Piano"), ("Gould", "Piano"))
    assert a1.conductors == ("Karajan",)
    assert tags_for(release, "A2").performers == (("Fournier", "Cello"),)


def test_credits_on_numbered_parts_reach_the_umbrella_and_stop_at_it():
    """Umbrella B4 over parts B4-B6: credits on B5 or B5-B6 cover it; B7 stays separate."""
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Beethoven")],
        [FakeTrackObj("B3", "Before"), index_track("Suite", ["B4", "B5", "B6"]), FakeTrackObj("B7", "After")],
        genres=["Classical"], credits=[
            FakeArtist("Kempff", role="Piano", tracks="B5"),
            FakeArtist("Gould", role="Piano", tracks="B5 to B6"),
            FakeArtist("Fournier", role="Cello", tracks="B4 to B6"),
            FakeArtist("Perlman", role="Violin", tracks="B6 to B7"),
        ],
    ))
    assert [t.position for t in release.tracks] == ["B3", "B4", "B7"]
    assert tags_for(release, "B4").performers == (
        ("Kempff", "Piano"), ("Gould", "Piano"), ("Fournier", "Cello"), ("Perlman", "Violin"),
    )
    assert tags_for(release, "B3").performers == ()
    assert tags_for(release, "B7").performers == (("Perlman", "Violin"),)


def test_real_release_payload_through_discogs_client():
    """Release 848848 (10cc, a rock record) through real discogs_client objects, no network:
    its production credits are never tagged, even if the genre were classical."""
    import discogs_client
    data = json.loads((Path(__file__).parent / "fixtures" / "discogs_release_848848.json").read_text())
    rel = discogs_client.Release(discogs_client.Client("VinylFlow-tests"), data)

    def no_fetch(*_):
        raise AssertionError("must not fetch")
    rel.refresh = no_fetch
    parsed = DiscogsRelease(rel)
    assert parsed.genres == ["Rock"]
    assert {c.role for c in parsed.credits} >= {"Producer", "Written-By", "Lacquer Cut By"}

    plain = tags_for(parsed, "A1")
    assert plain.composers == () and plain.performers == ()

    parsed.genres = ["Classical"]
    forced = tags_for(parsed, "A1")
    assert forced.composers == ("Eric Stewart", "Graham Gouldman")
    assert forced.performers == () and forced.conductors == () and forced.ensembles == ()
    assert forced.artist == parsed.artist


def test_a_payload_without_credits_or_genres_is_not_fetched_again():
    """discogs_client refreshes on any key missing from the payload: one more Discogs
    request per release. ``refresh`` stands in for that request."""
    import discogs_client
    data = json.loads((Path(__file__).parent / "fixtures" / "discogs_release_848848.json").read_text())
    del data["extraartists"], data["genres"]
    rel = discogs_client.Release(discogs_client.Client("VinylFlow-tests"), data)

    def no_fetch(*_):
        raise AssertionError("must not fetch")
    rel.refresh = no_fetch
    parsed = DiscogsRelease(rel)
    assert parsed.genres == [] and parsed.credits == []


# --- real files -------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def silent_file(tmp_path, fmt):
    path = tmp_path / f"t{OUTPUT_FORMATS[fmt]['extension']}"
    run_ffmpeg(
        ["-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", str(path)],
        capture_output=True,
        check=True,
    )
    return path


def full_release():
    return classical([
        FakeArtist("Kempff", role="Piano"), FakeArtist("Karajan", role="Conductor"),
        FakeArtist("BPO", role="Orchestra"), FakeArtist("Beethoven", role="Composed By"),
        FakeArtist("Some Producer", role="Producer"),
    ])


@needs_ffmpeg
def test_flac_read_back(tmp_path):
    path = silent_file(tmp_path, "flac")
    assert handler().tag_file(path, FakeTrack("B1"), full_release(), None, "flac")
    audio = FLAC(path)
    assert audio["COMPOSER"] == ["Beethoven"]
    assert audio["CONDUCTOR"] == ["Karajan"] and audio["ENSEMBLE"] == ["BPO"]
    assert audio["PERFORMER"] == ["Kempff (Piano)"]
    assert audio["ARTIST"] == ["Kempff, BPO, Karajan"] and audio["ALBUMARTIST"] == ["Beethoven"]


@needs_ffmpeg
def test_flac_multiple_composers_are_multi_value(tmp_path):
    release = classical([FakeArtist("A", role="Composed By"), FakeArtist("B", role="Music By")])
    path = silent_file(tmp_path, "flac")
    assert handler().tag_file(path, FakeTrack("A1"), release, None, "flac")
    assert FLAC(path)["COMPOSER"] == ["A", "B"]


@needs_ffmpeg
@pytest.mark.parametrize("fmt, opener", [("mp3", MP3), ("aiff", AIFF)])
def test_id3_read_back(tmp_path, fmt, opener):
    path = silent_file(tmp_path, fmt)
    assert handler().tag_file(path, FakeTrack("A2"), full_release(), None, fmt)
    tags = opener(path).tags
    assert str(tags["TCOM"]) == "Beethoven" and str(tags["TPE3"]) == "Karajan"
    assert str(tags["TXXX:ENSEMBLE"]) == "BPO"
    assert tags["TMCL"].people == [["Piano", "Kempff"]]
    assert str(tags["TPE1"]) == "Kempff, BPO, Karajan" and str(tags["TPE2"]) == "Beethoven"


@needs_ffmpeg
@pytest.mark.parametrize("fmt, opener", [("mp3", MP3), ("aiff", AIFF)])
def test_id3_multi_value_and_performer_without_instrument(tmp_path, fmt, opener):
    release = classical([
        FakeArtist("A", role="Composed By"), FakeArtist("B", role="Composed By"),
        FakeArtist("Perlman", role="Soloist"), FakeArtist("Kempff", role="Piano"),
    ])
    path = silent_file(tmp_path, fmt)
    assert handler().tag_file(path, FakeTrack("A1"), release, None, fmt)
    tags = opener(path).tags
    assert tags["TCOM"].text == ["A", "B"]
    assert tags["TMCL"].people == [["performer", "Perlman"], ["Piano", "Kempff"]]


@needs_ffmpeg
def test_non_classical_files_carry_no_classical_tags(tmp_path):
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Band")], [FakeTrackObj("A1", "x")], credits=[FakeArtist("K", role="Conductor")],
    ))
    flac_path = silent_file(tmp_path, "flac")
    assert handler().tag_file(flac_path, FakeTrack("A1"), release, None, "flac")
    audio = FLAC(flac_path)
    assert not {"COMPOSER", "CONDUCTOR", "ENSEMBLE", "PERFORMER"} & set(audio.keys())
    mp3_dir = tmp_path / "m"
    mp3_dir.mkdir()
    mp3_path = silent_file(mp3_dir, "mp3")
    assert handler().tag_file(mp3_path, FakeTrack("A1"), release, None, "mp3")
    tags = MP3(mp3_path).tags
    assert not {"TCOM", "TPE3", "TXXX:ENSEMBLE", "TMCL"} & set(tags.keys())
