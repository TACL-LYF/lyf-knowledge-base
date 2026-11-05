"""Citation registry: permanent IDs (C-0001…), links, and active/removed status.

One citation per Discord message and one per Drive file. A citation is created the
first time a record cites that message or file, and its ID never changes or gets
reused: an edited Drive file keeps its citation (its date and title are refreshed), and
a deleted message or removed file keeps its citation, marked `removed`.

Saved as `_state/citations.jsonl`, one citation per line. The run lock keeps numbering
safe when several people run the pipeline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

EXCERPT_CHARS = 300
UNKNOWN_AUTHOR = "unknown"

# Titles of Drive files that usually collect several people's words (debriefs, surveys,
# form responses, meeting or brainstorm notes): the file's owner didn't write it all.
COLLECTED = re.compile(
    r"\b(feedback|surveys?|debriefs?|responses?|forms?|retros?|retrospectives?|"
    r"evaluations?|evals?|compiled|consolidated|recaps?|summar(y|ies)|notes|"
    r"brainstorm\w*|meetings?|minutes|kickoff)\b",
    re.IGNORECASE,
)


def drive_author(owner: str | None, title: str | None, owner_is_editor: bool = False) -> str:
    """Who to credit for a Drive file: its owner, unless that's a guess.

    "unknown" when no owner is known (a Shared Drive file, where `owner` is only the
    last person to edit it), or when the title suggests the file collects other
    people's feedback or notes. The owner is still kept in the citation's `owner`.
    """
    if not owner or owner_is_editor or COLLECTED.search(title or ""):
        return UNKNOWN_AUTHOR
    return owner


def discord_key(message_id: str) -> str:
    return f"discord:{message_id}"


def drive_key(file_id: str) -> str:
    """`file_id` is the Drive file ID (without the `drive:` item prefix)."""
    return f"drive:{file_id}"


def normalize_time(value: str | None) -> str | None:
    """`2026-10-16T19:22:05.123000+00:00` → `2026-10-16T19:22:05Z` (UTC timestamps only)."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", value)
    return f"{m.group(1)}Z" if m else value


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Citations:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.by_key: dict[str, dict[str, Any]] = {r["key"]: r for r in self.rows}
        self.by_id: dict[str, dict[str, Any]] = {r["id"]: r for r in self.rows}
        self._next = 1 + max((int(r["id"].split("-")[1]) for r in self.rows), default=0)

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> Citations:
        if not path.is_file():
            return cls()
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls([json.loads(line) for line in lines if line.strip()])

    def save(self, path: Path) -> None:
        text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in self.rows)
        path.write_text(text, encoding="utf-8")

    # -- creating ------------------------------------------------------------

    def _add(self, key: str, fields: dict[str, Any], now: str) -> dict[str, Any]:
        row = {"id": f"C-{self._next:04d}", "key": key, **fields, "first_seen": now}
        row["status"] = "active"
        self._next += 1
        self.rows.append(row)
        self.by_key[key] = row
        self.by_id[row["id"]] = row
        return row

    def for_discord_message(self, item: dict[str, Any], msg: dict[str, Any], now: str) -> str:
        """The citation ID for a Discord message, created on first use."""
        key = discord_key(msg["id"])
        fields = {
            "item_id": item["id"],
            "source": "discord",
            "link": msg.get("link"),
            "author": msg.get("author"),
            "date": normalize_time(msg.get("timestamp")),
            "context": item.get("title"),
            "excerpt": excerpt(msg.get("content") or ""),
        }
        row = self.by_key.get(key)
        if row is None:
            return self._add(key, fields, now)["id"]
        row.update({k: v for k, v in fields.items() if k != "item_id"})  # edited text
        row["status"] = "active"
        return row["id"]

    def for_drive_file(self, entry: dict[str, Any], now: str) -> str:
        """The citation ID for a Drive file (manifest entry), created on first use."""
        key = drive_key(entry["id"].removeprefix("drive:"))
        fields = {
            "item_id": entry["id"],
            "source": entry.get("source"),
            "link": entry.get("link"),
            "author": drive_author(
                entry.get("owner"), entry.get("title"), bool(entry.get("owner_is_editor"))
            ),
            "owner": entry.get("owner"),
            "owner_is_editor": bool(entry.get("owner_is_editor")),
            "date": normalize_time(entry.get("modified_time")),
            "context": entry.get("title"),
            "excerpt": None,
        }
        row = self.by_key.get(key)
        if row is None:
            return self._add(key, fields, now)["id"]
        row.update({k: v for k, v in fields.items() if k != "excerpt"})
        row["status"] = "active"
        return row["id"]

    # -- status --------------------------------------------------------------

    def mark_removed(self, key: str) -> bool:
        """Mark the citation for this key removed. Returns True if it existed."""
        row = self.by_key.get(key)
        if row is None:
            return False
        row["status"] = "removed"
        return True

    def refresh_drive(self, entry: dict[str, Any]) -> None:
        """Keep a file's title, link and owner fresh after a rename (no re-extraction)."""
        row = self.by_key.get(drive_key(entry["id"].removeprefix("drive:")))
        if row is not None:
            row.update(
                {
                    "link": entry.get("link"),
                    "author": drive_author(
                        entry.get("owner"), entry.get("title"), bool(entry.get("owner_is_editor"))
                    ),
                    "owner": entry.get("owner"),
                    "owner_is_editor": bool(entry.get("owner_is_editor")),
                    "context": entry.get("title"),
                }
            )

    def apply_author_rule(self) -> int:
        """Re-credit saved Drive citations with `drive_author`, from what they record
        (citations saved before the rule existed credit the owner). Returns how many
        changed."""
        n = 0
        for row in self.rows:
            if row.get("source") == "discord":
                continue
            owner = row["owner"] if "owner" in row else row.get("author")
            if owner == UNKNOWN_AUTHOR:
                owner = None
            author = drive_author(owner, row.get("context"), bool(row.get("owner_is_editor")))
            if row.get("author") != author or row.get("owner") != owner:
                n += row.get("author") != author
                row["author"], row["owner"] = author, owner
        return n

    def get(self, citation_id: str) -> dict[str, Any] | None:
        return self.by_id.get(citation_id)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out
