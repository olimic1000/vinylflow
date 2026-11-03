"""Tests for ``audacity_bridge`` and the /api/audacity/* endpoints.

``FakeAudacity`` stands in for Audacity's mod-script-pipe: it serves the same
line protocol over real FIFOs, so the connection code (non-blocking open,
reply framing, timeouts) runs for real.  Its command handling is a small
model of the Audacity commands VinylFlow uses — it checks our side of the
conversation, not Audacity's behaviour.
"""

import json
import os
import sys
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient

import audacity_bridge
from audacity_bridge import (
    VINYLFLOW_LABEL_TRACK,
    AudacityCommandError,
    AudacityPipe,
    AudacityUnavailable,
    choose_labels,
    fetch_recording,
    quote,
    split_reply,
    write_labels,
)
from backend.api import app, session_store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX-only")

client = TestClient(app)


def _params(cmd):
    """Parse 'Name: A=1 B="two words"' into ('Name', {'A': '1', 'B': 'two words'})."""
    name, _, rest = cmd.partition(":")
    params, key, i = {}, "", 0
    rest = rest.strip()
    while i < len(rest):
        eq = rest.index("=", i)
        key = rest[i:eq].strip()
        if rest[eq + 1] == '"':
            close = rest.index('"', eq + 2)
            params[key] = rest[eq + 2 : close]
            i = close + 1
        else:
            end = rest.find(" ", eq)
            end = len(rest) if end == -1 else end
            params[key] = rest[eq + 1 : end]
            i = end
        while i < len(rest) and rest[i] == " ":
            i += 1
    return name.strip(), params


class FakeAudacity:
    """A mod-script-pipe server with a tiny model of an Audacity project."""

    def __init__(self, tmp_path, tracks=None, fail=(), export_aiff=False):
        self.to_path = str(tmp_path / "to")
        self.from_path = str(tmp_path / "from")
        os.mkfifo(self.to_path)
        os.mkfifo(self.from_path)
        self.tracks = tracks if tracks is not None else [
            {"name": "Side A", "kind": "wave", "start": 0.0, "end": 120.0, "channels": 2, "labels": []}
        ]
        self.fail = set(fail)
        # Real Audacity writes the format last picked in its export dialog,
        # whatever the filename says.
        self.export_aiff = export_aiff
        self.log = []
        self.selection = (0.0, 0.0)
        self.selected_tracks = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def pipe(self):
        return AudacityPipe(self.to_path, self.from_path)

    def quit(self):
        """Close both pipe ends, as Audacity does when it exits."""
        self._quit = True
        # Wake the server's blocking read so it notices and exits.
        with open(self.to_path, "w") as f:
            f.write("\n")
        self._thread.join(2)

    # Serving ----------------------------------------------------------------

    def _serve(self):
        with open(self.to_path, "r") as inbox, open(self.from_path, "w") as outbox:
            for line in inbox:
                if getattr(self, "_quit", False):
                    return
                cmd = line.rstrip("\n")
                self.log.append(cmd)
                name, params = _params(cmd)
                try:
                    output, ok = self._handle(name, params), name not in self.fail
                except Exception as e:  # model error -> Failed reply
                    output, ok = str(e), False
                outbox.write(f"{output}\nBatchCommand finished: {'OK' if ok else 'Failed!'}\n\n")
                outbox.flush()

    def _info(self, kind):
        if kind == "Tracks":
            return json.dumps([{k: v for k, v in t.items() if k != "labels"} for t in self.tracks])
        return json.dumps([[i, [list(l) for l in t["labels"]]] for i, t in enumerate(self.tracks) if t["labels"]])

    def _all_labels(self):
        return [(t, i) for t in self.tracks for i in range(len(t["labels"]))]

    def _handle(self, name, p):
        if name == "GetInfo":
            return self._info(p["Type"])
        if name == "SelectAll":
            self.selected_tracks = list(range(len(self.tracks)))
        elif name == "SelectTime":
            self.selection = (float(p["Start"]), float(p["End"]))
        elif name == "SelectTracks":
            self.selected_tracks = list(range(int(p["Track"]), int(p["Track"]) + int(p["TrackCount"])))
        elif name == "Select":
            self.selection = (float(p["Start"]), float(p["End"]))
            first = int(p["Track"])
            self.selected_tracks = list(range(first, first + int(p.get("TrackCount", 1))))
        elif name == "Export2":
            assert self.selection[0] == 0.0, "export must start at 0"
            assert 0 in self.selected_tracks, "audio track must be selected"
            if self.export_aiff:
                with open(p["Filename"], "wb") as f:
                    f.write(b"FORM\x00\x00\x10\x00AIFF" + b"\x00" * 4096)
                return ""
            with wave.open(p["Filename"], "wb") as w:
                w.setnchannels(int(p["NumChannels"]))
                w.setsampwidth(2)
                w.setframerate(1000)  # low rate: a full-length file stays small
                frames = int(self.selection[1] * 1000)
                w.writeframes(b"\x00\x00" * int(p["NumChannels"]) * frames)
        elif name == "RemoveTracks":
            self.tracks = [t for i, t in enumerate(self.tracks) if i not in self.selected_tracks]
        elif name == "NewLabelTrack":
            self.tracks.append({"name": "Label", "kind": "label", "start": 0, "end": 0, "labels": []})
        elif name == "SetTrack":
            self.tracks[int(p["Track"])]["name"] = p["Name"]
        elif name == "AddLabel":
            (target,) = self.selected_tracks
            self.tracks[target]["labels"].append([*self.selection, ""])
            self.tracks[target]["labels"].sort(key=lambda l: l[0])
        elif name == "SetLabel":
            track, i = self._all_labels()[int(p["Label"])]
            track["labels"][i] = [float(p["Start"]), float(p["End"]), p["Text"]]
        return ""


@pytest.fixture(autouse=True)
def _fresh_connection():
    audacity_bridge.reset_connection()
    yield
    audacity_bridge.reset_connection()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    server = FakeAudacity(tmp_path)
    monkeypatch.setattr(audacity_bridge, "_pipe_factory", server.pipe)
    monkeypatch.setattr(audacity_bridge, "running_in_container", lambda: False)
    return server


# --- pure helpers ----------------------------------------------------------


def test_split_reply():
    assert split_reply('[{"a": 1}]\nBatchCommand finished: OK') == ('[{"a": 1}]', True)
    assert split_reply("nope\nBatchCommand finished: Failed!") == ("nope", False)
    assert split_reply("no status line") == ("no status line", False)


def test_quote_drops_characters_audacity_cannot_escape():
    assert quote('A1 "Live"\nversion') == "\"A1 'Live' version\""


def test_choose_labels_prefers_the_vinylflow_track():
    tracks = [{"name": "Side A"}, {"name": "Marks"}, {"name": VINYLFLOW_LABEL_TRACK}]
    info = [[1, [[5, 5, "mine"]]], [2, [[0, 60, "A1"], [60, 120, "A2"]]]]
    assert choose_labels(tracks, info) == ([(0, 60, "A1"), (60, 120, "A2")], VINYLFLOW_LABEL_TRACK)


def test_choose_labels_merges_other_label_tracks():
    tracks = [{"name": "Side A"}, {"name": "Marks"}, {"name": "More"}]
    info = [[1, [[5, 5, "x"]]], [2, [[9, 9, "y"]]]]
    assert choose_labels(tracks, info) == ([(5.0, 5.0, "x"), (9.0, 9.0, "y")], "Marks, More")
    assert choose_labels(tracks, []) == ([], "")


# --- the pipe ----------------------------------------------------------------


def test_command_round_trip_and_failure(fake):
    pipe = fake.pipe()
    pipe.connect()
    try:
        assert json.loads(pipe.command("GetInfo: Type=Tracks Format=JSON"))[0]["name"] == "Side A"
        fake.fail.add("SelectAll")
        with pytest.raises(AudacityCommandError):
            pipe.command("SelectAll:")
    finally:
        pipe.close()


def test_connection_reconnects_after_audacity_restarts(tmp_path, monkeypatch):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = FakeAudacity(tmp_path / "old")
    monkeypatch.setattr(audacity_bridge, "_pipe_factory", old.pipe)
    with audacity_bridge.connection() as pipe:
        pipe.command("SelectAll:")

    old.quit()  # Audacity closes; a new one starts on fresh pipes
    new = FakeAudacity(tmp_path / "new")
    monkeypatch.setattr(audacity_bridge, "_pipe_factory", new.pipe)
    with audacity_bridge.connection() as pipe:
        pipe.command("SelectAll:")
    assert new.log == ["SelectAll:"]


def test_missing_pipe_means_audacity_unavailable(tmp_path):
    with pytest.raises(AudacityUnavailable, match="mod-script-pipe"):
        AudacityPipe(str(tmp_path / "none.to"), str(tmp_path / "none.from")).connect()


def test_stale_pipe_with_no_audacity_fails_fast(tmp_path):
    to_path, from_path = tmp_path / "to", tmp_path / "from"
    os.mkfifo(to_path)
    os.mkfifo(from_path)
    started = time.monotonic()
    with pytest.raises(AudacityUnavailable, match="isn't running"):
        AudacityPipe(str(to_path), str(from_path)).connect()
    assert time.monotonic() - started < 1.0


# --- project operations ------------------------------------------------------


def test_fetch_recording_exports_from_zero_and_reads_labels(fake, tmp_path):
    fake.tracks[0]["start"] = 2.0  # clip moved right: export must still start at 0
    fake.tracks.append({"name": "Marks", "kind": "label", "start": 0, "end": 0,
                        "labels": [[0.0, 0.0, "A1"], [61.5, 61.5, "A2"]]})
    target = tmp_path / "session" / "source.wav"
    with audacity_bridge.connection() as pipe:
        rec = fetch_recording(pipe, target)
    assert target.exists()
    assert rec.name == "Side A" and rec.duration == 120.0
    assert rec.labels == [(0.0, 0.0, "A1"), (61.5, 61.5, "A2")]
    assert rec.label_source == "Marks"
    assert rec.path == target
    assert any(c.startswith("Select: Start=0 End=120.000000 Track=0 TrackCount=2") for c in fake.log)


def test_fetch_recording_names_the_file_by_its_contents(tmp_path):
    server = FakeAudacity(tmp_path, export_aiff=True)
    pipe = server.pipe()
    pipe.connect()
    try:
        rec = fetch_recording(pipe, tmp_path / "s" / "source.wav")
    finally:
        pipe.close()
    assert rec.path == tmp_path / "s" / "source.aiff"
    assert rec.path.exists() and not (tmp_path / "s" / "source.wav").exists()


def test_fetch_recording_with_no_audio_is_an_error(tmp_path, monkeypatch):
    server = FakeAudacity(tmp_path, tracks=[])
    pipe = server.pipe()
    pipe.connect()
    try:
        with pytest.raises(AudacityCommandError, match="no audio"):
            fetch_recording(pipe, tmp_path / "x.wav")
    finally:
        pipe.close()


def test_write_labels_replaces_previous_vinylflow_track(fake):
    fake.tracks.append({"name": "Marks", "kind": "label", "start": 0, "end": 0, "labels": [[3.0, 3.0, "keep"]]})
    rows = [(60.0, 120.0, "A2 Second"), (0.0, 60.0, "A1 First")]
    with audacity_bridge.connection() as pipe:
        assert write_labels(pipe, rows) == 2
        assert write_labels(pipe, rows[:1]) == 1  # second send replaces, not duplicates

    names = [t["name"] for t in fake.tracks]
    assert names == ["Side A", "Marks", VINYLFLOW_LABEL_TRACK]
    assert fake.tracks[1]["labels"] == [[3.0, 3.0, "keep"]]
    assert fake.tracks[2]["labels"] == [[60.0, 120.0, "A2 Second"]]


# --- API ---------------------------------------------------------------------


def test_import_uses_project_labels_as_boundaries(fake):
    fake.tracks.append({"name": VINYLFLOW_LABEL_TRACK, "kind": "label", "start": 0, "end": 0,
                        "labels": [[1.0, 59.0, "A1 First"], [61.0, 119.0, "A2 Second"]]})
    resp = client.post("/api/audacity/import")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid = body["file"]["id"]
    try:
        assert body["file"]["filename"] == "Side A.wav"
        assert body["source"] == "labels" and body["label_track"] == VINYLFLOW_LABEL_TRACK
        assert [(t["start"], t["end"], t["label"]) for t in body["tracks"]] == [
            (1.0, 59.0, "A1 First"),
            (61.0, 119.0, "A2 Second"),
        ]
        session = session_store.get(sid)
        assert session.source_audio.exists()
        assert [(t.start, t.end) for t in session.boundaries] == [(1.0, 59.0), (61.0, 119.0)]
    finally:
        session_store.remove(sid)


def test_import_without_audacity_is_503(tmp_path, monkeypatch):
    monkeypatch.setattr(audacity_bridge, "running_in_container", lambda: False)
    monkeypatch.setattr(
        audacity_bridge, "_pipe_factory",
        lambda: AudacityPipe(str(tmp_path / "no.to"), str(tmp_path / "no.from")),
    )
    resp = client.post("/api/audacity/import")
    assert resp.status_code == 503
    assert "mod-script-pipe" in resp.json()["detail"]


def test_import_in_docker_is_400(monkeypatch):
    monkeypatch.setattr(audacity_bridge, "running_in_container", lambda: True)
    assert client.get("/api/audacity/status").json() == {"supported": False}
    assert client.post("/api/audacity/import").status_code == 400


def test_send_labels_endpoint(fake):
    resp = client.post(
        "/api/audacity/labels",
        json={"tracks": [{"start": 0, "end": 60, "name": "A1 First"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"labels": 1, "track": VINYLFLOW_LABEL_TRACK}
    assert fake.tracks[-1]["labels"] == [[0.0, 60.0, "A1 First"]]
    assert client.post("/api/audacity/labels", json={"tracks": []}).status_code == 422
