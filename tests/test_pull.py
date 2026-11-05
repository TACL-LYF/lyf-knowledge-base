import json

import pytest

from kb import pull
from kb.config import Config
from kb.sources import drive as gdrive

FAKE_SERVICES = gdrive.Services(drive=None, sheets=None, slides=None)


def cfg(tmp_path):
    return Config(
        discord_server_id="1",
        discord_channel_id="100",
        drive_source_folder_ids=["src"],
        drive_kb_folder_id="kb",
        data_dir=tmp_path,
    )


def drive_file(id, mime=gdrive.DOC_MIME):
    return {"id": id, "name": f"File {id}", "mimeType": mime, "owners": []}


def test_drive_pull_writes_files_and_keeps_going_after_an_error(tmp_path, monkeypatch):
    files = [drive_file("a"), drive_file("bad"), drive_file("c", gdrive.SLIDES_MIME)]
    monkeypatch.setattr(gdrive, "list_folder_files", lambda *a: iter(files))

    def export(services, f):
        if f["id"] == "bad":
            raise RuntimeError("export failed")
        return f"body of {f['id']}"

    monkeypatch.setattr(gdrive, "export_text", export)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "drive").mkdir()
    (raw / "drive" / "stale.md").write_text("old")

    result = pull.pull_drive(cfg(tmp_path), raw, lambda _: None, services=FAKE_SERVICES)

    assert sorted(p.name for p in (raw / "drive").iterdir()) == ["a.md", "c.md"]
    assert "body of c" in (raw / "drive" / "c.md").read_text()
    assert result["by_type"] == {"doc": 1, "slides": 1}
    assert [e["id"] for e in result["errors"]] == ["drive:bad"]
    assert not (raw / ".drive.partial").exists()


def test_failed_pull_leaves_previous_output(tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("listing failed")
        yield  # pragma: no cover

    monkeypatch.setattr(gdrive, "list_folder_files", boom)
    raw = tmp_path / "raw"
    (raw / "drive").mkdir(parents=True)
    (raw / "drive" / "keep.md").write_text("old")

    with pytest.raises(RuntimeError):
        pull.pull_drive(cfg(tmp_path), raw, lambda _: None, services=FAKE_SERVICES)
    assert (raw / "drive" / "keep.md").read_text() == "old"


def test_pull_summary_keeps_other_source_on_partial_pull(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "pull_drive", lambda *a: {"files": 2})
    monkeypatch.setattr(pull, "pull_discord", lambda *a: {"threads": 1})
    pull.pull(cfg(tmp_path), ("discord", "drive"), log=lambda _: None)
    monkeypatch.setattr(pull, "pull_discord", lambda *a: {"threads": 5})
    pull.pull(cfg(tmp_path), ("discord",), log=lambda _: None)

    summary = json.loads((tmp_path / "raw" / "pull_summary.json").read_text())
    assert summary["discord"] == {"threads": 5}
    assert summary["drive"] == {"files": 2}
