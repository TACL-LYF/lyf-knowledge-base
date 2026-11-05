"""`kb update`: find what's new in Discord and Drive since the last run, and save it.

One run:

1. Take the lock in `_state/` and download `_state/` into `data/state/`.
2. Discord: read past the cursors (or everything with `--full`), and keep only messages
   not seen before. Earlier messages in the same thread are kept as context.
3. Drive: list every file in the source folders (recursively) and compare to the
   manifest: new / changed / skip / removed. New and changed files are exported, and a
   change that leaves the text the same (e.g. a comment) is skipped by content hash.
4. Save raw snapshots locally in `data/raw/` (never uploaded, to keep Drive storage small).
5. Update the manifest, cursors and run log, upload `_state/`, release the lock.

State is uploaded only at step 5, so a run that fails earlier changes nothing and the
next run starts from the same place. Extraction (M3) and publishing (M4) slot in
between steps 4 and 5.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kb import state as st
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
    drive_export: Callable[[dict[str, Any]], str]
    kb_folder: st.RemoteFolder

    @classmethod
    def real(cls, cfg: Config, log: Log) -> Sources:
        services = gdrive.Services.from_key(cfg.google_credentials_path)
        cache = dsc.MemberCache(cfg.data_dir / MEMBER_CACHE)

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
                    services.drive, cfg.drive_source_folder_ids, cfg.drive_file_types, log
                )
            ),
            drive_export=lambda f: gdrive.export_text(services, f),
            kb_folder=st.DriveFolder(services.drive, cfg.drive_kb_folder_id),
        )


# ---------------------------------------------------------------------------
# Discord: what's new (pure)
# ---------------------------------------------------------------------------


@dataclass
class DiscordChanges:
    items: list[dict[str, Any]]  # only new (or, with --full, edited) messages; rest is context
    edited: list[dict[str, Any]] = field(default_factory=list)  # {item_id, message_id}
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

    return DiscordChanges(
        items=dsc.split_new(items, is_new),
        edited=edited,
        deleted=deleted,
        warnings=list(pull.warnings),
    )


def apply_discord(
    state: st.State, pull: dsc.DiscordPull, changes: DiscordChanges, now: str
) -> None:
    """Record processed items, deletions, and the new cursors in state."""
    for item in changes.items:
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
                "status": "ok",
            }
        )
        state.manifest[item["id"]] = entry

    for d in changes.deleted:
        entry = state.manifest[d["item_id"]]
        removed = entry.setdefault("removed_messages", [])
        if d["message_id"] not in removed:
            removed.append(d["message_id"])
        if set(removed) >= set(entry.get("messages") or {}):
            entry["status"] = "removed"
        entry["last_processed"] = now

    state.cursors["discord_channel"] = pull.channel_cursor
    threads = dict(state.cursors.get("discord_threads") or {})
    threads.update(pull.thread_cursors)
    state.cursors["discord_threads"] = threads


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
) -> DriveChanges:
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
        if entry.get("status") == "error" or since is None or modified is None or modified > since:
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
) -> None:
    todo = changes.new + changes.modified
    modified_ids = {f["id"] for f in changes.modified}
    width = len(str(len(todo)))
    for n, f in enumerate(todo, 1):
        label = f"({n:>{width}}/{len(todo)}) {f.get('name') or f['id']}"
        try:
            body = export(f)
        except Exception as e:  # one bad file shouldn't stop the run
            changes.errors[f["id"]] = f"{type(e).__name__}: {e}"
            log(f"{label}: error: {changes.errors[f['id']]}")
        else:
            changes.exported[f["id"]] = body
            if f["id"] in modified_ids:
                entry = manifest.get(f"drive:{f['id']}") or {}
                same = entry.get("status") == "ok" and entry.get("content_hash") == content_hash(
                    body
                )
                (changes.unchanged if same else changes.changed).append(f)
                log(f"{label}: {'same text, skipped' if same else 'changed'}")
            else:
                log(f"{label}: new")


def apply_drive(state: st.State, changes: DriveChanges, now: str) -> None:
    for f in changes.new + changes.modified:
        key = f"drive:{f['id']}"
        meta = gdrive.file_metadata(f)
        entry = state.manifest.get(key) or {"first_seen": now}
        entry.update(meta)
        entry["last_processed"] = now
        if f["id"] in changes.errors:
            entry["status"] = "error"
            entry["error"] = changes.errors[f["id"]]
        else:
            entry["status"] = "ok"
            entry.pop("error", None)
            entry["content_hash"] = content_hash(changes.exported[f["id"]])
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
    run: dict[str, Any],
    discord: DiscordChanges,
    drive: DriveChanges,
    files_by_id: dict[str, dict[str, Any]],
) -> str:
    """Write this run's new content to data/raw/ (local only; nothing is uploaded).

    Returns a one-line summary of what was written.

    Discord: one file per day, `discord/YYYY-MM-DD.json`, holding every run that day.
    Drive: `drive/<fileId>.md`, the latest export of each new or changed file.
    """
    raw_dir = cfg.data_dir / "raw"

    if discord.items or discord.deleted:
        local = raw_dir / "discord" / f"{run['started_at'][:10]}.json"
        day: dict[str, Any] = {"date": run["started_at"][:10], "runs": []}
        if local.is_file():
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

    unchanged = {f["id"] for f in drive.unchanged}
    to_write = [fid for fid in drive.exported if fid not in unchanged]
    if to_write:
        (raw_dir / "drive").mkdir(parents=True, exist_ok=True)
        for fid in to_write:
            text = gdrive.render_file(files_by_id[fid], drive.exported[fid])
            (raw_dir / "drive" / f"{fid}.md").write_text(text, encoding="utf-8")

    parts = []
    if discord.items or discord.deleted:
        parts.append(f"discord/{local.name} ({_plural(len(discord.items), 'item')})")
    if to_write:
        parts.append(f"drive/ ({_plural(len(to_write), 'file')})")
    return f"wrote {' and '.join(parts)} in {raw_dir}" if parts else "nothing new to write"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run: dict[str, Any]
    discord: DiscordChanges
    drive: DriveChanges
    dry_run: bool


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


def run_update(
    cfg: Config,
    *,
    dry_run: bool = False,
    full: bool = False,
    force_unlock: bool = False,
    log: Log = print,
    sources: Sources | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RunResult:
    stages = Stages(log, total=8 if sources is None else 7)
    mode = " (dry run: nothing will be saved)" if dry_run else ""
    log(f"kb update{' --full' if full else ''}{mode}")

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
            last = state.run_log[-1] if state.run_log else None
            if not names:
                s.result = "no saved state yet: this run reads everything"
            else:
                s.result = f"{_plural(len(names), 'file')}, {len(state.manifest)} items known"
                if last:
                    s.result += f"; last run {last.get('started_at')} by {last.get('who')}"
        started_at = clock().strftime("%Y-%m-%dT%H:%M:%SZ") if clock else st.now_iso()
        run = {
            "run_id": lock.run_id,
            "who": lock.holder,
            "started_at": started_at,
            "full": full,
            "dry_run": dry_run,
        }

        first = state.cursors.get("last_run_started_at") is None
        how = (
            "everything (--full)"
            if full
            else "everything (first run)"
            if first
            else "messages after the saved cursors"
        )
        with stages.stage(f"Reading Discord: {how}") as s:
            pull = sources.discord_fetch(None if (full or first) else state.cursors, full)
            discord = plan_discord(pull, state.manifest)
            for w in pull.warnings:
                s.detail(f"warning: {w}")
            s.result = discord_result(discord, full)

        with stages.stage("Listing Drive files and comparing to the manifest") as s:
            files = sources.drive_list()
            drive = classify_drive(files, state.manifest, state.cursors.get("last_run_started_at"))
            s.result = (
                f"{_plural(len(files), 'file')}: {len(drive.new)} new, "
                f"{len(drive.modified)} modified since the last run, "
                f"{len(drive.skipped)} unchanged, {len(drive.removed)} removed"
            )

        todo = len(drive.new) + len(drive.modified)
        if dry_run:
            stages.skip("Exporting new and modified Drive files", "dry run")
        elif not todo:
            stages.skip("Exporting new and modified Drive files", "nothing to export")
        else:
            with stages.stage(f"Exporting {_plural(todo, 'new or modified Drive file')}") as s:
                export_drive(drive, state.manifest, sources.drive_export, s.detail)
                s.result = (
                    f"{len(drive.new)} new, {len(drive.changed)} changed, "
                    f"{len(drive.unchanged)} with the same text (skipped), "
                    f"{_plural(len(drive.errors), 'error')}"
                )

        result = RunResult(run=run, discord=discord, drive=drive, dry_run=dry_run)
        if dry_run:
            stages.skip("Saving raw snapshots", "dry run")
            stages.skip("Saving state to Drive", "dry run")
            return result

        with stages.stage("Saving raw snapshots locally") as s:
            s.result = save_raw(cfg, run, discord, drive, {f["id"]: f for f in files})

        # (M3: extraction goes here. M4: synthesis and publish.)

        with stages.stage("Saving state to Drive") as s:
            cursors_before = dict(state.cursors)
            apply_discord(state, pull, discord, started_at)
            apply_drive(state, drive, started_at)
            state.cursors["last_run_started_at"] = started_at
            run.update(
                {
                    "finished_at": st.now_iso(),
                    "discord_channel_cursor": {
                        "before": cursors_before.get("discord_channel"),
                        "after": state.cursors["discord_channel"],
                    },
                    "summary": summarize(result),
                }
            )
            state.run_log.append(run)
            state.save()
            uploaded = st.upload(state_folder, local_state)
            s.result = f"uploaded {', '.join(uploaded)}"
        return result
    finally:
        if lock.file_id is not None:
            lock.release()
            log("Run lock released.")
        log(f"Finished in {stages.elapsed():.1f}s.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(r: RunResult) -> dict[str, Any]:
    d, g = r.discord, r.drive
    return {
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
            "changed": len(g.changed) if not r.dry_run else len(g.modified),
            "unchanged_text": len(g.unchanged),
            "skipped": len(g.skipped),
            "removed": len(g.removed),
            "errors": len(g.errors),
        },
    }


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
    log(
        f"Discord: {d['new_messages']} new messages in {len(r.discord.items)} items "
        f"({d['new_threads']} new threads, {d['updated_threads']} threads with new messages, "
        f"{d['channel_messages']} channel messages)"
    )
    listing(
        "  New:",
        [f"{i['title']} ({len(i['messages'])} msgs) {i['link']}" for i in r.discord.items],
    )
    if r.run["full"]:
        log(f"  Edited: {d['edited_messages']}, deleted: {d['deleted_messages']}")
        listing(
            "  Deleted messages:", [f"{x['title']}: {x['message_id']}" for x in r.discord.deleted]
        )

    if r.dry_run:
        log(
            f"Drive: {g['new']} new, {g['changed']} modified since the last run (text not "
            f"checked in a dry run), {g['skipped']} unchanged, {g['removed']} removed"
        )
        listing("  New:", [f.get("name", f["id"]) for f in r.drive.new])
        listing("  Modified:", [f.get("name", f["id"]) for f in r.drive.modified])
    else:
        log(
            f"Drive: {g['new']} new, {g['changed']} changed, {g['unchanged_text']} modified with "
            f"the same text (skipped), {g['skipped']} unchanged, {g['removed']} removed, "
            f"{g['errors']} errors"
        )
        listing("  New:", [f.get("name", f["id"]) for f in r.drive.new])
        listing("  Changed:", [f.get("name", f["id"]) for f in r.drive.changed])
    listing("  Removed:", [str((manifest.get(k) or {}).get("title") or k) for k in r.drive.removed])
    listing("  Errors:", [f"{fid}: {e}" for fid, e in r.drive.errors.items()])
    if r.dry_run:
        log("\nDry run: nothing was saved.")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status_lines(state: st.State, lock: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    if state.run_log:
        last = state.run_log[-1]
        out.append(
            f"Last run: {last.get('started_at')} by {last.get('who')}"
            + (" (--full)" if last.get("full") else "")
        )
        s = last.get("summary") or {}
        if s:
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

    errors = [e for e in state.manifest.values() if e.get("status") == "error"]
    if errors:
        out.append("Errors:")
        for e in errors[:10]:
            out.append(f"  - {e.get('title')}: {e.get('error')}")

    types_path = state.dir / "programming_types.json"
    if types_path.is_file():
        types = json.loads(types_path.read_text(encoding="utf-8"))
        out.append("Programming types:")
        for t in types:  # record counts per type arrive with extracted.jsonl in M3
            out.append(f"  - {t if isinstance(t, str) else t.get('name')}")
    else:
        out.append("Programming types: none yet (found during extraction, M3)")
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
