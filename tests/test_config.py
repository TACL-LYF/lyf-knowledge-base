from pathlib import Path

import pytest

from kb.config import ConfigError, load_config
from kb.sources.discord import message_link

CONFIG = """
discord:
  server_id: "111"
  channel_id: "222"
drive:
  source_folder_id: "src"
  kb_folder_id: "kb"
llm:
  model: test-model
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("DISCORD_BOT_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def write(tmp_path: Path, config: str, env: str = "") -> Path:
    (tmp_path / "config.yaml").write_text(config)
    (tmp_path / ".env").write_text(env)
    return tmp_path / "config.yaml"


def test_loads_ids_and_secrets(tmp_path):
    path = write(
        tmp_path,
        CONFIG,
        "DISCORD_BOT_TOKEN=tok\nGOOGLE_APPLICATION_CREDENTIALS=secrets/key.json\n",
    )
    cfg = load_config(path)
    assert cfg.discord_server_id == "111"
    assert cfg.discord_channel_id == "222"
    assert cfg.drive_kb_folder_id == "kb"
    assert cfg.llm_model == "test-model"
    assert cfg.discord_bot_token == "tok"
    assert cfg.google_credentials_path == tmp_path / "secrets/key.json"
    assert cfg.data_dir == tmp_path / "data"
    assert len(cfg.drive_file_types) == 3


def test_require_reports_missing(tmp_path):
    cfg = load_config(write(tmp_path, CONFIG))
    with pytest.raises(ConfigError, match="discord_bot_token"):
        cfg.require("discord_channel_id", "discord_bot_token")


def test_require_checks_key_file_exists(tmp_path):
    cfg = load_config(write(tmp_path, CONFIG, "GOOGLE_APPLICATION_CREDENTIALS=missing.json\n"))
    with pytest.raises(ConfigError, match="not found"):
        cfg.require("google_credentials_path")


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_message_link():
    assert message_link("1", "2", "3") == "https://discord.com/channels/1/2/3"
