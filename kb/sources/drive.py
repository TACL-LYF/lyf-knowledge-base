"""Google Drive source: list files in the source folders and export their text.

Each source folder is read recursively: files in its subfolders count too.

Exporters (each returns markdown):
- Docs   → Drive's built-in markdown export, with embedded images replaced by `[image]`
- Slides → text on every slide plus its speaker notes (Slides API)
- Sheets → every grid tab as a markdown table (Sheets API)
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from google.oauth2 import service_account
from googleapiclient.discovery import build

from kb.config import DEFAULT_EXPORT_WORKERS

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations.readonly",
]

T = TypeVar("T")

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"
SOURCE_TYPE = {DOC_MIME: "doc", SHEET_MIME: "sheet", SLIDES_MIME: "slides"}

FILE_FIELDS = (
    "id, name, mimeType, modifiedTime, webViewLink, owners(displayName), "
    "lastModifyingUser(displayName)"
)
NUM_RETRIES = 5  # googleapiclient retries 429 and 5xx with exponential backoff


def credentials(key_path: Path) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(str(key_path), scopes=SCOPES)


def drive_service(key_path: Path) -> Any:
    return build("drive", "v3", credentials=credentials(key_path), cache_discovery=False)


def service_account_email(key_path: Path) -> str:
    return credentials(key_path).service_account_email


@dataclass
class Services:
    """API clients needed to list and export files."""

    drive: Any
    sheets: Any
    slides: Any

    @classmethod
    def from_key(cls, key_path: Path) -> Services:
        return cls.from_credentials(credentials(key_path))

    @classmethod
    def from_credentials(cls, creds: service_account.Credentials) -> Services:
        return cls(
            drive=build("drive", "v3", credentials=creds, cache_discovery=False),
            sheets=build("sheets", "v4", credentials=creds, cache_discovery=False),
            slides=build("slides", "v1", credentials=creds, cache_discovery=False),
        )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_children(
    drive: Any, folder_id: str, *, page_size: int = 100, page_token: str | None = None
) -> dict[str, Any]:
    """One page of the direct children of a folder (files and subfolders)."""
    return (
        drive.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields=f"nextPageToken, files({FILE_FIELDS})",
            pageSize=page_size,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute(num_retries=NUM_RETRIES)
    )


def list_all_children(drive: Any, folder_id: str) -> list[dict[str, Any]]:
    """Every direct child of a folder, reading all pages."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        page = list_children(drive, folder_id, page_size=1000, page_token=token)
        out += page.get("files", [])
        token = page.get("nextPageToken")
        if not token:
            return out


def list_folder_files(
    drive: Any,
    folder_ids: Iterable[str],
    mime_types: Iterable[str] | None = None,
    log: Callable[[str], None] = lambda _: None,
    *,
    make_drive: Callable[[], Any] | None = None,
    workers: int = 1,
) -> Iterator[dict[str, Any]]:
    """Every file inside any of the folders, including files in their subfolders.

    A file reachable from more than one folder is returned once, and a folder reached
    twice is only read once. If mime_types is given, only files of those types are
    returned. Progress (folders read, files found) is logged every 10 folders.

    Folders are read breadth-first, one level at a time. With `make_drive` and
    `workers` > 1, the folders of a level are read `workers` at a time, each thread with
    its own Drive client from `make_drive`; results are still handled in folder order,
    so files and progress lines are the same whatever the number of workers.
    """
    wanted = set(mime_types) if mime_types is not None else None
    seen_files: set[str] = set()
    seen_folders: set[str] = set()
    level = list(folder_ids)
    pool = None
    if make_drive is not None and workers > 1:
        client = per_thread(make_drive)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="list")
    try:
        while level:
            # Each folder is read once: drop repeats within the level and folders read
            # in earlier levels (the order of the rest is kept).
            todo = list(dict.fromkeys(f for f in level if f not in seen_folders))
            if pool is not None:
                pages = pool.map(lambda fid: list_all_children(client(), fid), todo)
            else:
                pages = (list_all_children(drive, fid) for fid in todo)
            next_level: list[str] = []
            for n, (folder_id, children) in enumerate(zip(todo, pages, strict=True), 1):
                seen_folders.add(folder_id)
                for f in children:
                    if f["mimeType"] == FOLDER_MIME:
                        next_level.append(f["id"])
                        continue
                    if f["id"] in seen_files:
                        continue
                    if wanted is not None and f["mimeType"] not in wanted:
                        continue
                    seen_files.add(f["id"])
                    yield f
                queued = len(todo) - n + len(set(next_level) - seen_folders)
                if len(seen_folders) % 10 == 0 or not queued:
                    log(
                        f"folders read: {len(seen_folders)} ({queued} queued), "
                        f"in-scope files found: {len(seen_files)}"
                    )
            level = next_level
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def get_file(drive: Any, file_id: str, fields: str = FILE_FIELDS) -> dict[str, Any]:
    return (
        drive.files()
        .get(fileId=file_id, fields=fields, supportsAllDrives=True)
        .execute(num_retries=NUM_RETRIES)
    )


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


# Drive's markdown export embeds each image as base64: `![][image1]` in the text and a
# `[image1]: <data:image/png;base64,...>` definition at the end. Images were about 90% of
# all exported text, the model doesn't read them, and the original Doc (which citations
# link to) still has them.
_DATA_DEF = re.compile(
    r"^[ \t]*\[([^\]\n]+)\]:[ \t]*<?data:[^\s>]*>?"
    r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?"  # optional title
    r"[ \t]*(?:\n|$)",
    re.M,
)
_INLINE_DATA_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(<?data:[^)\s]*>?\)")
_RUN_OF_IMAGES = re.compile(r"\[image\](?:[ \t]*\[image\])+")


def _placeholder(alt: str) -> str:
    alt = alt.strip()
    return f"[image: {alt}]" if alt else "[image]"


def strip_embedded_images(md: str) -> str:
    """Replace base64-embedded images with `[image]` (or `[image: alt text]`).

    Done before the text is saved or hashed, so replacing a picture in a Doc doesn't
    change its content hash. A run of images with nothing between them becomes a single
    `[image]`. Images linked by URL are left alone.
    """
    labels = {m.group(1).lower() for m in _DATA_DEF.finditer(md)}
    md = _DATA_DEF.sub("", md)
    if labels:

        def ref(m: re.Match[str]) -> str:
            label = (m.group(2) or m.group(1)).lower()
            return _placeholder(m.group(1)) if label in labels else m.group(0)

        md = re.sub(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]", ref, md)
    md = _INLINE_DATA_IMAGE.sub(lambda m: _placeholder(m.group(1)), md)
    return _RUN_OF_IMAGES.sub("[image]", md)


def export_doc(drive: Any, file_id: str) -> str:
    data = (
        drive.files()
        .export(fileId=file_id, mimeType="text/markdown")
        .execute(num_retries=NUM_RETRIES)
    )
    text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
    return strip_embedded_images(text).strip() + "\n"


# ---------------------------------------------------------------------------
# Slides
# ---------------------------------------------------------------------------

SLIDES_FIELDS = (
    "title,"
    "slides(objectId,pageElements,"
    "slideProperties(notesPage(notesProperties(speakerNotesObjectId),pageElements)))"
)


def _text_of(text: dict[str, Any] | None) -> str:
    if not text:
        return ""
    parts = [el.get("textRun", {}).get("content", "") for el in text.get("textElements", [])]
    return "".join(parts).strip()


def _element_lines(el: dict[str, Any]) -> list[str]:
    """Text lines from one page element: shapes, tables, and groups (recursively)."""
    if "shape" in el:
        t = _text_of(el["shape"].get("text"))
        return [t] if t else []
    if "table" in el:
        rows = []
        for row in el["table"].get("tableRows", []):
            cells = [_text_of(c.get("text")).replace("\n", " ") for c in row.get("tableCells", [])]
            if any(cells):
                rows.append(" | ".join(cells))
        return rows
    if "elementGroup" in el:
        children = el["elementGroup"].get("children", [])
        return [line for child in children for line in _element_lines(child)]
    return []


def _speaker_notes(slide: dict[str, Any]) -> str:
    notes_page = slide.get("slideProperties", {}).get("notesPage", {})
    notes_id = notes_page.get("notesProperties", {}).get("speakerNotesObjectId")
    for el in notes_page.get("pageElements", []):
        if el.get("objectId") == notes_id:
            return _text_of(el.get("shape", {}).get("text"))
    return ""


def render_presentation(pres: dict[str, Any]) -> str:
    """Markdown for a presentation: one section per slide, speaker notes after the text."""
    out: list[str] = []
    for n, slide in enumerate(pres.get("slides", []), 1):
        out.append(f"## Slide {n}\n")
        lines = [line for el in slide.get("pageElements", []) for line in _element_lines(el)]
        out.append("\n\n".join(lines) + "\n" if lines else "_(no text)_\n")
        notes = _speaker_notes(slide)
        if notes:
            out.append(f"**Speaker notes:**\n\n{notes}\n")
    return "\n".join(out)


def export_slides(slides: Any, file_id: str) -> str:
    pres = (
        slides.presentations()
        .get(presentationId=file_id, fields=SLIDES_FIELDS)
        .execute(num_retries=NUM_RETRIES)
    )
    return render_presentation(pres)


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------

RANGES_PER_REQUEST = 50


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>").strip()


def render_table(values: list[list[Any]]) -> str:
    """A markdown table from a tab's values; the first non-blank row is the header."""
    rows = [[_cell(v) for v in row] for row in values]
    rows = [r for r in rows if any(r)]
    if not rows:
        return "_(empty)_\n"
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines) + "\n"


def _a1_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def export_sheet(sheets: Any, file_id: str) -> str:
    meta = (
        sheets.spreadsheets()
        .get(spreadsheetId=file_id, fields="sheets(properties(title,sheetType,hidden))")
        .execute(num_retries=NUM_RETRIES)
    )
    tabs = [
        s["properties"]
        for s in meta.get("sheets", [])
        if s["properties"].get("sheetType", "GRID") == "GRID"
    ]
    values: dict[str, list[list[Any]]] = {}
    for i in range(0, len(tabs), RANGES_PER_REQUEST):
        chunk = tabs[i : i + RANGES_PER_REQUEST]
        resp = (
            sheets.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=file_id,
                ranges=[_a1_sheet(t["title"]) for t in chunk],
                valueRenderOption="FORMATTED_VALUE",
            )
            .execute(num_retries=NUM_RETRIES)
        )
        for tab, vr in zip(chunk, resp.get("valueRanges", []), strict=False):
            values[tab["title"]] = vr.get("values", [])

    out = []
    for tab in tabs:
        hidden = " (hidden tab)" if tab.get("hidden") else ""
        out.append(f"## {tab['title']}{hidden}\n\n{render_table(values.get(tab['title'], []))}")
    return "\n".join(out) if out else "_(no tabs)_\n"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def export_text(services: Services, f: dict[str, Any]) -> str:
    """Body text of one in-scope file as markdown."""
    mime = f["mimeType"]
    if mime == DOC_MIME:
        return export_doc(services.drive, f["id"])
    if mime == SLIDES_MIME:
        return export_slides(services.slides, f["id"])
    if mime == SHEET_MIME:
        return export_sheet(services.sheets, f["id"])
    raise ValueError(f"Unsupported file type: {mime}")


# ---------------------------------------------------------------------------
# Exporting several files at once
# ---------------------------------------------------------------------------
#
# Nearly all export time is spent waiting on HTTP round-trips, so files are exported on
# threads. Google's batch endpoint can't do media downloads (files.export), so batching
# requests isn't an option.


def per_thread(make: Callable[[], T]) -> Callable[[], T]:
    """A getter for an object built once per thread with `make` (API clients can't be
    shared between threads: they use httplib2, which isn't thread-safe)."""
    local = threading.local()

    def get() -> T:
        if not hasattr(local, "value"):
            local.value = make()
        return local.value

    return get


def threaded_export(make_services: Callable[[], Services]) -> Callable[[dict[str, Any]], str]:
    """`export_text` that's safe to call from several threads at once.

    Each thread builds its own Services (with `make_services`) the first time it
    exports a file; see `per_thread`.
    """
    services = per_thread(make_services)
    return lambda f: export_text(services(), f)


@dataclass
class Exported:
    """One file's export: its body, or the error that stopped it."""

    file: dict[str, Any]
    body: str | None = None
    error: str | None = None


def export_files(
    files: list[dict[str, Any]],
    export: Callable[[dict[str, Any]], str],
    workers: int = DEFAULT_EXPORT_WORKERS,
) -> Iterator[Exported]:
    """Export the files on `workers` threads, yielding results in file order.

    `export` must be safe to call from several threads (see `threaded_export`). The
    caller handles each result (writing files, recording errors, logging) on its own
    thread as it comes, so output and logs are the same as exporting one at a time.
    An error in one file is returned in its result, not raised.
    """

    def one(f: dict[str, Any]) -> Exported:
        try:
            return Exported(f, body=export(f))
        except Exception as e:  # one bad file shouldn't stop the others
            return Exported(f, error=f"{type(e).__name__}: {e}")

    if workers <= 1 or len(files) <= 1:
        yield from map(one, files)
        return
    pool = ThreadPoolExecutor(max_workers=min(workers, len(files)), thread_name_prefix="export")
    try:
        yield from pool.map(one, files)
    finally:
        # If the caller stops early (an error, Ctrl-C), don't start the files still queued.
        pool.shutdown(wait=True, cancel_futures=True)


class RetryCounter(logging.Handler):
    """Counts googleapiclient's retries (429s, rate-limit 403s, 5xx, timeouts) while active.

    Used as a context manager around exports, so the run can say how often Google asked
    it to slow down.
    """

    LOGGER = "googleapiclient.http"

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.retries = 0
        self.rate_limited = 0
        self._lock_count = threading.Lock()
        self._logger = logging.getLogger(self.LOGGER)
        self._saved_propagate = True

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if not msg.startswith("Sleeping ") or " before retry " not in msg:
            return
        with self._lock_count:
            self.retries += 1
            if msg.endswith(("after 429", "after 403")):
                self.rate_limited += 1

    def __enter__(self) -> RetryCounter:
        self._logger.addHandler(self)
        if self._logger.level == logging.NOTSET or self._logger.level > logging.WARNING:
            self._saved_level: int | None = self._logger.level
            self._logger.setLevel(logging.WARNING)
        else:
            self._saved_level = None
        # Counted and summarized instead of printed one by one between progress lines.
        self._saved_propagate = self._logger.propagate
        self._logger.propagate = False
        return self

    def __exit__(self, *exc: object) -> None:
        self._logger.removeHandler(self)
        self._logger.propagate = self._saved_propagate
        if self._saved_level is not None:
            self._logger.setLevel(self._saved_level)

    def summary(self) -> str:
        """E.g. ", 12 retries (10 rate-limited)"; empty when there were none."""
        if not self.retries:
            return ""
        s = "y" if self.retries == 1 else "ies"
        return f", {self.retries} retr{s} ({self.rate_limited} rate-limited)"


def file_metadata(f: dict[str, Any]) -> dict[str, Any]:
    owners = [o.get("displayName") for o in f.get("owners", []) if o.get("displayName")]
    return {
        "id": f"drive:{f['id']}",
        "source": SOURCE_TYPE.get(f["mimeType"], f["mimeType"]),
        "title": f.get("name"),
        "link": f.get("webViewLink") or f"https://drive.google.com/open?id={f['id']}",
        # Files in a Shared Drive have no owner; fall back to the last person to edit.
        "owner": ", ".join(owners) or f.get("lastModifyingUser", {}).get("displayName"),
        "modified_time": f.get("modifiedTime"),
    }


def render_file(f: dict[str, Any], body: str) -> str:
    """The saved export: YAML front matter (metadata), the title, then the body."""
    meta = yaml.safe_dump(file_metadata(f), sort_keys=False, allow_unicode=True)
    return f"---\n{meta}---\n\n# {f.get('name')}\n\n{body.rstrip()}\n"
