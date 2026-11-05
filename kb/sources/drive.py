"""Google Drive source: list files in the source folder and export their text.

M0 has just enough to prove access (list one file, check the KB folder is writable).
M1 adds recursive listing and exporters; M2 adds the manifest comparison.
"""

from __future__ import annotations

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


def get_file(drive: Any, file_id: str, fields: str = FILE_FIELDS) -> dict[str, Any]:
    return drive.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()
