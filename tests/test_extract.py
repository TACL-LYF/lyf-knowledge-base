"""Unit tests for extraction checks, citations, programming types, and the LLM client."""

import json
from types import SimpleNamespace as NS

import pytest

from kb import extract as ext
from kb import llm as llm_mod
from kb.citations import Citations
from kb.config import Config


def cfg(**kw):
    base = dict(
        discord_server_id="1",
        discord_channel_id="2",
        drive_source_folder_ids=["s"],
        drive_kb_folder_id="k",
    )
    return Config(**{**base, **kw})


def discord_item():
    msgs = [
        {"id": "5", "author": "Ana", "timestamp": "2025-03-04T10:00:00+00:00", "content": "a"},
        {"id": "6", "author": "Bo", "timestamp": "2025-03-02T10:00:00+00:00", "content": "b"},
    ]
    return ext.discord_item(
        {
            "id": "discord-thread:5",
            "title": "#fb › thread: x",
            "messages": msgs,
            "context": [{"id": "4", "author": "Cy", "timestamp": None, "content": "old"}],
        }
    )


def rec(**kw):
    base = {
        "kind": "feedback",
        "text": "More food stalls please",
        "programming_type": None,
        "category": "feature_request",
        "sentiment": "negative",
        "info_type": None,
        "applies_to": "2023",
        "date_basis": "stated",
        "citations": ["5"],
        "quote": None,
    }
    return {**base, **kw}


def check(records, item=None, types=None):
    return ext.validate(
        {"records": records}, item or discord_item(), types or ext.ProgrammingTypes()
    )


def test_feedback_keeps_stated_date():
    (c,), problems = check([rec()])
    assert c.fields["applies_to"] == "2023" and c.fields["date_basis"] == "stated"
    assert c.message_ids == ["5"] and problems == []


def test_source_date_uses_earliest_cited_message_not_the_model():
    (c,), _ = check([rec(date_basis="source_date", applies_to="1999", citations=["5", "[6]"])])
    assert c.fields["applies_to"] == "2025-03-02"
    assert c.message_ids == ["5", "6"]


def test_bad_applies_to_falls_back_to_source_date():
    for bad in ("2023-13", "March 2023", "", "2023-02-30"):
        (c,), problems = check([rec(applies_to=bad, date_basis="inferred")])
        assert c.fields["date_basis"] == "source_date" and c.fields["applies_to"] == "2025-03-04"
        assert problems


@pytest.mark.parametrize("value", ["2024", "2024-09", "2024-09-30"])
def test_good_applies_to(value):
    assert ext.valid_applies_to(value)


def test_record_without_valid_citation_is_dropped():
    good, problems = check([rec(citations=["4"]), rec(citations=[])])
    assert good == [] and len([p for p in problems if p.endswith("dropped")]) == 2


def test_program_info_needs_a_programming_type_and_info_type():
    base = rec(kind="program_info", category=None, sentiment=None)
    good, _ = check(
        [
            {**base, "info_type": "description"},
            {**base, "programming_type": "Campfire"},
            {**base, "programming_type": "Campfire", "info_type": "history"},
        ]
    )
    assert [c.fields["info_type"] for c in good] == ["history"]
    assert good[0].fields["fact"] == "More food stalls please"


def test_drive_records_cite_the_file():
    item = ext.drive_item(
        {
            "id": "drive:f",
            "title": "Plan",
            "source": "doc",
            "modified_time": "2024-05-01T00:00:00Z",
        },
        "body",
    )
    (c,), _ = check([rec(citations=[], date_basis="source_date")], item=item)
    assert c.message_ids == [] and c.fields["applies_to"] == "2024-05-01"


def test_answer_without_records_list_raises():
    with pytest.raises(ValueError):
        ext.validate({"nope": 1}, discord_item(), ext.ProgrammingTypes())


def test_programming_types_reuse_existing_names():
    types = ext.ProgrammingTypes([{"name": "Night Market", "first_seen": None}])
    assert types.resolve("night  market") == "Night Market"
    assert types.resolve("campfire") == "Campfire"
    assert types.resolve("BBQ Social") == "BBQ Social"
    assert types.resolve("  ") is None
    assert not types.add("NIGHT MARKET", "now") and types.add("Campfire", "now")
    assert types.names == ["Night Market", "Campfire"]
    legacy = ext.ProgrammingTypes(["Dance"])  # plain names are accepted too
    assert legacy.names == ["Dance"]


def test_citation_ids_are_permanent(tmp_path):
    c = Citations()
    item = {"id": "discord-msg:5", "title": "t"}
    a = c.for_discord_message(item, {"id": "5", "content": "x", "timestamp": None}, "now")
    b = c.for_drive_file({"id": "drive:f", "title": "F", "source": "doc"}, "now")
    assert (a, b) == ("C-0001", "C-0002")
    assert c.for_discord_message(item, {"id": "5", "content": "y"}, "later") == "C-0001"
    c.mark_removed("drive:f")
    path = tmp_path / "c.jsonl"
    c.save(path)
    again = Citations.load(path)
    assert again.get("C-0002")["status"] == "removed"
    assert again.for_drive_file({"id": "drive:g", "source": "doc"}, "now") == "C-0003"
    assert again.for_drive_file({"id": "drive:f", "source": "doc"}, "now") == "C-0002"
    assert again.get("C-0002")["status"] == "active"  # the file came back


def test_split_text_keeps_paragraphs_and_size():
    text = "\n\n".join(["a" * 30, "b" * 30, "c" * 100])
    chunks = ext.split_text(text, 70)
    assert all(len(c) <= 70 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_discord_text_marks_context_and_new_messages():
    text = discord_item().text
    assert text.index("[4]") < text.index("Messages to extract:") < text.index("[5]")


def test_too_large_unless_allowed():
    item = ext.drive_item({"id": "drive:f", "title": "F"}, "x" * 4000)
    assert "max_item_tokens" in ext.too_large(item, cfg(llm_max_item_tokens=500))
    assert ext.too_large(item, cfg(llm_max_item_tokens=500, drive_allow_large_files=["f"])) is None


def test_cost_and_batch_discount():
    assert llm_mod.cost("claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(6.0)
    assert llm_mod.cost("claude-opus-5-5", 1_000_000, 100_000, batch=True) == pytest.approx(3.0)
    assert llm_mod.cost("mystery-model", 1, 1) is None
    u = llm_mod.Usage(model="mystery-model")
    u.add({"input_tokens": 1, "output_tokens": 1}, batch=False)
    assert u.cost_usd is None and "unknown" in u.line()


def test_estimate_counts_requests_and_skips():
    small = ext.drive_item({"id": "drive:a", "title": "A"}, "x" * 400)
    big = ext.drive_item({"id": "drive:b", "title": "B"}, "x" * 40_000)
    e = ext.estimate([small, big], ["Night Market"], cfg(llm_max_item_tokens=1000))
    assert e.requests == 1 and [i["id"] for i in e.skipped] == ["drive:b"]
    assert e.input_tokens > 100 and e.cost_usd > 0 and not e.batch


# ---------------------------------------------------------------------------
# AnthropicLLM against a fake SDK client
# ---------------------------------------------------------------------------


def message(text, stop="end_turn"):
    return NS(
        content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
        stop_reason=stop,
        stop_details=None,
        usage=NS(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeBatches:
    def __init__(self):
        self.created = []
        self.polls = 0

    def create(self, requests):
        self.created.append(requests)
        self.ids = [r["custom_id"] for r in requests]
        return NS(id="batch_1", processing_status="in_progress")

    def retrieve(self, batch_id):
        self.polls += 1
        counts = NS(processing=0, succeeded=len(self.ids), errored=0)
        return NS(id=batch_id, processing_status="ended", request_counts=counts)

    def results(self, batch_id):
        for cid in self.ids:
            if cid.endswith("bad"):
                err = NS(type="error", error=NS(type="api_error", message="overloaded"))
                yield NS(custom_id=cid, result=NS(type="errored", error=err))
            else:
                yield NS(custom_id=cid, result=NS(type="succeeded", message=message('{"ok":1}')))


class FakeClient:
    def __init__(self):
        self.batches = FakeBatches()
        self.streamed = []
        self.messages = NS(stream=self.stream, batches=self.batches)

    def stream(self, **params):
        self.streamed.append(params)
        stop = "max_tokens" if "long" in params["messages"][0]["content"] else "end_turn"
        return FakeStream(message('{"records": []}', stop))


def reqs(*ids):
    return [llm_mod.Request(i, "sys", f"user {i}", {"type": "object"}) for i in ids]


def test_direct_requests_use_structured_output_and_effort():
    client = FakeClient()
    model = llm_mod.AnthropicLLM(
        "k", "claude-opus-5-5", effort="high", client=client, batch_min_requests=5
    )
    out = model.run(reqs("a", "long"), lambda _: None)
    assert out["a"].text == '{"records": []}' and out["a"].usage["input_tokens"] == 10
    assert out["long"].error == "output hit max_tokens"
    p = client.streamed[0]
    assert p["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}},
        "effort": "high",
    }
    assert p["model"] == "claude-opus-5-5" and "thinking" not in p


def test_large_runs_go_through_the_batch_api(tmp_path):
    client = FakeClient()
    pending = tmp_path / "pending.json"
    model = llm_mod.AnthropicLLM(
        "k",
        "claude-opus-5-5",
        client=client,
        batch_min_requests=2,
        sleep=lambda _: None,
        pending_batch_path=pending,
    )
    out = model.run(reqs("a", "bbad"), lambda _: None)
    assert out["a"].text == '{"ok":1}' and out["a"].batch
    assert "overloaded" in out["bbad"].error
    assert client.streamed == [] and len(client.batches.created) == 1
    assert not pending.exists()  # cleared once the results are in


def test_interrupted_batch_is_picked_up_again(tmp_path):
    client = FakeClient()
    client.batches.ids = ["a", "b"]
    pending = tmp_path / "pending.json"
    pending.write_text(json.dumps({"batch_id": "batch_9", "custom_ids": ["a", "b", "c"]}))
    model = llm_mod.AnthropicLLM(
        "k",
        "m",
        client=client,
        batch_min_requests=1,
        sleep=lambda _: None,
        pending_batch_path=pending,
    )
    out = model.run(reqs("a", "b"), lambda _: None)
    assert client.batches.created == [] and set(out) == {"a", "b"}


def test_cached_llm_reuses_answers_and_forgets_bad_ones(tmp_path):
    class Counting:
        model = "m"
        calls = 0

        def run(self, requests, log):
            self.calls += len(requests)
            return {r.custom_id: llm_mod.Response(r.custom_id, text="{}") for r in requests}

    inner = Counting()
    cached = llm_mod.CachedLLM(inner, tmp_path)
    cached.run(reqs("a"), lambda _: None)
    assert cached.run(reqs("a"), lambda _: None)["a"].text == "{}" and inner.calls == 1
    cached.forget("a")
    cached.run(reqs("a"), lambda _: None)
    assert inner.calls == 2


def test_request_id_is_stable_and_batch_safe():
    a = llm_mod.request_id("m", None, "s", "u", {"x": 1})
    assert a == llm_mod.request_id("m", None, "s", "u", {"x": 1})
    assert a != llm_mod.request_id("m", "high", "s", "u", {"x": 1})
    assert len(a) <= 64 and a.isalnum()


def test_prompt_file_exists_for_the_current_version():
    assert "programming" in ext.system_prompt()
