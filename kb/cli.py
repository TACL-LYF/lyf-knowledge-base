"""Command-line entry point: `kb update`, `kb status`, `kb rebuild`."""

from __future__ import annotations

import click

from kb import __version__
from kb import pull as raw_pull
from kb.config import ConfigError, load_config
from kb.pull import SOURCES
from kb.sources.discord import DiscordError


def _not_yet(milestone: str) -> None:
    raise click.ClickException(f"Not implemented yet (arrives in {milestone}). See docs/PLAN.md.")


@click.group()
@click.version_option(__version__, prog_name="kb")
def main() -> None:
    """Consolidate feedback from Discord and Google Drive into one cited Google Doc."""


@main.command()
@click.option(
    "--dry-run", is_flag=True, help="Show what's new or changed; no LLM calls, no writes."
)
@click.option("--full", is_flag=True, help="Re-read the whole Discord channel (rarely needed).")
@click.option("--force-unlock", is_flag=True, help="Clear a stale run lock before starting.")
def update(dry_run: bool, full: bool, force_unlock: bool) -> None:
    """Read new content, extract feedback, and rebuild the doc."""
    _not_yet("M2–M4")


@main.command()
@click.option(
    "--source",
    "sources",
    type=click.Choice(SOURCES),
    multiple=True,
    help="Only pull this source (repeatable). Default: all.",
)
def pull(sources: tuple[str, ...]) -> None:
    """Read everything from Discord and Drive into data/raw/ (local only, full read)."""
    sources = sources or SOURCES
    try:
        cfg = load_config()
        if "discord" in sources:
            cfg.require("discord_server_id", "discord_channel_id", "discord_bot_token")
        if "drive" in sources:
            cfg.require("drive_source_folder_ids", "google_credentials_path")
        raw_pull.pull(cfg, sources, log=click.echo)
    except (ConfigError, DiscordError) as e:
        raise click.ClickException(str(e)) from e


@main.command()
def status() -> None:
    """Show last run, cursors, item counts, errors, and programming types."""
    _not_yet("M2")


@main.command()
def rebuild() -> None:
    """Extract everything again (e.g., after a big prompt change)."""
    _not_yet("M3")


if __name__ == "__main__":
    main()
