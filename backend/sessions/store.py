"""SessionStore — in-memory registry of live Sessions.

Replaces the three module-level dicts in ``backend.api`` (``uploaded_files``,
``processing_jobs``, ``websocket_connections`` — well, the first two) once
reads switch over in Step 4.  Step 2 introduces it alongside those dicts
with mirror writes only.

Thread-safety: a single ``threading.Lock`` guards mutations.  Sessions are
handed out by reference; callers mutate them outside the lock.  Within a
single asyncio event loop that is safe (Session mutations don't ``await``),
which is the only access pattern in api.py today.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .session import Session, SessionState


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(
        self,
        session_id: str,
        source_audio: Path,
        source_filename: str,
        source_duration: float,
        source_size: int,
    ) -> Session:
        session = Session(
            id=session_id,
            source_filename=source_filename,
            source_audio=source_audio,
            source_duration=source_duration,
            source_size=source_size,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def remove(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def reap_stale(
        self,
        ttl_hours: float,
        now: Optional[datetime] = None,
    ) -> list[str]:
        """Drop Sessions older than ttl_hours, except those mid-Pipeline.

        Returns the list of Session ids that were removed.  The caller is
        responsible for removing any on-disk artifacts (source file,
        output folder) — the store only owns memory.
        """
        cutoff = (now or datetime.now()) - timedelta(hours=ttl_hours)
        reaped: list[str] = []
        with self._lock:
            for sid, session in list(self._sessions.items()):
                if session.state == SessionState.PROCESSING:
                    continue
                if session.updated_at < cutoff:
                    del self._sessions[sid]
                    reaped.append(sid)
        return reaped
