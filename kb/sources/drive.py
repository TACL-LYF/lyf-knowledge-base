"""Google Drive source: list files in the source folders and export their text.

Each source folder is read recursively: files in its subfolders count too.

Exporters (each returns markdown):
- Docs   → Drive's built-in markdown export
- Slides → text on every slide plus its speaker notes (Slides API)
- Sheets → every grid tab as a markdown table (Sheets API)
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations.readonly",
]

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
        creds = credentials(key_path)
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


def list_folder_files(
    drive: Any, folder_ids: Iterable[str], mime_types: Iterable[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Every file inside any of the folders, including files in their subfolders.

    A file reachable from more than one folder is returned once, and a folder reached
    twice is only read once. If mime_types is given, only files of those types are
    returned.
    """
    wanted = set(mime_types) if mime_types is not None else None
    seen_files: set[str] = set()
    seen_folders: set[str] = set()
    queue = list(folder_ids)
    while queue:
        folder_id = queue.pop(0)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        token: str | None = None
        while True:
            page = list_children(drive, folder_id, page_size=1000, page_token=token)
            for f in page.get("files", []):
                if f["mimeType"] == FOLDER_MIME:
                    queue.append(f["id"])
                    continue
                if f["id"] in seen_files:
                    continue
                if wanted is not None and f["mimeType"] not in wanted:
                    continue
                seen_files.add(f["id"])
                yield f
            token = page.get("nextPageToken")
            if not token:
                break


def get_file(drive: Any, file_id: str, fields: str = FILE_FIELDS) -> dict[str, Any]:
    return (
        drive.files()
        .get(fileId=file_id, fields=fields, supportsAllDrives=True)
        .execute(num_retries=NUM_RETRIES)
    )


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


def export_doc(drive: Any, file_id: str) -> str:
    data = (
        drive.files()
        .export(fileId=file_id, mimeType="text/markdown")
        .execute(num_retries=NUM_RETRIES)
    )
    text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
    return text.strip() + "\n"


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
