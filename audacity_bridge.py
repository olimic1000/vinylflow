"""VinylFlow — talking to a running Audacity through mod-script-pipe.

Audacity ships a scripting module, mod-script-pipe, that listens on a pair
of named pipes once the user enables it (Preferences → Modules →
mod-script-pipe → Enabled, then restart Audacity).  VinylFlow's backend runs
on the same machine, so it can drive Audacity directly:

- ``fetch_recording``  export the open project to a WAV and read its labels
- ``write_labels``     put Track Boundaries back into the project as a
                       label track named ``VINYLFLOW_LABEL_TRACK``

Protocol: write one command per line (``Command: Param=value ...``); the
reply is the command's output, then a ``BatchCommand finished: OK`` line,
then an empty line.  See https://manual.audacityteam.org/man/scripting.html.

Only one client can hold the pipes at a time, so a single ``AudacityPipe``
is shared behind a lock (``connection``) and reconnected when Audacity
restarts.  Nothing here works inside Docker: the pipes live on the host.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

VINYLFLOW_LABEL_TRACK = "VinylFlow"

CONNECT_TIMEOUT = 5.0
COMMAND_TIMEOUT = 30.0
# Exporting a whole side (or a whole box set) can take a while.
EXPORT_TIMEOUT = 600.0

ENABLE_HELP = (
    "In Audacity, open Preferences → Modules, set mod-script-pipe to Enabled, "
    "then quit and reopen Audacity."
)


class AudacityUnavailable(RuntimeError):
    """Audacity isn't running, or its scripting module isn't enabled."""


class AudacityCommandError(RuntimeError):
    """Audacity answered, but the command failed."""


# --- pipe locations ---------------------------------------------------------


def pipe_paths() -> Tuple[str, str]:
    """(to-Audacity, from-Audacity) pipe paths for this platform and user."""
    if sys.platform == "win32":
        return r"\\.\pipe\ToSrvPipe", r"\\.\pipe\FromSrvPipe"
    uid = os.getuid()
    return f"/tmp/audacity_script_pipe.to.{uid}", f"/tmp/audacity_script_pipe.from.{uid}"


def running_in_container() -> bool:
    """True inside Docker, where the host's Audacity pipes are out of reach."""
    return os.path.exists("/.dockerenv")


# --- reply parsing (pure) ---------------------------------------------------

_FINISHED = "BatchCommand finished:"


def split_reply(reply: str) -> Tuple[str, bool]:
    """Split a raw reply into (output, ok)."""
    body_lines = []
    ok = False
    for line in reply.splitlines():
        if line.startswith(_FINISHED):
            ok = line[len(_FINISHED):].strip().upper().startswith("OK")
        else:
            body_lines.append(line)
    return "\n".join(body_lines).strip(), ok


def quote(value: str) -> str:
    """Quote a string parameter.  Audacity has no escape for '"', so drop it."""
    return '"' + str(value).replace('"', "'").replace("\n", " ").replace("\r", " ") + '"'


# --- the pipe ---------------------------------------------------------------


class AudacityPipe:
    """One open connection to mod-script-pipe."""

    def __init__(self, to_path: Optional[str] = None, from_path: Optional[str] = None):
        default_to, default_from = pipe_paths()
        self.to_path = to_path or default_to
        self.from_path = from_path or default_from
        self._eol = "\r\n\0" if sys.platform == "win32" else "\n"
        self._to = None
        self._from_fd: Optional[int] = None
        self._from_file = None
        self._buffer = b""

    # Connecting -------------------------------------------------------------

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        if sys.platform == "win32":
            self._connect_windows()
        else:
            self._connect_posix(timeout)

    def _connect_windows(self) -> None:
        try:
            self._to = open(self.to_path, "w", encoding="utf-8")
            self._from_file = open(self.from_path, "r", encoding="utf-8")
        except OSError:
            self.close()
            raise AudacityUnavailable(f"Couldn't reach Audacity. Is it open? {ENABLE_HELP}") from None

    def _connect_posix(self, timeout: float) -> None:
        if not os.path.exists(self.to_path):
            raise AudacityUnavailable(f"Couldn't reach Audacity. Is it open? {ENABLE_HELP}")
        # A non-blocking open fails at once with ENXIO when nobody is reading
        # the FIFO — a pipe left behind by an Audacity that has quit — instead
        # of hanging forever the way a plain open() would.
        try:
            fd = os.open(self.to_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.ENXIO, errno.ENOENT):
                raise AudacityUnavailable(
                    f"Audacity isn't running, or its scripting module is off. {ENABLE_HELP}"
                ) from None
            raise
        os.set_blocking(fd, True)
        self._to = os.fdopen(fd, "w", encoding="utf-8")

        # Audacity opens its reply pipe for writing only after it sees us on
        # the request pipe, so this open waits for it — bounded by a timeout.
        result: dict = {}

        def _open_reply():
            try:
                result["fd"] = os.open(self.from_path, os.O_RDONLY)
            except OSError as e:  # pragma: no cover - surfaced below
                result["error"] = e

        opener = threading.Thread(target=_open_reply, daemon=True)
        opener.start()
        opener.join(timeout)
        if "fd" not in result:
            self.close()
            raise AudacityUnavailable(f"Audacity didn't answer. {ENABLE_HELP}")
        self._from_fd = result["fd"]

    def close(self) -> None:
        for closer in (
            lambda: self._to and self._to.close(),
            lambda: self._from_file and self._from_file.close(),
            lambda: self._from_fd is not None and os.close(self._from_fd),
        ):
            try:
                closer()
            except OSError:
                pass
        self._to = self._from_file = None
        self._from_fd = None
        self._buffer = b""

    # Talking ----------------------------------------------------------------

    def command(self, cmd: str, timeout: float = COMMAND_TIMEOUT) -> str:
        """Send one command; return its output.  Raises on failure."""
        if self._to is None:
            raise AudacityUnavailable("Not connected to Audacity.")
        try:
            self._to.write(cmd + self._eol)
            self._to.flush()
            reply = self._read_reply(timeout)
        except (BrokenPipeError, ConnectionError, ValueError, OSError):
            self.close()
            raise AudacityUnavailable("Lost the connection to Audacity. Is it still open?") from None
        output, ok = split_reply(reply)
        if not ok:
            raise AudacityCommandError(f"Audacity couldn't run {cmd.split(':', 1)[0]}: {output or 'no details'}")
        return output

    def json_command(self, cmd: str, timeout: float = COMMAND_TIMEOUT):
        output = self.command(cmd, timeout)
        try:
            return json.loads(output) if output else []
        except json.JSONDecodeError:
            raise AudacityCommandError(f"Unexpected reply from Audacity to {cmd!r}") from None

    def _read_reply(self, timeout: float) -> str:
        lines: List[str] = []
        while True:
            line = self._readline(timeout)
            if line == "" and lines:
                return "\n".join(lines)
            if line:
                lines.append(line)

    def _readline(self, timeout: float) -> str:
        if self._from_file is not None:  # Windows: plain blocking file
            line = self._from_file.readline()
            if not line:
                raise ConnectionError("pipe closed")
            return line.rstrip("\r\n\0")

        import select

        while b"\n" not in self._buffer:
            ready, _, _ = select.select([self._from_fd], [], [], timeout)
            if not ready:
                raise TimeoutError
            chunk = os.read(self._from_fd, 65536)
            if not chunk:
                raise ConnectionError("pipe closed")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("utf-8", errors="replace").rstrip("\r\0")


# Audacity serves one client, so VinylFlow keeps one connection for all
# requests and serialises them.
_lock = threading.Lock()
_shared: Optional[AudacityPipe] = None
_pipe_factory: Callable[[], AudacityPipe] = AudacityPipe


def _alive(pipe: AudacityPipe) -> bool:
    """Cheap round trip: False when Audacity has quit (or restarted) since."""
    try:
        pipe.command('Message: Text=""', timeout=CONNECT_TIMEOUT)
        return True
    except (AudacityUnavailable, AudacityCommandError):
        return False


@contextmanager
def connection() -> Iterator[AudacityPipe]:
    """The shared pipe, connected, held exclusively for the ``with`` block.

    A connection kept from an earlier request is checked first, so the first
    click after the user restarts Audacity reconnects instead of failing.
    """
    global _shared
    with _lock:
        if _shared is not None and not _alive(_shared):
            _shared.close()
            _shared = None
        if _shared is None:
            pipe = _pipe_factory()
            pipe.connect()
            _shared = pipe
        try:
            yield _shared
        except AudacityUnavailable:
            _shared.close()
            _shared = None
            raise


def reset_connection() -> None:
    """Drop the shared connection (tests, shutdown)."""
    global _shared
    with _lock:
        if _shared is not None:
            _shared.close()
        _shared = None


# --- project operations -----------------------------------------------------


@dataclass
class Recording:
    """What ``fetch_recording`` pulled out of Audacity."""

    path: Path  # the exported audio (.wav or .aiff, matching its contents)
    name: str
    duration: float
    labels: List[Tuple[float, float, str]]
    label_source: str  # "" (no labels), or the label track(s) they came from


# An export with no audio frames is a bare header: well under this.
MIN_EXPORT_BYTES = 1024


def sniff_audio_suffix(path: Path) -> str:
    """'.aiff' or '.wav' from the file's header, '' when it is neither."""
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
        return ".wav"
    return ""


def wave_tracks(tracks: list) -> list:
    return [t for t in tracks if t.get("kind") == "wave"]


def choose_labels(tracks: list, label_info: list) -> Tuple[List[Tuple[float, float, str]], str]:
    """Pick which labels describe the track splits.

    A label track named ``VINYLFLOW_LABEL_TRACK`` (written by VinylFlow and
    maybe adjusted since) wins; otherwise every label in the project counts.
    ``label_info`` is GetInfo Type=Labels: ``[[track_index, [[s, e, text], ...]], ...]``.
    """
    by_track = []
    for entry in label_info or []:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        index, labels = entry
        name = tracks[index].get("name", "") if isinstance(index, int) and 0 <= index < len(tracks) else ""
        rows = [(float(s), float(e), str(text)) for s, e, text in labels]
        if rows:
            by_track.append((name, rows))

    ours = [rows for name, rows in by_track if name == VINYLFLOW_LABEL_TRACK]
    if ours:
        return ours[-1], VINYLFLOW_LABEL_TRACK
    rows = [row for _, track_rows in by_track for row in track_rows]
    names = ", ".join(dict.fromkeys(name or "Label" for name, _ in by_track))
    return rows, names


def fetch_recording(pipe: AudacityPipe, target: Path) -> Recording:
    """Export the open project's audio next to ``target`` and read its labels.

    Export2 writes in the format last chosen in Audacity's export dialog
    whatever the file is called, so the file is renamed to match its header:
    the returned ``path`` may be ``target`` with a .aiff suffix.

    The export starts at time 0, so label times and VinylFlow boundary times
    line up without any offset.
    """
    tracks = pipe.json_command("GetInfo: Type=Tracks Format=JSON")
    waves = wave_tracks(tracks)
    if not waves:
        raise AudacityCommandError("The Audacity project has no audio in it. Open or record a side first.")
    end = max(float(t.get("end", 0)) for t in waves)
    if end <= 0:
        raise AudacityCommandError("The Audacity project's audio is empty.")

    labels, source = choose_labels(tracks, pipe.json_command("GetInfo: Type=Labels Format=JSON"))

    target.parent.mkdir(parents=True, exist_ok=True)
    # An explicit time + track selection: "SelectAll:" leaves Export2 with
    # nothing to export (seen on Audacity 3.7).
    pipe.command(f"Select: Start=0 End={end:.6f} Track=0 TrackCount={len(tracks)} Mode=Set")
    channels = 1 if all(int(t.get("channels", 2)) == 1 for t in waves) else 2
    pipe.command(f"Export2: Filename={quote(str(target))} NumChannels={channels}", timeout=EXPORT_TIMEOUT)
    if not target.exists() or target.stat().st_size < MIN_EXPORT_BYTES:
        raise AudacityCommandError("Audacity didn't export any audio. Check the project isn't muted or empty.")

    suffix = sniff_audio_suffix(target)
    path = target
    if suffix and suffix != target.suffix.lower():
        path = target.with_suffix(suffix)
        target.rename(path)

    name = waves[0].get("name") or "Audacity recording"
    return Recording(path=path, name=name, duration=end, labels=labels, label_source=source)


def write_labels(pipe: AudacityPipe, rows: List[Tuple[float, float, str]]) -> int:
    """Replace the project's ``VINYLFLOW_LABEL_TRACK`` label track with ``rows``.

    Returns how many labels were written.
    """
    tracks = pipe.json_command("GetInfo: Type=Tracks Format=JSON")

    # Remove the label track a previous send left behind, from the bottom up
    # so indices stay valid.
    for index in reversed(range(len(tracks))):
        t = tracks[index]
        if t.get("kind") == "label" and t.get("name") == VINYLFLOW_LABEL_TRACK:
            pipe.command(f"SelectTracks: Track={index} TrackCount=1 Mode=Set")
            pipe.command("RemoveTracks:")

    tracks = pipe.json_command("GetInfo: Type=Tracks Format=JSON")
    existing = sum(len(labels) for _, labels in pipe.json_command("GetInfo: Type=Labels Format=JSON") or [])

    pipe.command("NewLabelTrack:")
    ours = len(tracks)  # appended at the bottom
    pipe.command(f"SetTrack: Track={ours} Name={quote(VINYLFLOW_LABEL_TRACK)}")

    ordered = sorted(rows, key=lambda r: (r[0], r[1]))
    for i, (start, end, name) in enumerate(ordered):
        pipe.command(f"Select: Start={start:.6f} End={end:.6f} Track={ours} TrackCount=1 Mode=Set")
        pipe.command("AddLabel:")
        # Our track is last, so its labels follow every existing one in
        # Audacity's project-wide label numbering.
        pipe.command(
            f"SetLabel: Label={existing + i} Text={quote(name)} Start={start:.6f} End={end:.6f}"
        )
    pipe.command(f"SelectTracks: Track={ours} TrackCount=1 Mode=Set")
    return len(ordered)
