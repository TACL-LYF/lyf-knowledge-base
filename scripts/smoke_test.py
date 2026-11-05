"""M0 smoke test: prove the real credentials can reach Discord and Drive.

Checks:
  1. Discord: read one message from the main channel.
  2. Discord: read one message from a thread in the channel (active or archived).
  3. Drive: list one file in the source folder.
  4. Drive: the Knowledge Base folder is in a Shared Drive and the service account can add files.

Run from the repo root:  python scripts/smoke_test.py
Read-only: nothing is written to Discord or Drive.
"""

from __future__ import annotations

import sys

from kb.config import ConfigError, load_config
from kb.sources import drive as gdrive
from kb.sources.discord import DiscordClient, DiscordError, message_link

OK, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((OK if ok else FAIL, name, detail))
    print(f"[{OK if ok else FAIL}] {name}: {detail}")


def preview(text: str, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def check_discord(cfg) -> None:
    with DiscordClient(cfg.discord_bot_token) as dc:
        try:
            channel = dc.get_channel(cfg.discord_channel_id)
            msgs = dc.get_messages(cfg.discord_channel_id, limit=1)
        except DiscordError as e:
            record("Discord channel message", False, str(e))
            return
        if not msgs:
            record("Discord channel message", False, f"#{channel.get('name')} has no messages")
        else:
            m = msgs[0]
            if not m.get("content") and not m.get("embeds") and not m.get("attachments"):
                note = " (empty content: is Message Content Intent on?)"
            else:
                note = ""
            link = message_link(cfg.discord_server_id, cfg.discord_channel_id, m["id"])
            record(
                "Discord channel message",
                True,
                f"#{channel.get('name')} · {m['author'].get('username')}: "
                f'"{preview(m.get("content", ""))}"{note} · {link}',
            )

        try:
            threads = dc.active_threads(cfg.discord_server_id, cfg.discord_channel_id)
            if not threads:
                threads = dc.archived_public_threads(cfg.discord_channel_id, limit=5)
        except DiscordError as e:
            record("Discord thread message", False, str(e))
            return
        if not threads:
            record("Discord thread message", False, "no active or archived public threads found")
            return
        t = threads[0]
        try:
            tmsgs = dc.get_messages(t["id"], limit=1)
        except DiscordError as e:
            record("Discord thread message", False, f"thread '{t.get('name')}': {e}")
            return
        if not tmsgs:
            record("Discord thread message", False, f"thread '{t.get('name')}' has no messages")
            return
        tm = tmsgs[0]
        record(
            "Discord thread message",
            True,
            f"thread '{t.get('name')}' · {tm['author'].get('username')}: "
            f'"{preview(tm.get("content", ""))}" · '
            f"{message_link(cfg.discord_server_id, t['id'], tm['id'])}",
        )


def check_drive(cfg) -> None:
    key = cfg.google_credentials_path
    print(f"       service account: {gdrive.service_account_email(key)}")
    svc = gdrive.drive_service(key)

    try:
        page = gdrive.list_children(svc, cfg.drive_source_folder_id, page_size=20)
        files = [f for f in page.get("files", []) if f["mimeType"] != gdrive.FOLDER_MIME]
        in_scope = [f for f in files if f["mimeType"] in cfg.drive_file_types]
        pick = (in_scope or files or page.get("files", []) or [None])[0]
        if pick is None:
            record("Drive source file", False, "source folder is empty or not shared")
        else:
            record(
                "Drive source file",
                True,
                f"{pick['name']} ({pick['mimeType'].rsplit('.', 1)[-1]}) · "
                f"{pick.get('webViewLink')}",
            )
    except Exception as e:  # googleapiclient raises HttpError with useful text
        record("Drive source file", False, f"{type(e).__name__}: {e}")

    try:
        kb = gdrive.get_file(
            svc, cfg.drive_kb_folder_id, fields="id, name, driveId, capabilities(canAddChildren)"
        )
        problems = []
        if not kb.get("driveId"):
            problems.append(
                "not in a Shared Drive (service accounts can't create files in My Drive)"
            )
        if not kb.get("capabilities", {}).get("canAddChildren"):
            problems.append("service account can't add files (needs Content manager)")
        record(
            "Drive Knowledge Base folder",
            not problems,
            f"'{kb['name']}': " + ("; ".join(problems) if problems else "writable, Shared Drive"),
        )
    except Exception as e:
        record("Drive Knowledge Base folder", False, f"{type(e).__name__}: {e}")


def main() -> int:
    try:
        cfg = load_config()
        cfg.require(
            "discord_server_id",
            "discord_channel_id",
            "discord_bot_token",
            "drive_source_folder_id",
            "drive_kb_folder_id",
            "google_credentials_path",
        )
    except ConfigError as e:
        print(f"[{FAIL}] config: {e}")
        return 1

    check_discord(cfg)
    check_drive(cfg)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
