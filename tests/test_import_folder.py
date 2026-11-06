"""Import from folder: listing, safe name resolution, in-place sessions and
the guarantee that no cleanup path ever touches the imported source."""

import hashlib
import os
import time
import wave
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import import_folder
from backend import api
from backend.api import app, session_store

client = TestClient(app)
OLD = time.time() - 3600


def _wav(path, seconds=1, age=3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * seconds)
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    imp = tmp_path / "incoming"
    imp.mkdir()
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(api, "UPLOAD_DIR", up)
    monkeypatch.setattr(api.config, "import_dir", str(imp))
    monkeypatch.setattr(api, "_granted_import_paths", set())
    before = {s.id for s in session_store.list()}
    yield imp, up, tmp_path
    for s in session_store.list():
        if s.id not in before:
            session_store.remove(s.id)


def _names(resp):
    return [f["name"] for f in resp.json()["files"]]


def test_listing_filters_and_order(env, tmp_path):
    imp, _, root = env
    _wav(imp / "b.wav")
    _wav(imp / "Album" / "Side A.WAV")
    _wav(imp / ".hidden.wav")
    _wav(imp / "._b.wav")
    _wav(imp / ".Trashes" / "x.wav")
    _wav(imp / "d1" / "d2" / "d3" / "ok.wav")
    _wav(imp / "d1" / "d2" / "d3" / "d4" / "too-deep.wav")
    (imp / "notes.txt").write_text("x")
    outside = _wav(root / "outside" / "o.wav")
    (imp / "link.wav").symlink_to(outside)
    (imp / "linkdir").symlink_to(outside.parent, target_is_directory=True)
    r = client.get("/api/import").json()
    assert r["enabled"] and r["error"] is None
    assert _names(client.get("/api/import")) == [
        "Album/Side A.WAV", "b.wav", "d1/d2/d3/ok.wav",
    ]
    assert all(f["stable"] and f["imported_id"] is None for f in r["files"])


def test_listing_unstable_and_disabled(env, monkeypatch):
    imp, _, _ = env
    _wav(imp / "fresh.wav", age=1)
    assert client.get("/api/import").json()["files"][0]["stable"] is False
    assert import_folder.list_audio(imp, now=time.time() + 60)[0]["stable"] is True
    monkeypatch.setattr(api.config, "import_dir", None)
    assert client.get("/api/import").json() == {
        "enabled": False, "dir": None, "error": None, "files": []}
    r = client.post("/api/import", json={"names": ["a.wav"]}).json()
    assert r["files"] == [] and r["skipped"][0]["name"] == "a.wav"


def test_import_in_place_and_idempotent(env):
    imp, up, _ = env
    src = _wav(imp / "Album" / "side.wav")
    digest = _sha(src)
    r = client.post("/api/import", json={"names": ["Album/side.wav"]}).json()
    assert r["skipped"] == []
    entry = r["files"][0]
    assert entry["in_place"] is True and entry["filename"] == "side.wav"
    assert entry["size"] == src.stat().st_size
    s = session_store.get(entry["id"])
    assert s.source_audio.resolve() == src.resolve() and s.in_place
    assert not list((up / entry["id"]).glob("source*"))  # no copy in the session dir
    again = client.post("/api/import", json={"names": ["Album/side.wav"]}).json()
    assert again["files"][0]["id"] == entry["id"]
    listed = client.get("/api/import").json()["files"][0]
    assert listed["imported_id"] == entry["id"]
    q = [e for e in client.get("/api/queue").json()["uploaded"] if e["id"] == entry["id"]]
    assert q[0]["in_place"] is True
    assert _sha(src) == digest


def test_import_rejects_bad_names(env):
    imp, _, root = env
    _wav(imp / "ok.wav")
    outside = _wav(root / "outside" / "o.wav")
    (imp / "link.wav").symlink_to(outside)
    (imp / "notes.txt").write_text("x")
    bad = ["../x.wav", "/abs/x.wav", "a/../../x.wav", "", "link.wav",
           str(outside), "notes.txt", "missing.wav", ".hid.wav"]
    r = client.post("/api/import", json={"names": bad}).json()
    assert r["files"] == []
    assert [s["name"] for s in r["skipped"]] == bad
    assert all(s["reason"] for s in r["skipped"])


def test_import_unstable_skipped_and_empty_body(env):
    imp, _, _ = env
    _wav(imp / "fresh.wav", age=1)
    r = client.post("/api/import", json={"names": ["fresh.wav"]}).json()
    assert r["files"] == [] and "copied" in r["skipped"][0]["reason"]
    assert client.post("/api/import", json={}).status_code == 400


def test_granted_paths(env):
    _, _, root = env
    f = _wav(root / "elsewhere" / "rec.aiff")
    r = client.post("/api/import", json={"paths": [str(f)]}).json()
    assert r["files"] == [] and r["skipped"]
    assert api.grant_import_paths([str(f), str(root / "nope.wav")]) == [str(f.resolve())]
    r = client.post("/api/import", json={"paths": [str(f)]}).json()
    assert r["files"][0]["in_place"] and r["skipped"] == []


def test_granted_path_inside_uploads_refused(env):
    _, up, _ = env
    f = _wav(up / "x" / "source.wav")
    api.grant_import_paths([str(f)])  # may be granted by the dialog helper...
    assert client.post("/api/import", json={"paths": [str(f)]}).json()["files"] == []


def test_cleanup_paths_leave_source_untouched(env):
    imp, up, _ = env
    for how in ("queue", "clear-all", "reap"):
        src = _wav(imp / f"{how}.wav")
        digest = _sha(src)
        sid = client.post("/api/import", json={"names": [src.name]}).json()["files"][0]["id"]
        assert (up / sid).is_dir()
        if how == "queue":
            assert client.delete(f"/api/queue/{sid}").status_code == 200
        elif how == "clear-all":
            client.delete("/api/temp/clear-all")
        else:
            session_store.get(sid).updated_at = datetime.now() - timedelta(days=2)
            api.reap_expired()
        assert not (up / sid).exists()
        assert session_store.get(sid) is None
        assert src.is_file() and _sha(src) == digest


@pytest.mark.parametrize("kind", ["inside", "equal", "parent"])
def test_overlap_disables_import(env, monkeypatch, kind):
    imp, up, root = env
    d = {"inside": up / "sub", "equal": up, "parent": root}[kind]
    d.mkdir(exist_ok=True)
    _wav(d / "a.wav")
    monkeypatch.setattr(api.config, "import_dir", str(d))
    r = client.get("/api/import").json()
    assert r["enabled"] and r["error"] and r["files"] == []
    p = client.post("/api/import", json={"names": ["a.wav"]}).json()
    assert p["files"] == [] and p["skipped"]


def test_missing_source_returns_410(env):
    imp, _, _ = env
    src = _wav(imp / "gone.wav")
    sid = client.post("/api/import", json={"names": ["gone.wav"]}).json()["files"][0]["id"]
    src.unlink()
    r = client.get(f"/api/audio/{sid}")
    assert r.status_code == 410 and "gone.wav" in r.json()["detail"]
    r = client.post("/api/process", json={
        "file_id": sid, "release_id": 1, "track_mapping": []})
    assert r.status_code == 410
    assert client.post("/api/analyze", json={"file_id": sid}).status_code == 410
    assert client.get(f"/api/waveform-peaks/{sid}").status_code == 410


def test_config_import_dir_read_only(env):
    imp, _, root = env
    assert client.get("/api/config").json()["import_dir"] == str(imp)
    client.put("/api/config", json={"import_dir": str(root)})
    assert client.get("/api/config").json()["import_dir"] == str(imp)
    assert api.config.import_dir == str(imp)


def test_listing_and_reimport_distinguish_queued_from_completed(env):
    """Reimport remains idempotent and reports export state, not just import state."""
    from backend.sessions.session import SessionState

    imp, _, _ = env
    _wav(imp / "again.wav")
    entry = client.post("/api/import", json={"names": ["again.wav"]}).json()["files"][0]
    session = session_store.get(entry["id"])
    for state, expected in ((SessionState.READY, "uploaded"),
                            (SessionState.FAILED, "error"),
                            (SessionState.COMPLETE, "completed")):
        session.state = state
        row = client.get("/api/import").json()["files"][0]
        assert row["imported_status"] == expected
        again = client.post("/api/import", json={"names": ["again.wav"]}).json()["files"][0]
        assert again["id"] == entry["id"]
        assert again["status"] == expected
