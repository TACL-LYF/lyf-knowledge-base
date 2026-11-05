"""M1 raw pulls: read everything from Discord and Drive and save it under data/raw/.

Local only and a full read every time; nothing is written to Discord or Drive.

    data/raw/
    ├── discord/threads/<threadId>.json    one item per thread
    ├── discord/messages/<messageId>.json  one item per standalone channel message
    ├── drive/<fileId>.md                  exported text, with metadata front matter
    └── pull_summary.json                  counts, every item with its link, errors

Server nicknames already looked up are remembered in data/raw/discord/members.json
(shared with `kb update`), so only new authors are looked up. `--refresh-names` looks
everyone up again.

Each source is written to a temporary directory first and swapped in only when it
finishes, so a failed pull leaves the previous output in place. Only the pull's own
subdirectories are replaced; `kb update`'s dated snapshots in discord/ are left alone.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kb.config import Config
from kb.progress import Stages
from kb.sources import discord as dsc
from kb.sources import drive as gdrive

Log = Callable[[str], None]
SOURCES = ("discord", "drive")
MEMBER_CACHE = Path("raw") / "discord" / "members.json"  # relative to data_dir


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _swap_in(partial: Path, final: Path) -> None:
    if final.exists():
        shutil.rmtree(final)
    partial.rename(final)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pull_discord(
    cfg: Config,
    raw_dir: Path,
    log: Log,
    refresh_names: bool = False,
    stages: Stages | None = None,
) -> dict[str, Any]:
    stages = stages or Stages(log, total=2)
    cache = dsc.MemberCache(cfg.data_dir / MEMBER_CACHE)
    with stages.stage("Reading Discord: the whole channel and every thread") as s:
        with dsc.DiscordClient(cfg.discord_bot_token) as client:
            pull = dsc.fetch_all(
                client,
                cfg.discord_server_id,
                cfg.discord_channel_id,
                s.detail,
                member_cache=cache,
                refresh_names=refresh_names,
            )
        items = dsc.build_items(pull)
        for w in pull.warnings:
            s.detail(f"warning: {w}")
        threads = sum(1 for i in items if i["kind"] == "thread")
        s.result = (
            f"{threads} threads + {len(items) - threads} channel messages "
            f"= {len(items)} items ({dsc.count_messages(items)} messages)"
        )

    with stages.stage("Writing Discord items") as s:
        partial = _fresh_dir(raw_dir / ".discord.partial")
        listed = []
        for item in items:
            if item["kind"] == "thread":
                sub, key = "threads", item["thread_id"]
            else:
                sub, key = "messages", item["messages"][0]["id"]
            rel = Path("discord") / sub / f"{key}.json"
            _write_json(partial / sub / f"{key}.json", item)
            listed.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "link": item["link"],
                    "messages": len(item["messages"]),
                    "path": rel.as_posix(),
                }
            )
        (raw_dir / "discord").mkdir(parents=True, exist_ok=True)
        for sub in ("threads", "messages"):
            (partial / sub).mkdir(exist_ok=True)
            _swap_in(partial / sub, raw_dir / "discord" / sub)
        partial.rmdir()
        s.result = f"wrote {len(items)} files to {raw_dir / 'discord'}"

    return {
        "threads": threads,
        "channel_messages": len(items) - threads,
        "messages": dsc.count_messages(items),
        "items": listed,
        "warnings": pull.warnings,
    }


def pull_drive(
    cfg: Config,
    raw_dir: Path,
    log: Log,
    make_services: Callable[[], gdrive.Services] | None = None,
    stages: Stages | None = None,
) -> dict[str, Any]:
    """Export every in-scope file. `make_services` builds API clients; it's called once
    for listing and once per export thread (clients can't be shared between threads)."""
    stages = stages or Stages(log, total=3)
    with stages.stage("Connecting to Google Drive") as s:
        if make_services is None:
            creds = gdrive.credentials(cfg.google_credentials_path)
            make_services = lambda: gdrive.Services.from_credentials(creds)  # noqa: E731
            s.result = f"signed in as {creds.service_account_email}"
        services = make_services()

    folders = cfg.drive_source_folder_ids
    with stages.stage(f"Listing files in {len(folders)} source folder(s), with subfolders") as s:
        files = list(
            gdrive.list_folder_files(
                services.drive,
                folders,
                cfg.drive_file_types,
                s.detail,
                make_drive=lambda: make_services().drive,
                workers=cfg.drive_export_workers,
            )
        )
        counts: dict[str, int] = {}
        for f in files:
            kind = gdrive.SOURCE_TYPE.get(f["mimeType"], f["mimeType"])
            counts[kind] = counts.get(kind, 0) + 1
        s.result = f"{len(files)} in-scope files" + (
            " (" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ")" if counts else ""
        )

    workers = cfg.drive_export_workers
    with stages.stage(f"Exporting {len(files)} Drive files ({workers} at a time)") as s:
        partial = _fresh_dir(raw_dir / ".drive.partial")
        listed, errors = [], []
        by_type: dict[str, int] = {}
        width = len(str(len(files)))
        export = gdrive.threaded_export(make_services)
        with gdrive.RetryCounter() as retries:
            results = gdrive.export_files(files, export, workers)
            for n, r in enumerate(results, 1):
                f = r.file
                meta = gdrive.file_metadata(f)
                label = f"({n:>{width}}/{len(files)}) {meta['title']}"
                if r.error is not None:  # one bad file shouldn't stop the pull
                    errors.append(
                        {
                            "id": meta["id"],
                            "title": meta["title"],
                            "link": meta["link"],
                            "error": r.error,
                        }
                    )
                    s.detail(f"{label}: error: {r.error}")
                else:
                    assert r.body is not None
                    (partial / f"{f['id']}.md").write_text(
                        gdrive.render_file(f, r.body), encoding="utf-8"
                    )
                    by_type[meta["source"]] = by_type.get(meta["source"], 0) + 1
                    listed.append({**meta, "path": f"drive/{f['id']}.md"})
                    s.detail(f"{label}: ok")
        _swap_in(partial, raw_dir / "drive")
        s.result = (
            f"{len(listed)} exported ("
            + ", ".join(f"{v} {k}" for k, v in sorted(by_type.items()))
            + f"), {len(errors)} errors{retries.summary()}, written to {raw_dir / 'drive'}"
        )

    return {"files": len(listed), "by_type": by_type, "items": listed, "errors": errors}


def pull(
    cfg: Config,
    sources: tuple[str, ...] = SOURCES,
    log: Log = print,
    refresh_names: bool = False,
) -> dict[str, Any]:
    """Pull the given sources into data/raw/ and write pull_summary.json."""
    raw_dir = cfg.data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    summary_path = raw_dir / "pull_summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():  # keep the other source's results on a partial pull
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = {}

    total = 2 * ("discord" in sources) + 3 * ("drive" in sources)
    stages = Stages(log, total=total)
    log(f"kb pull ({', '.join(sources)}): full read into {raw_dir}, nothing is written to Drive")
    summary["started_at"] = _now()
    if "discord" in sources:
        summary["discord"] = pull_discord(cfg, raw_dir, log, refresh_names, stages=stages)
    if "drive" in sources:
        summary["drive"] = pull_drive(cfg, raw_dir, log, stages=stages)
    summary["finished_at"] = _now()
    _write_json(summary_path, summary)
    log(f"Wrote {summary_path.name}. Finished in {stages.elapsed():.1f}s.")
    return summary
