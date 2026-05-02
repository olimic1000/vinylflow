"""SessionStore tests."""

import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from backend.sessions import (
    ProcessingRun,
    Session,
    SessionState,
    SessionStore,
)


def make_store() -> SessionStore:
    return SessionStore()


def make_filled_session(store: SessionStore) -> Session:
    sid = str(uuid.uuid4())
    s = store.create(
        session_id=sid,
        source_audio=Path(f"/tmp/{sid}/source.wav"),
        source_filename="test.wav",
        source_duration=60.0,
        source_size=1_000_000,
    )
    s.link_release(release={"id": 1})
    s.set_boundaries([{"number": 1}])
    return s


def test_create_returns_ready_session():
    store = make_store()
    s = store.create(
        session_id="abc",
        source_audio=Path("/tmp/abc/source.wav"),
        source_filename="abc.wav",
        source_duration=60.0,
        source_size=1_000_000,
    )
    assert s.id == "abc"
    assert s.state == SessionState.READY


def test_get_returns_same_instance():
    store = make_store()
    a = store.create(
        session_id="x",
        source_audio=Path("/tmp/x/source.wav"),
        source_filename="x.wav",
        source_duration=10.0,
        source_size=1,
    )
    assert store.get("x") is a


def test_get_unknown_returns_none():
    assert make_store().get("nope") is None


def test_list_returns_all_sessions():
    store = make_store()
    for i in range(3):
        store.create(
            session_id=f"s{i}",
            source_audio=Path(f"/tmp/s{i}/source.wav"),
            source_filename=f"s{i}.wav",
            source_duration=10.0,
            source_size=1,
        )
    assert {s.id for s in store.list()} == {"s0", "s1", "s2"}


def test_remove_returns_true_when_present():
    store = make_store()
    store.create(
        session_id="x",
        source_audio=Path("/tmp/x/source.wav"),
        source_filename="x.wav",
        source_duration=10.0,
        source_size=1,
    )
    assert store.remove("x") is True
    assert store.get("x") is None


def test_remove_returns_false_when_missing():
    assert make_store().remove("nope") is False


def test_reap_stale_skips_processing_sessions():
    store = make_store()
    s = make_filled_session(store)
    s.mark_processing(ProcessingRun(started_at=datetime.now()))
    # Force the session to look ancient.
    s.updated_at = datetime.now() - timedelta(hours=100)

    reaped = store.reap_stale(ttl_hours=1.0)
    assert reaped == []
    assert store.get(s.id) is s


def test_reap_stale_drops_old_complete_sessions():
    store = make_store()
    s = make_filled_session(store)
    s.mark_processing(ProcessingRun(started_at=datetime.now()))
    s.mark_complete(output_folder=Path("/tmp/out"), output_files=[])
    s.updated_at = datetime.now() - timedelta(hours=100)

    reaped = store.reap_stale(ttl_hours=1.0)
    assert reaped == [s.id]
    assert store.get(s.id) is None


def test_reap_stale_keeps_recent_sessions():
    store = make_store()
    s = make_filled_session(store)
    reaped = store.reap_stale(ttl_hours=1.0)
    assert reaped == []
    assert store.get(s.id) is s


def test_concurrent_creates_do_not_lose_sessions():
    store = make_store()
    n = 50
    barrier = threading.Barrier(n)

    def add(i: int) -> None:
        barrier.wait()
        store.create(
            session_id=f"s{i}",
            source_audio=Path(f"/tmp/s{i}/source.wav"),
            source_filename=f"s{i}.wav",
            source_duration=1.0,
            source_size=1,
        )

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.list()) == n
