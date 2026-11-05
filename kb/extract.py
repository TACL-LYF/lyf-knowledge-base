"""Extraction: source item → structured records (feedback and program_info) + citations.

For each new or changed item (a Discord thread or channel message, or a Drive file):

1. Size check: items over `llm.max_item_tokens` (characters ÷ 4) are skipped and
   reported, unless their file ID is in `drive.allow_large_files`. Allowed large files
   are split into chunks, each sent separately but cited as the whole file.
2. The item is rendered as text and sent to the extraction model with the versioned
   prompt in `prompts/`, the programming types found so far, and the named sessions of
   per-session types (`per_session_types` in config, such as Workshop). The model
   answers with JSON matching `SCHEMA`.
3. Every record is checked (see `validate`): it needs a citation, each cited Discord
   message must be one of the item's new messages, `program_info` needs a programming
   type, and `applies_to` must be `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. When `date_basis`
   is `source_date`, `applies_to` is set here from the citation's date, not taken from
   the model. `session` is kept only for per-session types, and reuses a known
   session's name when one matches.
4. A request that fails, or whose answer can't be read, is retried once. An item that
   still fails gets status `error` and is tried again next run; the rest of the run
   carries on.
5. Records get permanent IDs (R-0001…) and citation IDs, new programming types are
   added to `programming_types.json`, and new sessions to `sessions.json`.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from kb import llm as llm_mod
from kb.citations import Citations, discord_key, drive_key, normalize_time
from kb.config import REPO_ROOT, Config

Log = Callable[[str], None]

PROMPT_VERSION = 2
PROMPTS_DIR = REPO_ROOT / "prompts"

CHUNK_TOKENS = 60_000  # allowed large files are split into parts of about this size
OUTPUT_TOKENS_BASE = 1_500  # rough output estimate per request: base + 10% of input
OUTPUT_TOKENS_RATIO = 0.10

KINDS = ["feedback", "program_info"]
CATEGORIES = ["bug", "feature_request", "confusion", "praise", "other"]
SENTIMENTS = ["positive", "negative", "neutral", "mixed"]
INFO_TYPES = ["description", "history", "guidance"]
DATE_BASES = ["stated", "inferred", "source_date"]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "text": {"type": "string"},
        "programming_type": _nullable({"type": "string"}),
        "session": _nullable({"type": "string"}),
        "category": _nullable({"type": "string", "enum": CATEGORIES}),
        "sentiment": _nullable({"type": "string", "enum": SENTIMENTS}),
        "info_type": _nullable({"type": "string", "enum": INFO_TYPES}),
        "applies_to": {"type": "string"},
        "date_basis": {"type": "string", "enum": DATE_BASES},
        "citations": {"type": "array", "items": {"type": "string"}},
        "quote": _nullable({"type": "string"}),
    },
    "required": [
        "kind",
        "text",
        "programming_type",
        "session",
        "category",
        "sentiment",
        "info_type",
        "applies_to",
        "date_basis",
        "citations",
        "quote",
    ],
    "additionalProperties": False,
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": RECORD_SCHEMA}},
    "required": ["records"],
    "additionalProperties": False,
}


def system_prompt(version: int = PROMPT_VERSION) -> str:
    return (PROMPTS_DIR / f"extract_v{version}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Programming types and records (saved in _state/)
# ---------------------------------------------------------------------------


class ProgrammingTypes:
    """`_state/programming_types.json`: [{"name": "Night Market", "first_seen": "..."}]."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows: list[dict[str, Any]] = [
            r if isinstance(r, dict) else {"name": str(r), "first_seen": None} for r in rows or []
        ]

    @classmethod
    def load(cls, path: Path) -> ProgrammingTypes:
        if not path.is_file():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        text = json.dumps(self.rows, indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")

    @property
    def names(self) -> list[str]:
        return [r["name"] for r in self.rows]

    @staticmethod
    def _norm(name: str) -> str:
        return " ".join(name.lower().split())

    def resolve(self, name: str | None) -> str | None:
        """The saved spelling of a name (case and spacing ignored), else the name tidied
        (an all-lowercase new name is put in Title Case)."""
        if name is None:
            return None
        name = " ".join(name.split())
        if not name:
            return None
        for existing in self.names:
            if self._norm(existing) == self._norm(name):
                return existing
        return string.capwords(name) if name == name.lower() else name

    def add(self, name: str, now: str) -> bool:
        """Add a new type. Returns True if it wasn't already saved."""
        if self.resolve(name) in self.names:
            return False
        self.rows.append({"name": name, "first_seen": now})
        return True


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


class Sessions:
    """`_state/sessions.json`: the named sessions of per-session programming types.

    [{"programming_type": "Workshop", "name": "Active Listening", "year": "2025",
      "date": "2025-03-14", "first_seen": "..."}]

    A session is identified by its type, year, and name (case and spacing ignored), since
    two years can each have a workshop with the same title. `date` is the most precise
    `applies_to` seen for it, so the model can match "today's workshop" to it.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])

    @classmethod
    def load(cls, path: Path) -> Sessions:
        if not path.is_file():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        text = json.dumps(self.rows, indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")

    def _find(self, ptype: str, year: str, name: str) -> dict[str, Any] | None:
        for row in self.rows:
            if (
                _norm(row["programming_type"]) == _norm(ptype)
                and row["year"] == year
                and _norm(row["name"]) == _norm(name)
            ):
                return row
        return None

    def resolve(self, ptype: str, applies_to: str, name: str | None) -> str | None:
        """The saved spelling of a session name for that type and year, else the name tidied."""
        if name is None:
            return None
        name = " ".join(str(name).split())
        if not name:
            return None
        row = self._find(ptype, applies_to[:4], name)
        return row["name"] if row else name

    def add(self, ptype: str, name: str, applies_to: str, now: str) -> bool:
        """Add a new session, or sharpen a known one's date. True if it's new."""
        row = self._find(ptype, applies_to[:4], name)
        if row is not None:
            if len(applies_to) > len(row.get("date") or ""):
                row["date"] = applies_to
            return False
        self.rows.append(
            {
                "programming_type": ptype,
                "name": name,
                "year": applies_to[:4],
                "date": applies_to,
                "first_seen": now,
            }
        )
        return True

    def listing(self) -> list[str]:
        """One line per session, for the prompt: type, year, then date and name."""
        rows = sorted(
            self.rows, key=lambda r: (r["programming_type"].lower(), r.get("date") or "", r["name"])
        )
        return [
            f"- {r['programming_type']}, {r.get('date') or r['year']}: {r['name']}" for r in rows
        ]


class Records:
    """`_state/extracted.jsonl`: every saved record, one per line."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])
        self._next = 1 + max((int(r["record_id"].split("-")[1]) for r in self.rows), default=0)

    @classmethod
    def load(cls, path: Path) -> Records:
        if not path.is_file():
            return cls()
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls([json.loads(line) for line in lines if line.strip()])

    def save(self, path: Path) -> None:
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.rows)
        path.write_text(text, encoding="utf-8")

    def add(self, record: dict[str, Any]) -> dict[str, Any]:
        record = {"record_id": f"R-{self._next:04d}", **record}
        self._next += 1
        self.rows.append(record)
        return record

    def drop_item(self, item_id: str) -> int:
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.get("item_id") != item_id]
        return before - len(self.rows)

    def drop_citing(self, citation_ids: Iterable[str]) -> int:
        ids = set(citation_ids)
        before = len(self.rows)
        self.rows = [r for r in self.rows if not ids & set(r.get("citations", []))]
        return before - len(self.rows)


# ---------------------------------------------------------------------------
# Items: what gets sent to the model
# ---------------------------------------------------------------------------


@dataclass
class Item:
    """One thing to extract: a Discord item or a Drive file."""

    id: str  # manifest ID: discord-thread:…, discord-msg:…, drive:…
    title: str
    text: str  # the source as shown to the model (header + content)
    discord: dict[str, Any] | None = None  # the Discord item (messages + context)
    drive: dict[str, Any] | None = None  # Drive metadata (gdrive.file_metadata)
    replace: bool = False  # on success, drop the item's earlier records first
    edited_message_ids: list[str] = field(default_factory=list)  # their records are dropped

    @property
    def tokens(self) -> int:
        return llm_mod.estimate_tokens(self.text)

    @property
    def file_id(self) -> str | None:
        return self.id.removeprefix("drive:") if self.drive else None


def _discord_line(m: dict[str, Any]) -> str:
    when = normalize_time(m.get("timestamp")) or "?"
    head = f"[{m['id']}] {when} {m.get('author') or 'unknown'}"
    if m.get("reply_to"):
        head += f" (reply to [{m['reply_to']}])"
    lines = [f"{head}:", *("    " + line for line in (m.get("content") or "").splitlines())]
    files = [a.get("filename") for a in m.get("attachments") or [] if a.get("filename")]
    if files:
        lines.append(f"    (attachments: {', '.join(files)})")
    return "\n".join(lines)


def discord_item(item: dict[str, Any], replace: bool = False, edited: Iterable[str] = ()) -> Item:
    parts = [f"Source: Discord, {item.get('title')}"]
    if item.get("context"):
        parts.append("Earlier messages (context only, already processed: don't cite):")
        parts.extend(_discord_line(m) for m in item["context"])
        parts.append("")
    parts.append("Messages to extract:")
    parts.extend(_discord_line(m) for m in item["messages"])
    return Item(
        id=item["id"],
        title=item.get("title") or item["id"],
        text="\n".join(parts),
        discord=item,
        replace=replace,
        edited_message_ids=list(edited),
    )


SOURCE_NAMES = {"doc": "Google Doc", "sheet": "Google Sheet", "slides": "Google Slides deck"}


def _drive_header(meta: dict[str, Any], part: tuple[int, int] | None = None) -> str:
    lines = [
        f"Source: {SOURCE_NAMES.get(meta.get('source'), 'Google Drive file')}",
        f"Title: {meta.get('title')}",
        f"Owner: {meta.get('owner') or 'unknown'}",
        f"Last modified: {normalize_time(meta.get('modified_time')) or 'unknown'}",
    ]
    if part:
        lines.append(f"Part {part[0]} of {part[1]} of a long file")
    return "\n".join(lines) + "\n\n---\n\n"


def drive_item(meta: dict[str, Any], body: str) -> Item:
    return Item(
        id=meta["id"],
        title=meta.get("title") or meta["id"],
        text=_drive_header(meta) + body.strip() + "\n",
        drive={**meta, "body": body},
        replace=True,
    )


def split_text(text: str, max_chars: int) -> list[str]:
    """Split on paragraph breaks into pieces of at most `max_chars` (hard-split if needed)."""
    chunks: list[str] = []
    current = ""
    for para in re.split(r"(\n\s*\n)", text):
        while len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        if len(current) + len(para) > max_chars and current:
            chunks.append(current)
            current = ""
        current += para
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if c.strip()] or [""]


def item_texts(item: Item) -> list[str]:
    """The text of each request for an item: one, or one per chunk of a long file."""
    if item.drive is None or item.tokens <= CHUNK_TOKENS:
        return [item.text]
    pieces = split_text(item.drive["body"], CHUNK_TOKENS * 4)
    return [_drive_header(item.drive, (n, len(pieces))) + p for n, p in enumerate(pieces, 1)]


def _is_per_session(ptype: str | None, per_session: Iterable[str]) -> bool:
    return bool(ptype) and _norm(ptype or "") in {_norm(t) for t in per_session}


def user_message(
    types: list[str],
    source_text: str,
    per_session: Iterable[str] = (),
    sessions: Sessions | None = None,
) -> str:
    per_session = list(per_session)
    names = sorted({*types, *per_session}, key=str.lower)
    listed = (
        "\n".join(
            f"- {t}" + (" (per-session)" if _is_per_session(t, per_session) else "") for t in names
        )
        or "(none yet)"
    )
    known = (sessions.listing() if sessions else []) or ["(none yet)"]
    return (
        f"Programming types found so far:\n{listed}\n\n"
        f"Sessions of per-session types found so far (type, date, name):\n"
        + "\n".join(known)
        + f"\n\n<source>\n{source_text}\n</source>"
    )


# ---------------------------------------------------------------------------
# Size check and estimates
# ---------------------------------------------------------------------------


def too_large(item: Item, cfg: Config) -> str | None:
    """The reason an item is skipped for size, or None if it can be extracted."""
    if item.tokens <= cfg.llm_max_item_tokens:
        return None
    if item.file_id and item.file_id in cfg.drive_allow_large_files:
        return None
    hint = " (add its file ID to drive.allow_large_files to extract it)" if item.file_id else ""
    return (
        f"about {item.tokens:,} tokens, over llm.max_item_tokens "
        f"({cfg.llm_max_item_tokens:,}){hint}"
    )


@dataclass
class Estimate:
    items: list[dict[str, Any]]  # {id, title, tokens, requests, skipped}
    requests: int
    input_tokens: int
    output_tokens: int
    batch: bool
    cost_usd: float | None

    @property
    def skipped(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["skipped"]]


def estimate(items: list[Item], types: list[str], cfg: Config) -> Estimate:
    overhead = llm_mod.estimate_tokens(system_prompt() + user_message(types, ""))
    rows: list[dict[str, Any]] = []
    requests = input_tokens = output_tokens = 0
    for item in items:
        reason = too_large(item, cfg)
        n = 0 if reason else len(item_texts(item))
        tokens = item.tokens + n * overhead
        rows.append(
            {
                "id": item.id,
                "title": item.title,
                "tokens": item.tokens,
                "requests": n,
                "skipped": reason,
            }
        )
        if reason:
            continue
        requests += n
        input_tokens += tokens
        output_tokens += int(n * OUTPUT_TOKENS_BASE + tokens * OUTPUT_TOKENS_RATIO)
    batch = requests >= cfg.llm_batch_min_requests
    usd = llm_mod.cost(cfg.llm_extract_model, input_tokens, output_tokens, batch=batch)
    return Estimate(rows, requests, input_tokens, output_tokens, batch, usd)


# ---------------------------------------------------------------------------
# Checking records
# ---------------------------------------------------------------------------

APPLIES_TO = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def valid_applies_to(value: Any) -> bool:
    if not isinstance(value, str) or not APPLIES_TO.match(value.strip()):
        return False
    parts = [int(p) for p in value.strip().split("-")]
    try:
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return False
    return 1900 <= parts[0] <= 2100


def _clean_id(value: Any) -> str:
    return str(value).strip().strip("[]").strip()


@dataclass
class Checked:
    """A record that passed the checks, before it gets IDs."""

    fields: dict[str, Any]
    message_ids: list[str]  # cited Discord messages (empty for Drive)


def validate(
    raw: Any,
    item: Item,
    types: ProgrammingTypes,
    sessions: Sessions | None = None,
    per_session: Iterable[str] = (),
) -> tuple[list[Checked], list[str]]:
    """Check the model's records for one request. Returns (good records, problems)."""
    sessions = sessions or Sessions()
    per_session = list(per_session)
    problems: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("records"), list):
        raise ValueError("answer has no 'records' list")
    new_messages = {m["id"]: m for m in (item.discord or {}).get("messages", [])}
    out: list[Checked] = []
    for n, r in enumerate(raw["records"], 1):
        where = f"record {n}"
        if not isinstance(r, dict):
            problems.append(f"{where}: not an object")
            continue
        text = " ".join(str(r.get("text") or "").split())
        kind = r.get("kind")
        if kind not in KINDS:
            problems.append(f"{where}: unknown kind {kind!r}")
            continue
        if not text:
            problems.append(f"{where}: empty text")
            continue

        # Citations
        if item.discord is not None:
            cited = list(dict.fromkeys(_clean_id(c) for c in r.get("citations") or []))
            unknown = [c for c in cited if c not in new_messages]
            message_ids = [c for c in cited if c in new_messages]
            if unknown:
                problems.append(
                    f"{where}: dropped citations to messages not in the item: {', '.join(unknown)}"
                )
            if not message_ids:
                problems.append(f"{where}: no citation to a message in the item; dropped")
                continue
            dates = [normalize_time(new_messages[m].get("timestamp")) for m in message_ids]
        else:
            message_ids = []
            dates = [normalize_time((item.drive or {}).get("modified_time"))]

        # Programming type
        ptype = types.resolve(r.get("programming_type"))
        if kind == "program_info" and not ptype:
            problems.append(f"{where}: program_info without a programming type; dropped")
            continue

        # Dates: source_date (or anything unreadable) comes from the earliest citation
        basis = r.get("date_basis")
        applies_to = str(r.get("applies_to") or "").strip()
        if basis not in DATE_BASES:
            basis = "source_date"
        if basis != "source_date" and not valid_applies_to(applies_to):
            problems.append(
                f"{where}: applies_to {applies_to!r} isn't YYYY[-MM[-DD]]; used the citation's date"
            )
            basis = "source_date"
        if basis == "source_date":
            known = sorted(d for d in dates if d)
            if not known:
                problems.append(f"{where}: no date to fall back on; dropped")
                continue
            applies_to = known[0][:10]

        # Session: only per-session types have one; reuse a known session's name
        session = r.get("session")
        if not _is_per_session(ptype, per_session):
            if session:
                problems.append(
                    f"{where}: session {session!r} on a type that isn't per-session; cleared"
                )
            session = None
        else:
            session = sessions.resolve(ptype or "", applies_to, session)

        quote = r.get("quote")
        quote = " ".join(str(quote).split()) if quote else None
        if kind == "feedback":
            fields = {
                "kind": "feedback",
                "feedback": text,
                "programming_type": ptype,
                "category": r.get("category") if r.get("category") in CATEGORIES else "other",
                "sentiment": r.get("sentiment") if r.get("sentiment") in SENTIMENTS else "neutral",
            }
        else:
            info_type = r.get("info_type")
            if info_type not in INFO_TYPES:
                problems.append(f"{where}: program_info without an info_type; dropped")
                continue
            fields = {
                "kind": "program_info",
                "fact": text,
                "info_type": info_type,
                "programming_type": ptype,
            }
        fields.update(
            {"session": session, "applies_to": applies_to, "date_basis": basis, "quote": quote}
        )
        out.append(Checked(fields, message_ids))
    return out, problems


# ---------------------------------------------------------------------------
# Running extraction
# ---------------------------------------------------------------------------


@dataclass
class ExtractResult:
    ok: dict[str, int] = field(default_factory=dict)  # item ID → records saved
    errors: dict[str, str] = field(default_factory=dict)  # item ID → error
    skipped: dict[str, str] = field(default_factory=dict)  # item ID → reason (too large)
    deferred: dict[str, str] = field(default_factory=dict)  # item ID → why it waits (--limit…)
    problems: dict[str, list[str]] = field(default_factory=dict)  # item ID → record problems
    new_types: list[str] = field(default_factory=list)
    new_sessions: list[str] = field(default_factory=list)  # "Workshop 2025: Active Listening"
    usage: llm_mod.Usage = field(default_factory=llm_mod.Usage)

    @property
    def records(self) -> int:
        return sum(self.ok.values())

    def summary(self) -> dict[str, Any]:
        return {
            "items_ok": len(self.ok),
            "items_error": len(self.errors),
            "items_skipped": len(self.skipped),
            "items_deferred": len(self.deferred),
            "records": self.records,
            "records_dropped": sum(
                1 for ps in self.problems.values() for p in ps if p.endswith("dropped")
            ),
            "new_programming_types": self.new_types,
            "new_sessions": self.new_sessions,
            "usage": self.usage.to_dict(),
        }


@dataclass
class Store:
    """The parts of state extraction reads and writes."""

    records: Records
    citations: Citations
    types: ProgrammingTypes
    sessions: Sessions = field(default_factory=Sessions)


def _parse(text: str | None) -> Any:
    if text is None:
        raise ValueError("no answer")
    return json.loads(text)


def run(
    items: list[Item],
    store: Store,
    client: llm_mod.LLM | None,
    cfg: Config,
    now: str,
    log: Log = lambda _: None,
) -> ExtractResult:
    result = ExtractResult(usage=llm_mod.Usage(model=cfg.llm_extract_model))
    system = system_prompt()
    types_now = store.types.names

    todo: list[Item] = []
    for item in items:
        reason = too_large(item, cfg)
        if reason:
            result.skipped[item.id] = reason
            log(f"skipped (too large): {item.title}: {reason}")
        else:
            todo.append(item)
    if not todo:
        return result
    if client is None:
        raise RuntimeError("Extraction needs the Anthropic client (ANTHROPIC_API_KEY).")

    # One request per item, or per chunk of a long file.
    requests: dict[str, llm_mod.Request] = {}
    jobs: dict[str, list[str]] = {}  # item ID → request IDs
    for item in todo:
        jobs[item.id] = []
        for text in item_texts(item):
            user = user_message(types_now, text, cfg.per_session_types, store.sessions)
            rid = llm_mod.request_id(
                cfg.llm_extract_model, cfg.llm_extract_effort, system, user, SCHEMA
            )
            requests[rid] = llm_mod.Request(rid, system, user, SCHEMA)
            jobs[item.id].append(rid)
    by_id = {item.id: item for item in todo}
    log(
        f"{len(requests)} request{'s' if len(requests) != 1 else ''} for "
        f"{len(todo)} item{'s' if len(todo) != 1 else ''} ({cfg.llm_extract_model})"
    )

    # Ask, check, and retry failures once.
    answers: dict[str, tuple[list[Checked], list[str]]] = {}
    failures: dict[str, str] = {}
    owner = {rid: iid for iid, rids in jobs.items() for rid in rids}

    def ask(rids: list[str]) -> None:
        for rid, resp in client.run([requests[r] for r in rids], log).items():
            if resp.usage:
                result.usage.add(resp.usage, resp.batch)
            try:
                if resp.error:
                    raise ValueError(resp.error)
                answers[rid] = validate(
                    _parse(resp.text),
                    by_id[owner[rid]],
                    store.types,
                    store.sessions,
                    cfg.per_session_types,
                )
                failures.pop(rid, None)
            except (ValueError, json.JSONDecodeError) as e:
                failures[rid] = str(e)
                forget = getattr(client, "forget", None)
                if forget is not None:
                    forget(rid)

    ask(list(requests))
    if failures:
        log(f"retrying {len(failures)} failed request{'s' if len(failures) != 1 else ''}")
        ask(list(failures))

    # Save each item whose requests all succeeded.
    for item in todo:
        failed = [failures[r] for r in jobs[item.id] if r in failures]
        if failed:
            result.errors[item.id] = failed[0]
            log(f"error: {item.title}: {failed[0]}")
            continue
        checked = [c for r in jobs[item.id] for c in answers[r][0]]
        problems = [p for r in jobs[item.id] for p in answers[r][1]]
        if problems:
            result.problems[item.id] = problems
        result.ok[item.id] = save_item(item, checked, store, cfg, now, result)
    return result


def save_item(
    item: Item,
    checked: list[Checked],
    store: Store,
    cfg: Config,
    now: str,
    result: ExtractResult,
) -> int:
    """Replace or add the item's records, creating citations and programming types."""
    if item.replace:
        store.records.drop_item(item.id)
    if item.edited_message_ids:
        stale = [
            store.citations.by_key[k]["id"]
            for k in map(discord_key, item.edited_message_ids)
            if k in store.citations.by_key
        ]
        store.records.drop_citing(stale)

    if item.drive is not None:
        file_citation = store.citations.for_drive_file(item.drive, now)
    messages = {m["id"]: m for m in (item.discord or {}).get("messages", [])}
    for c in checked:
        if item.discord is not None:
            cites = [
                store.citations.for_discord_message(item.discord, messages[m], now)
                for m in c.message_ids
            ]
        else:
            cites = [file_citation]
        ptype = c.fields.get("programming_type")
        if ptype:
            ptype = store.types.resolve(ptype)
            c.fields["programming_type"] = ptype
            if store.types.add(ptype, now):
                result.new_types.append(ptype)
        session = c.fields.get("session")
        if ptype and session and store.sessions.add(ptype, session, c.fields["applies_to"], now):
            result.new_sessions.append(f"{ptype} {c.fields['applies_to'][:4]}: {session}")
        store.records.add(
            {
                "item_id": item.id,
                **c.fields,
                "citations": cites,
                "first_seen": now,
                "prompt_version": PROMPT_VERSION,
                "model": cfg.llm_extract_model,
            }
        )
    return len(checked)


def mark_removed(
    citations: Citations, *, drive_ids: Iterable[str] = (), message_ids: Iterable[str] = ()
) -> int:
    """Mark citations for removed Drive files (manifest IDs) and deleted messages."""
    n = 0
    for item_id in drive_ids:
        n += citations.mark_removed(drive_key(item_id.removeprefix("drive:")))
    for mid in message_ids:
        n += citations.mark_removed(discord_key(mid))
    return n


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_lines(
    records: Records,
    citations: Citations,
    types: ProgrammingTypes,
    sessions: Sessions | None = None,
) -> list[str]:
    out: list[str] = []
    rows = records.rows
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    out.append(
        f"Records: {len(rows)}"
        + (f" ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))})" if rows else "")
    )
    if rows:
        years: dict[str, int] = {}
        for r in rows:
            y = str(r.get("applies_to") or "?")[:4]
            years[y] = years.get(y, 0) + 1
        out.append(
            "  By year (applies_to): " + " · ".join(f"{y}: {n}" for y, n in sorted(years.items()))
        )
        weak = sum(1 for r in rows if r.get("date_basis") == "source_date")
        out.append(
            f"  Dated from the source's date (source_date): {weak} of {len(rows)} "
            f"({100 * weak / len(rows):.0f}%)"
        )
    counts = citations.counts()
    out.append(
        f"Citations: {len(citations.rows)}"
        + (f" ({', '.join(f'{n} {k}' for k, n in sorted(counts.items()))})" if counts else "")
    )
    if types.rows:
        per: dict[str, int] = {}
        for r in rows:
            if r.get("programming_type"):
                per[r["programming_type"]] = per.get(r["programming_type"], 0) + 1
        out.append(f"Programming types: {len(types.rows)}")
        for name in sorted(types.names, key=lambda n: (-per.get(n, 0), n.lower())):
            line = f"  - {name}: {per.get(name, 0)} records"
            named = [
                s for s in (sessions.rows if sessions else []) if s["programming_type"] == name
            ]
            if named:
                general = sum(
                    1 for r in rows if r.get("programming_type") == name and not r.get("session")
                )
                line += f" ({len(named)} sessions; {general} records not tied to one)"
            out.append(line)
    else:
        out.append("Programming types: none yet")
    return out
