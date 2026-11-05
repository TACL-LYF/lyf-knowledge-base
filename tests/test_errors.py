"""Known problems become messages that say what to do (kb/errors.py)."""

import json
from pathlib import Path

import anthropic
import httplib2
import httpx
import pytest
from click.testing import CliRunner
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from kb import cli, errors
from kb.config import Config, ConfigError
from kb.sources.discord import DiscordError
from kb.state import LockError

SA = "kb-pipeline@proj.iam.gserviceaccount.com"


@pytest.fixture
def cfg(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"client_email": SA}))
    return Config(
        discord_server_id="1",
        discord_channel_id="2",
        drive_source_folder_ids=["srcFolder123456"],
        drive_kb_folder_id="kbFolder1234567",
        google_credentials_path=key,
    )


def http_error(status, reason="", uri="https://www.googleapis.com/drive/v3/files/x"):
    resp = httplib2.Response({"status": status})
    body = {"error": {"code": status, "message": "m", "errors": [{"reason": reason}]}}
    return HttpError(resp, json.dumps(body).encode(), uri=uri)


def anthropic_error(cls, status, message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(status, request=req, json={"error": {"message": message}})
    return cls(message, response=resp, body=None)


def test_folder_not_found_names_the_setting_and_the_service_account(cfg):
    e = http_error(404, "notFound", "https://www.googleapis.com/drive/v3/files/kbFolder1234567")
    msg = errors.describe(e, cfg)
    assert "Knowledge Base folder (kbFolder1234567, drive.kb_folder_id)" in msg
    assert SA in msg


def test_source_folder_forbidden(cfg):
    e = http_error(403, "forbidden", "https://www.googleapis.com/drive/v3/files/srcFolder123456")
    msg = errors.describe(e, cfg)
    assert "source folder srcFolder123456" in msg and SA in msg


def test_api_not_enabled_names_the_api(cfg):
    e = http_error(
        403, "accessNotConfigured", "https://sheets.googleapis.com/v4/spreadsheets/abcdefghijkl"
    )
    assert "Google Sheets API isn't enabled" in errors.describe(e, cfg)


def test_storage_quota_means_not_a_shared_drive(cfg):
    assert "Shared Drive" in errors.describe(http_error(403, "storageQuotaExceeded"), cfg)


def test_view_only_access_needs_content_manager(cfg):
    msg = errors.describe(http_error(403, "insufficientFilePermissions"), cfg)
    assert "Content manager" in msg


def test_revoked_google_key():
    msg = errors.describe(RefreshError("invalid_grant: Invalid JWT Signature."))
    assert "deleted or disabled" in msg and "clock" in msg


def test_anthropic_out_of_credit():
    e = anthropic_error(
        anthropic.BadRequestError, 400, "Your credit balance is too low to access the API."
    )
    assert errors.is_billing_error(e)
    assert "out of credit" in errors.describe(e)


def test_other_bad_request_is_not_described():
    e = anthropic_error(anthropic.BadRequestError, 400, "max_tokens: too large")
    assert errors.describe(e) is None


def test_anthropic_bad_key():
    e = anthropic_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    assert "ANTHROPIC_API_KEY" in errors.describe(e)


def test_no_internet():
    assert "internet" in errors.describe(httplib2.ServerNotFoundError("no host"))
    assert "internet" in errors.describe(httpx.ConnectError("no route"))


def test_own_errors_pass_through():
    for e in (ConfigError("c"), DiscordError("d"), LockError("l")):
        assert errors.describe(e) == str(e)


def test_unknown_errors_are_not_hidden():
    assert errors.describe(KeyError("x")) is None


def test_cli_shows_the_message_not_a_traceback(monkeypatch, tmp_path):
    def boom(cfg):
        raise RefreshError("invalid_grant")

    key = tmp_path / "k.json"
    key.write_text("{}")
    config = Config("1", "2", ["s"], "kb", google_credentials_path=key, data_dir=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli.upd, "status", boom)
    result = CliRunner().invoke(cli.main, ["status"])
    assert result.exit_code == 1
    assert "Error: Google rejected the service account key" in result.output
    assert "Traceback" not in result.output


def test_cli_lets_unknown_errors_through(monkeypatch, tmp_path):
    key = tmp_path / "k.json"
    key.write_text("{}")
    config = Config("1", "2", ["s"], "kb", google_credentials_path=Path(key), data_dir=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli.upd, "status", lambda cfg: (_ for _ in ()).throw(KeyError("x")))
    result = CliRunner().invoke(cli.main, ["status"])
    assert isinstance(result.exception, KeyError)


def test_unrecognized_google_error_is_one_line_without_the_html(cfg):
    resp = httplib2.Response({"status": 418})
    e = HttpError(resp, b"<!DOCTYPE html><html>teapot</html>", uri="https://x/files/y?alt=json")
    msg = errors.describe(e, cfg)
    assert msg.startswith("Google API error 418")
    assert "\n" not in msg and "DOCTYPE" not in msg and "alt=json" not in msg
