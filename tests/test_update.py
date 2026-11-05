"""End-to-end `kb update` runs against a fake Discord server, fake Drive, and in-memory
Knowledge Base folder. Covers the M2 scenarios in docs/PLAN.md."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from kb import llm as llm_mod
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


def fake_answer(user):
    """One feedback record per new Discord message; one program fact per Drive file."""
    source = user.split("<source>", 1)[1]
    if source.lstrip().startswith("Source: Discord"):
        new = source.split("Messages to extract:", 1)[1]
        found = re.findall(r"^\[(\d+)\] \S+ [^:\n]+:\n    (.*)$", new, re.M)
        records = [
            {
                "kind": "feedback",
                "text": text or "(attachment)",
                "programming_type": "night market",
                "category": "other",
                "sentiment": "neutral",
                "info_type": None,
                "applies_to": "",
                "date_basis": "source_date",
                "citations": [mid],
                "quote": text,
            }
            for mid, text in found
        ]
    else:
        title = re.search(r"^Title: (.*)$", source, re.M).group(1)
        records = [
            {
                "kind": "program_info",
                "text": f"{title} describes the Night Market",
                "programming_type": "Night Market",
                "category": None,
                "sentiment": None,
                "info_type": "description",
                "applies_to": "2024",
                "date_basis": "inferred",
                "citations": [],
                "quote": None,
            }
        ]
    return {"records": records}


class FakeLLM:
    model = "claude-opus-5-5"

    def __init__(self):
        self.requests = []
        self.fail = []  # substrings of the request: those requests fail
        self.answer = fake_answer

    def run(self, requests, log):
        out = {}
        for r in requests:
            self.requests.append(r)
            usage = {"input_tokens": 1000, "output_tokens": 100}
            if any(f in r.user for f in self.fail):
                out[r.custom_id] = llm_mod.Response(r.custom_id, error="boom", usage=usage)
            else:
                text = json.dumps(self.answer(r.user))
                out[r.custom_id] = llm_mod.Response(r.custom_id, text=text, usage=usage)
        return out


class World:
    def __init__(self, tmp_path):
        self.discord = FakeDiscord()
        self.drive = FakeDrive()
        self.llm = FakeLLM()
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
            llm=self.llm,
        )

    def run(self, **kw):
        self.drive.exports.clear()
        self.llm.requests.clear()
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

    def jsonl(self, name):
        data = self.kb.subfolder(st.STATE_FOLDER).read(name)
        return [json.loads(line) for line in (data or b"").decode().splitlines() if line]

    def records(self):
        return self.jsonl(st.EXTRACTED)

    def citations(self):
        return {c["id"]: c for c in self.jsonl(st.CITATIONS)}


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
    assert world.drive.exports == ["a"]  # read-only: the estimate needs the text
    assert world.llm.requests == []
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


def test_raw_snapshots_are_uploaded_to_drive(world):
    raw = world.kb.subfolder(st.RAW_FOLDER)
    assert raw.subfolder("drive").read("a.md") is not None  # from the first run
    world.discord.channel.append(msg(95))
    world.drive.add("a", "edited", world.now - timedelta(minutes=5))
    world.run()
    assert set(world.kb.folders) == {st.STATE_FOLDER, st.RAW_FOLDER}
    assert world.kb.files == []
    assert "edited" in raw.subfolder("drive").read("a.md").decode()
    local = (world.cfg.data_dir / "raw" / "drive" / "a.md").read_text()
    assert raw.subfolder("drive").read("a.md").decode() == local
    day = json.loads(raw.subfolder("discord").read("2026-11-01.json"))
    assert [i["id"] for i in day["runs"][-1]["items"]] == ["discord-msg:95"]


def test_raw_discord_day_keeps_teammates_runs_from_drive(world):
    remote = world.kb.subfolder(st.RAW_FOLDER).subfolder("discord")
    day = json.loads(remote.read("2026-11-01.json"))
    day["runs"].append({"run_id": "teammate", "items": []})
    remote.write("2026-11-01.json", json.dumps(day).encode())
    (world.cfg.data_dir / "raw" / "discord" / "2026-11-01.json").unlink()  # other machine
    world.discord.channel.append(msg(97))
    world.run()
    runs = json.loads(remote.read("2026-11-01.json"))["runs"]
    assert [r["run_id"] for r in runs][1] == "teammate" and len(runs) == 3


def test_full_exports_every_drive_file_again(world):
    world.drive.bodies["b"] = "body b, exported differently"  # e.g. an exporter change
    r = world.run(full=True)
    assert sorted(world.drive.exports) == ["a", "b"]
    assert [f["id"] for f in r.drive.changed] == ["b"]
    assert [f["id"] for f in r.drive.unchanged] == ["a"]
    m = world.state()["manifest"]
    assert m["drive:b"]["content_hash"] == upd.content_hash("body b, exported differently")
    drive_raw = world.kb.subfolder(st.RAW_FOLDER).subfolder("drive")
    assert {f["name"] for f in drive_raw.files} == {"a.md", "b.md"}
    # A normal run afterwards doesn't export anything.
    world.run()
    assert world.drive.exports == []


def test_progress_shows_each_stage_and_its_result(world):
    world.discord.channel.append(msg(96))
    world.drive.add("a", "edited", world.now - timedelta(minutes=5))
    lines = []
    upd.run_update(world.cfg, log=lines.append, sources=world.sources(), clock=lambda: world.now)
    text = "\n".join(lines)
    assert "[1/8] Taking the run lock…" in text
    assert "[3/8] Reading Discord: messages after the saved cursors…" in text
    assert "✓ 1 new message in 1 item" in text
    assert "(1/1) Doc a: changed" in text
    assert "[7/8] Extracting records from 2 items…" in text
    assert "[8/8] Saving state to Drive…" in text
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


def test_failed_lock_release_is_a_warning_not_a_failed_run(world, monkeypatch):
    world.discord.channel.append(msg(97))

    def refuse(file_id):
        raise RuntimeError("File not found")

    monkeypatch.setattr(world.kb.subfolder(st.STATE_FOLDER), "trash_id", refuse)
    lines = []
    upd.run_update(world.cfg, log=lines.append, sources=world.sources(), clock=lambda: world.now)
    assert any("couldn't be released" in line for line in lines)
    assert world.state()["cursors"]["discord_channel"] == "97"  # the run still saved


def test_drive_folder_trashes_instead_of_deleting():
    calls = []

    class Files:
        def update(self, **kw):
            calls.append(kw)
            return self

        def execute(self, num_retries=0):
            return {}

    class Drive:
        def files(self):
            return Files()

    st.DriveFolder(Drive(), "folder").trash_id("abc")
    assert calls == [{"fileId": "abc", "body": {"trashed": True}, "supportsAllDrives": True}]


# ---------------------------------------------------------------------------
# M3: extraction and citations
# ---------------------------------------------------------------------------


def test_first_run_extracts_records_with_citations(world):
    records = world.records()
    cites = world.citations()
    assert len(records) == 6  # 4 Discord messages + 2 Drive files
    assert [r["record_id"] for r in records] == [f"R-{n:04d}" for n in range(1, 7)]
    assert len(cites) == 6 and all(c["status"] == "active" for c in cites.values())
    by_item = {r["item_id"]: r for r in records}
    thread = [r for r in records if r["item_id"] == "discord-thread:20"]
    assert len(thread) == 2
    c = cites[thread[0]["citations"][0]]
    assert c["source"] == "discord" and c["link"].endswith("/20/20")
    assert c["date"] == "2026-10-01T00:00:00Z" and c["author"] == "Nick u-alice"
    # source_date comes from the citation, not the model; the type name is normalized
    assert thread[0]["applies_to"] == "2026-10-01" and thread[0]["date_basis"] == "source_date"
    assert thread[0]["programming_type"] == "Night Market"
    drive = by_item["drive:a"]
    assert drive["kind"] == "program_info" and drive["applies_to"] == "2024"
    assert cites[drive["citations"][0]]["source"] == "doc"
    types = json.loads(world.kb.subfolder(st.STATE_FOLDER).read(st.PROGRAMMING_TYPES))
    assert [t["name"] for t in types] == ["Night Market"]
    m = world.state()["manifest"]
    assert all(e["prompt_version"] == 1 for e in m.values())


def test_second_run_makes_no_llm_calls(world):
    r = world.run()
    assert world.llm.requests == []
    assert r.extracted.usage.requests == 0


def test_only_new_thread_messages_are_extracted_with_context(world):
    world.discord.threads["20"]["messages"].append(msg(31, "late reply"))
    world.run()
    (req,) = world.llm.requests
    assert "Earlier messages (context only" in req.user
    assert req.user.index("[20]") < req.user.index("Messages to extract") < req.user.index("[31]")
    new = world.records()[-1]
    assert new["record_id"] == "R-0007" and new["feedback"] == "late reply"
    assert len(world.records()) == 7  # nothing replaced


def test_programming_types_are_given_to_the_model(world):
    world.discord.channel.append(msg(30, "more"))
    world.run()
    assert "Programming types found so far:\n- Night Market" in world.llm.requests[0].user


def test_edited_file_replaces_its_records_and_keeps_its_citation(world):
    old = next(r for r in world.records() if r["item_id"] == "drive:a")
    world.drive.add("a", "body a, edited", world.now - timedelta(minutes=5), name="Doc a v2")
    world.run()
    new = [r for r in world.records() if r["item_id"] == "drive:a"]
    assert len(new) == 1 and new[0]["record_id"] != old["record_id"]
    assert new[0]["citations"] == old["citations"]
    assert world.citations()[old["citations"][0]]["context"] == "Doc a v2"


def test_removed_file_marks_its_citation_removed(world):
    cid = next(r for r in world.records() if r["item_id"] == "drive:b")["citations"][0]
    del world.drive.files["b"]
    world.run()
    assert world.citations()[cid]["status"] == "removed"
    assert any(r["item_id"] == "drive:b" for r in world.records())  # kept, dropped from docs


def test_deleted_message_marks_its_citation_removed(world):
    cid = next(r for r in world.records() if r["feedback"] == "second")["citations"][0]
    world.discord.channel = [msg(10, "first")]
    world.run(full=True)
    assert world.citations()[cid]["status"] == "removed"


def test_full_reextracts_edited_message(world):
    old = next(r for r in world.records() if r["item_id"] == "discord-msg:10")
    world.discord.channel[0] = msg(10, "first, edited")
    world.run(full=True)
    (req,) = world.llm.requests
    (new,) = [r for r in world.records() if r["item_id"] == "discord-msg:10"]
    assert new["feedback"] == "first, edited" and new["record_id"] != old["record_id"]
    assert new["citations"] == old["citations"]
    assert world.citations()[new["citations"][0]]["excerpt"] == "first, edited"


def test_large_file_is_skipped_until_allowed(world):
    from dataclasses import replace

    world.cfg = replace(world.cfg, llm_max_item_tokens=50)
    world.drive.add("big", "x" * 1000, world.now - timedelta(minutes=5), name="Big doc")
    r = world.run()
    assert "drive:big" in r.extracted.skipped and world.llm.requests == []
    entry = world.state()["manifest"]["drive:big"]
    assert entry["status"] == "skipped" and "max_item_tokens" in entry["skip_reason"]
    assert "Big doc" in "\n".join(upd.status(world.cfg, kb_folder=world.kb))
    world.run()  # still too large, not looked at again
    assert world.drive.exports == []
    world.cfg = replace(world.cfg, drive_allow_large_files=["big"])
    world.run()
    assert world.drive.exports == ["big"] and len(world.llm.requests) == 1
    assert world.state()["manifest"]["drive:big"]["status"] == "ok"


def test_allowed_large_file_is_chunked_but_cited_as_one(world, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(upd.ext, "CHUNK_TOKENS", 30)
    world.cfg = replace(world.cfg, llm_max_item_tokens=50, drive_allow_large_files=["big"])
    body = "\n\n".join(f"paragraph {n} " + "y" * 60 for n in range(4))
    world.drive.add("big", body, world.now - timedelta(minutes=5), name="Big doc")
    world.run()
    assert len(world.llm.requests) == 4
    assert "Part 1 of 4" in world.llm.requests[0].user
    recs = [r for r in world.records() if r["item_id"] == "drive:big"]
    assert len(recs) == 4 and len({r["citations"][0] for r in recs}) == 1


def test_failed_request_is_retried_once_within_the_run(world):
    calls = {"n": 0}
    real = world.llm.answer

    def flaky(user):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"oops": True}  # unreadable answer
        return real(user)

    world.llm.answer = flaky
    world.discord.channel.append(msg(30, "retry me"))
    r = world.run()
    assert len(world.llm.requests) == 2 and r.extracted.errors == {}
    assert world.records()[-1]["feedback"] == "retry me"


def test_discord_extraction_error_is_retried_next_run(world):
    world.discord.channel.append(msg(30, "fails"))
    world.llm.fail = ["fails"]
    r = world.run()
    assert "discord-msg:30" in r.extracted.errors
    assert len(world.llm.requests) == 2  # first try and one retry
    entry = world.state()["manifest"]["discord-msg:30"]
    assert entry["status"] == "error" and entry["retry_item"]["messages"][0]["id"] == "30"
    assert world.state()["cursors"]["discord_channel"] == "30"  # the run still moved on
    # Next run: the message isn't read from Discord again, but it's extracted again.
    world.llm.fail = []
    world.discord.channel.append(msg(31, "also new"))
    world.run()
    assert {r.get("feedback") for r in world.records()} >= {"fails", "also new"}
    entry = world.state()["manifest"]["discord-msg:30"]
    assert entry["status"] == "ok" and "retry_item" not in entry


def test_drive_extraction_error_is_retried_next_run(world):
    world.drive.add("c", "body c", world.now - timedelta(minutes=5), name="Doc c")
    world.llm.fail = ["Doc c"]
    world.run()
    assert world.state()["manifest"]["drive:c"]["status"] == "error"
    world.llm.fail = []
    world.run()
    assert world.drive.exports == ["c"]
    assert world.state()["manifest"]["drive:c"]["status"] == "ok"
    assert any(r["item_id"] == "drive:c" for r in world.records())


def test_record_citing_context_or_unknown_message_is_dropped(world):
    def answer(user):
        rec = fake_answer(user)["records"][0]
        return {"records": [{**rec, "citations": ["20"]}, {**rec, "citations": ["31", "999"]}]}

    world.llm.answer = answer
    world.discord.threads["20"]["messages"].append(msg(31, "late"))
    r = world.run()
    assert [x["feedback"] for x in world.records()[6:]] == ["late"]
    problems = r.extracted.problems["discord-thread:20"]
    assert any("dropped" in p for p in problems) and any("999" in p for p in problems)


def test_rebuild_extracts_everything_again_and_keeps_citation_ids(world):
    before = world.citations()
    world.run(rebuild=True)
    assert len(world.llm.requests) == 5  # every item
    assert len(world.records()) == 6 and world.records()[0]["record_id"] == "R-0007"
    assert set(world.citations()) == set(before)
    assert world.run().extracted.usage.requests == 0


def test_items_read_before_extraction_existed_are_caught_up(world):
    folder = world.kb.subfolder(st.STATE_FOLDER)
    manifest = json.loads(folder.read(st.MANIFEST))
    for e in manifest.values():
        e.pop("prompt_version")
    folder.write(st.MANIFEST, json.dumps(manifest).encode())
    for name in (st.EXTRACTED, st.CITATIONS, st.PROGRAMMING_TYPES):
        folder.trash_id(next(f["id"] for f in folder.list() if f["name"] == name))
    r = world.run()
    assert len(world.llm.requests) == 5 and len(world.records()) == 6
    assert sorted(item_ids(r)) == ["discord-msg:10", "discord-msg:11", "discord-thread:20"]
    assert world.run().extracted.usage.requests == 0


def test_dry_run_estimates_tokens_and_cost(world):
    from dataclasses import replace

    world.cfg = replace(world.cfg, llm_max_item_tokens=200)
    world.discord.channel.append(msg(30, "hello"))
    world.drive.add("big", "z" * 2000, world.now - timedelta(minutes=5), name="Huge")
    r = world.run(dry_run=True)
    e = r.estimate
    assert e.requests == 1 and e.input_tokens > 0 and e.cost_usd > 0
    assert [i["title"] for i in e.skipped] == ["Huge"]
    lines = []
    upd.report(r, {}, lines.append)
    text = "\n".join(lines)
    assert "Estimated cost: $" in text and "Huge" in text and "too large" in text
    assert world.llm.requests == []


def test_run_summary_has_token_and_cost_totals(world):
    usage = world.state()["run_log"][-1]["summary"]["extraction"]["usage"]
    assert usage["requests"] == 5 and usage["input_tokens"] == 5000
    assert usage["cost_usd"] == pytest.approx(5 * (1000 * 4 + 100 * 20) / 1e6)
    lines = []
    world.discord.channel.append(msg(30))
    upd.report(world.run(), {}, lines.append)
    assert any(line.startswith("Tokens and cost: 1 LLM call") for line in lines)


def test_status_shows_records_by_year_and_weak_dates(world):
    text = "\n".join(upd.status(world.cfg, kb_folder=world.kb))
    assert "Records: 6 (4 feedback, 2 program_info)" in text
    assert "2024: 2 · 2026: 4" in text
    assert "source_date): 4 of 6 (67%)" in text
    assert "Night Market: 6 records" in text
    assert "Citations: 6 (6 active)" in text


# ---------------------------------------------------------------------------
# Steps (--pull / --extract) and --limit
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh(tmp_path):
    """The same sources as `world`, before any run."""
    w = World(tmp_path)
    w.discord.channel = [msg(10, "first"), msg(11, "second", author="bob")]
    w.discord.threads = {"20": {"name": "Night market", "messages": [msg(20), msg(21)]}}
    w.drive.add("a", "body a", T0 - timedelta(days=5))
    w.drive.add("b", "body b", T0 - timedelta(days=5))
    return w


def statuses(w):
    return {k: e["status"] for k, e in w.state()["manifest"].items()}


def test_limit_extracts_some_items_and_the_next_run_does_the_rest(fresh):
    r = fresh.run(limit=2)
    assert [i.id for i in r.items[:2]] == ["discord-msg:10", "drive:a"]  # alternating
    assert len(fresh.llm.requests) == 2
    s = statuses(fresh)
    assert sorted(k for k, v in s.items() if v == "ok") == ["discord-msg:10", "drive:a"]
    assert sorted(k for k, v in s.items() if v == "pending") == [
        "discord-msg:11",
        "discord-thread:20",
        "drive:b",
    ]
    m = fresh.state()["manifest"]
    assert m["discord-thread:20"]["retry_item"]["messages"][0]["id"] == "20"
    assert m["drive:b"]["pending_reason"] == "--limit 2"
    assert "3 pending" in "\n".join(upd.status(fresh.cfg, kb_folder=fresh.kb))

    r = fresh.run()
    assert len(fresh.llm.requests) == 3
    assert set(statuses(fresh).values()) == {"ok"}
    assert len(fresh.records()) == 6 and len(fresh.citations()) == 6
    assert "retry_item" not in fresh.state()["manifest"]["discord-thread:20"]
    assert fresh.run().extracted.usage.requests == 0


def test_pull_only_then_extract_only(fresh):
    r = fresh.run(steps=["pull"])
    assert fresh.llm.requests == [] and r.extracted.records == 0
    assert set(statuses(fresh).values()) == {"pending"}
    assert fresh.kb.subfolder(st.RAW_FOLDER).subfolder("drive").read("a.md") is not None

    fresh.discord.channel.append(msg(12, "not pulled yet"))
    cursors = fresh.state()["cursors"]
    r = fresh.run(steps=["extract"])
    assert fresh.drive.exports == [] and len(fresh.llm.requests) == 5
    assert set(statuses(fresh).values()) == {"ok"}
    assert fresh.state()["cursors"] == cursors  # Discord wasn't read
    texts = {r.get("feedback") for r in fresh.records()}
    assert "first" in texts and "not pulled yet" not in texts
    drive_fact = next(r for r in fresh.records() if r["item_id"] == "drive:a")
    assert drive_fact["fact"] == "Doc a describes the Night Market"
    assert fresh.state()["run_log"][-1]["steps"] == ["extract"]

    fresh.run()  # the next full run picks up the new message only
    assert [r.user.count("[12]") for r in fresh.llm.requests] == [1]


def test_extract_only_reads_the_drive_file_from_its_raw_snapshot(fresh):
    fresh.run(steps=["pull"])
    fresh.drive.bodies["a"] = "edited after the pull"
    fresh.run(steps=["extract"])
    sent = next(r.user for r in fresh.llm.requests if "Title: Doc a" in r.user)
    assert "body a" in sent and "edited after the pull" not in sent


def test_extract_only_retries_extraction_errors(fresh):
    fresh.llm.fail = ["body b", "[11]"]
    fresh.run()
    assert statuses(fresh)["drive:b"] == "error" and statuses(fresh)["discord-msg:11"] == "error"
    fresh.llm.fail = []
    fresh.run(steps=["extract"])
    assert set(statuses(fresh).values()) == {"ok"}


def test_extract_only_with_nothing_waiting_makes_no_calls(world):
    r = world.run(steps=["extract"])
    assert world.llm.requests == [] and r.items == []


def test_extract_only_skips_discord_items_it_has_no_messages_for(world):
    m = world.state()["manifest"]
    m["discord-msg:10"].pop("prompt_version")  # as if read before extraction existed
    folder = world.kb.subfolder(st.STATE_FOLDER)
    folder.write("manifest.json", json.dumps(m).encode())
    r = world.run(steps=["extract"])
    assert world.llm.requests == [] and len(r.waiting.need_pull) == 1
    assert statuses(world)["discord-msg:10"] == "ok"  # left for the next full run


def test_dry_run_with_limit_estimates_only_those_items(fresh):
    r = fresh.run(dry_run=True, limit=1)
    assert r.estimate.requests == 1 and len(r.deferred) == 4


def test_pending_rebuild_item_still_replaces_its_records(world):
    world.llm.fail = ["[20]"]
    world.run(rebuild=True)
    retry = world.state()["manifest"]["discord-thread:20"]["retry_item"]
    assert retry["replace"] is True
    world.llm.fail = []
    world.run()
    thread = [r for r in world.records() if r["item_id"] == "discord-thread:20"]
    assert len(thread) == 2  # replaced, not duplicated


def test_pick_alternates_sources():
    from kb import extract as ext

    d = [ext.discord_item({"id": f"discord-msg:{n}", "messages": []}) for n in (1, 2, 3)]
    g = [ext.drive_item({"id": f"drive:{n}"}, "x") for n in ("a",)]
    chosen, later = upd.pick(d + g, 3)
    assert [i.id for i in chosen] == ["discord-msg:1", "discord-msg:2", "drive:a"]
    assert [i.id for i in later] == ["discord-msg:3"]
    assert upd.pick(d, None) == (d, [])


def test_rendered_body_round_trips():
    f = {"id": "x", "name": "Plan", "mimeType": gdrive.DOC_MIME, "modifiedTime": None}
    body = "# Heading\n\nSome text.\n\n---\n\nMore."
    assert upd.rendered_body(gdrive.render_file(f, body)) == body
