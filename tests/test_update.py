"""End-to-end `kb update` runs against a fake Discord server, fake Drive, and in-memory
Knowledge Base folder. Covers the M2 scenarios in docs/PLAN.md."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from kb import state as st
from kb import update as upd
from kb.config import Config
from kb.sources import discord as dsc
from kb.sources import drive as gdrive

SERVER, CHANNEL = "1", "100"
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def msg(id, content="hi", author="alice", **extra):
    return {
        "id": str(id),
        "type": 0,
        "content": content,
        "timestamp": "2026-10-01T00:00:00+00:00",
        "author": {"id": f"u-{author}", "username": author},
        "attachments": [],
        **extra,
    }


class FakeDiscord:
    """Duck-typed DiscordClient over an editable channel."""

    def __init__(self):
        self.channel = []  # main-channel messages
        self.threads = {}  # thread id → {"name", "messages"}
        self.member_lookups = []

    def get_channel(self, channel_id):
        return {"id": CHANNEL, "name": "feedback", "type": 0}

    def all_messages(self, channel_id, *, after="0"):
        msgs = self.channel if channel_id == CHANNEL else self.threads[channel_id]["messages"]
        return sorted((m for m in msgs if int(m["id"]) > int(after)), key=lambda m: int(m["id"]))

    def active_threads(self, server_id, channel_id):
        return [
            {
                "id": tid,
                "name": t["name"],
                "parent_id": CHANNEL,
                "last_message_id": max((m["id"] for m in t["messages"]), key=int, default=None),
                "thread_metadata": {},
            }
            for tid, t in self.threads.items()
        ]

    def all_archived_threads(self, channel_id, kind="public"):
        return []

    def get_member(self, server_id, user_id):
        self.member_lookups.append(user_id)
        return {"nick": f"Nick {user_id}"}


class FakeDrive:
    def __init__(self):
        self.files = {}  # id → file dict
        self.bodies = {}
        self.exports = []

    def add(self, id, body, modified, name=None):
        self.files[id] = {
            "id": id,
            "name": name or f"Doc {id}",
            "mimeType": gdrive.DOC_MIME,
            "modifiedTime": iso(modified),
            "owners": [{"displayName": "Owner"}],
        }
        self.bodies[id] = body

    def export(self, f):
        self.exports.append(f["id"])
        return self.bodies[f["id"]]


class World:
    def __init__(self, tmp_path):
        self.discord = FakeDiscord()
        self.drive = FakeDrive()
        self.kb = st.MemoryFolder()
        self.cfg = Config(
            discord_server_id=SERVER,
            discord_channel_id=CHANNEL,
            drive_source_folder_ids=["src"],
            drive_kb_folder_id="kb",
            data_dir=tmp_path,
        )
        self.cache = dsc.MemberCache(tmp_path / "raw" / "discord" / "members.json")
        self.now = T0
        self.fail_after_fetch = False

    def sources(self):
        def discord_fetch(cursors, refresh):
            return dsc.fetch(
                self.discord,
                SERVER,
                CHANNEL,
                cursors,
                member_cache=self.cache,
                refresh_names=refresh,
            )

        def drive_list():
            if self.fail_after_fetch:
                raise RuntimeError("crash")
            return list(self.drive.files.values())

        return upd.Sources(
            discord_fetch=discord_fetch,
            drive_list=drive_list,
            drive_export=self.drive.export,
            kb_folder=self.kb,
        )

    def run(self, **kw):
        self.drive.exports.clear()
        result = upd.run_update(
            self.cfg, log=lambda _: None, sources=self.sources(), clock=lambda: self.now, **kw
        )
        self.now += timedelta(hours=1)
        return result

    def state(self):
        folder = self.kb.subfolder(st.STATE_FOLDER)
        return {
            name.removesuffix(".json"): json.loads(folder.read(name))
            for name in st.STATE_FILES
            if folder.read(name)
        }


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.discord.channel = [msg(10, "first"), msg(11, "second", author="bob")]
    w.discord.threads = {"20": {"name": "Night market", "messages": [msg(20), msg(21)]}}
    w.drive.add("a", "body a", T0 - timedelta(days=5))
    w.drive.add("b", "body b", T0 - timedelta(days=5))
    w.run()
    return w


def item_ids(result):
    return [i["id"] for i in result.discord.items]


def test_first_run_reads_everything_and_saves_state(world):
    s = world.state()
    assert set(s["manifest"]) == {
        "discord-msg:10",
        "discord-msg:11",
        "discord-thread:20",
        "drive:a",
        "drive:b",
    }
    assert s["cursors"]["discord_channel"] == "11"
    assert s["cursors"]["discord_threads"] == {"20": "21"}
    assert s["cursors"]["last_run_started_at"] == "2026-11-01T09:00:00Z"
    assert len(s["run_log"]) == 1
    # lock released
    assert st.read_lock(world.kb.subfolder(st.STATE_FOLDER)) is None


def test_second_run_finds_nothing_new(world):
    r = world.run()
    assert r.discord.items == []
    assert r.drive.new == [] and r.drive.modified == [] and r.drive.removed == []
    assert world.drive.exports == []
    assert len(r.drive.skipped) == 2


def test_new_channel_message(world):
    world.discord.channel.append(msg(30, "new one"))
    r = world.run()
    assert item_ids(r) == ["discord-msg:30"]
    assert world.state()["cursors"]["discord_channel"] == "30"


def test_new_message_in_old_thread_keeps_earlier_messages_as_context(world):
    world.discord.threads["20"]["messages"].append(msg(31, "late reply"))
    r = world.run()
    (item,) = r.discord.items
    assert item["id"] == "discord-thread:20"
    assert [m["id"] for m in item["messages"]] == ["31"]
    assert [m["id"] for m in item["context"]] == ["20", "21"]
    s = world.state()
    assert s["cursors"]["discord_threads"]["20"] == "31"
    assert set(s["manifest"]["discord-thread:20"]["messages"]) == {"20", "21", "31"}


def test_new_thread(world):
    world.discord.threads["40"] = {"name": "Campfire", "messages": [msg(40), msg(41)]}
    r = world.run()
    (item,) = r.discord.items
    assert item["id"] == "discord-thread:40"
    assert [m["id"] for m in item["messages"]] == ["40", "41"]
    assert item["context"] == []


def test_thread_started_from_old_message_treats_starter_as_context(world):
    # A thread is opened later on message 10, which was already processed on its own.
    placeholder = msg(50, "", type=dsc.THREAD_STARTER, referenced_message=msg(10, "first"))
    world.discord.threads["10"] = {"name": "About 10", "messages": [placeholder, msg(51)]}
    r = world.run()
    (item,) = r.discord.items
    assert [m["id"] for m in item["messages"]] == ["51"]
    assert [m["id"] for m in item["context"]] == ["10"]


def test_new_drive_file(world):
    world.drive.add("c", "body c", world.now - timedelta(minutes=5))
    r = world.run()
    assert [f["id"] for f in r.drive.new] == ["c"]
    assert world.drive.exports == ["c"]
    assert world.state()["manifest"]["drive:c"]["status"] == "ok"


def test_old_file_moved_into_folder_is_new(world):
    world.drive.add("old", "ancient", T0 - timedelta(days=400))
    r = world.run()
    assert [f["id"] for f in r.drive.new] == ["old"]


def test_edited_file_is_changed(world):
    world.drive.add("a", "body a, edited", world.now - timedelta(minutes=5))
    r = world.run()
    assert [f["id"] for f in r.drive.changed] == ["a"]
    s = world.state()["manifest"]["drive:a"]
    assert s["content_hash"] == upd.content_hash("body a, edited")
    assert "body a, edited" in (world.cfg.data_dir / "raw" / "drive" / "a.md").read_text()


def test_comment_only_change_is_skipped_by_hash(world):
    raw_a = world.cfg.data_dir / "raw" / "drive" / "a.md"
    before = raw_a.read_text()
    world.drive.add("a", "body a", world.now - timedelta(minutes=5))  # same text, newer time
    r = world.run()
    assert r.drive.changed == []
    assert [f["id"] for f in r.drive.unchanged] == ["a"]
    assert raw_a.read_text() == before
    # Next run doesn't look at it again.
    r = world.run()
    assert world.drive.exports == [] and [f["id"] for f in r.drive.skipped] == ["a", "b"]


def test_removed_file(world):
    del world.drive.files["b"]
    r = world.run()
    assert r.drive.removed == ["drive:b"]
    assert world.state()["manifest"]["drive:b"]["status"] == "removed"
    # It's reported once, not every run.
    assert world.run().drive.removed == []


def test_file_with_export_error_is_retried(world):
    world.drive.add("bad", "x", world.now - timedelta(minutes=5))
    bodies = world.drive.bodies
    del bodies["bad"]
    r = world.run()
    assert "bad" in r.drive.errors
    assert world.state()["manifest"]["drive:bad"]["status"] == "error"
    bodies["bad"] = "fixed"
    world.run()
    assert world.drive.exports == ["bad"]
    assert world.state()["manifest"]["drive:bad"]["status"] == "ok"


def test_crash_before_state_is_saved(world):
    before = world.state()
    world.discord.channel.append(msg(60, "lost?"))
    world.fail_after_fetch = True
    with pytest.raises(RuntimeError):
        world.run()
    assert world.state() == before
    assert st.read_lock(world.kb.subfolder(st.STATE_FOLDER)) is None
    world.fail_after_fetch = False
    assert item_ids(world.run()) == ["discord-msg:60"]


def test_full_reports_deleted_and_edited_messages(world):
    world.discord.channel = [msg(10, "first, edited"), msg(11, "second", author="bob")]
    world.discord.threads["20"]["messages"] = [msg(20)]  # 21 deleted
    r = world.run(full=True)
    assert [d["message_id"] for d in r.discord.deleted] == ["21"]
    assert r.discord.edited == [{"item_id": "discord-msg:10", "message_id": "10"}]
    assert item_ids(r) == ["discord-msg:10"]
    m = world.state()["manifest"]
    assert m["discord-thread:20"]["removed_messages"] == ["21"]
    assert m["discord-thread:20"]["status"] == "ok"
    # A normal run afterwards finds nothing.
    assert world.run().discord.items == []


def test_full_marks_item_removed_when_all_messages_deleted(world):
    world.discord.channel = [msg(10, "first")]
    r = world.run(full=True)
    assert [d["item_id"] for d in r.discord.deleted] == ["discord-msg:11"]
    assert world.state()["manifest"]["discord-msg:11"]["status"] == "removed"


def test_dry_run_saves_nothing_and_takes_no_lock(world):
    before = world.state()
    world.discord.channel.append(msg(70))
    world.drive.add("a", "changed", world.now - timedelta(minutes=5))
    r = world.run(dry_run=True)
    assert item_ids(r) == ["discord-msg:70"]
    assert [f["id"] for f in r.drive.modified] == ["a"]
    assert world.drive.exports == []
    assert world.state() == before


def test_raw_discord_snapshot_is_one_file_per_day(world):
    world.discord.channel.append(msg(80))
    world.run()
    day = json.loads((world.cfg.data_dir / "raw" / "discord" / "2026-11-01.json").read_text())
    assert len(day["runs"]) == 2  # the first run and this one
    assert [i["id"] for i in day["runs"][1]["items"]] == ["discord-msg:80"]


def test_member_names_are_cached_between_runs(world):
    assert sorted(world.discord.member_lookups) == ["u-alice", "u-bob"]
    world.discord.channel.append(msg(90, author="carol"))
    world.discord.channel.append(msg(91, author="alice"))
    r = world.run()
    assert world.discord.member_lookups[2:] == ["u-carol"]
    assert {i["messages"][0]["author"] for i in r.discord.items} == {"Nick u-alice", "Nick u-carol"}
    cached = json.loads(world.cache.path.read_text())
    assert cached["u-carol"]["username"] == "carol"
    assert cached["u-carol"]["display_name"] == "Nick u-carol"


def test_full_refreshes_member_names(world):
    world.discord.member_lookups.clear()
    world.run(full=True)
    assert sorted(world.discord.member_lookups) == ["u-alice", "u-bob"]


def test_lock_held_by_another_run(world):
    folder = world.kb.subfolder(st.STATE_FOLDER)
    held = st.Lock(folder, holder="someone@else", clock=lambda: world.now)
    held.acquire()
    with pytest.raises(st.LockError, match="someone@else"):
        world.run()
    held.release()
    world.run()


def test_stale_lock_needs_force_unlock(world):
    folder = world.kb.subfolder(st.STATE_FOLDER)
    st.Lock(folder, holder="crashed@box", clock=lambda: world.now - timedelta(hours=3)).acquire()
    with pytest.raises(st.LockError, match="--force-unlock"):
        world.run()
    world.run(force_unlock=True)
    assert st.read_lock(folder) is None


def test_status_lines(world):
    lines = upd.status(world.cfg, kb_folder=world.kb)
    text = "\n".join(lines)
    assert "Last run: 2026-11-01T09:00:00Z" in text
    assert "Lock: free" in text
    assert "discord: 3 ok" in text and "drive: 2 ok" in text


def test_first_dry_run_creates_nothing_in_drive(tmp_path):
    w = World(tmp_path)
    w.discord.channel = [msg(10)]
    w.drive.add("a", "body", T0)
    r = w.run(dry_run=True)
    assert item_ids(r) == ["discord-msg:10"]
    assert w.kb.folders == {} and w.kb.files == []
    assert upd.status(w.cfg, kb_folder=w.kb)[0] == "Last run: none yet"


def test_raw_snapshots_are_never_uploaded(world):
    world.discord.channel.append(msg(95))
    world.drive.add("a", "edited", world.now - timedelta(minutes=5))
    world.run()
    assert set(world.kb.folders) == {st.STATE_FOLDER}
    assert world.kb.files == []


def test_progress_shows_each_stage_and_its_result(world):
    world.discord.channel.append(msg(96))
    world.drive.add("a", "edited", world.now - timedelta(minutes=5))
    lines = []
    upd.run_update(world.cfg, log=lines.append, sources=world.sources(), clock=lambda: world.now)
    text = "\n".join(lines)
    assert "[1/7] Taking the run lock…" in text
    assert "[3/7] Reading Discord: messages after the saved cursors…" in text
    assert "✓ 1 new message in 1 item" in text
    assert "(1/1) Doc a: changed" in text
    assert "[7/7] Saving state to Drive…" in text
    assert "Run lock released." in text
    assert lines[-1].startswith("Finished in ")


def test_progress_marks_the_failed_stage(world):
    world.fail_after_fetch = True
    lines = []
    with pytest.raises(RuntimeError):
        upd.run_update(world.cfg, log=lines.append, sources=world.sources())
    failed = [line for line in lines if "✗" in line]
    assert len(failed) == 1 and failed[0].endswith("s: crash")
    assert "Listing Drive files" in lines[lines.index(failed[0]) - 1]
    assert "Run lock released." in lines
