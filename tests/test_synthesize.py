"""Synthesis: preparing records, checking answers, reuse between runs, rendering, and
publishing (with the fake model and publisher in synth_fakes.py)."""

import json

import pytest
from synth_fakes import FakePublisher, FakeSynthLLM, markdown_to_doc

from kb import publish as pub
from kb import render
from kb import synthesize as synth
from kb.citations import Citations
from kb.config import Config
from kb.extract import ProgrammingTypes, Records, Sessions, Store

NOW = "2026-11-05T09:00:00Z"


def cfg(**kw):
    base = dict(
        discord_server_id="1",
        discord_channel_id="2",
        drive_source_folder_ids=["s"],
        drive_kb_folder_id="k",
        llm_batch_min_requests=1000,
    )
    return Config(**{**base, **kw})


def cite(cid, date="2024-05-01T10:00:00Z", status="active", author="Ana"):
    return {
        "id": cid,
        "key": f"k-{cid}",
        "source": "discord",
        "link": f"https://discord.com/channels/1/2/{cid}",
        "author": author,
        "date": date,
        "context": "#feedback",
        "status": status,
    }


def fb(rid, text, ptype=None, applies_to="2024", cites=None, session=None, **kw):
    return {
        "record_id": rid,
        "item_id": f"item-{rid}",
        "kind": "feedback",
        "feedback": text,
        "programming_type": ptype,
        "session": session,
        "category": kw.pop("category", "other"),
        "sentiment": "negative",
        "applies_to": applies_to,
        "date_basis": kw.pop("date_basis", "stated"),
        "quote": kw.pop("quote", None),
        "citations": cites or [f"C-{rid[2:]}"],
        "first_seen": kw.pop("first_seen", "2026-11-01T00:00:00Z"),
        **kw,
    }


def fact(rid, text, ptype, applies_to="2024", info_type="description", session=None, cites=None):
    return {
        "record_id": rid,
        "item_id": f"item-{rid}",
        "kind": "program_info",
        "fact": text,
        "info_type": info_type,
        "programming_type": ptype,
        "session": session,
        "applies_to": applies_to,
        "date_basis": "inferred",
        "quote": None,
        "citations": cites or [f"C-{rid[2:]}"],
        "first_seen": "2026-11-01T00:00:00Z",
    }


def make_store(rows, types=(), sessions=(), removed=()):
    cids = sorted({c for r in rows for c in r["citations"]}, key=lambda c: int(c.split("-")[1]))
    citations = Citations([cite(c, status="removed" if c in removed else "active") for c in cids])
    names = list(types) or sorted({r["programming_type"] for r in rows if r["programming_type"]})
    return Store(
        records=Records(rows),
        citations=citations,
        types=ProgrammingTypes([{"name": n, "first_seen": None} for n in names]),
        sessions=Sessions(list(sessions)),
    )


def corpus_of(store, per_session=("Workshop",), year=2026):
    return synth.prepare(
        store.records, store.citations, store.types, store.sessions, per_session, year
    )


def run(store, saved=None, llm=None, **kw):
    saved = saved if saved is not None else synth.SynthState()
    llm = llm or FakeSynthLLM()
    syn = synth.run(
        store.records,
        store.citations,
        store.types,
        store.sessions,
        saved,
        llm,
        cfg(**kw),
        2026,
    )
    return syn, saved, llm


# ---------------------------------------------------------------------------
# Programming type merges
# ---------------------------------------------------------------------------


def test_merged_type_names_resolve_to_the_canonical_name():
    types = ProgrammingTypes([{"name": "Families"}, {"name": "Family"}, {"name": "Dance"}])
    types.set_merges({"Family": "Families"})
    assert types.resolve("family") == "Families"
    assert types.canonical_names == ["Families", "Dance"]
    assert types.merges == {"Family": "Families"}
    assert not types.add("FAMILY", NOW)  # already known, as an alias
    types.set_merges({})
    assert types.resolve("family") == "Family" and "merged_into" not in types.rows[1]


def test_merge_answers_are_checked():
    types = ProgrammingTypes(
        [{"name": n} for n in ("Families", "Family", "Family Time", "Workshop", "Workshops")]
    )
    raw = {
        "merges": [
            {"alias": "family", "canonical": "Family Time", "reason": ""},
            {"alias": "Family Time", "canonical": "Families", "reason": ""},  # chain
            {"alias": "Workshop", "canonical": "Workshops", "reason": ""},  # per-session alias
            {"alias": "Nope", "canonical": "Families", "reason": ""},  # unknown
            {"alias": "Families", "canonical": "Families", "reason": ""},  # itself
        ]
    }
    assert synth.check_merges(raw, types, ["Workshop"]) == {
        "Family": "Families",
        "Family Time": "Families",
    }


# ---------------------------------------------------------------------------
# Preparing records
# ---------------------------------------------------------------------------


def test_prepare_drops_records_whose_sources_are_all_removed_and_maps_merges():
    rows = [
        fb("R-1", "gone", "Family", cites=["C-1"]),
        fb("R-2", "half gone", "Family", cites=["C-1", "C-2"]),
    ]
    store = make_store(rows, removed={"C-1"})
    store.types.rows.append({"name": "Families"})
    store.types.set_merges({"Family": "Families"})
    corpus = corpus_of(store)
    assert list(corpus.records) == ["R-2"]
    assert corpus.records["R-2"]["programming_type"] == "Families"
    assert store.records.rows[1]["programming_type"] == "Family"  # saved records untouched


def test_prepare_matches_workshop_feedback_to_the_session_held_that_day():
    sessions = [
        {
            "programming_type": "Workshop",
            "name": "Active Listening",
            "year": "2025",
            "date": "2025-03-14",
        },
        {"programming_type": "Workshop", "name": "Budgeting", "year": "2025", "date": "2025-04-01"},
        {"programming_type": "Workshop", "name": "Cooking", "year": "2025", "date": "2025-04-01"},
    ]
    rows = [
        fb("R-1", "today's workshop was great", "Workshop", applies_to="2025-03-14"),
        fb("R-2", "two workshops that day", "Workshop", applies_to="2025-04-01"),
        fb("R-3", "workshops always run long", "Workshop", applies_to="2025"),
    ]
    corpus = corpus_of(make_store(rows, sessions=sessions))
    assert corpus.records["R-1"]["session"] == "Active Listening"
    assert corpus.records["R-2"]["session"] is None  # ambiguous
    assert corpus.records["R-3"]["session"] is None


def test_ranking_weights_recent_feedback_higher():
    rows = [fb(f"R-{i}", "old", "Dance", applies_to="2020") for i in range(1, 5)]
    rows += [fb("R-9", "new", "Dance", applies_to="2026")]
    corpus = corpus_of(make_store(rows))
    old = corpus.score(["R-1", "R-2", "R-3", "R-4"])
    assert corpus.score(["R-9"]) == 1.0 and old == pytest.approx(4 * 0.5**3)
    assert corpus.years(["R-1", "R-9"]) == {"2020": 1, "2026": 1}


# ---------------------------------------------------------------------------
# Checking answers
# ---------------------------------------------------------------------------


def _type_unit(store, ptype):
    corpus = corpus_of(store)
    units = synth.build_units(corpus, synth.SynthState(), "m")
    return next(u for u in units if u.key == f"type:{ptype}"), corpus


def test_answers_keep_only_the_units_records_of_the_right_kind():
    rows = [
        fb("R-1", "Food ran out", "Night Market", applies_to="2023"),
        fact("R-2", "One night, courtyard", "Night Market", applies_to="2023"),
        fact("R-3", "Three nights, north lot", "Night Market", applies_to="2024"),
        fb("R-4", "Other type", "Dance"),
    ]
    unit, corpus = _type_unit(make_store(rows), "Night Market")
    raw = {
        "summary": "s",
        "working": [{"text": "x", "record_ids": ["R-4"]}],  # another type's record
        "improve": [],
        "themes": [
            {
                "name": "Food",
                "description": "d",
                "record_ids": ["R-1", "R-2", "R-99"],
                "likely_resolved": {"change": "Moved to three nights", "record_ids": ["R-3"]},
            }
        ],
        "requests": [],
        "open_questions": [],
        "description": [{"text": "Three nights", "record_ids": ["[R-3]"]}],
        "history": [
            {
                "period": "2024",
                "text": "One night → three",
                "basis": "found",
                "record_ids": ["R-2", "R-3"],
            }
        ],
        "guidance": [],
        "feedback_by_period": [],
    }
    out, problems = synth.check(unit, raw, corpus)
    assert out["working"] == []
    assert out["themes"][0]["record_ids"] == ["R-1"]  # the fact and unknown ID dropped
    assert out["themes"][0]["likely_resolved"]["record_ids"] == ["R-3"]
    assert out["description"] == [{"text": "Three nights", "record_ids": ["R-3"]}]
    assert out["history"][0]["basis"] == "found"
    assert out["history"][0]["record_ids"] == ["R-2", "R-3"]
    assert any("R-99" in p for p in problems) and any("R-4" in p for p in problems)


def test_likely_resolved_is_dropped_when_feedback_continues_after_the_change():
    rows = [
        fb("R-1", "Food ran out", "Night Market", applies_to="2025"),
        fact("R-3", "Three nights", "Night Market", applies_to="2024", info_type="history"),
    ]
    unit, corpus = _type_unit(make_store(rows), "Night Market")
    theme = {
        "name": "Food",
        "description": "",
        "record_ids": ["R-1"],
        "likely_resolved": {"change": "three nights", "record_ids": ["R-3"]},
    }
    out, problems = synth.check(unit, {"themes": [theme]}, corpus)
    assert out["themes"][0]["likely_resolved"] is None
    assert any("likely resolved" in p for p in problems)


# ---------------------------------------------------------------------------
# Running and reuse
# ---------------------------------------------------------------------------


def base_rows():
    return [
        fb("R-1", "Food ran out", "Night Market", applies_to="2023", quote="ran out by 8pm"),
        fact("R-2", "One night in the courtyard", "Night Market", applies_to="2023"),
        fact("R-3", "Three nights in the north lot", "Night Market", applies_to="2024"),
        fb("R-4", "Too loud", "Dance"),
        fb("R-5", "Schedules came late", None),
    ]


def test_first_run_asks_every_unit_then_reuses_them():
    store = make_store(base_rows())
    syn, saved, llm = run(store)
    assert llm.keys() == ["merge", "type", "type", "themes", "summary"]
    assert set(syn.called) == {"merge", "type:Dance", "type:Night Market", "themes", "summary"}
    assert syn.summary[0]["text"] == "The main takeaway"

    syn2, _, llm2 = run(store, saved)
    assert llm2.requests == [] and syn2.called == []
    assert syn2.outputs == syn.outputs


def test_a_new_record_only_re_synthesizes_its_type_and_the_summary():
    store = make_store(base_rows())
    _, saved, _ = run(store)
    store.records.rows.append(fb("R-6", "Even louder", "Dance", cites=["C-6"]))
    store.citations = Citations([*store.citations.rows, cite("C-6")])
    syn, _, llm = run(store, saved)
    assert llm.keys() == ["type", "summary"]
    assert "Programming type: Dance" in llm.requests[0].user
    assert "Theme names from the previous run (reuse where they still fit): Main Theme" in (
        llm.requests[0].user
    )
    assert "type:Night Market" in syn.reused


def test_a_failed_unit_shows_its_last_good_answer_and_is_tried_again():
    store = make_store(base_rows())
    _, saved, _ = run(store)
    old_hash = saved.units["type:Dance"]["hash"]
    store.records.rows.append(fb("R-6", "Even louder", "Dance", cites=["C-6"]))
    store.citations = Citations([*store.citations.rows, cite("C-6")])
    llm = FakeSynthLLM()
    llm.fail = ["Programming type: Dance"]
    syn, saved, _ = run(store, saved, llm)
    assert "type:Dance" in syn.failed and syn.stale == ["type:Dance"]
    assert syn.outputs["type:Dance"]["themes"][0]["record_ids"] == ["R-4"]
    assert saved.units["type:Dance"]["hash"] == old_hash  # not marked done
    assert syn.unthemed("type:Dance", ["R-4", "R-6"]) == ["R-6"]  # shown as unthemed

    syn, _, llm = run(store, saved)
    assert llm.keys()[0] == "type" and not syn.failed


def test_merges_are_saved_and_regroup_records():
    rows = [fb("R-1", "a", "Families"), fb("R-2", "b", "Family"), fb("R-3", "c", "Family Time")]
    store = make_store(rows)
    llm = FakeSynthLLM()
    llm.merges = [("Family", "Families"), ("Family Time", "Families")]
    syn, saved, _ = run(store, llm=llm)
    assert store.types.merges == {"Family": "Families", "Family Time": "Families"}
    assert syn.corpus.types() == ["Families"]
    assert syn.outputs["type:Families"]["themes"][0]["record_ids"] == ["R-1", "R-2", "R-3"]
    # The merge step is reused while the names stay the same.
    _, _, llm2 = run(store, saved)
    assert "merge" not in llm2.keys()


def workshop_store():
    sessions = [
        {
            "programming_type": "Workshop",
            "name": "Active Listening",
            "year": "2024",
            "date": "2024-03-14",
        },
        {"programming_type": "Workshop", "name": "Budgeting", "year": "2025", "date": "2025-04-01"},
    ]
    rows = [
        fact("R-1", "Workshops last an hour", "Workshop", applies_to="2023"),
        fact(
            "R-2",
            "Covered listening skills",
            "Workshop",
            applies_to="2024",
            session="Active Listening",
        ),
        fb(
            "R-3",
            "Loved active listening",
            "Workshop",
            applies_to="2024",
            session="Active Listening",
        ),
        fact("R-4", "Covered budgets", "Workshop", applies_to="2025", session="Budgeting"),
        fb("R-5", "Today's workshop was great", "Workshop", applies_to="2025-04-01"),
        fb("R-6", "Workshops always run long", "Workshop", applies_to="2025"),
        fb("R-7", "That workshop was confusing", "Workshop", applies_to="2025-06-02"),
    ]
    return make_store(rows, sessions=sessions)


def test_sessions_are_synthesized_by_year_and_kept_out_of_the_general_themes():
    syn, _, llm = run(workshop_store())
    keys = sorted(k for k in syn.outputs if k.startswith("sessions:"))
    assert keys == ["sessions:Workshop:2024", "sessions:Workshop:2025"]
    y2025 = syn.outputs["sessions:Workshop:2025"]
    budgeting = y2025["sessions"][0]
    assert budgeting["name"] == "Budgeting"
    assert budgeting["feedback_record_ids"] == ["R-5"]  # matched by date in code
    assert y2025["unmatched"]["record_ids"] == ["R-7"]
    # The general unit only saw general records, and themes leave out claimed ones.
    general = next(r for r in llm.requests if "Programming type: Workshop (per-session" in r.user)
    assert "R-2" not in general.user and "R-5" not in general.user
    themes = syn.themes("type:Workshop")
    assert [t["record_ids"] for t in themes] == [["R-6"]]  # R-7 went to "Other … feedback"


def test_recurring_points_are_lifted_into_general_themes_and_guidance():
    def custom(system, user):
        if "one year of a per-session" in system and "Year: 2024" in user:
            return {
                "sessions": [],
                "unmatched": {"summary": None, "record_ids": []},
                "recurring": [
                    {"text": "Sessions ran over", "kind": "feedback", "record_ids": ["R-3"]},
                    {"text": "Send reading ahead", "kind": "guidance", "record_ids": ["R-2"]},
                ],
            }
        return None

    llm = FakeSynthLLM()
    llm.custom = custom
    syn, _, _ = run(workshop_store(), llm=llm)
    names = [t["name"] for t in syn.themes("type:Workshop")]
    assert "Across 2024 sessions: Sessions ran over" in names
    assert syn.guidance("Workshop")[-1]["text"] == "Send reading ahead (across 2024 sessions)"


def test_fingerprint_changes_with_records_citations_and_merges():
    store = make_store(base_rows())
    c = cfg()
    fp = synth.fingerprint(store.records, store.citations, store.types, c)
    assert fp == synth.fingerprint(store.records, store.citations, store.types, c)
    store.citations.rows[0]["status"] = "removed"
    fp2 = synth.fingerprint(store.records, store.citations, store.types, c)
    assert fp2 != fp
    store.types.rows.append({"name": "Nm", "merged_into": "Night Market"})
    assert synth.fingerprint(store.records, store.citations, store.types, c) != fp2


def test_plan_counts_only_changed_units():
    store = make_store(base_rows())
    p = synth.plan(
        store.records, store.citations, store.types, store.sessions, synth.SynthState(), cfg(), 2026
    )
    assert {u.key for u in p.todo} == {"merge", "type:Dance", "type:Night Market", "themes"}
    assert p.input_tokens > 0 and p.cost_usd is not None
    _, saved, _ = run(store)
    p = synth.plan(store.records, store.citations, store.types, store.sessions, saved, cfg(), 2026)
    assert p.todo == [] and p.input_tokens == 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

LINKS = render.Links("FB", "PG", "SH")


def test_feedback_doc_has_cited_themes_quotes_and_an_appendix():
    rows = base_rows()
    rows.append(fb("R-6", "Gone now", "Dance", cites=["C-6"]))
    store = make_store(rows, removed={"C-7"})
    store.records.rows[5]["citations"] = ["C-6", "C-7"]
    store.citations = Citations([*store.citations.rows, cite("C-7", status="removed")])
    syn, _, _ = run(store)
    md = render.feedback_doc(syn, LINKS, updated=NOW, by="ana@pc", since=None)
    assert "Covers 2023–2024" in md
    assert "This is the first update" in md
    assert "## Night Market" in md and "### Main Theme" in md
    assert "[\\[C-1\\]](https://discord.com/channels/1/2/C-1)" in md
    assert "\\[C-7\\] (removed)" in md
    assert "> “ran out by 8pm” — Ana, 2023" in md
    assert "*1 citation · 2023: 1 · last mentioned 2024-05-01*" in md
    assert "# Other themes" in md and "## Loose Theme" in md
    assert (
        "[Night Market in Programs](https://docs.google.com/document/d/PG/edit#kb=h1/night-market)"
        in md
    )
    appendix = md.split("# Appendix: citations used in this doc", 1)[1]
    assert "Discord links open only for members" in appendix
    assert (
        "- [\\[C-1\\]](https://discord.com/channels/1/2/C-1) · Ana · 2024-05-01 · \\#feedback"
        in appendix
    )
    assert "- \\[C-7\\] (removed) · Ana" in appendix


def test_new_since_last_update_lists_records_first_seen_after_the_last_publish():
    rows = base_rows()
    rows.append(
        fb("R-6", "Brand new point", "Dance", cites=["C-6"], first_seen="2026-11-05T09:00:00Z")
    )
    syn, _, _ = run(make_store(rows))
    md = render.feedback_doc(syn, LINKS, updated=NOW, by="x", since="2026-11-02T00:00:00Z")
    new = md.split("# New since last update", 1)[1].split("# Executive summary", 1)[0]
    assert "1 new piece of feedback" in new
    assert "**Dance** (2024): Brand new point [\\[C-6\\]]" in new
    assert "Food ran out" not in new


def test_likely_resolved_themes_are_labeled_and_listed_last():
    rows = [
        fb("R-1", "Sold out early", "Night Market", applies_to="2023"),
        fb("R-2", "Parking is hard", "Night Market", applies_to="2023"),
        fact(
            "R-3", "Moved to three nights", "Night Market", applies_to="2024", info_type="history"
        ),
    ]

    def custom(system, user):
        if "one programming type's sections" not in system:
            return None
        return {
            "summary": "s",
            "working": [],
            "improve": [],
            "themes": [
                {
                    "name": "Sold Out",
                    "description": "Food sold out.",
                    "record_ids": ["R-1"],
                    "likely_resolved": {"change": "Three nights since 2024", "record_ids": ["R-3"]},
                },
                {
                    "name": "Parking",
                    "description": "",
                    "record_ids": ["R-2"],
                    "likely_resolved": None,
                },
            ],
            "requests": [],
            "open_questions": [],
            "description": [],
            "history": [
                {
                    "period": "2024",
                    "text": "One night became three",
                    "basis": "found",
                    "record_ids": ["R-3"],
                }
            ],
            "guidance": [],
            "feedback_by_period": [],
        }

    llm = FakeSynthLLM()
    llm.custom = custom
    syn, _, _ = run(make_store(rows), llm=llm)
    md = render.feedback_doc(syn, LINKS, updated=NOW, by="x", since=None)
    assert md.index("### Parking") < md.index("### Sold Out (likely resolved)")
    assert (
        "**Likely resolved:** the feedback stopped after this change: Three nights since 2024" in md
    )
    programs = render.programs_doc(syn, LINKS, updated=NOW, by="x")
    assert "**2024**: One night became three [\\[C-3\\]]" in programs
    assert "*(found by comparing sources)*" in programs


def test_programs_doc_has_an_index_history_and_sessions_by_year():
    syn, _, _ = run(workshop_store())
    md = render.programs_doc(syn, LINKS, updated=NOW, by="x")
    assert "- [Workshop](https://docs.google.com/document/d/PG/edit#kb=h1/workshop)" in md
    assert "## By year" in md
    assert md.index("### 2025") < md.index("### 2024")  # newest first
    assert "#### Budgeting (2025-04-01)" in md and "#### Active Listening (2024-03-14)" in md
    assert "#### Other workshop feedback, 2025" in md
    # Session content isn't part of the general history.
    history = md.split("## History", 1)[1].split("##", 1)[0]
    assert "Covered" not in history


def test_text_from_records_is_escaped():
    assert render.esc("a *b* [c](d) #1 <x>") == "a \\*b\\* \\[c\\](d) \\#1 \\<x\\>"


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_heading_links_are_rewritten_to_heading_ids():
    doc = "https://docs.google.com/document/d/"
    md_a = (
        f"# Index\n\n- [Dance]({doc}A/edit#kb=h1/dance)\n\n# Dance\n\n"
        f"text [x]({doc}B/edit#kb=h2/missing)\n"
    )
    md_b = "## Other\n"
    a, b = markdown_to_doc(md_a), markdown_to_doc(md_b)
    targets = {"A": pub.headings(a), "B": pub.headings(b)}
    assert targets["A"] == {"h1/index": "h.1", "h1/dance": "h.2"}
    requests, missing = pub.link_fixes(a, targets)
    urls = [r["updateTextStyle"]["textStyle"]["link"]["url"] for r in requests]
    assert urls == [
        "https://docs.google.com/document/d/A/edit#heading=h.2",
        "https://docs.google.com/document/d/B/edit",  # heading not found: top of the doc
    ]
    assert missing == 1


def test_publish_creates_the_files_once_and_updates_them_in_place():
    store = make_store(base_rows())
    syn, saved, _ = run(store)
    fake = FakePublisher()
    r = pub.publish(syn, store, saved, fake, now=NOW, by="ana@pc")
    assert sorted(r.created) == ["Citations", "Consolidated Feedback", "Programs"]
    assert set(saved.docs) == {"feedback", "programs", "citations"}
    links = dict(fake.links("Programs"))
    assert (
        links["Night Market"]
        == f"https://docs.google.com/document/d/{saved.docs['programs']}/edit#heading=h.2"
    )
    fb_links = fake.links("Consolidated Feedback")
    assert ("Night Market in Programs", links["Night Market"]) in fb_links
    assert r.links_missing == 0

    tabs = fake.by_name("Citations")["tabs"]
    assert tabs["Citations"][0][0] == "ID" and len(tabs["Citations"]) == 6
    assert isinstance(tabs["Citations"][1][7], render.Link)
    assert [row[6] for row in tabs["Records"][1:]] == ["2023", "2023", "2024", "2024", "2024"]

    before = dict(saved.docs)
    r2 = pub.publish(syn, store, saved, fake, now=NOW, by="ana@pc")
    assert r2.created == [] and saved.docs == before and len(fake.files) == 3


def test_publish_finds_files_by_name_and_replaces_trashed_ones():
    store = make_store(base_rows())
    syn, saved, _ = run(store)
    fake = FakePublisher()
    existing = fake.create("Consolidated Feedback", pub.DOC_MIME)
    pub.publish(syn, store, saved, fake, now=NOW, by="x")
    assert saved.docs["feedback"] == existing  # found by name, not duplicated
    fake.files[saved.docs["programs"]]["trashed"] = True
    r = pub.publish(syn, store, saved, fake, now=NOW, by="x")
    assert r.replaced == ["Programs"] and r.created == ["Programs"]


def test_sheet_cells_keep_text_as_text_and_links_clickable():
    assert (
        pub.sheet_cell(render.Link('https://x/"q"', "C-1")) == '=HYPERLINK("https://x/""q""","C-1")'
    )
    assert pub.sheet_cell("=SUM(A1)") == "'=SUM(A1)"
    assert pub.sheet_cell("-1 for this") == "'-1 for this"
    assert pub.sheet_cell("2024") == "'2024" and pub.sheet_cell("2024-09") == "'2024-09"
    assert pub.sheet_cell("2024-09-01") == "'2024-09-01" and pub.sheet_cell("plain") == "plain"


def test_synth_state_round_trips(tmp_path):
    s = synth.SynthState(units={"type:X": {"hash": "h", "output": {}}}, docs={"feedback": "F"})
    s.last_published_at = NOW
    s.save(tmp_path / "s.json")
    back = synth.SynthState.load(tmp_path / "s.json")
    assert back.units == s.units and back.docs == s.docs and back.last_published_at == NOW
    assert json.loads((tmp_path / "s.json").read_text())["docs"] == {"feedback": "F"}


def test_a_session_listed_twice_is_merged():
    store = workshop_store()

    def custom(system, user):
        if "one year of a per-session" in system and "Year: 2024" in user:
            entry = {
                "name": "Active Listening",
                "covered": [{"text": "Listening", "record_ids": ["R-2"]}],
                "guidance": [],
                "feedback_summary": None,
                "feedback_record_ids": [],
            }
            second = {
                **entry,
                "covered": [],
                "feedback_summary": "Loved",
                "feedback_record_ids": ["R-3"],
            }
            return {
                "sessions": [entry, second],
                "unmatched": {"summary": None, "record_ids": []},
                "recurring": [],
            }
        return None

    llm = FakeSynthLLM()
    llm.custom = custom
    syn, _, _ = run(store, llm=llm)
    (s,) = syn.outputs["sessions:Workshop:2024"]["sessions"]
    assert s["feedback_record_ids"] == ["R-3"] and s["feedback_summary"] == "Loved"
    assert len(s["covered"]) == 1
