"""Discord source: read the channel and its threads via the REST API.

M0 has just enough to prove access (one channel message, one thread message).
M1 adds full reads with pagination; M2 adds cursors.
"""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/lyf-knowledge-base, 0.1.0)"


class DiscordError(Exception):
    pass


def message_link(server_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{server_id}/{channel_id}/{message_id}"


class DiscordClient:
    """Thin read-only wrapper over the Discord REST API using a bot token."""

    def __init__(self, token: str, timeout: float = 30.0) -> None:
        self._http = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DiscordClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> Any:
        resp = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        if resp.status_code == 401:
            raise DiscordError("Discord rejected the bot token (401). Check DISCORD_BOT_TOKEN.")
        if resp.status_code == 403:
            raise DiscordError(
                f"Discord denied access to {path} (403). The bot needs View Channel and "
                "Read Message History on this channel."
            )
        if resp.status_code == 404:
            raise DiscordError(f"Discord resource not found: {path} (404). Check the IDs.")
        resp.raise_for_status()
        return resp.json()

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._get(f"/channels/{channel_id}")

    def get_messages(
        self, channel_id: str, *, limit: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        """Messages in a channel or thread, newest first (Discord's order)."""
        return self._get(f"/channels/{channel_id}/messages", limit=limit, after=after)

    def active_threads(self, server_id: str, channel_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/guilds/{server_id}/threads/active")
        return [t for t in data.get("threads", []) if t.get("parent_id") == channel_id]

    def archived_public_threads(self, channel_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        data = self._get(f"/channels/{channel_id}/threads/archived/public", limit=limit)
        return data.get("threads", [])
