from datetime import UTC, datetime, timedelta

import pytest

from kb import state as st

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def test_lock_race_loser_backs_off(monkeypatch):
    folder = st.MemoryFolder()
    first = st.Lock(folder, holder="a", clock=lambda: NOW)
    first.acquire()
    second = st.Lock(folder, holder="b", clock=lambda: NOW)

    # The second run's first look at the folder happened before the first run's lock
    # file appeared, so it goes ahead and creates its own.
    real_list = folder.list
    calls = []

    def list_files():
        calls.append(1)
        return [] if len(calls) == 1 else real_list()

    monkeypatch.setattr(folder, "list", list_files)
    with pytest.raises(st.LockError, match="same moment"):
        second.acquire()
    assert [f["id"] for f in real_list()] == [first.file_id]


def test_lock_release_is_idempotent():
    folder = st.MemoryFolder()
    lock = st.Lock(folder, clock=lambda: NOW)
    lock.acquire()
    assert st.read_lock(folder)["holder"] == lock.holder
    lock.release()
    lock.release()
    assert folder.list() == []


def test_fresh_lock_cannot_be_forced():
    folder = st.MemoryFolder()
    st.Lock(folder, holder="busy", clock=lambda: NOW).acquire()
    later = st.Lock(folder, timeout_minutes=60, clock=lambda: NOW + timedelta(minutes=10))
    with pytest.raises(st.LockError, match="in progress"):
        later.acquire(force_unlock=True)


def test_download_replaces_local_copy_and_upload_round_trips(tmp_path):
    folder = st.MemoryFolder()
    folder.write("cursors.json", b'{"discord_channel": "5"}')
    folder.create(st.LOCK_NAME, b"{}")
    local = tmp_path / "state"
    local.mkdir()
    (local / "stale.json").write_text("{}")

    assert st.download(folder, local) == ["cursors.json"]
    assert sorted(p.name for p in local.iterdir()) == ["cursors.json"]

    state = st.State(local).load()
    assert state.cursors["discord_channel"] == "5"
    assert state.cursors["discord_threads"] == {}
    state.manifest["x"] = {"id": "x"}
    state.save()
    st.upload(folder, local)
    assert b'"x"' in folder.read("manifest.json")
    assert sum(1 for f in folder.list() if f["name"] == "cursors.json") == 1


def test_parse_time_handles_drive_fractional_seconds():
    a = st.parse_time("2026-11-04T09:00:00Z")
    b = st.parse_time("2026-11-04T09:00:00.500Z")
    assert b > a
