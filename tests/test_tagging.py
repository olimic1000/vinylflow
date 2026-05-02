"""Tests for the format-neutral tagging core in ``metadata_handler``.

The Mutagen apply functions (``_apply_vorbis``, ``_apply_id3``) write to
real Mutagen objects — they're tested via fakes that mimic the parts of
the API used.  Real-format integration is covered by manual desktop-app
runs.
"""

from typing import List, Optional

from mutagen.id3 import APIC, COMM, TALB, TDRC, TIT2, TPE1, TPUB, TRCK, TXXX

from metadata_handler import MetadataHandler, TagSet


class FakeDiscogsTrack:
    def __init__(self, position: str, title: str):
        self.position = position
        self.title = title


class FakeRelease:
    def __init__(
        self,
        id: int,
        artist: str,
        title: str,
        year: Optional[int],
        label: Optional[str],
        tracks: List[FakeDiscogsTrack],
    ):
        self.id = id
        self.artist = artist
        self.title = title
        self.year = year
        self.label = label
        self.tracks = tracks


class FakeTrack:
    def __init__(self, vinyl_number: str):
        self.vinyl_number = vinyl_number


def make_handler() -> MetadataHandler:
    return MetadataHandler(discogs_token="", user_agent="")


def make_release(year=1985, label="Test Label") -> FakeRelease:
    return FakeRelease(
        id=12345,
        artist="Test Artist",
        title="Test Album",
        year=year,
        label=label,
        tracks=[
            FakeDiscogsTrack(position="A1", title="Track One"),
            FakeDiscogsTrack(position="A2", title="Track Two"),
        ],
    )


def test_build_tag_set_happy_path():
    handler = make_handler()
    release = make_release()
    track = FakeTrack(vinyl_number="A1")

    tags = handler._build_tag_set(track, release, cover_data=b"fake-cover")

    assert tags is not None
    assert tags.artist == "Test Artist"
    assert tags.album == "Test Album"
    assert tags.title == "Track One"
    assert tags.track_number == "A1"
    assert tags.year == 1985
    assert tags.label == "Test Label"
    assert tags.release_id == 12345
    assert tags.cover_data == b"fake-cover"
    assert tags.comment == "Digitized from vinyl"


def test_build_tag_set_returns_none_when_position_unmapped():
    handler = make_handler()
    release = make_release()
    track = FakeTrack(vinyl_number="B99")  # Not in release.tracks

    assert handler._build_tag_set(track, release, cover_data=None) is None


def test_build_tag_set_normalises_empty_label_to_none():
    handler = make_handler()
    release = make_release(label="")
    track = FakeTrack(vinyl_number="A1")

    tags = handler._build_tag_set(track, release, cover_data=None)

    assert tags is not None
    assert tags.label is None


def test_build_tag_set_carries_missing_year():
    handler = make_handler()
    release = make_release(year=None)
    track = FakeTrack(vinyl_number="A1")

    tags = handler._build_tag_set(track, release, cover_data=None)

    assert tags is not None
    assert tags.year is None


class FakeFlacAudio:
    def __init__(self):
        self.values: dict = {}
        self.pictures: list = []

    def __setitem__(self, key, value):
        self.values[key] = value

    def add_picture(self, p):
        self.pictures.append(p)


def test_apply_vorbis_writes_expected_tags():
    handler = make_handler()
    audio = FakeFlacAudio()
    tags = TagSet(
        artist="A", album="B", title="C", track_number="A1",
        year=1990, label="Lbl", release_id=42, cover_data=b"cover",
    )

    handler._apply_vorbis(audio, tags)

    assert audio.values["ARTIST"] == "A"
    assert audio.values["ALBUM"] == "B"
    assert audio.values["TITLE"] == "C"
    assert audio.values["TRACKNUMBER"] == "A1"
    assert audio.values["DATE"] == "1990"
    assert audio.values["LABEL"] == "Lbl"
    assert audio.values["DISCOGS_RELEASE_ID"] == "42"
    assert audio.values["COMMENT"] == "Digitized from vinyl"
    assert len(audio.pictures) == 1
    assert audio.pictures[0].data == b"cover"
    assert audio.pictures[0].mime == "image/jpeg"
    assert audio.pictures[0].type == 3


def test_apply_vorbis_omits_label_when_none():
    handler = make_handler()
    audio = FakeFlacAudio()
    tags = TagSet(
        artist="A", album="B", title="C", track_number="A1",
        year=1990, label=None, release_id=42, cover_data=None,
    )

    handler._apply_vorbis(audio, tags)

    assert "LABEL" not in audio.values
    assert audio.pictures == []


def test_apply_vorbis_writes_empty_date_when_year_missing():
    handler = make_handler()
    audio = FakeFlacAudio()
    tags = TagSet(
        artist="A", album="B", title="C", track_number="A1",
        year=None, label=None, release_id=42, cover_data=None,
    )

    handler._apply_vorbis(audio, tags)

    assert audio.values["DATE"] == ""


class FakeId3Audio:
    def __init__(self):
        self.tags: dict = {}


def test_apply_id3_writes_expected_frames():
    handler = make_handler()
    audio = FakeId3Audio()
    tags = TagSet(
        artist="A", album="B", title="C", track_number="A1",
        year=1990, label="Lbl", release_id=42, cover_data=b"cover",
    )

    handler._apply_id3(audio, tags)

    assert isinstance(audio.tags["TIT2"], TIT2)
    assert audio.tags["TIT2"].text == ["C"]
    assert isinstance(audio.tags["TPE1"], TPE1)
    assert audio.tags["TPE1"].text == ["A"]
    assert isinstance(audio.tags["TALB"], TALB)
    assert audio.tags["TALB"].text == ["B"]
    assert isinstance(audio.tags["TRCK"], TRCK)
    assert audio.tags["TRCK"].text == ["A1"]
    assert isinstance(audio.tags["TDRC"], TDRC)
    assert isinstance(audio.tags["TPUB"], TPUB)
    assert audio.tags["TPUB"].text == ["Lbl"]
    assert isinstance(audio.tags["TXXX:DISCOGS_RELEASE_ID"], TXXX)
    assert audio.tags["TXXX:DISCOGS_RELEASE_ID"].text == ["42"]
    assert isinstance(audio.tags["COMM"], COMM)
    assert audio.tags["COMM"].text == ["Digitized from vinyl"]
    assert isinstance(audio.tags["APIC"], APIC)
    assert audio.tags["APIC"].data == b"cover"


def test_apply_id3_omits_year_label_cover_when_absent():
    handler = make_handler()
    audio = FakeId3Audio()
    tags = TagSet(
        artist="A", album="B", title="C", track_number="A1",
        year=None, label=None, release_id=42, cover_data=None,
    )

    handler._apply_id3(audio, tags)

    assert "TDRC" not in audio.tags
    assert "TPUB" not in audio.tags
    assert "APIC" not in audio.tags
    # Other required tags still written
    assert "TIT2" in audio.tags
    assert "TXXX:DISCOGS_RELEASE_ID" in audio.tags
