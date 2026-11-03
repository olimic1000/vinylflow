"""Track/disc totals and numbering scope for the ``sequential_disc_per_lp`` scheme.

Releases are built through the real ``DiscogsRelease`` parser from fakes that mimic
discogs_client objects.  No Discogs calls anywhere.
"""

import shutil
import subprocess

import pytest
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from audio_processor import OUTPUT_FORMATS
from metadata_handler import DiscogsRelease, MetadataHandler

SEQ = "sequential_disc_per_lp"


class FakeArtist:
    def __init__(self, name):
        self.name = name
        self.data = {"id": 1, "name": name, "anv": "", "join": "", "role": "", "tracks": ""}


class FakeTrackObj:
    def __init__(self, position, title):
        self.position, self.title, self.duration = position, title, "3:00"
        self.artists, self.credits = [], []


class FakeLabel:
    name = "Test Label"


class FakeReleaseObj:
    def __init__(self, tracklist):
        self.id, self.title, self.year = 7, "Test Album", 1985
        self.artists, self.tracklist, self.genres = [FakeArtist("Band")], list(tracklist), ["Rock"]
        self.credits = []
        self.labels, self.formats, self.images = [FakeLabel()], [{"name": "Vinyl"}], []


class FakeTrack:
    def __init__(self, vinyl_number):
        self.vinyl_number = vinyl_number


def release_of(*positions):
    return DiscogsRelease(FakeReleaseObj([FakeTrackObj(p, f"T {p}") for p in positions]))


def two_lp_release():
    return release_of("A1", "A2", "B1", "C1", "C2", "D1")


def handler():
    return MetadataHandler(discogs_token="", user_agent="")


def tags_for(release, position, numbering=SEQ):
    return handler()._build_tag_set(FakeTrack(position), release, None, numbering)


def test_totals_track_per_disc_and_discs_in_release():
    release = two_lp_release()
    first, last = tags_for(release, "B1"), tags_for(release, "D1")
    assert (first.disc_number, first.track_number, first.track_total, first.disc_total) == ("1", "3", "3", "2")
    assert (last.disc_number, last.track_number, last.track_total, last.disc_total) == ("2", "3", "3", "2")


def test_vinyl_numbering_has_no_totals_or_disc():
    tags = tags_for(two_lp_release(), "A1", numbering="vinyl")
    assert (tags.track_number, tags.track_total, tags.disc_number, tags.disc_total) == ("A1", None, None, None)


def test_side_exported_alone_continues_after_the_other_side():
    """Numbering follows the release tracklist, so side B recorded as its own file still
    starts after side A, and a track that is not exported leaves a gap, not a duplicate."""
    tags = tags_for(two_lp_release(), "B1")
    assert (tags.track_number, tags.track_total) == ("3", "3")


def test_single_lp_has_one_disc():
    tags = tags_for(release_of("A1", "A2", "B1", "B2"), "B2")
    assert (tags.disc_number, tags.disc_total, tags.track_number, tags.track_total) == ("1", "1", "4", "4")


def test_positions_sort_numerically():
    release = release_of("A10", "A2", "A1", "A3")
    assert tags_for(release, "A10").track_number == "4"


def test_sub_tracks_number_in_tracklist_order():
    """A2a/A2b/A2c share a sort key; they must keep the tracklist's order on every run."""
    release = release_of("A1", "A2a", "A2b", "A2c", "B1")
    numbers = [tags_for(release, p).track_number for p in ("A1", "A2a", "A2b", "A2c", "B1")]
    assert numbers == ["1", "2", "3", "4", "5"]
    assert tags_for(release, "B1").track_total == "5"


def test_disc_total_agrees_across_the_release_when_side_letters_skip():
    release = release_of("A1", "B1", "E1", "F1")
    assert {tags_for(release, p).disc_total for p in ("A1", "B1", "E1", "F1")} == {"3"}


def test_unrecognised_position_gets_no_sequential_number():
    """The caller falls back to the vinyl scheme (no totals) when this returns (None, None)."""
    assert MetadataHandler._sequential_disc_per_lp("X", ["A1", "X"]) == (None, None)


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def silent_file(tmp_path, fmt):
    path = tmp_path / f"t{OUTPUT_FORMATS[fmt]['extension']}"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
def test_flac_read_back(tmp_path):
    path = silent_file(tmp_path, "flac")
    assert handler().tag_file(path, FakeTrack("B1"), two_lp_release(), None, "flac", SEQ)
    audio = FLAC(path)
    assert audio["TRACKNUMBER"] == ["3"] and audio["TRACKTOTAL"] == ["3"]
    assert audio["DISCNUMBER"] == ["1"] and audio["DISCTOTAL"] == ["2"]
    assert audio["VINYLPOSITION"] == ["B1"]


@needs_ffmpeg
def test_flac_vinyl_scheme_writes_no_totals(tmp_path):
    path = silent_file(tmp_path, "flac")
    assert handler().tag_file(path, FakeTrack("B1"), two_lp_release(), None, "flac", "vinyl")
    audio = FLAC(path)
    assert audio["TRACKNUMBER"] == ["B1"]
    assert not {"TRACKTOTAL", "DISCTOTAL", "DISCNUMBER"} & set(audio.keys())


@needs_ffmpeg
@pytest.mark.parametrize("fmt, opener", [("mp3", MP3), ("aiff", AIFF)])
def test_id3_read_back(tmp_path, fmt, opener):
    path = silent_file(tmp_path, fmt)
    assert handler().tag_file(path, FakeTrack("C2"), two_lp_release(), None, fmt, SEQ)
    tags = opener(path).tags
    assert str(tags["TRCK"]) == "2/3" and str(tags["TPOS"]) == "2/2"


@needs_ffmpeg
def test_id3_vinyl_scheme_has_plain_track_and_no_disc(tmp_path):
    path = silent_file(tmp_path, "mp3")
    assert handler().tag_file(path, FakeTrack("B1"), two_lp_release(), None, "mp3", "vinyl")
    tags = MP3(path).tags
    assert str(tags["TRCK"]) == "B1" and "TPOS" not in tags
