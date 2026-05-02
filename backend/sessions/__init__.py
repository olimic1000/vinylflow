"""Session module — owns the user-facing unit of work.

A Session is born when a user uploads a Source Audio file and ends when
the Pipeline has produced (or failed to produce) tagged tracks in the
Output Folder.  See ``CONTEXT.md`` for the domain glossary.

This package is intentionally small in Step 1 of the extraction:
``session.py`` holds the dataclass + state machine, ``events.py`` holds
value types (OutputSpec, ProgressEvent, ProcessingRun).  The store and
pipeline arrive in subsequent steps.
"""

from . import pipeline
from .events import OutputSpec, ProcessingRun, ProgressEvent
from .session import InvalidSessionState, Session, SessionState
from .store import SessionStore

__all__ = [
    "InvalidSessionState",
    "OutputSpec",
    "ProcessingRun",
    "ProgressEvent",
    "Session",
    "SessionState",
    "SessionStore",
    "pipeline",
]
