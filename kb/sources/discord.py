"""Discord source: read the channel and its threads via the REST API, and build items.

A full read fetches everything. An incremental read takes message-ID cursors (the last
message read in the main channel and in each thread) and fetches only what's newer.

An **item** is the unit later sent to the LLM:
- one per thread: every message in the thread, including the message that started it;
- one per main-channel message that isn't part of a thread. If it's a reply, the
  replied-to message is attached as read-only context.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
    """Everything read from the channel in one pass, before items are built.

    In an incremental read, `channel_messages` holds only messages after the channel
    cursor, and `threads` only threads with messages after their cursor. Those threads
    are read in full so earlier messages can be given as context.
    """

    server_id: str
    channel: dict[str, Any]
    channel_messages: list[dict[str, Any]]
    threads: list[dict[str, Any]]
    thread_messages: dict[str, list[dict[str, Any]]]
    # user ID → name shown in the server (nickname), for authors who are still members
    member_names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Threads that exist but couldn't be read (their messages must not count as deleted)
    unreadable_threads: set[str] = field(default_factory=set)
    # Cursors to save once the run succeeds: last message ID read, channel and per thread
    channel_cursor: str | None = None
    thread_cursors: dict[str, str] = field(default_factory=dict)
    full: bool = True


def _max_id(messages: Iterable[dict[str, Any]], current: str | None) -> str | None:
    ids = [int(m["id"]) for m in messages]
    if current:
        ids.append(int(current))
    return str(max(ids)) if ids else None


def _newer(message_id: str | None, cursor: str | None) -> bool:
    if not message_id:
        return False
    return cursor is None or int(message_id) > int(cursor)


def fetch_all(
    client: DiscordClient,
    server_id: str,
    channel_id: str,
    log: Callable[[str], None] = lambda _: None,
    *,
    member_cache: MemberCache | None = None,
    refresh_names: bool = False,
) -> DiscordPull:
    """Read the main channel and every thread in it (active, archived public and private)."""
    return fetch(
        client,
        server_id,
        channel_id,
        None,
        log,
        member_cache=member_cache,
        refresh_names=refresh_names,
    )


def fetch(
    client: DiscordClient,
    server_id: str,
    channel_id: str,
    cursors: dict[str, Any] | None,
    log: Callable[[str], None] = lambda _: None,
    *,
    member_cache: MemberCache | None = None,
    refresh_names: bool = False,
) -> DiscordPull:
    """Read the channel and its threads.

    With `cursors=None` everything is read. Otherwise `cursors` is
    `{"discord_channel": id, "discord_threads": {thread_id: id}}`: only channel messages
    after the channel cursor are read, and only threads whose latest message is newer
    than their cursor (threads with no cursor are new and always read).
    """
    full = cursors is None
    cursors = cursors or {}
    chan_cursor: str | None = cursors.get("discord_channel")
    thread_cursors: dict[str, str] = dict(cursors.get("discord_threads") or {})
    warnings: list[str] = []
    channel = client.get_channel(channel_id)

    if channel.get("type") in FORUM_TYPES:
        channel_messages: list[dict[str, Any]] = []
    else:
        channel_messages = client.all_messages(channel_id, after=chan_cursor or "0")
    new_word = "" if full else "new "
    log(f"#{channel.get('name')}: {len(channel_messages)} {new_word}channel messages")

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
    if not full:
        ordered = [
            t
            for t in ordered
            if t["id"] not in thread_cursors
            or _newer(t.get("last_message_id"), thread_cursors[t["id"]])
        ]
        log(f"threads: {len(threads)} found, {len(ordered)} with new messages to read")
    else:
        log(f"threads: {len(threads)} found (active and archived), reading all")
    thread_messages: dict[str, list[dict[str, Any]]] = {}
    unreadable: set[str] = set()
    for i, t in enumerate(ordered, 1):
        try:
            thread_messages[t["id"]] = client.all_messages(t["id"])
        except DiscordForbidden as e:
            warnings.append(f"Thread '{t.get('name')}' ({t['id']}) was skipped: {e}")
            unreadable.add(t["id"])
            continue
        if i % 10 == 0 or i == len(ordered):
            log(f"threads: {i}/{len(ordered)} read")

    for tid, msgs in thread_messages.items():
        latest = _max_id(msgs, thread_cursors.get(tid))
        if latest:
            thread_cursors[tid] = latest

    all_fetched = channel_messages + [m for msgs in thread_messages.values() for m in msgs]
    member_names = fetch_member_names(
        client, server_id, all_fetched, cache=member_cache, refresh=refresh_names, log=log
    )

    return DiscordPull(
        server_id=server_id,
        member_names=member_names,
        channel=channel,
        channel_messages=channel_messages,
        threads=[t for t in ordered if t["id"] in thread_messages],
        thread_messages=thread_messages,
        warnings=warnings,
        unreadable_threads=unreadable,
        channel_cursor=_max_id(channel_messages, chan_cursor),
        thread_cursors=thread_cursors,
        full=full,
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemberCache:
    """Local record of server members already looked up: user ID → names.

    Looking up a member's server nickname is one API call per author, so names are
    remembered between runs in a local JSON file and only new authors are looked up.
    Members who have no nickname or have left the server are remembered too
    (`nick: null`), so they aren't looked up again either. Pass `refresh=True` to
    `fetch_member_names` to look everyone up again (e.g. after nicknames change).

        {"<user id>": {"username": "...", "nick": "..." | null,
                       "display_name": "...", "checked_at": "..."}}
    """

    def __init__(self, path: Path | None = None, entries: dict[str, dict] | None = None):
        self.path = path
        self.entries: dict[str, dict[str, Any]] = entries if entries is not None else {}
        if entries is None and path is not None and path.is_file():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.entries = {}

    def __contains__(self, user_id: str) -> bool:
        return user_id in self.entries

    def nick(self, user_id: str) -> str | None:
        return (self.entries.get(user_id) or {}).get("nick")

    def record(self, author: dict[str, Any], nick: str | None) -> None:
        self.entries[author["id"]] = {
            "username": author.get("username"),
            "nick": nick,
            "display_name": nick or author.get("global_name") or author.get("username"),
            "checked_at": _now(),
        }

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.entries, indent=2, ensure_ascii=False, sort_keys=True)
        self.path.write_text(data + "\n", encoding="utf-8")


def fetch_member_names(
    client: DiscordClient,
    server_id: str,
    messages: Iterable[dict[str, Any]],
    *,
    cache: MemberCache | None = None,
    refresh: bool = False,
    log: Callable[[str], None] = lambda _: None,
) -> dict[str, str]:
    """Server nicknames for every human author, including authors of replied-to messages.

    With a cache, authors already in it aren't looked up again (unless `refresh`), and
    every author looked up is added to it. The cache is saved before returning.
    """
    authors: dict[str, dict[str, Any]] = {}
    for m in messages:
        for x in (m, m.get("referenced_message") or {}):
            author = x.get("author") or {}
            if author.get("id") and not author.get("bot"):
                authors[author["id"]] = author
    names: dict[str, str] = {}
    to_look_up = [u for u in authors if cache is None or refresh or u not in cache]
    if to_look_up:
        cached = len(authors) - len(to_look_up)
        log(
            f"server nicknames: looking up {len(to_look_up)} of {len(authors)} authors"
            + (f" ({cached} already cached)" if cached else "")
        )
    looked_up = 0
    for uid in sorted(authors):
        if cache is not None and uid in cache and not refresh:
            nick = cache.nick(uid)
        else:
            looked_up += 1
            if looked_up % 25 == 0:
                log(f"server nicknames: {looked_up}/{len(to_look_up)} looked up")
            member = client.get_member(server_id, uid)
            nick = (member or {}).get("nick") or None
            if cache is not None:
                cache.record(authors[uid], nick)
        if nick:
            names[uid] = nick
    if cache is not None:
        cache.save()
    how = f"{len(to_look_up)} looked up" if to_look_up else "all from the local cache"
    log(f"server nicknames: {len(names)} of {len(authors)} authors have one ({how})")
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


def message_hash(m: dict[str, Any]) -> str:
    """Hash of a simplified message's text and attachment names, to spot edits.

    Attachment URLs are left out because Discord signs them and they change over time.
    """
    names = [a.get("filename") or "" for a in m.get("attachments", [])]
    payload = json.dumps([m.get("content") or "", names], ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_new(
    items: Iterable[dict[str, Any]], is_new: Callable[[dict[str, Any]], bool]
) -> list[dict[str, Any]]:
    """Keep only new messages in each item; earlier ones become read-only context.

    Items with no new messages are dropped. Context keeps chronological order, with
    any existing context (e.g. a replied-to message) first.
    """
    out = []
    for item in items:
        new = [m for m in item["messages"] if is_new(m)]
        if not new:
            continue
        old = [m for m in item["messages"] if not is_new(m)]
        out.append({**item, "messages": new, "context": item["context"] + old})
    return out


def human_message_ids(pull: DiscordPull) -> set[str]:
    """IDs of every human message in a pull, including thread starters from the channel."""
    ids: set[str] = set()
    for item in build_items(pull):
        ids.update(m["id"] for m in item["messages"])
    return ids
