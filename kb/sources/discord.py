"""Discord source: read the channel and its threads via the REST API, and build items.

M1 reads everything (full read). M2 adds cursors so later runs read only new messages.

An **item** is the unit later sent to the LLM:
- one per thread: every message in the thread, including the message that started it;
- one per main-channel message that isn't part of a thread. If it's a reply, the
  replied-to message is attached as read-only context.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/lyf-knowledge-base, 0.1.0)"

# Message types that carry human-written content. Everything else (joins, pins,
# "thread created" notices, slash-command output, …) is a system message.
DEFAULT = 0
REPLY = 19
THREAD_STARTER = 21  # placeholder at the top of a thread pointing at its parent message
CONTENT_TYPES = {DEFAULT, REPLY}

# Channel types whose posts all live in threads (no messages in the channel itself).
FORUM_TYPES = {15, 16}  # GUILD_FORUM, GUILD_MEDIA

PAGE_SIZE = 100
MAX_RETRIES = 5


class DiscordError(Exception):
    pass


class DiscordForbidden(DiscordError):
    """The bot lacks a permission for this endpoint (HTTP 403)."""


class DiscordNotFound(DiscordError):
    """The resource doesn't exist or the bot can't see it (HTTP 404)."""


def message_link(server_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{server_id}/{channel_id}/{message_id}"


def channel_link(server_id: str, channel_id: str) -> str:
    return f"https://discord.com/channels/{server_id}/{channel_id}"


class DiscordClient:
    """Thin read-only wrapper over the Discord REST API using a bot token."""

    def __init__(
        self,
        token: str,
        timeout: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
        )
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DiscordClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(MAX_RETRIES + 1):
            resp = self._http.get(path, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    break
                if resp.status_code == 429:
                    try:
                        wait = float(resp.json().get("retry_after", 1.0))
                    except ValueError:
                        wait = float(resp.headers.get("Retry-After", 1.0))
                else:
                    wait = 2.0**attempt
                self._sleep(wait + 0.1)
                continue
            break
        if resp.status_code == 401:
            raise DiscordError("Discord rejected the bot token (401). Check DISCORD_BOT_TOKEN.")
        if resp.status_code == 403:
            raise DiscordForbidden(
                f"Discord denied access to {path} (403). The bot needs View Channel and "
                "Read Message History on this channel."
            )
        if resp.status_code == 404:
            raise DiscordNotFound(f"Discord resource not found: {path} (404). Check the IDs.")
        if resp.is_error:
            raise DiscordError(f"Discord returned {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._get(f"/channels/{channel_id}")

    def get_messages(
        self, channel_id: str, *, limit: int = PAGE_SIZE, after: str | None = None
    ) -> list[dict[str, Any]]:
        """One page of messages in a channel or thread, newest first (Discord's order)."""
        return self._get(f"/channels/{channel_id}/messages", limit=limit, after=after)

    def all_messages(self, channel_id: str, *, after: str = "0") -> list[dict[str, Any]]:
        """Every message in a channel or thread after the given ID, oldest first.

        Pages forward with `after`, the same mechanism the M2 cursors will use.
        """
        out: list[dict[str, Any]] = []
        cursor = after
        while True:
            page = self.get_messages(channel_id, limit=PAGE_SIZE, after=cursor)
            if not page:
                break
            page.sort(key=lambda m: int(m["id"]))
            out.extend(page)
            cursor = page[-1]["id"]
            if len(page) < PAGE_SIZE:
                break
        return out

    def get_member(self, server_id: str, user_id: str) -> dict[str, Any] | None:
        """A user's server profile (nickname etc.), or None if they've left the server."""
        try:
            return self._get(f"/guilds/{server_id}/members/{user_id}")
        except DiscordNotFound:
            return None

    def active_threads(self, server_id: str, channel_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/guilds/{server_id}/threads/active")
        return [t for t in data.get("threads", []) if t.get("parent_id") == channel_id]

    def archived_public_threads(self, channel_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """One page of archived public threads (used by the smoke test)."""
        data = self._get(f"/channels/{channel_id}/threads/archived/public", limit=limit)
        return data.get("threads", [])

    def all_archived_threads(self, channel_id: str, kind: str = "public") -> list[dict[str, Any]]:
        """Every archived thread of a kind ("public" or "private"), paging by archive time."""
        out: list[dict[str, Any]] = []
        before: str | None = None
        while True:
            data = self._get(
                f"/channels/{channel_id}/threads/archived/{kind}", limit=100, before=before
            )
            threads = data.get("threads", [])
            out.extend(threads)
            if not data.get("has_more") or not threads:
                break
            before = threads[-1]["thread_metadata"]["archive_timestamp"]
        return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@dataclass
class DiscordPull:
    """Everything read from the channel in one pass, before items are built."""

    server_id: str
    channel: dict[str, Any]
    channel_messages: list[dict[str, Any]]
    threads: list[dict[str, Any]]
    thread_messages: dict[str, list[dict[str, Any]]]
    # user ID → name shown in the server (nickname), for authors who are still members
    member_names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def fetch_all(
    client: DiscordClient,
    server_id: str,
    channel_id: str,
    log: Callable[[str], None] = lambda _: None,
) -> DiscordPull:
    """Read the main channel and every thread in it (active, archived public and private)."""
    warnings: list[str] = []
    channel = client.get_channel(channel_id)

    if channel.get("type") in FORUM_TYPES:
        channel_messages: list[dict[str, Any]] = []
    else:
        channel_messages = client.all_messages(channel_id)
    log(f"#{channel.get('name')}: {len(channel_messages)} channel messages")

    threads: dict[str, dict[str, Any]] = {}
    for t in client.active_threads(server_id, channel_id):
        threads[t["id"]] = t
    for t in client.all_archived_threads(channel_id, "public"):
        threads.setdefault(t["id"], t)
    try:
        for t in client.all_archived_threads(channel_id, "private"):
            threads.setdefault(t["id"], t)
    except DiscordForbidden:
        warnings.append(
            "Archived private threads were skipped: reading them needs the Manage Threads "
            "permission."
        )

    ordered = sorted(threads.values(), key=lambda t: int(t["id"]))
    thread_messages: dict[str, list[dict[str, Any]]] = {}
    for i, t in enumerate(ordered, 1):
        try:
            thread_messages[t["id"]] = client.all_messages(t["id"])
        except DiscordForbidden as e:
            warnings.append(f"Thread '{t.get('name')}' ({t['id']}) was skipped: {e}")
            continue
        if i % 25 == 0 or i == len(ordered):
            log(f"threads: {i}/{len(ordered)} read")

    member_names = fetch_member_names(
        client,
        server_id,
        channel_messages + [m for msgs in thread_messages.values() for m in msgs],
    )
    log(f"server nicknames: {len(member_names)} found")

    return DiscordPull(
        server_id=server_id,
        member_names=member_names,
        channel=channel,
        channel_messages=channel_messages,
        threads=[t for t in ordered if t["id"] in thread_messages],
        thread_messages=thread_messages,
        warnings=warnings,
    )


def fetch_member_names(
    client: DiscordClient, server_id: str, messages: Iterable[dict[str, Any]]
) -> dict[str, str]:
    """Server nicknames for every human author, including authors of replied-to messages."""
    user_ids: set[str] = set()
    for m in messages:
        for x in (m, m.get("referenced_message") or {}):
            author = x.get("author") or {}
            if author.get("id") and not author.get("bot"):
                user_ids.add(author["id"])
    names: dict[str, str] = {}
    for uid in sorted(user_ids):
        member = client.get_member(server_id, uid)
        if member and member.get("nick"):
            names[uid] = member["nick"]
    return names


# ---------------------------------------------------------------------------
# Building items (pure: no network)
# ---------------------------------------------------------------------------


def _is_human_content(m: dict[str, Any]) -> bool:
    if m.get("author", {}).get("bot"):
        return False
    if m.get("type", DEFAULT) not in CONTENT_TYPES:
        return False
    return bool((m.get("content") or "").strip() or m.get("attachments"))


def author_name(author: dict[str, Any], member_names: dict[str, str] | None = None) -> str:
    """Server nickname, else global display name, else username."""
    nick = (member_names or {}).get(author.get("id", ""))
    return nick or author.get("global_name") or author.get("username") or "unknown"


def simplify_message(
    m: dict[str, Any],
    server_id: str,
    channel_id: str,
    member_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The fields kept for each message. `channel_id` is the thread ID for thread messages."""
    ref = m.get("message_reference") or {}
    author = m.get("author", {})
    return {
        "id": m["id"],
        "author": author_name(author, member_names),
        "author_username": author.get("username"),
        "author_id": author.get("id"),
        "timestamp": m.get("timestamp"),
        "edited_timestamp": m.get("edited_timestamp"),
        "content": m.get("content") or "",
        "link": message_link(server_id, channel_id, m["id"]),
        "reply_to": ref.get("message_id") if m.get("type") == REPLY else None,
        "attachments": [
            {"filename": a.get("filename"), "url": a.get("url")} for a in m.get("attachments", [])
        ],
    }


def build_items(pull: DiscordPull) -> list[dict[str, Any]]:
    """Turn a pull into items: one per thread, one per standalone channel message."""
    server_id = pull.server_id
    channel_id = pull.channel["id"]
    channel_name = pull.channel.get("name") or channel_id
    names = pull.member_names
    by_id = {m["id"]: m for m in pull.channel_messages}
    used_as_starter: set[str] = set()
    items: list[dict[str, Any]] = []

    for t in pull.threads:
        tid = t["id"]
        raw = pull.thread_messages.get(tid, [])
        messages = [simplify_message(m, server_id, tid, names) for m in raw if _is_human_content(m)]

        # A thread started from a channel message has the same ID as that message, and the
        # message itself lives in the parent channel. Put it at the top of the thread item.
        if not any(m["id"] == tid for m in raw):
            starter = by_id.get(tid)
            if starter is None:
                placeholder = next((m for m in raw if m.get("type") == THREAD_STARTER), None)
                starter = (placeholder or {}).get("referenced_message")
            if starter is not None:
                used_as_starter.add(tid)
                if _is_human_content(starter):
                    messages.insert(0, simplify_message(starter, server_id, channel_id, names))

        if not messages:
            continue
        items.append(
            {
                "id": f"discord-thread:{tid}",
                "source": "discord",
                "kind": "thread",
                "title": f"#{channel_name} › thread: {t.get('name') or tid}",
                "link": channel_link(server_id, tid),
                "channel_id": channel_id,
                "thread_id": tid,
                "created_at": messages[0]["timestamp"],
                "archived": bool(t.get("thread_metadata", {}).get("archived")),
                "messages": messages,
                "context": [],
            }
        )

    for m in pull.channel_messages:
        if m["id"] in used_as_starter or not _is_human_content(m):
            continue
        msg = simplify_message(m, server_id, channel_id, names)
        context = []
        replied = m.get("referenced_message") if m.get("type") == REPLY else None
        if replied:
            context.append(simplify_message(replied, server_id, channel_id, names))
        items.append(
            {
                "id": f"discord-msg:{m['id']}",
                "source": "discord",
                "kind": "message",
                "title": f"#{channel_name} › message from {msg['author']}",
                "link": msg["link"],
                "channel_id": channel_id,
                "thread_id": None,
                "created_at": msg["timestamp"],
                "messages": [msg],
                "context": context,
            }
        )

    items.sort(key=lambda i: int(i["messages"][0]["id"]))
    return items


def count_messages(items: Iterable[dict[str, Any]]) -> int:
    return sum(len(i["messages"]) for i in items)
