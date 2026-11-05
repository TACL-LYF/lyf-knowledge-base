"""Shared state in Drive: the `_state/` folder, the run lock, cursors, manifest, run log.

    Knowledge Base/_state/
    ├── cursors.json     Discord cursors + when the last successful run started
    ├── manifest.json    one entry per source item (Discord thread/message, Drive file)
    ├── run_log.json     history of runs
    ├── citations.jsonl  running list of citations (kb/citations.py)
    ├── extracted.jsonl  saved records (kb/extract.py)
    ├── programming_types.json
    ├── sessions.json    named sessions of per-session types (kb/extract.py)
    └── lock             held while a run is in progress

Drive is the source of truth. A run takes the lock, downloads `_state/` into
`data/state/`, works on the local copy, and uploads it only after the run succeeds. If
the run fails, the lock is released and nothing is uploaded, so the next run starts
from the same place.

Remote folders are accessed through a small interface (`RemoteFolder`) with a Drive
implementation and an in-memory one for tests.
"""

from __future__ import annotations

import getpass
import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

STATE_FOLDER = "_state"
RAW_FOLDER = "raw"  # raw snapshots, next to _state/ in the Knowledge Base folder
LOCK_NAME = "lock"

CURSORS = "cursors.json"
MANIFEST = "manifest.json"
RUN_LOG = "run_log.json"
STATE_FILES = (CURSORS, MANIFEST, RUN_LOG)
# Written by extraction (kb/extract.py, kb/citations.py):
CITATIONS = "citations.jsonl"
EXTRACTED = "extracted.jsonl"
PROGRAMMING_TYPES = "programming_types.json"
SESSIONS = "sessions.json"

RUN_LOG_KEEP = 200  # older run log entries are dropped

FOLDER_MIME = "application/vnd.google-apps.folder"
NUM_RETRIES = 5


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> datetime | None:
    """Parse an RFC 3339 timestamp (Drive uses fractional seconds, we don't)."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def who() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # no login name (e.g. some containers)
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


# ---------------------------------------------------------------------------
# Remote folders
# ---------------------------------------------------------------------------


class RemoteFolder(Protocol):
    def list(self) -> list[dict[str, Any]]:
        """Files (not subfolders) in the folder: dicts with id, name, createdTime."""

    def read(self, name: str) -> bytes | None:
        """Contents of the file with this name, or None if there isn't one."""

    def read_id(self, file_id: str) -> bytes:
        """Contents of the file with this ID (from `list`)."""

    def write(self, name: str, data: bytes, mime: str = "application/json") -> None:
        """Create the file, or replace the contents of the existing one with this name."""

    def create(self, name: str, data: bytes, mime: str = "application/json") -> str:
        """Always create a new file (even if one has this name); returns its ID."""

    def trash_id(self, file_id: str) -> None:
        """Move a file to the trash (removes it from listings)."""

    def subfolder(self, name: str, create: bool = True) -> RemoteFolder | None:
        """The child folder with this name: created if missing, or None if `create=False`."""


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveFolder:
    """A Drive folder (works in Shared Drives)."""

    def __init__(self, drive: Any, folder_id: str) -> None:
        self.drive = drive
        self.id = folder_id

    def _query(self, q: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        while True:
            page = (
                self.drive.files()
                .list(
                    q=f"'{self.id}' in parents and trashed = false and {q}",
                    fields="nextPageToken, files(id, name, mimeType, createdTime)",
                    pageSize=1000,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    orderBy="createdTime",
                )
                .execute(num_retries=NUM_RETRIES)
            )
            out.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return out

    def list(self) -> list[dict[str, Any]]:
        return self._query(f"mimeType != '{FOLDER_MIME}'")

    def _find(self, name: str) -> dict[str, Any] | None:
        found = self._query(f"name = '{_q(name)}' and mimeType != '{FOLDER_MIME}'")
        return found[0] if found else None

    def read(self, name: str) -> bytes | None:
        f = self._find(name)
        if f is None:
            return None
        return self.read_id(f["id"])

    def read_id(self, file_id: str) -> bytes:
        data = (
            self.drive.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute(num_retries=NUM_RETRIES)
        )
        return data if isinstance(data, bytes) else str(data).encode("utf-8")

    def _media(self, data: bytes, mime: str) -> Any:
        from googleapiclient.http import MediaInMemoryUpload

        return MediaInMemoryUpload(data, mimetype=mime, resumable=False)

    def write(self, name: str, data: bytes, mime: str = "application/json") -> None:
        f = self._find(name)
        if f is None:
            self.create(name, data, mime)
            return
        self.drive.files().update(
            fileId=f["id"], media_body=self._media(data, mime), supportsAllDrives=True
        ).execute(num_retries=NUM_RETRIES)

    def create(self, name: str, data: bytes, mime: str = "application/json") -> str:
        f = (
            self.drive.files()
            .create(
                body={"name": name, "parents": [self.id]},
                media_body=self._media(data, mime),
                fields="id",
                supportsAllDrives=True,
            )
            .execute(num_retries=NUM_RETRIES)
        )
        return f["id"]

    def trash_id(self, file_id: str) -> None:
        # Trash rather than delete: in a Shared Drive, a Content manager can trash files
        # but not delete them permanently (Drive answers a delete with 404 "File not
        # found"). Trashed files are left out of every listing and Drive empties the
        # Shared Drive trash after 30 days.
        self.drive.files().update(
            fileId=file_id, body={"trashed": True}, supportsAllDrives=True
        ).execute(num_retries=NUM_RETRIES)

    def subfolder(self, name: str, create: bool = True) -> DriveFolder | None:
        found = self._query(f"name = '{_q(name)}' and mimeType = '{FOLDER_MIME}'")
        if found:
            return DriveFolder(self.drive, found[0]["id"])
        if not create:
            return None
        f = (
            self.drive.files()
            .create(
                body={"name": name, "parents": [self.id], "mimeType": FOLDER_MIME},
                fields="id",
                supportsAllDrives=True,
            )
            .execute(num_retries=NUM_RETRIES)
        )
        return DriveFolder(self.drive, f["id"])


class MemoryFolder:
    """In-memory RemoteFolder for tests."""

    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.folders: dict[str, MemoryFolder] = {}
        self._n = 0

    def list(self) -> list[dict[str, Any]]:
        return [{k: f[k] for k in ("id", "name", "createdTime")} for f in self.files]

    def read(self, name: str) -> bytes | None:
        f = next((f for f in self.files if f["name"] == name), None)
        return None if f is None else f["data"]

    def read_id(self, file_id: str) -> bytes:
        return next(f["data"] for f in self.files if f["id"] == file_id)

    def write(self, name: str, data: bytes, mime: str = "application/json") -> None:
        f = next((f for f in self.files if f["name"] == name), None)
        if f is None:
            self.create(name, data, mime)
        else:
            f["data"] = data

    def create(self, name: str, data: bytes, mime: str = "application/json") -> str:
        self._n += 1
        fid = f"f{self._n}"
        created = (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self._n)).isoformat()
        self.files.append({"id": fid, "name": name, "data": data, "createdTime": created})
        return fid

    def trash_id(self, file_id: str) -> None:
        self.files = [f for f in self.files if f["id"] != file_id]

    def subfolder(self, name: str, create: bool = True) -> MemoryFolder | None:
        if not create:
            return self.folders.get(name)
        return self.folders.setdefault(name, MemoryFolder())


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class LockError(Exception):
    """Another run holds the lock (or holds a stale one that needs --force-unlock)."""


def _lock_age(info: dict[str, Any], now: datetime) -> timedelta | None:
    acquired = parse_time(info.get("acquired_at"))
    return None if acquired is None else now - acquired


def read_lock(folder: RemoteFolder) -> dict[str, Any] | None:
    """Who holds the lock and since when, or None if nobody does."""
    data = folder.read(LOCK_NAME)
    if data is None:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {}


class Lock:
    """The run lock: a `lock` file in `_state/`.

    Drive has no atomic "create if missing", so after creating our lock file we list
    all lock files and keep ours only if it's the oldest. A run that loses the race
    deletes its file and fails.
    """

    def __init__(
        self,
        folder: RemoteFolder,
        timeout_minutes: int = 60,
        *,
        holder: str | None = None,
        clock: Any = None,
    ) -> None:
        self.folder = folder
        self.timeout = timedelta(minutes=timeout_minutes)
        self.holder = holder or who()
        self.run_id = uuid.uuid4().hex[:12]
        self._clock = clock or (lambda: datetime.now(UTC))
        self.file_id: str | None = None

    def acquire(self, force_unlock: bool = False) -> None:
        now = self._clock()
        existing = [f for f in self.folder.list() if f["name"] == LOCK_NAME]
        if existing:
            info = read_lock(self.folder) or {}
            age = _lock_age(info, now)
            stale = age is None or age > self.timeout
            desc = f"held by {info.get('holder', 'unknown')} since {info.get('acquired_at', '?')}"
            if not stale:
                raise LockError(
                    f"Another run is in progress ({desc}). Wait for it to finish. A lock "
                    f"older than {int(self.timeout.total_seconds() // 60)} minutes counts as "
                    "stale and can be cleared with --force-unlock."
                )
            if not force_unlock:
                raise LockError(
                    f"A stale lock was found ({desc}); the run that took it probably "
                    "crashed. Re-run with --force-unlock to clear it."
                )
            for f in existing:
                self.folder.trash_id(f["id"])

        info = {
            "holder": self.holder,
            "run_id": self.run_id,
            "acquired_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.file_id = self.folder.create(LOCK_NAME, json.dumps(info).encode("utf-8"))
        locks = sorted(
            (f for f in self.folder.list() if f["name"] == LOCK_NAME),
            key=lambda f: (f.get("createdTime") or "", f["id"]),
        )
        if locks and locks[0]["id"] != self.file_id:
            self.folder.trash_id(self.file_id)
            self.file_id = None
            raise LockError("Another run took the lock at the same moment. Try again.")

    def release(self) -> None:
        if self.file_id is not None:
            self.folder.trash_id(self.file_id)
            self.file_id = None

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Local state files
# ---------------------------------------------------------------------------


def empty_cursors() -> dict[str, Any]:
    return {"discord_channel": None, "discord_threads": {}, "last_run_started_at": None}


class State:
    """The local copy of `_state/` in `data/state/`."""

    def __init__(self, local_dir: Path) -> None:
        self.dir = local_dir
        self.cursors: dict[str, Any] = empty_cursors()
        self.manifest: dict[str, dict[str, Any]] = {}
        self.run_log: list[dict[str, Any]] = []

    def _read(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def load(self) -> State:
        self.cursors = {**empty_cursors(), **self._read(CURSORS, {})}
        self.manifest = self._read(MANIFEST, {})
        self.run_log = self._read(RUN_LOG, [])
        return self

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_log = self.run_log[-RUN_LOG_KEEP:]
        for name, data in (
            (CURSORS, self.cursors),
            (MANIFEST, self.manifest),
            (RUN_LOG, self.run_log),
        ):
            text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            (self.dir / name).write_text(text, encoding="utf-8")


def download(folder: RemoteFolder | None, local_dir: Path) -> list[str]:
    """Replace the local copy with the files in `_state/` (except the lock).

    `folder=None` (no `_state/` in Drive yet) leaves an empty local copy.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    for p in local_dir.iterdir():
        if p.is_file():
            p.unlink()
    names: list[str] = []
    if folder is None:
        return names
    for f in folder.list():
        if f["name"] == LOCK_NAME or f["name"] in names:
            continue
        data = folder.read(f["name"])
        if data is not None:
            (local_dir / f["name"]).write_bytes(data)
            names.append(f["name"])
    return names


def upload(folder: RemoteFolder, local_dir: Path) -> list[str]:
    """Upload every file in the local copy to `_state/`, replacing what's there."""
    names = []
    for p in sorted(local_dir.iterdir()):
        if p.is_file() and p.name != LOCK_NAME:
            folder.write(p.name, p.read_bytes())
            names.append(p.name)
    return names
