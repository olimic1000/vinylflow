"""Artist names, album artist, compilations and omitted-empty tags.

Releases are built through the real ``DiscogsRelease`` parser from fakes that
mimic discogs_client objects: an ``Artist`` keeps its raw payload in ``.data``
(``name``, ``join``, ...); ``Track.artists`` is a list of those.  Some fakes only
carry attributes, which must keep working.  No Discogs calls anywhere.
"""

import shutil
from types import SimpleNamespace

import pytest
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from audio_processor import OUTPUT_FORMATS, run_ffmpeg
from metadata_handler import DiscogsRelease, MetadataHandler


class FakeArtist:
    """discogs_client-style artist: name plus the raw payload under ``.data``."""

    def __init__(self, name, join=""):
        self.name = name
        self.data = {"id": 1, "name": name, "join": join}


class FakeTrackObj:
    def __init__(self, position, title, artists=()):
        self.position, self.title, self.duration = position, title, "3:00"
        self.artists = list(artists)


class FakeLabel:
    name = "Test Label"


class FakeReleaseObj:
    def __init__(self, artists, tracklist, year=1985):
        self.id, self.title, self.year = 7, "Test Album", year
        self.artists, self.tracklist = list(artists), list(tracklist)
        self.labels, self.formats, self.images = [FakeLabel()], [{"name": "Vinyl"}], []


class FakeTrack:
    def __init__(self, vinyl_number):
        self.vinyl_number = vinyl_number


def handler():
    return MetadataHandler(discogs_token="", user_agent="")


def tags_for(release, position):
    return handler()._build_tag_set(FakeTrack(position), release, None)


def compilation(year=1985):
    return DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")],
        [FakeTrackObj("A1", "Song", artists=[FakeArtist("Blur (2)", " / "), FakeArtist("Pulp")]),
         FakeTrackObj("A2", "No artist")],
        year=year,
    ))


def plain_release():
    return DiscogsRelease(FakeReleaseObj([FakeArtist("Nirvana (2)")], [FakeTrackObj("A1", "One")]))


# --- artist names -----------------------------------------------------------

def test_artist_names_use_join_strings_and_drop_disambiguation():
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Simon (3)", "&"), FakeArtist("Garfunkel (2)", ","), FakeArtist("Third")],
        [FakeTrackObj("A1", "One")],
    ))
    assert release.artist == "Simon & Garfunkel, Third"
    assert tags_for(release, "A1").album_artist == "Simon & Garfunkel, Third"
    assert handler().create_album_folder_name(release) == "Simon & Garfunkel, Third - Test Album"


def test_missing_join_falls_back_to_comma():
    release = DiscogsRelease(FakeReleaseObj([FakeArtist("A"), FakeArtist("B")], [FakeTrackObj("A1", "x")]))
    assert release.artist == "A, B"


def test_disambiguation_suffix_is_stripped_from_artist_and_folder():
    release = plain_release()
    assert release.artist == "Nirvana"
    assert handler().create_album_folder_name(release) == "Nirvana - Test Album"


def test_attribute_only_artists_are_joined_too():
    """Artists without a raw ``.data`` payload (plain attribute fakes) still work."""
    artists = [SimpleNamespace(name="Hall (2)", join="&"), SimpleNamespace(name="Oates", join="")]
    release = DiscogsRelease(FakeReleaseObj(artists, [FakeTrackObj("A1", "x")]))
    assert release.artist == "Hall & Oates"


def test_no_artists_is_unknown_artist():
    release = DiscogsRelease(FakeReleaseObj([], [FakeTrackObj("A1", "x")]))
    assert release.artist == "Unknown Artist"


def test_various_detection_ignores_suffix_and_case():
    from release_credits import VARIOUS_ARTISTS, clean_artist_name, is_various
    assert VARIOUS_ARTISTS == "Various Artists"
    assert clean_artist_name("Various (2)") == "Various"
    assert is_various("various") and is_various("Various Artists") and not is_various("Variousish")


# --- album artist and compilations -----------------------------------------

def test_regular_album_has_album_artist_and_no_compilation_flag():
    tags = tags_for(plain_release(), "A1")
    assert tags.artist == "Nirvana" and tags.album_artist == "Nirvana"
    assert tags.compilation is False


def test_compilation_uses_track_artists_and_various_album_artist():
    release = compilation()
    first, second = tags_for(release, "A1"), tags_for(release, "A2")
    assert first.artist == "Blur / Pulp"
    assert first.album_artist == "Various Artists" and first.compilation
    assert second.artist == "Various"  # no track artist: falls back to the release artist


def test_track_artist_is_parsed_for_regular_numbered_and_umbrella_tracks():
    suite = SimpleNamespace(
        data={"position": "", "type_": "index", "title": "Suite", "duration": "",
              "sub_tracks": [{"position": "B1a", "title": "I", "duration": "1:00"},
                             {"position": "B1b", "title": "II", "duration": "2:00"}]},
        position="", title="Suite", duration="", artists=[FakeArtist("Orchestra X")],
    )
    release = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")],
        [FakeTrackObj("A1", "One", artists=[FakeArtist("Solo")]), suite],
    ))
    by_position = {t.position: t for t in release.tracks}
    assert by_position["A1"].artist == "Solo"
    assert by_position["B1"].artist == "Orchestra X" and by_position["B1"].parts
    assert tags_for(release, "B1").artist == "Orchestra X"

    numbered = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")],
        [FakeTrackObj("1", "One", artists=[FakeArtist("P")]), FakeTrackObj("2", "Two", artists=[FakeArtist("Q")])],
    ))
    assert [t.artist for t in numbered.tracks] == ["P", "Q"]

    repeated = DiscogsRelease(FakeReleaseObj(
        [FakeArtist("Various")], [FakeTrackObj("A", "One", artists=[FakeArtist("R")])],
    ))
    assert repeated.tracks[0].position == "A1" and repeated.tracks[0].artist == "R"


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


@needs_ffmpeg
def test_flac_regular_album_read_back(tmp_path):
    path = silent_file(tmp_path, "flac")
    assert handler().tag_file(path, FakeTrack("A1"), plain_release(), None, "flac")
    audio = FLAC(path)
    assert audio["ARTIST"] == ["Nirvana"] and audio["ALBUMARTIST"] == ["Nirvana"]
    assert "COMPILATION" not in audio


@needs_ffmpeg
def test_flac_compilation_without_year_has_no_date(tmp_path):
    path = silent_file(tmp_path, "flac")
    assert handler().tag_file(path, FakeTrack("A1"), compilation(year=""), None, "flac")
    audio = FLAC(path)
    assert "DATE" not in audio
    assert audio["ARTIST"] == ["Blur / Pulp"] and audio["ALBUMARTIST"] == ["Various Artists"]
    assert audio["COMPILATION"] == ["1"]


@needs_ffmpeg
@pytest.mark.parametrize("fmt, opener", [("mp3", MP3), ("aiff", AIFF)])
def test_id3_compilation_read_back(tmp_path, fmt, opener):
    path = silent_file(tmp_path, fmt)
    assert handler().tag_file(path, FakeTrack("A1"), compilation(year=""), None, fmt)
    tags = opener(path).tags
    assert str(tags["TPE1"]) == "Blur / Pulp" and str(tags["TPE2"]) == "Various Artists"
    assert str(tags["TCMP"]) == "1"
    assert "TDRC" not in tags


@needs_ffmpeg
def test_id3_regular_album_has_album_artist_and_no_tcmp(tmp_path):
    path = silent_file(tmp_path, "mp3")
    assert handler().tag_file(path, FakeTrack("A1"), plain_release(), None, "mp3")
    tags = MP3(path).tags
    assert str(tags["TPE2"]) == "Nirvana" and "TCMP" not in tags and "TDRC" in tags
