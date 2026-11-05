"""Command-line entry point: `kb update`, `kb status`, `kb rebuild`."""

from __future__ import annotations

import click

from kb import __version__


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
def status() -> None:
    """Show last run, cursors, item counts, errors, and programming types."""
    _not_yet("M2")


@main.command()
def rebuild() -> None:
    """Extract everything again (e.g., after a big prompt change)."""
    _not_yet("M3")


if __name__ == "__main__":
    main()
