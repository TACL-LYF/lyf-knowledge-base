import httpx

from kb.sources.discord import (
    REPLY,
    THREAD_STARTER,
    DiscordClient,
    DiscordPull,
    build_items,
    fetch_member_names,
)

SERVER, CHANNEL = "1", "100"


def msg(id, content="hi", *, author="alice", bot=False, type=0, **extra):
    return {
        "id": str(id),
        "type": type,
        "content": content,
        "timestamp": f"2026-10-01T00:00:{int(id) % 60:02d}+00:00",
        "edited_timestamp": None,
        "author": {"id": f"u-{author}", "username": author, "global_name": None, "bot": bot},
        "attachments": [],
        **extra,
    }


def thread(id, name="Night market"):
    return {"id": str(id), "name": name, "parent_id": CHANNEL, "thread_metadata": {}}


def make_pull(channel_messages, threads=(), thread_messages=None):
    return DiscordPull(
        server_id=SERVER,
        channel={"id": CHANNEL, "name": "feedback", "type": 0},
        channel_messages=channel_messages,
        threads=list(threads),
        thread_messages=thread_messages or {},
    )


def by_id(items):
    return {i["id"]: i for i in items}


def test_standalone_messages_drop_bots_and_system_messages():
    pull = make_pull(
        [
            msg(10, "good event"),
            msg(11, "beep", bot=True),
            msg(12, "", type=7),  # member join
            msg(13, "   "),  # empty
            msg(14, "", attachments=[{"filename": "a.png", "url": "https://x/a.png"}]),
        ]
    )
    items = by_id(build_items(pull))
    assert set(items) == {"discord-msg:10", "discord-msg:14"}
    m = items["discord-msg:10"]
    assert m["kind"] == "message"
    assert m["link"] == "https://discord.com/channels/1/100/10"
    assert m["messages"][0]["content"] == "good event"
    assert m["context"] == []
    assert items["discord-msg:14"]["messages"][0]["attachments"][0]["filename"] == "a.png"


def test_reply_includes_replied_message_as_context():
    original = msg(10, "what did you think?", author="bob")
    reply = msg(
        11,
        "too crowded",
        type=REPLY,
        message_reference={"message_id": "10"},
        referenced_message=original,
    )
    items = by_id(build_items(make_pull([original, reply])))
    item = items["discord-msg:11"]
    assert item["messages"][0]["reply_to"] == "10"
    assert [c["id"] for c in item["context"]] == ["10"]
    assert item["context"][0]["author"] == "bob"
    # The replied-to message is still its own item too.
    assert "discord-msg:10" in items


def test_thread_started_from_channel_message_absorbs_the_starter():
    starter = msg(20, "Night market thoughts?")
    created_notice = msg(21, "Night market", type=18)  # THREAD_CREATED system message
    placeholder = msg(22, "", type=THREAD_STARTER, message_reference={"message_id": "20"})
    in_thread = [placeholder, msg(23, "more food stalls"), msg(24, "ok", bot=True)]
    pull = make_pull([starter, created_notice], [thread(20)], {"20": in_thread})

    items = by_id(build_items(pull))
    assert set(items) == {"discord-thread:20"}
    t = items["discord-thread:20"]
    assert t["kind"] == "thread"
    assert t["title"] == "#feedback › thread: Night market"
    assert [m["id"] for m in t["messages"]] == ["20", "23"]
    # The starter lives in the parent channel; thread messages link via the thread ID.
    assert t["messages"][0]["link"] == "https://discord.com/channels/1/100/20"
    assert t["messages"][1]["link"] == "https://discord.com/channels/1/20/23"


def test_thread_starter_falls_back_to_referenced_message():
    starter = msg(30, "older than the channel read")
    placeholder = msg(31, "", type=THREAD_STARTER, referenced_message=starter)
    pull = make_pull([], [thread(30)], {"30": [placeholder, msg(32, "agree")]})
    t = build_items(pull)[0]
    assert [m["id"] for m in t["messages"]] == ["30", "32"]


def test_forum_style_thread_keeps_its_own_first_message():
    pull = make_pull([], [thread(40, "Campfire")], {"40": [msg(40, "post"), msg(41, "reply")]})
    t = build_items(pull)[0]
    assert [m["id"] for m in t["messages"]] == ["40", "41"]


def test_thread_with_only_bot_messages_is_skipped():
    pull = make_pull([], [thread(50)], {"50": [msg(50, "auto", bot=True)]})
    assert build_items(pull) == []


def test_items_are_in_chronological_order():
    pull = make_pull([msg(60), msg(80)], [thread(70)], {"70": [msg(70)]})
    assert [i["id"] for i in build_items(pull)] == [
        "discord-msg:60",
        "discord-thread:70",
        "discord-msg:80",
    ]


# --- client pagination and retries -------------------------------------------------


def client_for(handler):
    return DiscordClient("tok", transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_all_messages_pages_forward_and_returns_oldest_first():
    ids = list(range(1, 251))

    def handler(request):
        after = int(request.url.params["after"])
        page = [i for i in ids if i > after][:100]
        return httpx.Response(200, json=[{"id": str(i)} for i in reversed(page)])

    with client_for(handler) as c:
        out = c.all_messages("100")
    assert [int(m["id"]) for m in out] == ids


def test_rate_limit_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"id": "100", "name": "feedback"})

    with client_for(handler) as c:
        assert c.get_channel("100")["name"] == "feedback"
    assert len(calls) == 2


def test_archived_threads_page_by_archive_timestamp():
    pages = {
        None: {
            "threads": [{"id": "2", "thread_metadata": {"archive_timestamp": "T2"}}],
            "has_more": True,
        },
        "T2": {
            "threads": [{"id": "1", "thread_metadata": {"archive_timestamp": "T1"}}],
            "has_more": False,
        },
    }

    def handler(request):
        return httpx.Response(200, json=pages[request.url.params.get("before")])

    with client_for(handler) as c:
        assert [t["id"] for t in c.all_archived_threads("100")] == ["2", "1"]


def test_author_prefers_server_nickname_then_global_name_then_username():
    nick = msg(10, author="alice")
    global_only = msg(11, author="bob")
    global_only["author"]["global_name"] = "Bobby"
    plain = msg(12, author="carol")
    original = msg(9, "question", author="alice")
    reply = msg(13, author="carol", type=REPLY, referenced_message=original)
    pull = make_pull([original, nick, global_only, plain, reply])
    pull.member_names = {"u-alice": "Alice (Camp Lead)"}

    items = by_id(build_items(pull))
    assert items["discord-msg:10"]["messages"][0]["author"] == "Alice (Camp Lead)"
    assert items["discord-msg:10"]["messages"][0]["author_username"] == "alice"
    assert items["discord-msg:11"]["messages"][0]["author"] == "Bobby"
    assert items["discord-msg:12"]["messages"][0]["author"] == "carol"
    assert items["discord-msg:13"]["context"][0]["author"] == "Alice (Camp Lead)"


def test_fetch_member_names_skips_bots_and_departed_members():
    requested = []

    def handler(request):
        uid = request.url.path.rsplit("/", 1)[-1]
        requested.append(uid)
        if uid == "u-gone":
            return httpx.Response(404, json={"message": "Unknown Member"})
        if uid == "u-nonick":
            return httpx.Response(200, json={"nick": None})
        return httpx.Response(200, json={"nick": f"Nick {uid}"})

    messages = [
        msg(1, author="alice"),
        msg(2, author="alice"),
        msg(3, author="gone"),
        msg(4, author="nonick"),
        msg(5, author="robot", bot=True),
        msg(6, author="carol", referenced_message=msg(0, author="dave")),
    ]
    with client_for(handler) as c:
        names = fetch_member_names(c, SERVER, messages)
    assert names == {"u-alice": "Nick u-alice", "u-carol": "Nick u-carol", "u-dave": "Nick u-dave"}
    assert sorted(requested) == ["u-alice", "u-carol", "u-dave", "u-gone", "u-nonick"]
