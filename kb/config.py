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
DEFAULT_MAX_ITEM_TOKENS = 100_000
DEFAULT_BATCH_MIN_REQUESTS = 20
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Programming types where each occurrence has its own content (a workshop on one topic,
# then another on a different one), so records name the session. See PLAN.md.
DEFAULT_PER_SESSION_TYPES = ["Workshop"]


class ConfigError(Exception):
    """Raised when config.yaml or .env is missing something required."""


@dataclass(frozen=True)
class Config:
    discord_server_id: str
    discord_channel_id: str
    drive_source_folder_ids: list[str]
    drive_kb_folder_id: str
    drive_file_types: list[str] = field(default_factory=lambda: list(DEFAULT_FILE_TYPES))
    drive_allow_large_files: list[str] = field(default_factory=list)
    llm_extract_model: str = DEFAULT_EXTRACT_MODEL
    llm_synthesis_model: str = DEFAULT_SYNTHESIS_MODEL
    llm_extract_effort: str | None = None  # None: the model's default
    llm_max_item_tokens: int = DEFAULT_MAX_ITEM_TOKENS
    llm_batch_min_requests: int = DEFAULT_BATCH_MIN_REQUESTS
    per_session_types: list[str] = field(default_factory=lambda: list(DEFAULT_PER_SESSION_TYPES))
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
    programming = raw.get("programming") or {}
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

    effort = str(llm.get("extract_effort") or "").strip() or None
    if effort is not None and effort not in EFFORTS:
        raise ConfigError(
            f"llm.extract_effort must be one of {', '.join(EFFORTS)} (or left out), not {effort!r}."
        )

    per_session = programming.get("per_session_types")
    per_session_types = (
        list(DEFAULT_PER_SESSION_TYPES)
        if per_session is None
        else [" ".join(str(t).split()) for t in _id_list(per_session)]
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
        llm_extract_effort=effort,
        llm_max_item_tokens=int(llm.get("max_item_tokens") or DEFAULT_MAX_ITEM_TOKENS),
        llm_batch_min_requests=int(llm.get("batch_min_requests") or DEFAULT_BATCH_MIN_REQUESTS),
        drive_allow_large_files=_id_list(drive.get("allow_large_files")),
        per_session_types=per_session_types,
        lock_timeout_minutes=int(state.get("lock_timeout_minutes") or 60),
        data_dir=_resolve(str(paths.get("data_dir") or "data"), base),
        discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", "").strip(),
        google_credentials_path=_resolve(creds, base) if creds else None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    )
