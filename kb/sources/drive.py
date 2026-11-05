"""Google Drive source: list files in the source folders and export their text.

Only the direct children of each source folder are read for now; recursive listing
comes in a later milestone. M1 adds exporters; M2 adds the manifest comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations.readonly",
]

FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id, name, mimeType, modifiedTime, webViewLink, owners(displayName)"


def credentials(key_path: Path) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(str(key_path), scopes=SCOPES)


def drive_service(key_path: Path) -> Any:
    return build("drive", "v3", credentials=credentials(key_path), cache_discovery=False)


def service_account_email(key_path: Path) -> str:
    return credentials(key_path).service_account_email


def list_children(
    drive: Any, folder_id: str, *, page_size: int = 100, page_token: str | None = None
) -> dict[str, Any]:
    """One page of the direct children of a folder (not recursive)."""
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
        .execute()
    )


def list_folder_files(
    drive: Any, folder_ids: Iterable[str], mime_types: Iterable[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Every file directly inside any of the folders (subfolders are skipped, not entered).

    A file reachable from more than one folder is returned once. If mime_types is
    given, only files of those types are returned.
    """
    wanted = set(mime_types) if mime_types is not None else None
    seen: set[str] = set()
    for folder_id in folder_ids:
        token: str | None = None
        while True:
            page = list_children(drive, folder_id, page_size=1000, page_token=token)
            for f in page.get("files", []):
                if f["mimeType"] == FOLDER_MIME or f["id"] in seen:
                    continue
                if wanted is not None and f["mimeType"] not in wanted:
                    continue
                seen.add(f["id"])
                yield f
            token = page.get("nextPageToken")
            if not token:
                break


def get_file(drive: Any, file_id: str, fields: str = FILE_FIELDS) -> dict[str, Any]:
    return drive.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()
