from pathlib import Path

import pytest

from kb.config import ConfigError, load_config
from kb.sources.discord import message_link

CONFIG = """
discord:
  server_id: "111"
  channel_id: "222"
drive:
  source_folder_ids:
    - "src1"
    - "src2"
  kb_folder_id: "kb"
llm:
  extract_model: extract-model
  synthesis_model: synthesis-model
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
    assert cfg.drive_source_folder_ids == ["src1", "src2"]
    assert cfg.drive_kb_folder_id == "kb"
    assert cfg.llm_extract_model == "extract-model"
    assert cfg.llm_synthesis_model == "synthesis-model"
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


def test_source_folder_ids_accepts_string_and_drops_blanks_and_duplicates(tmp_path):
    base = CONFIG.split("drive:")[0]
    one = load_config(write(tmp_path, base + 'drive:\n  source_folder_ids: "only"\n'))
    assert one.drive_source_folder_ids == ["only"]
    messy = base + 'drive:\n  source_folder_ids: ["a", "", "a", "b"]\n'
    assert load_config(write(tmp_path, messy)).drive_source_folder_ids == ["a", "b"]


def test_empty_source_folder_ids_is_missing(tmp_path):
    base = CONFIG.split("drive:")[0]
    cfg = load_config(write(tmp_path, base + 'drive:\n  source_folder_ids:\n    - ""\n'))
    with pytest.raises(ConfigError, match="drive_source_folder_ids"):
        cfg.require("drive_source_folder_ids")


def test_old_source_folder_id_key_is_rejected(tmp_path):
    new_key = 'source_folder_ids:\n    - "src1"\n    - "src2"'
    old = CONFIG.replace(new_key, 'source_folder_id: "src"')
    with pytest.raises(ConfigError, match="source_folder_ids"):
        load_config(write(tmp_path, old))


def test_message_link():
    assert message_link("1", "2", "3") == "https://discord.com/channels/1/2/3"


def test_models_default_when_unset(tmp_path):
    cfg = load_config(write(tmp_path, CONFIG.split("llm:")[0]))
    assert cfg.llm_extract_model == "claude-opus-5-5"
    assert cfg.llm_synthesis_model == "claude-opus-5-5"


def test_old_single_model_setting_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="extract_model"):
        load_config(write(tmp_path, CONFIG.split("llm:")[0] + "llm:\n  model: x\n"))


def test_extraction_settings_default_when_unset(tmp_path):
    cfg = load_config(write(tmp_path, CONFIG))
    assert cfg.llm_max_item_tokens == 100_000
    assert cfg.llm_batch_min_requests == 20
    assert cfg.llm_extract_effort is None
    assert cfg.drive_allow_large_files == []


def test_extraction_settings(tmp_path):
    extra = "  max_item_tokens: 5000\n  batch_min_requests: 3\n  extract_effort: high\n"
    config = CONFIG + extra
    config = config.replace(
        '  kb_folder_id: "kb"\n', '  kb_folder_id: "kb"\n  allow_large_files: ["big"]\n'
    )
    cfg = load_config(write(tmp_path, config))
    assert (cfg.llm_max_item_tokens, cfg.llm_batch_min_requests) == (5000, 3)
    assert cfg.llm_extract_effort == "high" and cfg.drive_allow_large_files == ["big"]


def test_bad_effort_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="extract_effort"):
        load_config(write(tmp_path, CONFIG + "  extract_effort: extreme\n"))


def test_per_session_types_default_to_workshop_and_can_be_changed(tmp_path):
    assert load_config(write(tmp_path, CONFIG)).per_session_types == ["Workshop"]
    custom = CONFIG + "programming:\n  per_session_types: [Workshop, '  Speaker   Series ']\n"
    assert load_config(write(tmp_path, custom)).per_session_types == ["Workshop", "Speaker Series"]
    none = CONFIG + "programming:\n  per_session_types: []\n"
    assert load_config(write(tmp_path, none)).per_session_types == []


def test_export_workers_default_and_override(tmp_path):
    assert load_config(write(tmp_path, CONFIG)).drive_export_workers == 8
    config = CONFIG.replace('  kb_folder_id: "kb"\n', '  kb_folder_id: "kb"\n  export_workers: 3\n')
    assert load_config(write(tmp_path, config)).drive_export_workers == 3


@pytest.mark.parametrize("value", ["0", "-2", "lots"])
def test_bad_export_workers_is_rejected(tmp_path, value):
    config = CONFIG.replace(
        '  kb_folder_id: "kb"\n', f'  kb_folder_id: "kb"\n  export_workers: {value}\n'
    )
    with pytest.raises(ConfigError, match="export_workers"):
        load_config(write(tmp_path, config))
