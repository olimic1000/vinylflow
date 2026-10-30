"""Discogs index tracks (suites with sub-tracks) and headings in the tracklist parser.

discogs_client's Track exposes neither ``type_`` nor ``sub_tracks``; both live only in the
raw ``.data`` payload, which is what these fakes provide (plus one attribute-style fake)."""
from types import SimpleNamespace

from metadata_handler import DiscogsRelease


def raw(position, title, duration="", type_="track", sub_tracks=None):
    data = {"position": position, "type_": type_, "title": title, "duration": duration}
    if sub_tracks is not None:
        data["sub_tracks"] = sub_tracks
    return SimpleNamespace(data=data, position=position, title=title, duration=duration, artists=[], credits=[])


def release(tracklist):
    return DiscogsRelease(SimpleNamespace(id=848848, title="Deceptive Bends", year=1977, artists=[], labels=[],
                                          formats=[], images=[], genres=[], credits=[], data={}, tracklist=tracklist))


def suite(positions=("B4", "B5", "B6"), durations=("4:00", "3:30", "3:50"), own=""):
    return raw("", "Feel The Benefit", own, "index",
               [{"position": p, "type_": "track", "title": f"Part {i}", "duration": d}
                for i, (p, d) in enumerate(zip(positions, durations), start=1)])


def rows(rel):
    return [(t.position, t.title, t.duration_str) for t in rel.tracks]


def test_index_track_becomes_one_umbrella_track_after_b3():
    rel = release([raw("A1", "Good Morning Judge", "2:54"), raw("B3", "People In Love", "3:51"), suite()])
    assert rows(rel) == [("A1", "Good Morning Judge", "2:54"), ("B3", "People In Love", "3:51"),
                         ("B4", "Feel The Benefit", "11:20")]
    umbrella = rel.tracks[-1]
    assert umbrella.duration_seconds == 680 and [p["position"] for p in umbrella.parts] == ["B4", "B5", "B6"]


def test_umbrella_uses_its_own_length_and_lettered_parts_share_a_base():
    rel = release([raw("B1", "x", "3:00"), suite(("B2a", "B2b"), ("1:00", "2:00"), own="3:30")])
    assert rows(rel)[-1] == ("B2", "Feel The Benefit", "3:30")


def test_umbrella_length_unknown_when_a_part_has_none():
    rel = release([raw("A1", "x", "3:00"), suite(durations=("4:00", "", "3:50"))])
    assert rows(rel)[-1] == ("B4", "Feel The Benefit", "")


def test_index_without_parts_and_headings_are_skipped():
    rel = release([raw("", "Side One", type_="heading"), raw("A1", "x", "3:00"),
                   raw("", "Empty Suite", type_="index", sub_tracks=[]), raw("A2", "y", "2:00")])
    assert [t.position for t in rel.tracks] == ["A1", "A2"]


def test_numbered_tracklist_keeps_the_umbrella_in_order():
    rel = release([raw("1", "One", "3:00"), raw("2", "Two", "3:00"), suite(("3", "4"), ("2:00", "2:00"))])
    assert rows(rel) == [("A1", "One", "3:00"), ("A2", "Two", "3:00"), ("B1", "Feel The Benefit", "4:00")]


def test_attribute_style_fakes_still_work():
    parts = [SimpleNamespace(position="B4", title="Part 1", duration="1:00"),
             SimpleNamespace(position="B5", title="Part 2", duration="1:00")]
    index = SimpleNamespace(position="", title="Suite", duration="", type_="index", sub_tracks=parts,
                            artists=[], credits=[])
    rel = release([raw("B3", "x", "3:00"), index])
    assert rows(rel)[-1] == ("B4", "Suite", "2:00")


def test_real_release_payload_through_discogs_client():
    """Release 848848 (10cc - Deceptive Bends): "Feel The Benefit" is an index track over
    B4-B6 with no length of its own. Real discogs_client objects, no network."""
    import json
    from pathlib import Path
    import discogs_client
    data = json.loads((Path(__file__).parent / "fixtures" / "discogs_release_848848.json").read_text())
    rel = discogs_client.Release(discogs_client.Client("VinylFlow-tests"), data)

    def no_fetch(*_):
        raise AssertionError("must not fetch")
    rel.refresh = no_fetch
    parsed = DiscogsRelease(rel)
    assert [t.position for t in parsed.tracks] == ["A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3", "B4"]
    suite = parsed.tracks[-1]
    assert (suite.title, suite.duration_str) == ("Feel The Benefit", "11:34")
    assert [p["title"] for p in suite.parts] == ["Reminisce And Speculation", "A Latin Break", "Feel The Benefit"]
