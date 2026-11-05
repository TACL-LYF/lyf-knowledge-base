"""`kb update`: find what's new in Discord and Drive since the last run, and save it.

One run:

1. Take the lock in `_state/` and download `_state/` into `data/state/`.
2. Discord: read past the cursors (or everything with `--full`), and keep only messages
   not seen before. Earlier messages in the same thread are kept as context.
3. Drive: list every file in the source folders (recursively) and compare to the
   manifest: new / changed / skip / removed. New and changed files are exported (with
   `--full`, every file is), and a change that leaves the text the same (e.g. a comment)
   is skipped by content hash.
4. Save raw snapshots in `data/raw/` and upload them to `raw/` in the Knowledge Base folder.
5. Extract records from every new or changed item (kb/extract.py). Items over the size
   limit are skipped; an item whose extraction fails gets status `error` and is tried
   again next run.
6. Update the manifest, cursors, citations, records and run log, upload `_state/`,
   release the lock.

State is uploaded only at step 6, so a run that fails earlier changes nothing and the
next run starts from the same place (answers already received from the model are cached
locally, so they aren't paid for twice). Publishing (M4) slots in before step 6.

Sources: `--source discord` or `--source drive` reads, and extracts, only that source.
`cursors.last_run_started_at` is Drive's cursor and moves only when Drive is read.

Steps: `--pull` runs 1–4 and 6, leaving new items `pending`; `--extract` skips 2–4 and
extracts pending items (Discord items keep their messages in the manifest; Drive files
are read from their raw snapshot in Drive). `--limit N` extracts at most N items and
leaves the rest `pending`; `--limit 0` skips extraction entirely.

A manifest entry without `prompt_version` has never been extracted (for example, items
read before extraction existed). Such Drive files are exported and extracted again, and
such Discord items are read again in full, so the first run after M2 catches up on its
own. `kb rebuild` clears `prompt_version` everywhere to extract everything again;
`kb requeue` only clears it, so the following runs (with `--limit`, `--extract`) do the work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from kb import extract as ext
from kb import llm as llm_mod
from kb import state as st
from kb.citations import Citations
from kb.config import Config
from kb.progress import Stages
from kb.pull import MEMBER_CACHE
from kb.sources import discord as dsc
from kb.sources import drive as gdrive

Log = Callable[[str], None]


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sources (injected so tests can fake them)
# ---------------------------------------------------------------------------


@dataclass
class Sources:
    """How a run reads Discord, Drive, and the Knowledge Base folder."""

    # cursors (None = full read), refresh_names → DiscordPull
    discord_fetch: Callable[[dict[str, Any] | None, bool], dsc.DiscordPull]
    drive_list: Callable[[], list[dict[str, Any]]]
    drive_export: Callable[[dict[str, Any]], str]  # must be thread-safe (export_files)
    kb_folder: st.RemoteFolder
    llm: llm_mod.LLM | None = None  # None: no extraction possible (e.g. a dry run)

    @classmethod
    def real(cls, cfg: Config, log: Log) -> Sources:
        creds = gdrive.credentials(cfg.google_credentials_path)
        services = gdrive.Services.from_credentials(creds)
        cache = dsc.MemberCache(cfg.data_dir / MEMBER_CACHE)
        client: llm_mod.LLM | None = None
        if cfg.anthropic_api_key:
            client = llm_mod.CachedLLM(
                llm_mod.AnthropicLLM(
                    cfg.anthropic_api_key,
                    cfg.llm_extract_model,
                    effort=cfg.llm_extract_effort,
                    batch_min_requests=cfg.llm_batch_min_requests,
                    pending_batch_path=cfg.data_dir / "cache" / "pending_batch.json",
                ),
                cfg.data_dir / "cache" / "extract",
            )

        def discord_fetch(cursors: dict[str, Any] | None, refresh: bool) -> dsc.DiscordPull:
            with dsc.DiscordClient(cfg.discord_bot_token) as client:
                return dsc.fetch(
                    client,
                    cfg.discord_server_id,
                    cfg.discord_channel_id,
                    cursors,
                    log,
                    member_cache=cache,
                    refresh_names=refresh,
                )

        return cls(
            discord_fetch=discord_fetch,
            drive_list=lambda: list(
                gdrive.list_folder_files(
                    services.drive,
                    cfg.drive_source_folder_ids,
                    cfg.drive_file_types,
                    log,
                    make_drive=lambda: gdrive.Services.from_credentials(creds).drive,
                    workers=cfg.drive_export_workers,
                )
            ),
            # Called from several threads at once, so each builds its own clients.
            drive_export=gdrive.threaded_export(lambda: gdrive.Services.from_credentials(creds)),
            kb_folder=st.DriveFolder(services.drive, cfg.drive_kb_folder_id),
            llm=client,
        )


def needs_extraction(entry: dict[str, Any]) -> bool:
    """An item that was read but never extracted (e.g. before M3, or after `kb rebuild`)."""
    return entry.get("status") in ("ok", None) and entry.get("prompt_version") is None


# ---------------------------------------------------------------------------
# Discord: what's new (pure)
# ---------------------------------------------------------------------------


@dataclass
class DiscordChanges:
    items: list[dict[str, Any]]  # only new (or, with --full, edited) messages; rest is context
    edited: list[dict[str, Any]] = field(default_factory=list)  # {item_id, message_id}
    whole: set[str] = field(default_factory=set)  # items never extracted: sent in full
    deleted: list[dict[str, Any]] = field(default_factory=list)  # {item_id, message_id, title}
    warnings: list[str] = field(default_factory=list)

    @property
    def new_messages(self) -> int:
        return dsc.count_messages(self.items)


def known_messages(manifest: dict[str, dict[str, Any]]) -> dict[str, str]:
    """message ID → content hash, for every Discord message already processed."""
    known: dict[str, str] = {}
    for entry in manifest.values():
        if entry.get("source") != "discord":
            continue
        removed = set(entry.get("removed_messages", []))
        for mid, h in (entry.get("messages") or {}).items():
            if mid not in removed:
                known[mid] = h
    return known


def plan_discord(pull: dsc.DiscordPull, manifest: dict[str, dict[str, Any]]) -> DiscordChanges:
    """Compare a pull to the manifest. With a full pull, also find edits and deletions."""
    known = known_messages(manifest)
    items = dsc.build_items(pull)

    edited: list[dict[str, Any]] = []

    def is_new(m: dict[str, Any]) -> bool:
        h = known.get(m["id"])
        return h is None or (pull.full and h != dsc.message_hash(m))

    if pull.full:
        for item in items:
            for m in item["messages"]:
                h = known.get(m["id"])
                if h is not None and h != dsc.message_hash(m):
                    edited.append({"item_id": item["id"], "message_id": m["id"]})

    deleted: list[dict[str, Any]] = []
    if pull.full:
        present = {m["id"] for item in items for m in item["messages"]}
        for item_id, entry in manifest.items():
            if entry.get("source") != "discord" or entry.get("status") == "removed":
                continue
            if entry.get("thread_id") in pull.unreadable_threads:
                continue
            removed = set(entry.get("removed_messages", []))
            for mid in entry.get("messages") or {}:
                if mid not in present and mid not in removed:
                    deleted.append(
                        {"item_id": item_id, "message_id": mid, "title": entry.get("title")}
                    )

    # Items never extracted are sent whole; the rest only with their new messages.
    whole = {i["id"] for i in items if i["id"] in manifest and needs_extraction(manifest[i["id"]])}
    out = [i for i in items if i["id"] in whole]
    out += dsc.split_new([i for i in items if i["id"] not in whole], is_new)
    out.sort(key=lambda i: int(i["messages"][0]["id"]))

    return DiscordChanges(
        items=out,
        whole=whole,
        edited=edited,
        deleted=deleted,
        warnings=list(pull.warnings),
    )


def apply_discord(
    state: st.State,
    pull: dsc.DiscordPull,
    changes: DiscordChanges,
    now: str,
    extracted: ext.ExtractResult | None = None,
    retried: list[dict[str, Any]] = (),
) -> None:
    """Record processed items, extraction results, deletions, and the new cursors."""
    extracted = extracted or ext.ExtractResult()
    edited: dict[str, list[str]] = {}
    for e in changes.edited:
        edited.setdefault(e["item_id"], []).append(e["message_id"])
    for item in [
        *changes.items,
        *(r for r in retried if r["id"] not in {i["id"] for i in changes.items}),
    ]:
        entry = state.manifest.get(item["id"]) or {
            "id": item["id"],
            "source": "discord",
            "kind": item["kind"],
            "thread_id": item.get("thread_id"),
            "first_seen": now,
            "messages": {},
            "removed_messages": [],
        }
        entry["messages"].update({m["id"]: dsc.message_hash(m) for m in item["messages"]})
        entry.update(
            {
                "title": item["title"],
                "link": item["link"],
                "last_message_id": str(max(int(m) for m in entry["messages"])),
                "last_processed": now,
            }
        )
        retry = {
            **item,
            "replace": bool(item.get("replace") or item["id"] in changes.whole),
            "edited": sorted({*item.get("edited", ()), *edited.get(item["id"], ())}),
        }
        _set_extraction_status(entry, item["id"], extracted, retry_item=retry)
        state.manifest[item["id"]] = entry

    for d in changes.deleted:
        entry = state.manifest[d["item_id"]]
        removed = entry.setdefault("removed_messages", [])
        if d["message_id"] not in removed:
            removed.append(d["message_id"])
        if set(removed) >= set(entry.get("messages") or {}):
            entry["status"] = "removed"
        entry["last_processed"] = now

    # After a full read, an entry never extracted that didn't come back as an item has
    # nothing left to extract (e.g. a message that later became the start of a thread).
    if pull.full:
        seen = {i["id"] for i in [*changes.items, *retried]}
        for item_id, entry in state.manifest.items():
            if (
                entry.get("source") == "discord"
                and needs_extraction(entry)
                and item_id not in seen
                and entry.get("thread_id") not in pull.unreadable_threads
            ):
                entry["prompt_version"] = ext.PROMPT_VERSION
                entry.setdefault("records", 0)

    state.cursors["discord_channel"] = pull.channel_cursor
    threads = dict(state.cursors.get("discord_threads") or {})
    threads.update(pull.thread_cursors)
    state.cursors["discord_threads"] = threads


def _set_extraction_status(
    entry: dict[str, Any],
    item_id: str,
    extracted: ext.ExtractResult,
    retry_item: dict[str, Any] | None = None,
) -> None:
    """ok / error / skipped / pending, from the extraction result for this item.

    Discord messages aren't read again once the cursors move past them, so an item that
    failed or is still waiting keeps its messages in `retry_item`. A Drive file is
    exported again (or, with `kb update --extract`, read from its raw snapshot).
    """
    for k in ("error", "error_step", "skip_reason", "retry_item", "pending_reason"):
        entry.pop(k, None)
    if item_id in extracted.errors:
        entry["status"] = "error"
        entry["error"] = extracted.errors[item_id]
        entry["error_step"] = "extract"
        if retry_item is not None:
            entry["retry_item"] = retry_item
    elif item_id in extracted.deferred:
        entry["status"] = "pending"
        entry["pending_reason"] = extracted.deferred[item_id]
        if retry_item is not None:
            entry["retry_item"] = retry_item
    elif item_id in extracted.skipped:
        entry["status"] = "skipped"
        entry["skip_reason"] = extracted.skipped[item_id]
    else:
        entry["status"] = "ok"
        if item_id in extracted.ok:
            entry["prompt_version"] = ext.PROMPT_VERSION
            entry["records"] = extracted.ok[item_id]


def discord_retries(
    manifest: dict[str, dict[str, Any]], changes: DiscordChanges
) -> list[dict[str, Any]]:
    """Discord items that failed or were left waiting last time, merged with new messages."""
    new = {i["id"]: i for i in changes.items}
    out = []
    for item_id, entry in manifest.items():
        retry = entry.get("retry_item")
        if entry.get("status") not in ("error", "pending") or not retry:
            continue
        if item_id in new:  # failed messages first, then this run's new ones
            fresh = new[item_id]
            ids = {m["id"] for m in retry["messages"]}
            msgs = retry["messages"] + [m for m in fresh["messages"] if m["id"] not in ids]
            ids = {m["id"] for m in msgs}
            new[item_id] = {
                **fresh,
                "messages": msgs,
                "context": [m for m in fresh["context"] if m["id"] not in ids],
                "replace": retry.get("replace", False),
                "edited": retry.get("edited", []),
            }
        else:
            out.append(retry)
    changes.items[:] = [new[i["id"]] for i in changes.items]
    return out


# ---------------------------------------------------------------------------
# Drive: what's new (pure, except for exporting)
# ---------------------------------------------------------------------------


@dataclass
class DriveChanges:
    new: list[dict[str, Any]] = field(default_factory=list)  # never seen (or seen, then removed)
    modified: list[dict[str, Any]] = field(default_factory=list)  # modified since last run
    skipped: list[dict[str, Any]] = field(default_factory=list)  # not modified since last run
    removed: list[str] = field(default_factory=list)  # manifest IDs no longer listed
    # filled in after export:
    exported: dict[str, str] = field(default_factory=dict)  # file ID → body
    changed: list[dict[str, Any]] = field(default_factory=list)  # modified and text differs
    unchanged: list[dict[str, Any]] = field(default_factory=list)  # modified but text same
    errors: dict[str, str] = field(default_factory=dict)  # file ID → error message


def classify_drive(
    files: list[dict[str, Any]],
    manifest: dict[str, dict[str, Any]],
    last_run_started_at: str | None,
    full: bool = False,
    retry_skipped: Callable[[str, dict[str, Any]], bool] = lambda _id, _e: False,
) -> DriveChanges:
    """Sort listed files into new / modified / skipped, and find removed ones.

    With `full`, every known file counts as modified, so it's exported again. That way a
    change to an exporter reaches files that haven't been edited; files whose text comes
    out the same are still skipped by content hash.

    Files never extracted or still waiting for extraction, files whose export or
    extraction failed, and files skipped for size that `retry_skipped` now allows also
    count as modified.
    """
    since = st.parse_time(last_run_started_at)
    out = DriveChanges()
    listed = set()
    for f in files:
        key = f"drive:{f['id']}"
        listed.add(key)
        entry = manifest.get(key)
        if entry is None or entry.get("status") == "removed":
            out.new.append(f)
            continue
        modified = st.parse_time(f.get("modifiedTime"))
        if (
            full
            or entry.get("status") in ("error", "pending")
            or needs_extraction(entry)
            or (entry.get("status") == "skipped" and retry_skipped(f["id"], entry))
            or since is None
            or modified is None
            or modified > since
        ):
            out.modified.append(f)
        else:
            out.skipped.append(f)
    out.removed = [
        key
        for key, entry in manifest.items()
        if key.startswith("drive:") and key not in listed and entry.get("status") != "removed"
    ]
    return out


def export_drive(
    changes: DriveChanges,
    manifest: dict[str, dict[str, Any]],
    export: Callable[[dict[str, Any]], str],
    log: Log,
    workers: int = gdrive.DEFAULT_EXPORT_WORKERS,
) -> None:
    """Export new and modified files, `workers` at a time; results are handled in order."""
    todo = changes.new + changes.modified
    modified_ids = {f["id"] for f in changes.modified}
    width = len(str(len(todo)))
    for n, r in enumerate(gdrive.export_files(todo, export, workers), 1):
        f = r.file
        label = f"({n:>{width}}/{len(todo)}) {f.get('name') or f['id']}"
        if r.error is not None:  # one bad file shouldn't stop the run
            changes.errors[f["id"]] = r.error
            log(f"{label}: error: {r.error}")
        else:
            assert r.body is not None
            body = r.body
            changes.exported[f["id"]] = body
            if f["id"] in modified_ids:
                entry = manifest.get(f"drive:{f['id']}") or {}
                same = (
                    entry.get("status") == "ok"
                    and not needs_extraction(entry)
                    and entry.get("content_hash") == content_hash(body)
                )
                (changes.unchanged if same else changes.changed).append(f)
                log(f"{label}: {'same text, skipped' if same else 'changed'}")
            else:
                log(f"{label}: new")


def apply_drive(
    state: st.State,
    changes: DriveChanges,
    now: str,
    extracted: ext.ExtractResult | None = None,
    tokens: dict[str, int] | None = None,
) -> None:
    extracted = extracted or ext.ExtractResult()
    unchanged = {f["id"] for f in changes.unchanged}
    for f in changes.new + changes.modified:
        key = f"drive:{f['id']}"
        meta = gdrive.file_metadata(f)
        entry = state.manifest.get(key) or {"first_seen": now}
        entry.update(meta)
        entry["last_processed"] = now
        if f["id"] in changes.errors:
            entry["status"] = "error"
            entry["error"] = changes.errors[f["id"]]
            entry["error_step"] = "export"
        elif f["id"] in unchanged:  # same text as last time: nothing new to extract
            entry["status"] = "ok"
            for k in ("error", "error_step", "pending_reason"):
                entry.pop(k, None)
        else:
            entry["content_hash"] = content_hash(changes.exported[f["id"]])
            if tokens and key in tokens:
                entry["estimated_tokens"] = tokens[key]
            _set_extraction_status(entry, key, extracted)
        state.manifest[key] = entry

    for f in changes.skipped:  # keep title/link/modified time fresh (renames don't re-export)
        entry = state.manifest[f"drive:{f['id']}"]
        meta = gdrive.file_metadata(f)
        entry.update({k: meta[k] for k in ("title", "link", "owner", "modified_time")})

    for key in changes.removed:
        state.manifest[key]["status"] = "removed"
        state.manifest[key]["last_processed"] = now


# ---------------------------------------------------------------------------
# Raw snapshots
# ---------------------------------------------------------------------------


def save_raw(
    cfg: Config,
    kb_folder: st.RemoteFolder,
    run: dict[str, Any],
    discord: DiscordChanges,
    drive: DriveChanges,
    files_by_id: dict[str, dict[str, Any]],
    log: Log = lambda _: None,
) -> str:
    """Write this run's new content to data/raw/ and upload it to raw/ in the KB folder.

    Returns a one-line summary of what was written.

    Discord: one file per day, `discord/YYYY-MM-DD.json`, holding every run that day (by
    anyone: the copy in Drive is read first, so teammates' runs aren't overwritten).
    Drive: `drive/<fileId>.md`, the latest export of each new or changed file. With
    `--full`, every exported file is written, so `raw/drive/` is filled in completely.
    """
    raw_dir = cfg.data_dir / "raw"
    remote_raw = kb_folder.subfolder(st.RAW_FOLDER)
    assert remote_raw is not None  # created on demand

    if discord.items or discord.deleted:
        local = raw_dir / "discord" / f"{run['started_at'][:10]}.json"
        remote_discord = remote_raw.subfolder("discord")
        assert remote_discord is not None
        day: dict[str, Any] = {"date": run["started_at"][:10], "runs": []}
        existing = remote_discord.read(local.name)
        if existing is not None:
            day = json.loads(existing)
        elif local.is_file():
            day = json.loads(local.read_text(encoding="utf-8"))
        day["runs"].append(
            {
                "run_id": run["run_id"],
                "started_at": run["started_at"],
                "full": run["full"],
                "items": discord.items,
                "edited": discord.edited,
                "deleted": discord.deleted,
            }
        )
        local.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(day, indent=2, ensure_ascii=False) + "\n"
        local.write_text(text, encoding="utf-8")
        remote_discord.write(local.name, text.encode("utf-8"))

    unchanged = {f["id"] for f in drive.unchanged}
    to_write = [fid for fid in drive.exported if run["full"] or fid not in unchanged]
    if to_write:
        (raw_dir / "drive").mkdir(parents=True, exist_ok=True)
        remote_drive = remote_raw.subfolder("drive")
        assert remote_drive is not None
        for n, fid in enumerate(to_write, 1):
            text = gdrive.render_file(files_by_id[fid], drive.exported[fid])
            (raw_dir / "drive" / f"{fid}.md").write_text(text, encoding="utf-8")
            remote_drive.write(f"{fid}.md", text.encode("utf-8"), mime="text/markdown")
            if n % 25 == 0 and n < len(to_write):
                log(f"uploaded {n}/{len(to_write)} Drive exports")

    parts = []
    if discord.items or discord.deleted:
        parts.append(f"discord/{local.name} ({_plural(len(discord.items), 'item')})")
    if to_write:
        parts.append(f"drive/ ({_plural(len(to_write), 'file')})")
    if not parts:
        return "nothing new to write"
    return f"wrote {' and '.join(parts)} to {raw_dir} and raw/ in Drive"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run: dict[str, Any]
    discord: DiscordChanges
    drive: DriveChanges
    dry_run: bool
    estimate: ext.Estimate | None = None
    extracted: ext.ExtractResult | None = None
    items: list[ext.Item] = field(default_factory=list)  # extracted now, then waiting ones
    deferred: dict[str, str] = field(default_factory=dict)  # item ID → why it waits
    waiting: Waiting | None = None  # set by `--extract` runs


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def discord_result(d: DiscordChanges, full: bool) -> str:
    threads_new = sum(1 for i in d.items if i["kind"] == "thread" and not i["context"])
    threads_more = sum(1 for i in d.items if i["kind"] == "thread" and i["context"])
    channel = sum(1 for i in d.items if i["kind"] == "message")
    out = (
        f"{_plural(d.new_messages, 'new message')} in {_plural(len(d.items), 'item')} "
        f"({_plural(threads_new, 'new thread')}, {threads_more} older threads with new "
        f"messages, {_plural(channel, 'channel message')})"
    )
    if full:
        out += f"; {len(d.edited)} edited, {len(d.deleted)} deleted"
    return out


def load_store(local_state: Any) -> ext.Store:
    return ext.Store(
        records=ext.Records.load(local_state / st.EXTRACTED),
        citations=Citations.load(local_state / st.CITATIONS),
        types=ext.ProgrammingTypes.load(local_state / st.PROGRAMMING_TYPES),
        sessions=ext.Sessions.load(local_state / st.SESSIONS),
    )


def save_store(store: ext.Store, local_state: Any) -> None:
    store.records.save(local_state / st.EXTRACTED)
    store.citations.save(local_state / st.CITATIONS)
    store.types.save(local_state / st.PROGRAMMING_TYPES)
    store.sessions.save(local_state / st.SESSIONS)


def build_items(
    discord: DiscordChanges,
    retried: list[dict[str, Any]],
    drive: DriveChanges,
    files_by_id: dict[str, dict[str, Any]],
) -> list[ext.Item]:
    """Everything to extract this run: Discord items (and retries), new/changed files."""
    edited: dict[str, list[str]] = {}
    for e in discord.edited:
        edited.setdefault(e["item_id"], []).append(e["message_id"])
    items = [
        ext.discord_item(
            i,
            replace=bool(i.get("replace")) or i["id"] in discord.whole,
            edited=sorted({*i.get("edited", ()), *edited.get(i["id"], ())}),
        )
        for i in [*discord.items, *retried]
    ]
    for f in drive.new + drive.changed:
        if f["id"] in drive.exported:
            items.append(ext.drive_item(gdrive.file_metadata(f), drive.exported[f["id"]]))
    return items


STEPS = ("pull", "extract")  # M4 adds "synthesize"
SOURCES = ("discord", "drive")


def pick(items: list[Any], limit: int | None) -> tuple[list[Any], list[Any]]:
    """The first `limit` items, alternating Discord and Drive; the rest wait.

    Works on anything with `.id` and `.discord` (None for Drive): extraction items, or
    the not-yet-loaded Drive files of `waiting_items`.
    """
    if limit is None or limit >= len(items):
        return items, []
    discord = [i for i in items if i.discord is not None]
    drive = [i for i in items if i.discord is None]
    mixed = [i for pair in zip(discord, drive, strict=False) for i in pair]
    n = min(len(discord), len(drive))
    mixed += discord[n:] + drive[n:]
    chosen = {i.id for i in mixed[:limit]}
    return [i for i in items if i.id in chosen], [i for i in items if i.id not in chosen]


def rendered_body(text: str) -> str:
    """The export body from a raw snapshot written by `gdrive.render_file`."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5 :].lstrip("\n")
    if text.startswith("# "):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text.strip("\n")


@dataclass
class Waiting:
    """Items `kb update --extract` can extract without reading Discord or Drive."""

    items: list[ext.Item] = field(default_factory=list)  # to extract this run
    deferred: list[str] = field(default_factory=list)  # item IDs left waiting (--limit)
    retry_items: dict[str, dict[str, Any]] = field(default_factory=dict)  # Discord, by ID
    need_pull: list[str] = field(default_factory=list)  # titles: no saved copy to extract from


class RawSnapshots(Protocol):
    def has(self, file_id: str) -> bool: ...
    def read(self, file_id: str) -> str | None: ...


@dataclass
class _DriveCandidate:
    """A waiting Drive file whose snapshot hasn't been downloaded yet (see `pick`)."""

    id: str
    entry: dict[str, Any]
    discord: None = None


def waiting_items(
    manifest: dict[str, dict[str, Any]],
    raw: RawSnapshots,
    limit: int | None = None,
    detail: Log = lambda _: None,
    read: Iterable[str] = SOURCES,
) -> Waiting:
    """Discord items with saved messages, and Drive files with a raw snapshot, to extract.

    Only the snapshots of the items chosen for this run (the first `limit`, picked as
    `pick` does) are downloaded; the rest are listed in `deferred` without being read.
    Only items from the sources in `read` are considered.
    """
    read = set(read)
    out = Waiting()
    candidates: list[Any] = []  # Discord items and Drive candidates, in manifest order
    for item_id, entry in manifest.items():
        status = entry.get("status")
        if entry.get("source") == "discord":
            if "discord" not in read:
                continue
            retry = entry.get("retry_item")
            if status in ("error", "pending") and retry:
                out.retry_items[item_id] = retry
                candidates.append(
                    ext.discord_item(
                        retry, replace=bool(retry.get("replace")), edited=retry.get("edited", ())
                    )
                )
            elif needs_extraction(entry):
                out.need_pull.append(str(entry.get("title") or item_id))
            continue
        if not item_id.startswith("drive:") or "drive" not in read:
            continue
        if not (
            status == "pending"
            or (status == "error" and entry.get("error_step") == "extract")
            or needs_extraction(entry)
        ):
            continue
        if not raw.has(item_id.removeprefix("drive:")):
            out.need_pull.append(str(entry.get("title") or item_id))
            continue
        candidates.append(_DriveCandidate(item_id, entry))

    chosen, later = pick(candidates, limit)
    out.deferred = [c.id for c in later]
    to_read = sum(1 for c in chosen if isinstance(c, _DriveCandidate))
    if to_read:
        detail(f"downloading {_plural(to_read, 'Drive snapshot')} from raw/drive/")
    done = 0
    for c in chosen:
        if not isinstance(c, _DriveCandidate):
            out.items.append(c)
            continue
        entry = c.entry
        text = raw.read(c.id.removeprefix("drive:"))
        done += 1
        if done == to_read or done % 25 == 0:
            detail(f"{done}/{to_read} downloaded")
        if text is None:  # removed since the listing
            out.need_pull.append(str(entry.get("title") or c.id))
            continue
        meta = {k: entry.get(k) for k in ("id", "source", "title", "link", "owner")}
        meta["modified_time"] = entry.get("modified_time")
        out.items.append(ext.drive_item(meta, rendered_body(text)))
    return out


def apply_waiting(
    state: st.State,
    waiting: Waiting,
    extracted: ext.ExtractResult,
    now: str,
) -> None:
    """Record the results of `kb update --extract` in the manifest."""
    for item in waiting.items:
        entry = state.manifest[item.id]
        entry["last_processed"] = now
        if item.drive is not None:
            entry["estimated_tokens"] = item.tokens
        _set_extraction_status(entry, item.id, extracted, waiting.retry_items.get(item.id))
    for item_id in waiting.deferred:  # not read this run: only the status changes
        _set_extraction_status(
            state.manifest[item_id], item_id, extracted, waiting.retry_items.get(item_id)
        )


def run_update(
    cfg: Config,
    *,
    dry_run: bool = False,
    full: bool = False,
    rebuild: bool = False,
    steps: Iterable[str] = STEPS,
    limit: int | None = None,
    read: Iterable[str] = SOURCES,
    force_unlock: bool = False,
    log: Log = print,
    sources: Sources | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RunResult:
    """One run. `steps` picks what runs: `pull` (read Discord and Drive, save raw
    snapshots), `extract` (send items to the model). With only `pull`, new items are
    saved as `pending`; with only `extract`, the pending items are extracted from what
    the last pull saved. `limit` caps how many items are extracted; the rest wait.

    `read` picks the sources (`discord`, `drive`): only those are read, and only their
    items are extracted. The Drive cursor (`last_run_started_at`) moves only when Drive
    is read, so a Discord-only run doesn't hide Drive edits from the next run.
    """
    steps = set(steps)
    unknown = steps - set(STEPS)
    if unknown or not steps:
        raise ValueError(f"steps must be some of {', '.join(STEPS)}, got {sorted(steps)}")
    read = set(read)
    if read - set(SOURCES) or not read:
        raise ValueError(f"read must be some of {', '.join(SOURCES)}, got {sorted(read)}")
    read_discord, read_drive = "discord" in read, "drive" in read
    pulling, extracting = "pull" in steps, "extract" in steps
    stages = Stages(log, total=9 if sources is None else 8)
    flags = [
        "--full" if full else "",
        "" if steps == set(STEPS) else " ".join(f"--{x}" for x in STEPS if x in steps),
        f"--limit {limit}" if limit is not None else "",
        "" if read == set(SOURCES) else " ".join(f"--source {x}" for x in SOURCES if x in read),
    ]
    mode = " (dry run: nothing will be saved)" if dry_run else ""
    log(f"kb {'rebuild' if rebuild else 'update'}" + "".join(f" {f}" for f in flags if f) + mode)

    if sources is None:
        with stages.stage("Connecting to Google Drive") as s:
            sources = Sources.real(cfg, stages.detail)
            s.result = f"signed in as {gdrive.service_account_email(cfg.google_credentials_path)}"

    # A dry run writes nothing to Drive, not even an empty _state/ folder on a first run.
    state_folder = sources.kb_folder.subfolder(st.STATE_FOLDER, create=not dry_run)
    local_state = cfg.data_dir / "state"

    lock = st.Lock(state_folder or st.MemoryFolder(), cfg.lock_timeout_minutes, clock=clock)
    if dry_run:
        stages.skip("Taking the run lock", "dry run")
    else:
        with stages.stage("Taking the run lock") as s:
            lock.acquire(force_unlock=force_unlock)
            s.result = f"lock taken by {lock.holder} (run {lock.run_id})"
    try:
        with stages.stage("Downloading state from Drive") as s:
            names = st.download(state_folder, local_state)
            state = st.State(local_state).load()
            store = load_store(local_state)
            last = state.run_log[-1] if state.run_log else None
            if not names:
                s.result = "no saved state yet: this run reads everything"
            else:
                s.result = f"{_plural(len(names), 'file')}, {len(state.manifest)} items known"
                if last:
                    s.result += f"; last run {last.get('started_at')} by {last.get('who')}"
            if rebuild:  # everything counts as never extracted
                for entry in state.manifest.values():
                    entry.pop("prompt_version", None)
        started_at = clock().strftime("%Y-%m-%dT%H:%M:%SZ") if clock else st.now_iso()
        run = {
            "run_id": lock.run_id,
            "who": lock.holder,
            "started_at": started_at,
            "full": full,
            "rebuild": rebuild,
            "dry_run": dry_run,
            "steps": [x for x in STEPS if x in steps],
            "limit": limit,
            "sources": [x for x in SOURCES if x in read],
        }

        discord, drive, retried = DiscordChanges(items=[]), DriveChanges(), []
        files_by_id: dict[str, dict[str, Any]] = {}
        pull: dsc.DiscordPull | None = None
        waiting: Waiting | None = None
        if pulling:
            pull, discord, retried, drive, files_by_id = _pull(
                cfg,
                state,
                sources,
                stages,
                full=full,
                rebuild=rebuild,
                read_discord=read_discord,
                read_drive=read_drive,
            )
            items = build_items(discord, retried, drive, files_by_id)
        else:
            stages.skip("Reading Discord", "--extract only")
            stages.skip("Listing Drive files", "--extract only")
            with stages.stage("Loading items waiting for extraction") as s:
                s.detail("listing raw/drive/ in the Knowledge Base folder")
                waiting = waiting_items(
                    state.manifest,
                    _raw_snapshots(sources.kb_folder) if read_drive else _NoSnapshots(),
                    limit,
                    s.detail,
                    read=read,
                )
                items = waiting.items
                s.result = f"{_plural(len(items), 'item')} to extract"
                if waiting.deferred:
                    s.result += f", {len(waiting.deferred)} more wait (--limit {limit})"
                if waiting.need_pull:
                    s.result += (
                        f"; {_plural(len(waiting.need_pull), 'item')} need a full "
                        "`kb update` first (no saved copy to extract from)"
                    )

        if not extracting:
            items, later = [], items
            why = "--pull only"
        else:
            items, later = pick(items, limit)  # --extract: waiting_items already picked
            why = f"--limit {limit}"
        later_ids = [i.id for i in later] + (waiting.deferred if waiting else [])
        # `--limit 0` reads (unless --extract) but extracts nothing, like --pull.
        no_extract = None if extracting and limit != 0 else why

        result = RunResult(
            run=run, discord=discord, drive=drive, dry_run=dry_run, items=items + later
        )
        result.deferred = {i: why for i in later_ids}
        result.waiting = waiting
        if dry_run:
            stages.skip("Saving raw snapshots", "dry run")
            if not no_extract:
                with stages.stage("Estimating extraction size and cost (no LLM calls)") as s:
                    result.estimate = ext.estimate(items, store.types.names, cfg)
                    s.result = estimate_line(result.estimate, cfg)
                    if later_ids:
                        s.result += f"; {len(later_ids)} more wait ({why})"
            else:
                stages.skip("Estimating extraction size and cost", no_extract)
            stages.skip("Saving state to Drive", "dry run")
            return result

        if pulling:
            with stages.stage("Saving raw snapshots locally and in Drive") as s:
                s.result = save_raw(
                    cfg, sources.kb_folder, run, discord, drive, files_by_id, s.detail
                )
        else:
            stages.skip("Saving raw snapshots", "--extract only")

        if no_extract:
            stages.skip(
                "Extracting records", f"{no_extract}; {_plural(len(later_ids), 'item')} wait"
            )
            result.extracted = ext.ExtractResult(usage=llm_mod.Usage(model=cfg.llm_extract_model))
        elif not items:
            stages.skip("Extracting records", "nothing new")
            result.extracted = ext.ExtractResult(usage=llm_mod.Usage(model=cfg.llm_extract_model))
        else:
            title = f"Extracting records from {_plural(len(items), 'item')}"
            if later_ids:
                title += f" ({len(later_ids)} more wait: {why})"
            with stages.stage(title) as s:
                result.extracted = ext.run(items, store, sources.llm, cfg, started_at, s.detail)
                x = result.extracted
                s.result = (
                    f"{_plural(x.records, 'record')} from {_plural(len(x.ok), 'item')}, "
                    f"{len(x.skipped)} skipped (too large), {_plural(len(x.errors), 'error')}; "
                    f"{x.usage.line()}"
                )
        result.extracted.deferred.update(result.deferred)

        # (M4: synthesis and publish go here.)

        with stages.stage("Saving state to Drive") as s:
            cursors_before = dict(state.cursors)
            tokens = {i.id: i.tokens for i in result.items}
            if pull is not None:  # Discord was read
                apply_discord(state, pull, discord, started_at, result.extracted, retried)
                ext.mark_removed(
                    store.citations, message_ids=[d["message_id"] for d in discord.deleted]
                )
            if pulling and read_drive:
                apply_drive(state, drive, started_at, result.extracted, tokens)
                for f in drive.skipped:
                    store.citations.refresh_drive(state.manifest[f"drive:{f['id']}"])
                ext.mark_removed(store.citations, drive_ids=drive.removed)
                state.cursors["last_run_started_at"] = started_at  # Drive's cursor
            if waiting is not None:
                apply_waiting(state, waiting, result.extracted, started_at)
            run.update({"finished_at": st.now_iso(), "summary": summarize(result)})
            if pull is not None:
                run["discord_channel_cursor"] = {
                    "before": cursors_before.get("discord_channel"),
                    "after": state.cursors["discord_channel"],
                }
            state.run_log.append(run)
            state.save()
            save_store(store, local_state)
            uploaded = st.upload(state_folder, local_state)
            s.result = f"uploaded {', '.join(uploaded)}"
        return result
    finally:
        _release(lock, log)
        log(f"Finished in {stages.elapsed():.1f}s.")


def _pull(
    cfg: Config,
    state: st.State,
    sources: Sources,
    stages: Stages,
    *,
    full: bool,
    rebuild: bool,
    read_discord: bool = True,
    read_drive: bool = True,
) -> tuple[
    dsc.DiscordPull | None,
    DiscordChanges,
    list[dict[str, Any]],
    DriveChanges,
    dict[str, dict[str, Any]],
]:
    """Read Discord past the cursors, list Drive, and export new and changed files.

    A dry run exports too (read-only): the size and cost estimate needs the text.
    A source that isn't read is skipped, and comes back empty (Discord's pull as None).
    """
    files_by_id: dict[str, dict[str, Any]] = {}
    if not read_discord:
        stages.skip("Reading Discord", "--source drive")
        pull, discord, retried = None, DiscordChanges(items=[]), []
    else:
        pull, discord, retried = _read_discord(state, sources, stages, full=full, rebuild=rebuild)
    if not read_drive:
        stages.skip("Listing Drive files", "--source discord")
        stages.skip("Exporting new and modified Drive files", "--source discord")
        return pull, discord, retried, DriveChanges(), files_by_id

    with stages.stage("Listing Drive files and comparing to the manifest") as s:
        files = sources.drive_list()
        files_by_id = {f["id"]: f for f in files}
        drive = classify_drive(
            files,
            state.manifest,
            state.cursors.get("last_run_started_at"),
            full=full,
            retry_skipped=lambda fid, e: (
                fid in cfg.drive_allow_large_files
                or int(e.get("estimated_tokens") or 0) <= cfg.llm_max_item_tokens
            ),
        )
        s.result = (
            f"{_plural(len(files), 'file')}: {len(drive.new)} new, "
            f"{len(drive.modified)} modified since the last run or not yet extracted, "
            f"{len(drive.skipped)} unchanged, {len(drive.removed)} removed"
        )

    todo = len(drive.new) + len(drive.modified)
    if not todo:
        stages.skip("Exporting new and modified Drive files", "nothing to export")
    else:
        workers = cfg.drive_export_workers
        title = f"Exporting {_plural(todo, 'new or modified Drive file')} ({workers} at a time)"
        with stages.stage(title) as s, gdrive.RetryCounter() as retries:
            export_drive(drive, state.manifest, sources.drive_export, s.detail, workers)
            s.result = (
                f"{len(drive.new)} new, {len(drive.changed)} changed, "
                f"{len(drive.unchanged)} with the same text (skipped), "
                f"{_plural(len(drive.errors), 'error')}{retries.summary()}"
            )
    return pull, discord, retried, drive, files_by_id


def _read_discord(
    state: st.State,
    sources: Sources,
    stages: Stages,
    *,
    full: bool,
    rebuild: bool,
) -> tuple[dsc.DiscordPull, DiscordChanges, list[dict[str, Any]]]:
    """Read Discord past the saved cursors (or all of it), and plan what to extract."""
    first = state.cursors.get("discord_channel") is None
    catch_up = any(
        e.get("source") == "discord" and needs_extraction(e) for e in state.manifest.values()
    )
    how = (
        "everything (--full)"
        if full
        else "everything (rebuild)"
        if rebuild
        else "everything (first run)"
        if first
        else "everything (catching up on items never extracted)"
        if catch_up
        else "messages after the saved cursors"
    )
    with stages.stage(f"Reading Discord: {how}") as s:
        read_all = full or rebuild or first or catch_up
        pull = sources.discord_fetch(None if read_all else state.cursors, full or rebuild)
        discord = plan_discord(pull, state.manifest)
        retried = discord_retries(state.manifest, discord)
        for w in pull.warnings:
            s.detail(f"warning: {w}")
        s.result = discord_result(discord, full)
        if retried:
            s.result += f"; {_plural(len(retried), 'item')} waiting from earlier runs"
    return pull, discord, retried


class _RemoteSnapshots:
    """Drive files' raw snapshots in raw/drive/ in the Knowledge Base folder.

    The folder is listed once, so checking for a snapshot costs nothing and reading one
    is a single download. Only the copy in Drive is used: local files in data/raw/ may
    come from `kb pull` or an older exporter, and needn't match what the last
    `kb update` pulled.
    """

    def __init__(self, kb_folder: st.RemoteFolder) -> None:
        raw = kb_folder.subfolder(st.RAW_FOLDER, create=False)
        self.folder = raw.subfolder("drive", create=False) if raw is not None else None
        self.ids: dict[str, str] = {}
        for f in self.folder.list() if self.folder is not None else []:
            self.ids.setdefault(f["name"], f["id"])  # oldest first, like `read`

    def has(self, file_id: str) -> bool:
        return f"{file_id}.md" in self.ids

    def read(self, file_id: str) -> str | None:
        fid = self.ids.get(f"{file_id}.md")
        if fid is None or self.folder is None:
            return None
        return self.folder.read_id(fid).decode("utf-8")


class _NoSnapshots:
    """Stands in for raw/drive/ when Drive isn't read (`--source discord`)."""

    def has(self, file_id: str) -> bool:
        return False

    def read(self, file_id: str) -> str | None:
        return None


def _raw_snapshots(kb_folder: st.RemoteFolder) -> RawSnapshots:
    return _RemoteSnapshots(kb_folder)


def estimate_line(e: ext.Estimate, cfg: Config) -> str:
    how = "through the Batch API, half price" if e.batch else "sent directly"
    return (
        f"{_plural(e.requests, 'request')} ({how}): about {e.input_tokens:,} input and "
        f"{e.output_tokens:,} output tokens, estimated cost "
        f"{llm_mod.format_cost(e.cost_usd)} with {cfg.llm_extract_model}; "
        f"{len(e.skipped)} skipped (too large)"
    )


def _release(lock: st.Lock, log: Log) -> None:
    """Release the lock without letting a failure hide how the run itself went."""
    if lock.file_id is None:
        return
    try:
        lock.release()
    except Exception as e:
        log(
            f"warning: the run lock couldn't be released ({type(e).__name__}: {e}). "
            "Everything above still applies. The next run will report the lock as held; "
            "once it's stale, clear it with `kb update --force-unlock`."
        )
    else:
        log("Run lock released.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(r: RunResult) -> dict[str, Any]:
    d, g = r.discord, r.drive
    out = {
        "discord": {
            "new_threads": sum(1 for i in d.items if i["kind"] == "thread" and not i["context"]),
            "updated_threads": sum(1 for i in d.items if i["kind"] == "thread" and i["context"]),
            "channel_messages": sum(1 for i in d.items if i["kind"] == "message"),
            "new_messages": d.new_messages,
            "edited_messages": len(d.edited),
            "deleted_messages": len(d.deleted),
        },
        "drive": {
            "new": len(g.new),
            "changed": len(g.changed),
            "unchanged_text": len(g.unchanged),
            "skipped": len(g.skipped),
            "removed": len(g.removed),
            "errors": len(g.errors),
        },
    }
    if r.extracted is not None:
        out["extraction"] = r.extracted.summary()
    if r.estimate is not None:
        e = r.estimate
        out["estimate"] = {
            "requests": e.requests,
            "input_tokens": e.input_tokens,
            "output_tokens": e.output_tokens,
            "batch": e.batch,
            "cost_usd": None if e.cost_usd is None else round(e.cost_usd, 4),
            "skipped": len(e.skipped),
        }
    return out


def report(r: RunResult, manifest: dict[str, dict[str, Any]], log: Log, limit: int = 20) -> None:
    s = summarize(r)
    d, g = s["discord"], s["drive"]

    def listing(title: str, lines: list[str]) -> None:
        if not lines:
            return
        log(title)
        for line in lines[:limit]:
            log(f"  - {line}")
        if len(lines) > limit:
            log(f"  … and {len(lines) - limit} more")

    log("")
    log("Summary")
    if "pull" in r.run.get("steps", STEPS):
        log(
            f"Discord: {d['new_messages']} new messages in {len(r.discord.items)} items "
            f"({d['new_threads']} new threads, {d['updated_threads']} threads with new "
            f"messages, {d['channel_messages']} channel messages)"
        )
        listing(
            "  New:",
            [f"{i['title']} ({len(i['messages'])} msgs) {i['link']}" for i in r.discord.items],
        )
        if r.run["full"]:
            log(f"  Edited: {d['edited_messages']}, deleted: {d['deleted_messages']}")
            listing(
                "  Deleted messages:",
                [f"{x['title']}: {x['message_id']}" for x in r.discord.deleted],
            )

        log(
            f"Drive: {g['new']} new, {g['changed']} changed, {g['unchanged_text']} modified "
            f"with the same text (skipped), {g['skipped']} unchanged, {g['removed']} removed, "
            f"{g['errors']} errors"
        )
        listing("  New:", [f.get("name", f["id"]) for f in r.drive.new])
        listing("  Changed:", [f.get("name", f["id"]) for f in r.drive.changed])
        listing(
            "  Removed:", [str((manifest.get(k) or {}).get("title") or k) for k in r.drive.removed]
        )
        listing("  Errors:", [f"{fid}: {e}" for fid, e in r.drive.errors.items()])
    if r.waiting is not None:
        log(f"Waiting for extraction before this run: {_plural(len(r.items), 'item')}")
        listing(
            "  Need a full `kb update` first (no saved copy to extract from):",
            r.waiting.need_pull,
        )

    titles = {i.id: i.title for i in r.items}
    if r.estimate is not None:
        e = r.estimate
        log(
            f"Extraction estimate: {_plural(len(e.items), 'item')}, "
            f"{_plural(e.requests, 'request')}"
        )
        ranked = sorted(e.items, key=lambda i: -i["tokens"])
        listing(
            "  Per item (largest first, about this many tokens):",
            [
                f"{i['tokens']:>9,}  {i['title']}"
                + ("  [skipped: too large]" if i["skipped"] else "")
                for i in ranked
            ],
        )
        log(
            f"  Total: about {e.input_tokens:,} input tokens and {e.output_tokens:,} output "
            f"tokens (a rough guess) over {_plural(e.requests, 'request')}"
            + (" through the Batch API (half price)" if e.batch else "")
        )
        log(f"  Estimated cost: {llm_mod.format_cost(e.cost_usd)}")
        listing(
            "  Would be skipped (too large):",
            [f"{i['title']}: {i['skipped']}" for i in e.skipped],
        )
    if r.extracted is not None:
        x = r.extracted
        log(
            f"Extraction: {_plural(x.records, 'record')} from {_plural(len(x.ok), 'item')}, "
            f"{len(x.skipped)} skipped, {_plural(len(x.errors), 'error')}"
        )
        if x.new_types:
            log(f"  New programming types: {', '.join(x.new_types)}")
        if x.new_sessions:
            log(f"  New sessions: {', '.join(x.new_sessions)}")
        listing(
            "  Skipped (too large):", [f"{titles.get(k, k)}: {v}" for k, v in x.skipped.items()]
        )
        listing(
            "  Errors (tried again next run):",
            [f"{titles.get(k, k)}: {v}" for k, v in x.errors.items()],
        )
        dropped = [f"{titles.get(k, k)}: {p}" for k, ps in x.problems.items() for p in ps]
        listing("  Record problems:", dropped)
        log(f"Tokens and cost: {x.usage.line()}")
    if r.deferred:
        why = next(iter(r.deferred.values()))
        log(
            f"Waiting for extraction ({why}): {_plural(len(r.deferred), 'item')}. "
            "The next `kb update` (or `kb update --extract`) extracts them."
        )
    if r.dry_run:
        log("\nDry run: nothing was saved.")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_lines(state: st.State, lock: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    if state.run_log:
        last = state.run_log[-1]
        flags = [
            "--full" if last.get("full") else "",
            "rebuild" if last.get("rebuild") else "",
            "requeue" if last.get("requeue") else "",
            *(f"--{x}" for x in last.get("steps") or STEPS if len(last.get("steps") or STEPS) == 1),
            f"--limit {last['limit']}" if last.get("limit") is not None else "",
            *(
                f"--source {x}"
                for x in last.get("sources") or SOURCES
                if len(last.get("sources") or SOURCES) == 1
            ),
        ]
        flags = [f for f in flags if f]
        out.append(
            f"Last run: {last.get('started_at')} by {last.get('who')}"
            + (f" ({' '.join(flags)})" if flags else "")
        )
        s = last.get("summary") or {}
        if last.get("requeue"):
            out.append(f"  {_plural(s.get('requeued', 0), 'item')} marked to extract again")
        elif s:
            dsum, gsum = s.get("discord", {}), s.get("drive", {})
            out.append(
                f"  Discord: {dsum.get('new_messages', 0)} new messages; "
                f"Drive: {gsum.get('new', 0)} new, {gsum.get('changed', 0)} changed, "
                f"{gsum.get('removed', 0)} removed, {gsum.get('errors', 0)} errors"
            )
    else:
        out.append("Last run: none yet")
    out.append(
        "Lock: "
        + (f"held by {lock.get('holder')} since {lock.get('acquired_at')}" if lock else "free")
    )

    c = state.cursors
    out.append(
        f"Cursors: channel {c.get('discord_channel') or '-'}, "
        f"{len(c.get('discord_threads') or {})} threads, "
        f"last run started {c.get('last_run_started_at') or '-'}"
    )

    counts: dict[str, dict[str, int]] = {}
    for e in state.manifest.values():
        src = "discord" if e.get("source") == "discord" else "drive"
        by = counts.setdefault(src, {})
        by[e.get("status", "?")] = by.get(e.get("status", "?"), 0) + 1
    out.append("Items:")
    if not counts:
        out.append("  none yet")
    for src, by in sorted(counts.items()):
        out.append(f"  {src}: " + ", ".join(f"{n} {k}" for k, n in sorted(by.items())))

    waiting = sum(1 for e in state.manifest.values() if needs_extraction(e))
    if waiting:
        out.append(f"  {waiting} items not extracted yet (the next `kb update` extracts them)")
    pending = sum(1 for e in state.manifest.values() if e.get("status") == "pending")
    if pending:
        out.append(
            f"  {pending} pending: pulled but waiting for extraction "
            "(`kb update` or `kb update --extract`)"
        )

    errors = [e for e in state.manifest.values() if e.get("status") == "error"]
    if errors:
        out.append("Errors (tried again next run):")
        for e in errors[:10]:
            out.append(f"  - {e.get('title')}: {e.get('error')}")
        if len(errors) > 10:
            out.append(f"  … and {len(errors) - 10} more")

    skipped = [e for e in state.manifest.values() if e.get("status") == "skipped"]
    if skipped:
        out.append("Skipped (too large):")
        for e in skipped:
            out.append(f"  - {e.get('title')} ({e.get('id')}): {e.get('skip_reason')}")

    usage = (
        ((state.run_log[-1].get("summary") or {}).get("extraction") or {}).get("usage")
        if state.run_log
        else None
    )
    if usage and usage.get("requests"):
        u = llm_mod.Usage(**usage)
        out.append(f"Last run's extraction: {u.line()}")

    store = load_store(state.dir)
    out.extend(ext.status_lines(store.records, store.citations, store.types, store.sessions))
    return out


def status(cfg: Config, kb_folder: st.RemoteFolder | None = None) -> list[str]:
    if kb_folder is None:
        services = gdrive.Services.from_key(cfg.google_credentials_path)
        kb_folder = st.DriveFolder(services.drive, cfg.drive_kb_folder_id)
    folder = kb_folder.subfolder(st.STATE_FOLDER, create=False)
    local = cfg.data_dir / "state"
    st.download(folder, local)
    lock = st.read_lock(folder) if folder is not None else None
    return status_lines(st.State(local).load(), lock)


def requeue(
    cfg: Config,
    *,
    kb_folder: st.RemoteFolder | None = None,
    force_unlock: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Mark every extracted item as never extracted, so later runs extract it again.

    Only clears `prompt_version` in the manifest: nothing is read or sent to the model,
    and each item keeps its records and citations until its new records replace them.
    Run `kb update` (with `--limit` and `--extract` as usual) afterwards. Returns the
    number of items requeued.
    """
    if kb_folder is None:
        services = gdrive.Services.from_key(cfg.google_credentials_path)
        kb_folder = st.DriveFolder(services.drive, cfg.drive_kb_folder_id)
    folder = kb_folder.subfolder(st.STATE_FOLDER, create=False)
    if folder is None:  # nothing pulled yet, so nothing to requeue
        return 0
    lock = st.Lock(folder, cfg.lock_timeout_minutes, clock=clock)
    lock.acquire(force_unlock=force_unlock)
    try:
        local = cfg.data_dir / "state"
        st.download(folder, local)
        state = st.State(local).load()
        started_at = clock().strftime("%Y-%m-%dT%H:%M:%SZ") if clock else st.now_iso()
        n = 0
        for entry in state.manifest.values():
            if entry.pop("prompt_version", None) is not None:
                n += 1
        state.run_log.append(
            {
                "run_id": lock.run_id,
                "who": lock.holder,
                "started_at": started_at,
                "finished_at": st.now_iso(),
                "requeue": True,
                "summary": {"requeued": n},
            }
        )
        state.save()
        st.upload(folder, local)
        return n
    finally:
        _release(lock, print)
