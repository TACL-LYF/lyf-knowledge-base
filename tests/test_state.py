import os
import signal
import time
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


def test_someone_elses_fresh_lock_cannot_be_forced():
    folder = st.MemoryFolder()
    st.Lock(folder, holder="busy", clock=lambda: NOW).acquire()
    later = st.Lock(
        folder, timeout_minutes=60, holder="me", clock=lambda: NOW + timedelta(minutes=10)
    )
    with pytest.raises(st.LockError, match="in progress.*Wait for it to finish"):
        later.acquire(force_unlock=True)
    assert st.read_lock(folder)["holder"] == "busy"


def test_own_fresh_lock_can_be_forced():
    # e.g. left behind when a run of ours was interrupted
    folder = st.MemoryFolder()
    st.Lock(folder, holder="me", clock=lambda: NOW).acquire()
    later = st.Lock(
        folder, timeout_minutes=60, holder="me", clock=lambda: NOW + timedelta(minutes=10)
    )
    with pytest.raises(st.LockError, match="in progress.*--force-unlock"):
        later.acquire()
    later.acquire(force_unlock=True)
    assert later.cleared and "held by me" in later.cleared
    assert [f["id"] for f in folder.list()] == [later.file_id]


def test_stale_lock_needs_force_and_is_then_cleared():
    folder = st.MemoryFolder()
    st.Lock(folder, holder="crashed", clock=lambda: NOW).acquire()
    later = st.Lock(
        folder, timeout_minutes=60, holder="me", clock=lambda: NOW + timedelta(minutes=61)
    )
    with pytest.raises(st.LockError, match="stale"):
        later.acquire()
    later.acquire(force_unlock=True)
    assert st.read_lock(folder)["holder"] == "me"


def test_ctrl_c_during_release_still_releases_then_interrupts():
    folder = st.MemoryFolder()
    lock = st.Lock(folder, clock=lambda: NOW)
    lock.acquire()
    real_trash = folder.trash_id

    def trash_with_ctrl_c(file_id):
        os.kill(os.getpid(), signal.SIGINT)  # pressed while the lock is being deleted
        time.sleep(0.05)
        real_trash(file_id)

    folder.trash_id = trash_with_ctrl_c
    with pytest.raises(KeyboardInterrupt):
        lock.release()
    assert folder.list() == [] and lock.file_id is None
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


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
