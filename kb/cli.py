"""Command-line entry point: `kb update`, `kb status`, `kb rebuild`, `kb requeue`, `kb pull`."""

from __future__ import annotations

import anthropic
import click
from googleapiclient.errors import HttpError

from kb import __version__
from kb import pull as raw_pull
from kb import update as upd
from kb.config import ConfigError, load_config
from kb.pull import SOURCES
from kb.sources.discord import DiscordError
from kb.state import LockError

DISCORD_SETTINGS = ("discord_server_id", "discord_channel_id", "discord_bot_token")
DRIVE_SETTINGS = ("drive_source_folder_ids",)


def _run(
    dry_run: bool,
    full: bool,
    rebuild: bool,
    force_unlock: bool,
    steps: tuple[str, ...] = upd.STEPS,
    limit: int | None = None,
    read: tuple[str, ...] = upd.SOURCES,
) -> None:
    try:
        cfg = load_config()
        cfg.require("drive_kb_folder_id", "google_credentials_path")
        if "pull" in steps:
            cfg.require(
                *(DISCORD_SETTINGS if "discord" in read else ()),
                *(DRIVE_SETTINGS if "drive" in read else ()),
            )
        if not dry_run and "extract" in steps:
            cfg.require("anthropic_api_key")
        result = upd.run_update(
            cfg,
            dry_run=dry_run,
            full=full,
            rebuild=rebuild,
            steps=steps,
            limit=limit,
            read=read,
            force_unlock=force_unlock,
            log=click.echo,
        )
    except (ConfigError, DiscordError, LockError) as e:
        raise click.ClickException(str(e)) from e
    except HttpError as e:
        raise click.ClickException(f"Google API error: {e}") from e
    except anthropic.AuthenticationError as e:
        raise click.ClickException(
            "The Anthropic API rejected the key (401). Check ANTHROPIC_API_KEY in .env."
        ) from e
    except anthropic.PermissionDeniedError as e:
        raise click.ClickException(f"The Anthropic API denied the request (403): {e}") from e
    except anthropic.NotFoundError as e:
        raise click.ClickException(
            f"Anthropic API: model not found ({e}). Check llm.extract_model in config.yaml."
        ) from e
    upd.report(result, upd.st.State(cfg.data_dir / "state").load().manifest, click.echo)


@click.group()
@click.version_option(__version__, prog_name="kb")
def main() -> None:
    """Consolidate feedback from Discord and Google Drive into one cited Google Doc."""


@main.command()
@click.option(
    "--dry-run", is_flag=True, help="Show what's new or changed; no LLM calls, no writes."
)
@click.option("--full", is_flag=True, help="Re-read the whole Discord channel (rarely needed).")
@click.option(
    "--pull",
    "only_pull",
    is_flag=True,
    help="Only read Discord and Drive; new items wait (pending) for extraction.",
)
@click.option(
    "--extract",
    "only_extract",
    is_flag=True,
    help="Only extract items waiting from earlier runs; don't read Discord or Drive.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=0),
    help="Extract at most this many items (Discord and Drive alternating); the rest wait. "
    "0 skips extraction.",
)
@click.option(
    "--source",
    "read",
    type=click.Choice(upd.SOURCES),
    multiple=True,
    help="Only read and extract this source (repeatable). Default: all.",
)
@click.option("--force-unlock", is_flag=True, help="Clear a stale run lock before starting.")
def update(
    dry_run: bool,
    full: bool,
    only_pull: bool,
    only_extract: bool,
    limit: int | None,
    read: tuple[str, ...],
    force_unlock: bool,
) -> None:
    """Read new content and extract feedback and program facts from it.

    By default every step runs. --pull and --extract run just that step (both together
    is the same as neither). Items that are read but not extracted wait as `pending` and
    are extracted by the next run that extracts. --source limits the run to Discord or
    Drive. For example, after `kb requeue`, `--pull --source discord` saves Discord's
    messages for extraction without exporting every Drive file again.

    Use --dry-run first to see the token and cost estimate, and --limit 5 for a small
    first real run. The docs themselves arrive in M4.
    """
    steps = (
        tuple(s for s, on in zip(upd.STEPS, (only_pull, only_extract), strict=True) if on)
        or upd.STEPS
    )
    if limit is not None and steps == ("pull",):
        raise click.UsageError("--limit applies to extraction; it can't be used with --pull.")
    if full and steps == ("extract",):
        raise click.UsageError(
            "--full applies to reading Discord; it can't be used with --extract."
        )
    _run(
        dry_run,
        full,
        rebuild=False,
        force_unlock=force_unlock,
        steps=steps,
        limit=limit,
        read=read or upd.SOURCES,
    )


@main.command()
@click.option(
    "--source",
    "sources",
    type=click.Choice(SOURCES),
    multiple=True,
    help="Only pull this source (repeatable). Default: all.",
)
@click.option(
    "--refresh-names",
    is_flag=True,
    help="Look up every author's server nickname again instead of using the local cache.",
)
def pull(sources: tuple[str, ...], refresh_names: bool) -> None:
    """Read everything from Discord and Drive into data/raw/ (local only, full read)."""
    sources = sources or SOURCES
    try:
        cfg = load_config()
        if "discord" in sources:
            cfg.require("discord_server_id", "discord_channel_id", "discord_bot_token")
        if "drive" in sources:
            cfg.require("drive_source_folder_ids", "google_credentials_path")
        raw_pull.pull(cfg, sources, log=click.echo, refresh_names=refresh_names)
    except (ConfigError, DiscordError) as e:
        raise click.ClickException(str(e)) from e


@main.command()
def status() -> None:
    """Show last run, cursors, item counts, errors, and programming types."""
    try:
        cfg = load_config()
        cfg.require("drive_kb_folder_id", "google_credentials_path")
        for line in upd.status(cfg):
            click.echo(line)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    except HttpError as e:
        raise click.ClickException(f"Google API error: {e}") from e


@main.command()
@click.option("--dry-run", is_flag=True, help="Show the token and cost estimate only.")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@click.option("--force-unlock", is_flag=True, help="Clear a stale run lock before starting.")
def rebuild(dry_run: bool, yes: bool, force_unlock: bool) -> None:
    """Extract everything again (e.g., after a big prompt change).

    Reads the whole Discord channel, exports every Drive file, and sends every item to
    the model again. Citation IDs stay the same; each item's records are replaced once
    its new ones are in.
    """
    if not dry_run and not yes:
        click.confirm(
            "This sends every item to the model again, which costs about as much as the "
            "first run (see `kb rebuild --dry-run` for an estimate). Continue?",
            abort=True,
        )
    _run(dry_run, full=False, rebuild=True, force_unlock=force_unlock)


@main.command()
@click.option("--force-unlock", is_flag=True, help="Clear a stale run lock before starting.")
def requeue(force_unlock: bool) -> None:
    """Mark every item to be extracted again, without extracting anything yet.

    Use after a prompt change, then run `kb update` as usual: `--dry-run` for the cost,
    `--limit N` for a sample, and `--extract` to work through the rest without
    exporting Drive files again. Each item keeps its records until new ones replace
    them, and citation IDs stay the same.
    """
    try:
        cfg = load_config()
        cfg.require("drive_kb_folder_id", "google_credentials_path")
        n = upd.requeue(cfg, force_unlock=force_unlock)
    except (ConfigError, LockError) as e:
        raise click.ClickException(str(e)) from e
    except HttpError as e:
        raise click.ClickException(f"Google API error: {e}") from e
    click.echo(
        f"{n} items marked to extract again. Next: `kb update --dry-run` for the cost, "
        "then `kb update --limit 5` for a sample."
    )


if __name__ == "__main__":
    main()
