"""Publish: the Consolidated Feedback and Programs docs and the Citations sheet.

All three live in the Knowledge Base folder and are updated in place, so their links
never change:

1. Find each file: by the ID saved in `_state/synthesis.json`, else by name in the
   Knowledge Base folder (so a run that failed after creating them doesn't make
   duplicates), else create it (empty, to get its ID for the links between them).
2. Render both docs as markdown (kb/render.py) and upload each with `files.update`,
   which converts markdown to a Google Doc, links included.
3. Rewrite the heading links (`#kb=h2/night-market`) to real `#heading=h.…` links,
   using the heading IDs Google assigned on import (Docs API `batchUpdate`).
4. Rewrite the Citations sheet's two tabs: Citations (one row per citation, removed
   ones included) and Records (one row per record, with a filter so it can be narrowed
   to one year, type, or category).

Google access goes through `Publisher`, with a Drive implementation and a fake in tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from kb import render
from kb.synthesize import Synthesis, SynthState

Log = Callable[[str], None]

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
NUM_RETRIES = 5
UPLOAD_TIMEOUT = 600  # seconds

FEEDBACK_NAME = "Consolidated Feedback"
PROGRAMS_NAME = "Programs"
SHEET_NAME = "Citations"
FILES = (
    ("feedback", FEEDBACK_NAME, DOC_MIME),
    ("programs", PROGRAMS_NAME, DOC_MIME),
    ("citations", SHEET_NAME, SHEET_MIME),
)
CITATIONS_TAB = "Citations"
RECORDS_TAB = "Records"


class Publisher(Protocol):
    def exists(self, file_id: str) -> bool:
        """The file is there and not in the trash."""

    def find(self, name: str, mime: str) -> str | None:
        """The ID of a file with this name and type in the Knowledge Base folder."""

    def create(self, name: str, mime: str) -> str:
        """Create an empty file in the Knowledge Base folder; returns its ID."""

    def upload_markdown(self, doc_id: str, text: str) -> None:
        """Replace the doc's contents with this markdown."""

    def get_doc(self, doc_id: str) -> dict[str, Any]:
        """The doc's structure (Docs API `documents.get`)."""

    def update_doc(self, doc_id: str, requests: list[dict[str, Any]]) -> None:
        """Docs API `batchUpdate`."""

    def write_sheet(self, sheet_id: str, tabs: dict[str, list[list[Any]]], fresh: bool) -> None:
        """Replace each tab's contents (creating missing tabs). `fresh`: just created, so
        its default empty tab can go."""


# ---------------------------------------------------------------------------
# Heading links
# ---------------------------------------------------------------------------

_DOC_ID = re.compile(r"/document/d/([^/#?]+)")


def _walk(content: list[dict[str, Any]]):
    """Every paragraph in a doc body, including those inside tables."""
    for el in content or []:
        if "paragraph" in el:
            yield el["paragraph"]
        elif "table" in el:
            for row in el["table"].get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    yield from _walk(cell.get("content") or [])
        elif "tableOfContents" in el:
            yield from _walk(el["tableOfContents"].get("content") or [])


def headings(doc: dict[str, Any]) -> dict[str, str]:
    """`h<level>/<slug>` → heading ID, for the first heading with that level and text."""
    out: dict[str, str] = {}
    for p in _walk((doc.get("body") or {}).get("content") or []):
        style = p.get("paragraphStyle") or {}
        m = re.fullmatch(r"HEADING_(\d)", style.get("namedStyleType") or "")
        hid = style.get("headingId")
        if not m or not hid:
            continue
        text = "".join((e.get("textRun") or {}).get("content", "") for e in p.get("elements") or [])
        out.setdefault(f"h{m.group(1)}/{render.slug(text.strip())}", hid)
    return out


def link_fixes(
    doc: dict[str, Any], targets: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], int]:
    """Docs API requests turning `#kb=` links into heading links.

    `targets`: doc ID → its `headings()`. A link whose heading isn't found points to the
    top of its doc. Returns (requests, number of links left pointing at the top).
    """
    requests: list[dict[str, Any]] = []
    missing = 0
    for p in _walk((doc.get("body") or {}).get("content") or []):
        for e in p.get("elements") or []:
            run = e.get("textRun") or {}
            url = ((run.get("textStyle") or {}).get("link") or {}).get("url") or ""
            if render.ANCHOR not in url:
                continue
            m = _DOC_ID.search(url)
            if not m:
                continue
            target_doc = m.group(1)
            anchor = url.split(render.ANCHOR, 1)[1]
            hid = (targets.get(target_doc) or {}).get(anchor)
            new = render.doc_url(target_doc) + (f"#heading={hid}" if hid else "")
            missing += hid is None
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": e["startIndex"], "endIndex": e["endIndex"]},
                        "textStyle": {"link": {"url": new}},
                        "fields": "link",
                    }
                }
            )
    return requests, missing


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    ids: dict[str, str] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)  # names of files created this run
    replaced: list[str] = field(default_factory=list)  # saved IDs that no longer worked
    links_fixed: int = 0
    links_missing: int = 0
    warnings: list[str] = field(default_factory=list)

    def urls(self) -> dict[str, str]:
        return {
            "feedback": render.doc_url(self.ids["feedback"]),
            "programs": render.doc_url(self.ids["programs"]),
            "citations": render.sheet_url(self.ids["citations"]),
        }


def ensure_files(pub: Publisher, saved: SynthState, log: Log) -> PublishResult:
    """Find or create the three files, saving their IDs in `saved.docs`."""
    result = PublishResult()
    for key, name, mime in FILES:
        fid = saved.docs.get(key)
        if fid and not pub.exists(fid):
            result.replaced.append(name)
            log(f"warning: the saved {name} file ({fid}) is gone or in the trash; replacing it")
            fid = None
        if not fid:
            fid = pub.find(name, mime)
            if fid:
                log(f"found {name} in the Knowledge Base folder")
        if not fid:
            fid = pub.create(name, mime)
            result.created.append(name)
            log(f"created {name}")
        saved.docs[key] = fid
        result.ids[key] = fid
    return result


def publish(
    syn: Synthesis,
    store: Any,
    saved: SynthState,
    pub: Publisher,
    *,
    now: str,
    by: str,
    log: Log = lambda _: None,
) -> PublishResult:
    """Write both docs and the Citations sheet. `store`: records, citations, types."""
    result = ensure_files(pub, saved, log)
    links = render.Links(result.ids["feedback"], result.ids["programs"], result.ids["citations"])
    feedback_md = render.feedback_doc(syn, links, updated=now, by=by, since=saved.last_published_at)
    programs_md = render.programs_doc(syn, links, updated=now, by=by)
    log(
        f"uploading {FEEDBACK_NAME} ({len(feedback_md) // 1000:,} KB of markdown) and "
        f"{PROGRAMS_NAME} ({len(programs_md) // 1000:,} KB)"
    )
    pub.upload_markdown(result.ids["feedback"], feedback_md)
    pub.upload_markdown(result.ids["programs"], programs_md)

    docs = {k: pub.get_doc(result.ids[k]) for k in ("feedback", "programs")}
    targets = {result.ids[k]: headings(d) for k, d in docs.items()}
    for key, d in docs.items():
        requests, missing = link_fixes(d, targets)
        if requests:
            pub.update_doc(result.ids[key], requests)
        result.links_fixed += len(requests) - missing
        result.links_missing += missing
    if result.links_missing:
        result.warnings.append(
            f"{result.links_missing} section links couldn't find their heading and open the "
            "top of the doc instead"
        )

    log("writing the Citations sheet")
    pub.write_sheet(
        result.ids["citations"],
        {
            CITATIONS_TAB: render.citation_rows(store.citations),
            RECORDS_TAB: render.record_rows(store.records, store.citations, store.types),
        },
        fresh=SHEET_NAME in result.created,
    )
    return result


# ---------------------------------------------------------------------------
# Google implementation
# ---------------------------------------------------------------------------


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def sheet_cell(value: Any) -> Any:
    """A value for USER_ENTERED input: links become HYPERLINK formulas, and text that
    would be read as a formula or number is kept as text."""
    if isinstance(value, render.Link):
        url = value.url.replace('"', '""')
        label = value.label.replace('"', '""')
        return f'=HYPERLINK("{url}","{label}")'
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "'"):
        return "'" + value
    if isinstance(value, str) and re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", value):
        return "'" + value  # keep "2024", "2024-09" and "2024-09-01" as text, alike
    return value


class DrivePublisher:
    def __init__(self, creds: Any, kb_folder_id: str) -> None:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        def client(api: str, version: str) -> Any:
            # Converting a large markdown upload into a Doc can take well over the default
            # 60-second socket timeout.
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=UPLOAD_TIMEOUT))
            return build(api, version, http=http, cache_discovery=False)

        self.drive = client("drive", "v3")
        self.sheets = client("sheets", "v4")
        self.docs = client("docs", "v1")
        self.folder = kb_folder_id

    def exists(self, file_id: str) -> bool:
        from googleapiclient.errors import HttpError

        try:
            f = (
                self.drive.files()
                .get(fileId=file_id, fields="id, trashed", supportsAllDrives=True)
                .execute(num_retries=NUM_RETRIES)
            )
        except HttpError as e:
            if e.resp.status == 404:
                return False
            raise
        return not f.get("trashed")

    def find(self, name: str, mime: str) -> str | None:
        found = (
            self.drive.files()
            .list(
                q=(
                    f"'{self.folder}' in parents and name = '{_q(name)}' and "
                    f"mimeType = '{mime}' and trashed = false"
                ),
                fields="files(id)",
                orderBy="createdTime",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(num_retries=NUM_RETRIES)
            .get("files", [])
        )
        return found[0]["id"] if found else None

    def create(self, name: str, mime: str) -> str:
        f = (
            self.drive.files()
            .create(
                body={"name": name, "mimeType": mime, "parents": [self.folder]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute(num_retries=NUM_RETRIES)
        )
        return f["id"]

    def upload_markdown(self, doc_id: str, text: str) -> None:
        """Replace the doc with the markdown. The doc is emptied first (Docs API): Drive
        converts markdown into an empty doc in under a minute, but replacing a large
        existing doc can run past Google's request timeout (408)."""
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaInMemoryUpload

        self._clear(doc_id)
        for attempt in (1, 2):
            media = MediaInMemoryUpload(
                text.encode("utf-8"), mimetype="text/markdown", resumable=True
            )
            try:
                self.drive.files().update(
                    fileId=doc_id, media_body=media, supportsAllDrives=True
                ).execute(num_retries=NUM_RETRIES)
                return
            except (TimeoutError, HttpError) as e:
                status = getattr(getattr(e, "resp", None), "status", None)
                if attempt == 2 or not (isinstance(e, TimeoutError) or status == 408):
                    raise

    def _clear(self, doc_id: str) -> None:
        content = (self.get_doc(doc_id).get("body") or {}).get("content") or []
        end = content[-1].get("endIndex", 1) if content else 1
        if end > 2:  # an empty doc is a single newline
            self.update_doc(
                doc_id,
                [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1}}}],
            )

    def get_doc(self, doc_id: str) -> dict[str, Any]:
        return self.docs.documents().get(documentId=doc_id).execute(num_retries=NUM_RETRIES)

    def update_doc(self, doc_id: str, requests: list[dict[str, Any]]) -> None:
        for i in range(0, len(requests), 500):
            self.docs.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests[i : i + 500]}
            ).execute(num_retries=NUM_RETRIES)

    def _tabs(self, sheet_id: str) -> dict[str, dict[str, Any]]:
        meta = (
            self.sheets.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties")
            .execute(num_retries=NUM_RETRIES)
        )
        return {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}

    def _empty(self, sheet_id: str, tab: str) -> bool:
        got = (
            self.sheets.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:Z20")
            .execute(num_retries=NUM_RETRIES)
        )
        return not got.get("values")

    def _batch(self, sheet_id: str, requests: list[dict[str, Any]]) -> None:
        if requests:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id, body={"requests": requests}
            ).execute(num_retries=NUM_RETRIES)

    def write_sheet(self, sheet_id: str, tabs: dict[str, list[list[Any]]], fresh: bool) -> None:
        existing = self._tabs(sheet_id)
        setup: list[dict[str, Any]] = []
        for title in tabs:
            if title not in existing:
                setup.append({"addSheet": {"properties": {"title": title}}})
        # A new spreadsheet comes with an empty "Sheet1": remove it (when empty).
        for t, p in existing.items():
            if t not in tabs and (fresh or (t == "Sheet1" and self._empty(sheet_id, t))):
                setup.append({"deleteSheet": {"sheetId": p["sheetId"]}})
        self._batch(sheet_id, setup)
        props = self._tabs(sheet_id)

        # Make each tab big enough, then clear and write it.
        resize = []
        for title, rows in tabs.items():
            p = props[title]
            grid = p.get("gridProperties") or {}
            want_rows, want_cols = max(len(rows), 1), max((len(r) for r in rows), default=1)
            resize.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": p["sheetId"],
                            "gridProperties": {
                                "rowCount": max(want_rows, grid.get("rowCount", 0)),
                                "columnCount": max(want_cols, grid.get("columnCount", 0)),
                                "frozenRowCount": 1,
                            },
                        },
                        "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
                    }
                }
            )
        self._batch(sheet_id, resize)
        values = self.sheets.spreadsheets().values()
        values.batchClear(
            spreadsheetId=sheet_id, body={"ranges": [f"'{t}'" for t in tabs]}
        ).execute(num_retries=NUM_RETRIES)
        values.batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"'{t}'!A1", "values": [[sheet_cell(v) for v in row] for row in rows]}
                    for t, rows in tabs.items()
                ],
            },
        ).execute(num_retries=NUM_RETRIES)

        # Bold header and a filter over every row.
        style = []
        for title, rows in tabs.items():
            sid = props[title]["sheetId"]
            cols = max((len(r) for r in rows), default=1)
            style += [
                {
                    "repeatCell": {
                        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                },
                {
                    "setBasicFilter": {
                        "filter": {
                            "range": {
                                "sheetId": sid,
                                "startRowIndex": 0,
                                "endRowIndex": len(rows),
                                "startColumnIndex": 0,
                                "endColumnIndex": cols,
                            }
                        }
                    }
                },
            ]
        self._batch(sheet_id, style)
