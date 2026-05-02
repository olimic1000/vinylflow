"""Session — the user-facing unit of work.

States: ``READY -> PROCESSING -> COMPLETE | FAILED``.  Both COMPLETE and
FAILED can transition back to PROCESSING when the user re-runs the
Pipeline (e.g. after fixing mappings).  Mutation methods enforce valid
transitions; invalid ones raise ``InvalidSessionState``.

``release`` and ``boundaries`` are typed ``Any`` here so the Session
module stays independent of the lower-level shapes
(``metadata_handler.DiscogsRelease``, ``audio_processor.Track``).  The
Pipeline binds those concrete types.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .events import ProcessingRun


class SessionState(str, Enum):
    READY = "ready"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class InvalidSessionState(RuntimeError):
    """A Session mutation was attempted from an incompatible state."""


@dataclass
class Session:
    id: str
    source_filename: str
    source_audio: Path
    source_duration: float
    source_size: int

    state: SessionState = SessionState.READY
    release: Optional[Any] = None
    boundaries: list[Any] = field(default_factory=list)
    mappings: dict[int, str] = field(default_factory=dict)
    last_run: Optional[ProcessingRun] = None

    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def create(
        cls,
        source_audio: Path,
        source_filename: str,
        source_duration: float,
        source_size: int,
    ) -> "Session":
        return cls(
            id=str(uuid.uuid4()),
            source_filename=source_filename,
            source_audio=source_audio,
            source_duration=source_duration,
            source_size=source_size,
        )

    def link_release(self, release: Any) -> None:
        self._assert_mutable()
        self.release = release
        self._touch()

    def set_boundaries(self, boundaries: list[Any]) -> None:
        self._assert_mutable()
        self.boundaries = list(boundaries)
        self._touch()

    def set_mappings(self, mappings: dict[int, str]) -> None:
        self._assert_mutable()
        self.mappings = dict(mappings)
        self._touch()

    def mark_processing(self, run: ProcessingRun) -> None:
        self._assert_mutable()
        if self.release is None:
            raise InvalidSessionState("cannot start processing without a linked Release")
        if not self.boundaries:
            raise InvalidSessionState("cannot start processing without track boundaries")
        self.last_run = run
        self.state = SessionState.PROCESSING
        self._touch()

    def mark_complete(self, output_folder: Path, output_files: list[str]) -> None:
        self._assert_state(SessionState.PROCESSING)
        assert self.last_run is not None
        self.last_run.completed_at = datetime.now()
        self.last_run.output_folder = output_folder
        self.last_run.output_files = list(output_files)
        self.last_run.final_fraction = 1.0
        self.state = SessionState.COMPLETE
        self._touch()

    def mark_failed(self, error: str) -> None:
        self._assert_state(SessionState.PROCESSING)
        assert self.last_run is not None
        self.last_run.completed_at = datetime.now()
        self.last_run.error = error
        self.state = SessionState.FAILED
        self._touch()

    def can_process(self) -> bool:
        return (
            self.state in {SessionState.READY, SessionState.COMPLETE, SessionState.FAILED}
            and self.release is not None
            and bool(self.boundaries)
        )

    def _assert_mutable(self) -> None:
        self._assert_state(SessionState.READY, SessionState.COMPLETE, SessionState.FAILED)

    def _assert_state(self, *allowed: SessionState) -> None:
        if self.state not in allowed:
            allowed_names = ", ".join(s.value for s in allowed)
            raise InvalidSessionState(
                f"operation not allowed in state {self.state.value}; need one of {allowed_names}"
            )

    def _touch(self) -> None:
        self.updated_at = datetime.now()
