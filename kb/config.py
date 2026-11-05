"""Load settings from config.yaml (non-secret) and .env (secrets)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

DEFAULT_FILE_TYPES = [
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
]

# See "Choosing models" in docs/PLAN.md. Extraction mistakes are saved permanently, so it
# gets the stronger model; synthesis is rebuilt every run and can be switched any time.
DEFAULT_EXTRACT_MODEL = "claude-opus-5-5"
DEFAULT_SYNTHESIS_MODEL = "claude-opus-5-5"


class ConfigError(Exception):
    """Raised when config.yaml or .env is missing something required."""


@dataclass(frozen=True)
class Config:
    discord_server_id: str
    discord_channel_id: str
    drive_source_folder_ids: list[str]
    drive_kb_folder_id: str
    drive_file_types: list[str] = field(default_factory=lambda: list(DEFAULT_FILE_TYPES))
    llm_extract_model: str = DEFAULT_EXTRACT_MODEL
    llm_synthesis_model: str = DEFAULT_SYNTHESIS_MODEL
    lock_timeout_minutes: int = 60
    data_dir: Path = REPO_ROOT / "data"

    # Secrets (from .env / environment)
    discord_bot_token: str = ""
    google_credentials_path: Path | None = None
    anthropic_api_key: str = ""

    def require(self, *names: str) -> None:
        """Raise ConfigError listing any of the named settings that are empty."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(
                "Missing settings: "
                + ", ".join(missing)
                + ". IDs go in config.yaml (see config.example.yaml); "
                "secrets go in .env (see .env.example)."
            )
        path = self.google_credentials_path
        if "google_credentials_path" in names and path is not None and not path.is_file():
            raise ConfigError(
                f"Service account key not found at {path}. "
                "Save it as secrets/kb-pipeline.json or set GOOGLE_APPLICATION_CREDENTIALS."
            )


def _resolve(path_str: str, base: Path) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else base / p


def _id_list(value: object) -> list[str]:
    """A YAML list (or a single string) of IDs, blanks dropped and duplicates removed."""
    items = value if isinstance(value, list) else [value]
    ids = [str(v).strip() for v in items if v is not None]
    return list(dict.fromkeys(i for i in ids if i))


def load_config(path: Path | None = None, env_file: Path | None = None) -> Config:
    """Read config.yaml and .env. Validation of required fields is left to callers."""
    path = path or DEFAULT_CONFIG_PATH
    base = path.parent
    load_dotenv(env_file or base / ".env", override=False)

    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml "
            "and fill in the IDs."
        )
    raw = yaml.safe_load(path.read_text()) or {}

    discord = raw.get("discord") or {}
    drive = raw.get("drive") or {}
    llm = raw.get("llm") or {}
    state = raw.get("state") or {}
    paths = raw.get("paths") or {}

    if "source_folder_id" in drive:
        raise ConfigError(
            "drive.source_folder_id was replaced by drive.source_folder_ids, a list of folder "
            "IDs. Rename it in config.yaml (see config.example.yaml)."
        )

    if "model" in llm:
        raise ConfigError(
            "llm.model was replaced by llm.extract_model and llm.synthesis_model, so the two "
            "steps can use different models. Update config.yaml (see config.example.yaml)."
        )

    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    return Config(
        discord_server_id=str(discord.get("server_id") or ""),
        discord_channel_id=str(discord.get("channel_id") or ""),
        drive_source_folder_ids=_id_list(drive.get("source_folder_ids")),
        drive_kb_folder_id=str(drive.get("kb_folder_id") or ""),
        drive_file_types=list(drive.get("file_types") or DEFAULT_FILE_TYPES),
        llm_extract_model=str(llm.get("extract_model") or DEFAULT_EXTRACT_MODEL),
        llm_synthesis_model=str(llm.get("synthesis_model") or DEFAULT_SYNTHESIS_MODEL),
        lock_timeout_minutes=int(state.get("lock_timeout_minutes") or 60),
        data_dir=_resolve(str(paths.get("data_dir") or "data"), base),
        discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
        google_credentials_path=_resolve(creds, base) if creds else None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    )
