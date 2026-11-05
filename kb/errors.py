"""Turn the failures people actually hit into messages that say what to do.

`describe(e, cfg)` returns one or two sentences for a known problem (a Google key that
was deleted, a folder that isn't shared, an Anthropic account out of credit, no
internet, the run lock), or None for anything else, which the CLI then shows with its
type so it can still be looked up. The README's "If something goes wrong" section lists
the same problems.
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any

from kb.config import Config, ConfigError
from kb.sources.discord import DiscordError
from kb.state import LockError


def _sa_email(cfg: Config | None) -> str:
    """The service account's email, for sharing hints, or a description if unreadable."""
    path = cfg.google_credentials_path if cfg else None
    if path is not None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))["client_email"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return "the service account (client_email in the key file)"


def _google_reasons(e: Any) -> set[str]:
    reasons: set[str] = set()
    details = getattr(e, "error_details", None)
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict):
                reasons.update(str(d.get(k)) for k in ("reason", "domain") if d.get(k))
    try:
        body = json.loads(getattr(e, "content", b"") or b"{}")
        err = body.get("error") or {}
        if isinstance(err, dict):
            reasons.add(str(err.get("status") or ""))
            for d in err.get("errors") or []:
                reasons.add(str(d.get("reason") or ""))
            for d in err.get("details") or []:
                reasons.add(str(d.get("reason") or ""))
    except (ValueError, AttributeError, TypeError):
        pass
    reasons.discard("")
    return reasons


def _api_name(e: Any) -> str:
    uri = str(getattr(e, "uri", "") or "")
    for host, name in (
        ("sheets.googleapis", "Google Sheets API"),
        ("slides.googleapis", "Google Slides API"),
        ("docs.googleapis", "Google Docs API"),
    ):
        if host in uri:
            return name
    return "Google Drive API"


def _file_id(e: Any) -> str | None:
    uri = str(getattr(e, "uri", "") or "")
    m = re.search(r"/(?:files|documents|spreadsheets|presentations)/([A-Za-z0-9_-]{10,})", uri)
    return m.group(1) if m else None


def describe_google_http(e: Any, cfg: Config | None = None) -> str | None:
    status = getattr(getattr(e, "resp", None), "status", None)
    reasons = _google_reasons(e)
    email = _sa_email(cfg)
    fid = _file_id(e)
    what = f"file or folder {fid}" if fid else "a file or folder"
    if cfg is not None and fid and fid == cfg.drive_kb_folder_id:
        what = f"the Knowledge Base folder ({fid}, drive.kb_folder_id)"
    elif cfg is not None and fid and fid in cfg.drive_source_folder_ids:
        what = f"source folder {fid} (drive.source_folder_ids)"

    if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
        return (
            f"The {_api_name(e)} isn't enabled in the service account's Google Cloud "
            "project. Enable it under APIs & Services → Library (README step 2.1), wait a "
            "minute, and run again."
        )
    if "storageQuotaExceeded" in reasons:
        return (
            "Google Drive refused to create a file: service accounts have no storage of "
            "their own. The Knowledge Base folder must be in a Shared Drive, with "
            f"{email} as Content manager (README step 3)."
        )
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "RESOURCE_EXHAUSTED"} or (
        status == 429
    ):
        return (
            "Google's rate limit was hit even after retrying. Wait a few minutes and run "
            "again; if it keeps happening, lower drive.export_workers in config.yaml."
        )
    if status == 404:
        return (
            f"Google couldn't find {what}. Either the ID in config.yaml is wrong, or it "
            f"isn't shared with {email}. Share source folders as Viewer and the Knowledge "
            "Base folder's Shared Drive with Content manager (README step 3)."
        )
    if status == 403:
        if reasons & {"insufficientFilePermissions", "cannotAddChildren", "teamDriveFileLimit"}:
            return (
                f"{email} can see {what} but can't change it. Add it to the Knowledge Base "
                "folder's Shared Drive as Content manager (README step 3)."
            )
        return (
            f"Google denied access to {what}. Check that it's shared with {email} "
            "(Viewer for source folders, Content manager for the Knowledge Base Shared "
            "Drive)."
        )
    if status == 401:
        return _GOOGLE_KEY_REJECTED
    if status is not None and int(status) >= 500:
        return f"Google returned a server error ({status}) even after retrying. Try again later."
    if status == 408:
        return "A Google request timed out (408). Check your connection and run again."
    # Anything else: one line, never the HTML page Google sometimes sends as the body.
    reason = (str(getattr(e, "reason", "") or "").splitlines() or [""])[0][:200]
    where = str(getattr(e, "uri", "") or "").split("?")[0]
    return (
        f"Google API error {status}"
        + (f" ({reason})" if reason else "")
        + (f" for {where}" if where else "")
    )


_GOOGLE_KEY_REJECTED = (
    "Google rejected the service account key. It may have been deleted or disabled in "
    "Google Cloud (IAM & Admin → Service Accounts → Keys); get the current key from the "
    "team's password manager, or create a new one (README step 2.3). If the key is "
    "current, check that your computer's clock is right."
)


def describe(e: BaseException, cfg: Config | None = None) -> str | None:
    """A message for a known problem, or None."""
    if isinstance(e, ConfigError | DiscordError | LockError):
        return str(e)

    from googleapiclient.errors import HttpError

    if isinstance(e, HttpError):
        return describe_google_http(e, cfg)

    from google.auth import exceptions as gauth

    if isinstance(e, gauth.RefreshError):
        return _GOOGLE_KEY_REJECTED
    if isinstance(e, gauth.MalformedError) or (
        isinstance(e, ValueError) and "service account info" in str(e).lower()
    ):
        return (
            "The service account key file isn't a valid key. Download it again as JSON "
            "(README step 2.3) and save it as secrets/kb-pipeline.json."
        )
    if isinstance(e, gauth.TransportError):
        return _NO_CONNECTION.format(what="Google")

    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "The Anthropic API rejected the key (401). Check ANTHROPIC_API_KEY in .env."
    if isinstance(e, anthropic.PermissionDeniedError):
        return (
            f"The Anthropic API denied the request (403): {e.message}. The key may belong "
            "to a workspace without access to the model in config.yaml."
        )
    if isinstance(e, anthropic.NotFoundError):
        return (
            "The Anthropic API doesn't know that model. Check llm.extract_model and "
            "llm.synthesis_model in config.yaml."
        )
    if isinstance(e, anthropic.BadRequestError) and is_billing_error(e):
        return (
            "The Anthropic account is out of credit or has hit its spend limit. Add credit "
            "or raise the limit in the Anthropic Console (Billing), then run again. "
            "Answers already received are cached, so they aren't paid for twice."
        )
    if isinstance(e, anthropic.RateLimitError):
        return "The Anthropic API rate limit was hit. Wait a few minutes and run again."
    if isinstance(e, anthropic.APIConnectionError):
        return _NO_CONNECTION.format(what="the Anthropic API")
    if isinstance(e, anthropic.InternalServerError):
        return "The Anthropic API is having problems (5xx/overloaded). Try again later."

    import httplib2
    import httpx

    if isinstance(e, httpx.TransportError):
        return _NO_CONNECTION.format(what="Discord")
    if isinstance(e, httplib2.ServerNotFoundError | socket.gaierror | ConnectionError):
        return _NO_CONNECTION.format(what="Google")
    if isinstance(e, TimeoutError):
        return "A request timed out. Check your connection and run again."
    return None


_NO_CONNECTION = "Couldn't connect to {what}. Check your internet connection and run again."


def is_billing_error(e: Any) -> bool:
    """An Anthropic 400 that means no requests can succeed until billing is fixed."""
    text = str(getattr(e, "message", "") or e).lower()
    return "credit balance" in text or "spend limit" in text or "billing" in text
