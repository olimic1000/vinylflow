"""State-machine tests for ``backend.sessions.Session``."""

import time
from datetime import datetime
from pathlib import Path

import pytest

from backend.sessions import (
    InvalidSessionState,
    ProcessingRun,
    Session,
    SessionState,
)


def make_session() -> Session:
    return Session.create(
        source_audio=Path("/tmp/test/source.wav"),
        source_filename="test.wav",
        source_duration=180.0,
        source_size=10_000_000,
    )


def make_run() -> ProcessingRun:
    return ProcessingRun(started_at=datetime.now())


def populate_for_processing(s: Session) -> None:
    s.link_release(release={"id": 123, "title": "Test Release"})
    s.set_boundaries([{"number": 1, "start": 0.0, "end": 60.0}])
    s.set_mappings({1: "A1"})


def test_new_session_is_ready():
    s = make_session()
    assert s.state == SessionState.READY
    assert s.release is None
    assert s.boundaries == []
    assert s.mappings == {}
    assert s.last_run is None


def test_create_assigns_unique_ids():
    a, b = make_session(), make_session()
    assert a.id != b.id


def test_link_release_in_ready():
    s = make_session()
    s.link_release(release={"id": 123})
    assert s.release == {"id": 123}


def test_set_boundaries_replaces_list():
    s = make_session()
    s.set_boundaries([{"number": 1}, {"number": 2}])
    s.set_boundaries([{"number": 3}])
    assert len(s.boundaries) == 1


def test_set_mappings_replaces_dict():
    s = make_session()
    s.set_mappings({1: "A1", 2: "A2"})
    s.set_mappings({1: "B1"})
    assert s.mappings == {1: "B1"}


def test_cannot_process_without_release():
    s = make_session()
    s.set_boundaries([{"number": 1}])
    with pytest.raises(InvalidSessionState, match="Release"):
        s.mark_processing(make_run())


def test_cannot_process_without_boundaries():
    s = make_session()
    s.link_release(release={"id": 123})
    with pytest.raises(InvalidSessionState, match="boundaries"):
        s.mark_processing(make_run())


def test_full_happy_path():
    s = make_session()
    populate_for_processing(s)

    run = make_run()
    s.mark_processing(run)
    assert s.state == SessionState.PROCESSING
    assert s.last_run is run

    s.mark_complete(output_folder=Path("/tmp/out"), output_files=["A1-test.flac"])
    assert s.state == SessionState.COMPLETE
    assert s.last_run.output_folder == Path("/tmp/out")
    assert s.last_run.output_files == ["A1-test.flac"]
    assert s.last_run.completed_at is not None
    assert s.last_run.final_fraction == 1.0


def test_failure_path():
    s = make_session()
    populate_for_processing(s)
    s.mark_processing(make_run())

    s.mark_failed("ffmpeg crashed")
    assert s.state == SessionState.FAILED
    assert s.last_run.error == "ffmpeg crashed"
    assert s.last_run.completed_at is not None


def test_re_processing_from_complete():
    s = make_session()
    populate_for_processing(s)
    s.mark_processing(make_run())
    s.mark_complete(output_folder=Path("/tmp/out"), output_files=[])

    s.set_mappings({1: "B1"})
    second_run = make_run()
    s.mark_processing(second_run)
    assert s.state == SessionState.PROCESSING
    assert s.last_run is second_run


def test_re_processing_from_failed():
    s = make_session()
    populate_for_processing(s)
    s.mark_processing(make_run())
    s.mark_failed("transient error")

    s.mark_processing(make_run())
    assert s.state == SessionState.PROCESSING


def test_cannot_link_release_while_processing():
    s = make_session()
    populate_for_processing(s)
    s.mark_processing(make_run())

    with pytest.raises(InvalidSessionState):
        s.link_release(release={"id": 999})


def test_cannot_set_boundaries_while_processing():
    s = make_session()
    populate_for_processing(s)
    s.mark_processing(make_run())

    with pytest.raises(InvalidSessionState):
        s.set_boundaries([{"number": 99}])


def test_cannot_mark_complete_when_not_processing():
    s = make_session()
    with pytest.raises(InvalidSessionState):
        s.mark_complete(output_folder=Path("/tmp/out"), output_files=[])


def test_cannot_mark_failed_when_not_processing():
    s = make_session()
    with pytest.raises(InvalidSessionState):
        s.mark_failed("nope")


def test_can_process_predicate():
    s = make_session()
    assert not s.can_process()
    s.link_release(release={"id": 123})
    assert not s.can_process()
    s.set_boundaries([{"number": 1}])
    assert s.can_process()


def test_can_process_false_during_processing():
    s = make_session()
    populate_for_processing(s)
    s.mark_processing(make_run())
    assert not s.can_process()


def test_touch_updates_timestamp():
    s = make_session()
    initial = s.updated_at
    time.sleep(0.001)
    s.link_release(release={"id": 1})
    assert s.updated_at > initial
