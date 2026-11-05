"""Render a synthesis as markdown for the two docs, and as rows for the Citations sheet.

Every feedback point and program fact ends with citation markers such as `[C-0042]`,
each a markdown link to the citation's saved link (the Discord message or the Drive
file), so the markdown upload turns them into clickable links. A removed citation is
plain text with "(removed)". Each doc ends with an appendix of the citations it uses.

Links to a heading (the Programs index, and the links between the two docs) are
written as `<doc url>#kb=h<level>/<slug>`. Google assigns heading IDs only when the
markdown is imported, so after uploading, `publish.fix_anchors` rewrites each of these
to the real `#heading=h.…` link. If that step can't run, the links still open the
right doc.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from kb.synthesize import Synthesis

ANCHOR = "#kb="
NEW_LIST_LIMIT = 100  # "New since last update" lists at most this many feedback records
QUOTES_PER_THEME = 3

ACCESS_NOTE = (
    "Discord links open only for members of the server who can see the channel, and Drive "
    "links follow each file's sharing settings."
)


def doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


_ESCAPE = re.compile(r"([\\`*_\[\]<>#|~])")


def esc(text: str) -> str:
    """Escape text from records and the model so markdown shows it as written."""
    return _ESCAPE.sub(r"\\\1", " ".join(str(text).split()))


@dataclass
class Links:
    feedback_id: str
    programs_id: str
    sheet_id: str

    def anchor(self, doc: str, level: int, heading: str) -> str:
        doc_id = self.feedback_id if doc == "feedback" else self.programs_id
        return f"{doc_url(doc_id)}{ANCHOR}h{level}/{slug(heading)}"


@dataclass
class Doc:
    """One doc being written: its lines and the citations it has used."""

    syn: Synthesis
    lines: list[str] = field(default_factory=list)
    used: set[str] = field(default_factory=set)

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def heading(self, level: int, text: str) -> None:
        self.blank()
        self.lines.append(f"{'#' * level} {esc(text)}")
        self.lines.append("")

    def marker(self, cid: str) -> str:
        self.used.add(cid)
        c = self.syn.corpus.citations.get(cid) or {}
        if c.get("status") == "removed" or not c.get("link"):
            return f"\\[{cid}\\] (removed)" if c.get("status") == "removed" else f"\\[{cid}\\]"
        return f"[\\[{cid}\\]]({c['link']})"

    def markers(self, rids: Iterable[str]) -> str:
        return " ".join(self.marker(c) for c in self.syn.corpus.citation_ids(rids))

    def point(self, p: dict[str, Any], prefix: str = "") -> str:
        return f"- {prefix}{esc(p['text'])} {self.markers(p['record_ids'])}".rstrip()

    def points(self, points: list[dict[str, Any]]) -> None:
        for p in points:
            self.add(self.point(p))

    def text(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


def _years(counts: dict[str, int]) -> str:
    return " · ".join(f"{y}: {n}" for y, n in counts.items())


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def appendix(d: Doc) -> None:
    d.heading(1, "Appendix: citations used in this doc")
    d.add(ACCESS_NOTE, "")
    cites = d.syn.corpus.citations
    for cid in sorted(d.used, key=lambda c: int(c.split("-")[1])):
        c = cites.get(cid) or {}
        parts = [
            esc(c.get("author") or "unknown"),
            (c.get("date") or "")[:10] or "no date",
            esc(c.get("context") or ""),
        ]
        if c.get("status") == "removed":
            head = f"\\[{cid}\\] (removed)"
        elif c.get("link"):
            head = f"[\\[{cid}\\]]({c['link']})"
        else:
            head = f"\\[{cid}\\]"
        d.add(f"- {head} · " + " · ".join(p for p in parts if p))


# ---------------------------------------------------------------------------
# Consolidated Feedback
# ---------------------------------------------------------------------------


def feedback_doc(
    syn: Synthesis,
    links: Links,
    *,
    updated: str,
    by: str,
    since: str | None,
) -> str:
    """The Consolidated Feedback doc. `since`: the last publish (None on the first)."""
    d = Doc(syn)
    corpus = syn.corpus
    fb = [r for r in corpus.records.values() if r["kind"] == "feedback"]
    years = sorted(str(r["applies_to"])[:4] for r in fb)
    span = f"{years[0]}–{years[-1]}" if years else "no feedback yet"
    sources = len({r["item_id"] for r in fb})
    cites = len(corpus.citation_ids(r["record_id"] for r in fb))
    d.add(
        f"Last updated {updated[:10]} by {esc(by)} · Covers {span} (the period feedback is "
        f"about) · {_plural(len(fb), 'piece')} of feedback from {_plural(sources, 'source')}, "
        f"{_plural(cites, 'citation')}",
        "",
        f"[Programs]({doc_url(links.programs_id)}): what each programming type is, its "
        f"history, and how to run it · [Citations sheet]({sheet_url(links.sheet_id)}): every "
        "citation, and every record with filters by year, type, and category",
    )
    if syn.failed:
        d.add(
            "",
            f"Note: {_plural(len(syn.failed), 'section')} couldn't be regrouped this run "
            "and show the last good version, or the feedback as saved. The next update "
            "tries again.",
        )

    _new_section(d, since)

    d.heading(1, "Executive summary")
    if syn.summary:
        for t in syn.summary:
            refs = []
            for ref in t["themes"]:
                key, name = syn.theme_refs.get(ref, ("", ""))
                if key.startswith("type:"):
                    ptype = key.removeprefix("type:")
                    refs.append(
                        f"[{esc(ptype)}]({links.anchor('feedback', 2, ptype)}) › {esc(name)}"
                    )
                elif key == "themes":
                    other = links.anchor("feedback", 1, "Other themes")
                    refs.append(f"[Other themes]({other}) › {esc(name)}")
            see = f" (see {'; '.join(refs)})" if refs else ""
            d.add(f"- {esc(t['text'])}{see}")
    else:
        d.add("No summary this run.")

    d.heading(1, "Programming types")
    types = syn.feedback_types()
    if not types:
        d.add("No feedback about a programming type yet.")
    for ptype in types:
        _type_section(d, links, ptype)

    d.heading(1, "Other themes")
    d.add("Feedback that isn't about one programming type.")
    loose = [r["record_id"] for r in fb if not r.get("programming_type")]
    if not loose:
        d.blank()
        d.add("None yet.")
    else:
        _themes(d, "themes", 2, loose)

    d.heading(1, "Feature requests and bugs")
    requests = syn.requests()
    for cat, title in (("feature_request", "Feature requests"), ("bug", "Bugs")):
        d.heading(2, title)
        rows = [q for q in requests if q["category"] == cat]
        if not rows:
            d.add("None.")
        for q in rows:
            where = f"**{esc(q['programming_type'])}**: " if q["programming_type"] else ""
            last = corpus.last_mentioned(q["record_ids"])
            line = d.point(q, where)
            if last:
                line += f" · last mentioned {last}"
            if q.get("likely_resolved"):
                lr = q["likely_resolved"]
                marks = d.markers(lr["record_ids"])
                line += f" · **likely resolved** after: {esc(lr['change'])} {marks}"
            d.add(line)

    d.heading(1, "Open questions and unclear feedback")
    questions = syn.open_questions()
    if not questions:
        d.add("None.")
    for q in questions:
        where = f"**{esc(q['programming_type'])}**: " if q["programming_type"] else ""
        d.add(d.point(q, where))

    appendix(d)
    return d.text()


def _new_section(d: Doc, since: str | None) -> None:
    corpus = d.syn.corpus
    d.heading(1, "New since last update")
    if since is None:
        d.add(
            f"This is the first update, so all {_plural(len(corpus.records), 'record')} are new. "
            "From the next update on, this section lists what each update adds."
        )
        return
    new = [r for r in corpus.records.values() if str(r.get("first_seen") or "") > since]
    if not new:
        d.add(f"Nothing new since the last update ({since[:10]}).")
        return
    fb = [r for r in new if r["kind"] == "feedback"]
    facts = [r for r in new if r["kind"] == "program_info"]
    d.add(
        f"Since {since[:10]}: {_plural(len(fb), 'new piece')} of feedback and "
        f"{_plural(len(facts), 'new program fact')}.",
        "",
    )
    fb.sort(key=lambda r: ((r.get("programming_type") or "~").lower(), str(r["applies_to"])))
    for r in fb[:NEW_LIST_LIMIT]:
        where = esc(r.get("programming_type") or "Other")
        if r.get("session"):
            where += f" › {esc(r['session'])}"
        d.add(
            f"- **{where}** ({r['applies_to']}): {esc(r['feedback'])} {d.markers([r['record_id']])}"
        )
    if len(fb) > NEW_LIST_LIMIT:
        d.add(
            f"- … and {len(fb) - NEW_LIST_LIMIT} more (the Records tab of the Citations sheet "
            "lists them all; filter First seen)"
        )
    if facts:
        per: dict[str, int] = {}
        for r in facts:
            per[r["programming_type"]] = per.get(r["programming_type"], 0) + 1
        listed = ", ".join(
            f"{esc(t)} ({n})" for t, n in sorted(per.items(), key=lambda x: (-x[1], x[0]))
        )
        d.blank()
        d.add(f"New program facts, now part of Programs: {listed}.")


def _type_section(d: Doc, links: Links, ptype: str) -> None:
    syn, corpus = d.syn, d.syn.corpus
    o = syn.type_output(ptype)
    rows = corpus.of_type(ptype)
    fb = [r["record_id"] for r in rows if r["kind"] == "feedback"]
    per = corpus.is_per_session(ptype)
    d.heading(2, ptype)
    if o.get("summary"):
        d.add(esc(o["summary"]), "")
    line = f"{_plural(len(fb), 'piece')} of feedback · {_years(corpus.years(fb))}"
    if any(r["kind"] == "program_info" for r in rows):
        line += f" · [{esc(ptype)} in Programs]({links.anchor('programs', 1, ptype)})"
    d.add(f"*{line}*")
    if per:
        years = [y for y, _ in syn.session_outputs(ptype)]
        if years:
            d.blank()
            d.add(
                "Feedback on individual sessions is in Programs, by year: "
                + " · ".join(f"[{y}]({links.anchor('programs', 1, ptype)})" for y in years)
                + ". The themes below are about the type in general, and points that came up "
                "in several sessions."
            )
    for key, title in (("working", "What's working"), ("improve", "What to improve")):
        if o.get(key):
            d.blank()
            d.add(f"**{title}**", "")
            d.points(o[key])
    general = fb
    if per:
        claimed = syn.session_claimed(ptype)
        general = [r for r in fb if r not in claimed and not corpus.records[r].get("session")]
    _themes(d, f"type:{ptype}", 3, general)


def _themes(d: Doc, key: str, level: int, rids: list[str]) -> None:
    syn, corpus = d.syn, d.syn.corpus
    themes = syn.themes(key)
    for t in themes:
        ids = t["record_ids"]
        name = t["name"] + (" (likely resolved)" if t.get("likely_resolved") else "")
        d.heading(level, name)
        if t.get("description"):
            d.add(esc(t["description"]), "")
        cites = corpus.citation_ids(ids)
        meta = f"{_plural(len(cites), 'citation')} · {_years(corpus.years(ids))}"
        last = corpus.last_mentioned(ids)
        if last:
            meta += f" · last mentioned {last}"
        d.add(f"*{meta}*")
        if t.get("likely_resolved"):
            lr = t["likely_resolved"]
            d.blank()
            d.add(
                f"**Likely resolved:** the feedback stopped after this change: "
                f"{esc(lr['change'])} {d.markers(lr['record_ids'])}"
            )
        quotes = corpus.quotes(ids, QUOTES_PER_THEME)
        if quotes:
            d.blank()
            for q in quotes:
                marks = " ".join(d.marker(c) for c in q["citations"])
                d.add(f"> “{esc(q['text'])}” — {esc(q['author'])}, {q['applies_to']} {marks}", ">")
            d.lines.pop()  # no empty quote line after the last quote
        d.blank()
        d.add(f"Citations: {d.markers(ids)}")
    rest = syn.unthemed(key, rids)
    if not (syn.outputs.get(key) or {}) and rids:
        d.blank()
        d.add("*This section couldn't be regrouped this run; the feedback is listed as saved.*")
    if rest:
        d.heading(level, "Other feedback" if themes else "Feedback")
        for rid in sorted(rest, key=lambda r: str(corpus.records[r]["applies_to"]), reverse=True):
            r = corpus.records[rid]
            d.add(f"- {esc(r['feedback'])} ({r['applies_to']}) {d.markers([rid])}")


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------

BASIS_NOTE = {"found": " *(found by comparing sources)*", "conflict": " *(sources disagree)*"}


def programs_doc(syn: Synthesis, links: Links, *, updated: str, by: str) -> str:
    d = Doc(syn)
    types = syn.program_types()
    d.add(
        f"Last updated {updated[:10]} by {esc(by)} · {_plural(len(types), 'programming type')} · "
        f"[Consolidated Feedback]({doc_url(links.feedback_id)}) · "
        f"[Citations sheet]({sheet_url(links.sheet_id)})",
        "",
        "Each programming type's description, its history (dated by the period each source "
        "describes; dates marked “about” are approximate), how to run it, and what the "
        "feedback said in each period.",
    )
    d.heading(1, "Index")
    if not types:
        d.add("No program facts yet.")
    for ptype in types:
        d.add(f"- [{esc(ptype)}]({links.anchor('programs', 1, ptype)})")
    feedback_types = set(syn.feedback_types())
    for ptype in types:
        _program_section(d, links, ptype, ptype in feedback_types)
    appendix(d)
    return d.text()


def _program_section(d: Doc, links: Links, ptype: str, has_feedback: bool) -> None:
    syn, corpus = d.syn, d.syn.corpus
    o = syn.type_output(ptype)
    per = corpus.is_per_session(ptype)
    d.heading(1, ptype)
    if not o:
        facts = [
            r for r in corpus.of_type(ptype) if r["kind"] == "program_info" and not r.get("session")
        ]
        if facts:
            d.add("*This section couldn't be written this run; the facts are listed as saved.*", "")
            for r in sorted(facts, key=lambda r: str(r["applies_to"])):
                d.add(f"- {r['applies_to']}: {esc(r['fact'])} {d.markers([r['record_id']])}")
    sections = [
        ("Description", o.get("description") or []),
        ("History", o.get("history") or []),
        ("Running it", syn.guidance(ptype)),
    ]
    for title, items in sections:
        if not items:
            continue
        d.heading(2, title)
        if title == "History":
            for h in items:
                period = f"**{esc(h['period'])}**: " if h.get("period") else ""
                d.add(d.point(h, period) + BASIS_NOTE.get(h.get("basis"), ""))
        else:
            d.points(items)
    see = (
        f"[{esc(ptype)} in Consolidated Feedback]({links.anchor('feedback', 2, ptype)})"
        if has_feedback
        else ""
    )
    if per:
        _by_year(d, ptype)
    elif o.get("feedback_by_period"):
        d.heading(2, "Feedback by period")
        for p in o["feedback_by_period"]:
            period = f"**{esc(p['period'])}**: " if p.get("period") else ""
            d.add(d.point(p, period))
    if see:
        d.blank()
        d.add(f"All feedback on this programming type: {see}")


def _by_year(d: Doc, ptype: str) -> None:
    syn, corpus = d.syn, d.syn.corpus
    years = syn.session_outputs(ptype)
    if not years:
        return
    dates = {
        (s["year"], s["name"].lower()): s.get("date")
        for s in corpus.sessions.rows
        if s["programming_type"].lower() == ptype.lower()
    }
    d.heading(2, "By year")
    for year, o in years:
        d.heading(3, year)
        if not o:
            d.add("*This year couldn't be written this run.*")
            continue

        def order(s: dict[str, Any], year: str = year) -> tuple[str, str]:
            when = dates.get((year, s["name"].lower())) or year
            return (when if len(when) > 4 else year + "~", s["name"].lower())

        for s in sorted(o.get("sessions") or [], key=order):
            when = dates.get((year, s["name"].lower()))
            title = s["name"] + (f" ({when})" if when and when != year else "")
            d.heading(4, title)
            if s["covered"]:
                d.add("**What it covered**", "")
                d.points(s["covered"])
                d.blank()
            if s["guidance"]:
                d.add("**Guidance**", "")
                d.points(s["guidance"])
                d.blank()
            if s["feedback_record_ids"]:
                _session_feedback(d, s.get("feedback_summary"), s["feedback_record_ids"])
        um = o.get("unmatched") or {}
        if um.get("record_ids"):
            d.heading(4, f"Other {ptype.lower()} feedback, {year}")
            d.add("Feedback about a session that doesn't say which one.", "")
            _session_feedback(d, um.get("summary"), um["record_ids"])


def _session_feedback(d: Doc, summary: str | None, rids: list[str]) -> None:
    corpus = d.syn.corpus
    d.add("**Feedback**", "")
    if summary:
        d.add(f"{esc(summary)} {d.markers(rids)}")
    else:
        d.add(d.markers(rids))
    quotes = corpus.quotes(rids, 2)
    if quotes:
        d.blank()
        for q in quotes:
            marks = " ".join(d.marker(c) for c in q["citations"])
            d.add(f"> “{esc(q['text'])}” — {esc(q['author'])} {marks}", ">")
        d.lines.pop()
    d.blank()


# ---------------------------------------------------------------------------
# Citations sheet
# ---------------------------------------------------------------------------

CITATION_COLUMNS = [
    "ID",
    "Status",
    "Source",
    "Author",
    "Drive owner",
    "Date",
    "Context",
    "Excerpt",
    "Link",
    "First seen",
    "Item",
]
RECORD_COLUMNS = [
    "Record ID",
    "Kind",
    "Programming type",
    "Type as extracted",
    "Session",
    "Applies to",
    "Year",
    "Date basis",
    "Category / info type",
    "Sentiment",
    "Feedback or fact",
    "Quote",
    "Citations",
    "First seen",
    "Item",
]


@dataclass
class Link:
    """A sheet cell shown as a clickable link."""

    url: str
    label: str


def citation_rows(citations: Any) -> list[list[Any]]:
    rows: list[list[Any]] = [CITATION_COLUMNS]
    for c in citations.rows:
        link: Any = c.get("link") or ""
        if link and c.get("status") != "removed":
            link = Link(link, link)
        rows.append(
            [
                c["id"],
                c.get("status") or "",
                c.get("source") or "",
                c.get("author") or "",
                c.get("owner") or "",
                (c.get("date") or "")[:10],
                c.get("context") or "",
                c.get("excerpt") or "",
                link,
                (c.get("first_seen") or "")[:10],
                c.get("item_id") or "",
            ]
        )
    return rows


def record_rows(records: Any, citations: Any, types: Any) -> list[list[Any]]:
    rows: list[list[Any]] = [RECORD_COLUMNS]
    for r in records.rows:
        cites = r.get("citations") or []
        extracted = r.get("programming_type") or ""
        rows.append(
            [
                r["record_id"],
                r["kind"],
                types.canonical(extracted) or "" if extracted else "",
                extracted,
                r.get("session") or "",
                r.get("applies_to") or "",
                str(r.get("applies_to") or "")[:4],
                r.get("date_basis") or "",
                r.get("category") or r.get("info_type") or "",
                r.get("sentiment") or "",
                r.get("feedback") or r.get("fact") or "",
                r.get("quote") or "",
                " ".join(
                    c
                    + (" (removed)" if (citations.get(c) or {}).get("status") == "removed" else "")
                    for c in cites
                ),
                (r.get("first_seen") or "")[:10],
                r.get("item_id") or "",
            ]
        )
    return rows
