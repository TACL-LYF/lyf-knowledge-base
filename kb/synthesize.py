"""Synthesis: every saved record → programming types, themes, and program histories.

Extraction saves records once; synthesis regroups *all* of them on every run that has
something new, so the docs never collect duplicate or stale themes. It runs in steps:

1. **Merge type names.** One call is given every programming type name with a few
   sample records and the previous run's merges, and names the duplicates ("Family" →
   "Families"). Merges are saved in `programming_types.json` (`merged_into`), so later
   extractions use the canonical name too.
2. **Prepare the records** (`prepare`): map merged names, drop records whose citations
   have all been removed, and give workshop feedback with no session the session held
   that day, when exactly one was.
3. **Units**, one model call each, all sent together:
   - `type:<name>`: one programming type's feedback themes, requests, open questions,
     current description, history (stated changes, changes found by comparing dated
     snapshots, and conflicts), guidance, and feedback by period. For a per-session
     type (Workshop), only its general records (`session: null`).
   - `sessions:<type>:<year>`: one year of a per-session type: each session's content,
     guidance, and feedback, feedback that names no session matched by date, and
     points that recur across sessions (lifted into the type's general sections).
   - `themes`: feedback not tied to any programming type, grouped into themes.
4. **Executive summary** (`summary`): one call over every unit's summary and themes.

The model cites records only by record ID. Code turns those into citations, and works
out each theme's citation count, breakdown by year, last mention, quotes, and its rank
(recent feedback weighted higher), so none of that depends on the model.

Each unit's answer is saved in `_state/synthesis.json` with a hash of its input. A unit
whose records haven't changed reuses the saved answer instead of calling the model, so
a run that adds one Discord message only re-synthesizes that message's type (plus the
summary), whoever runs it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kb import llm as llm_mod
from kb.citations import Citations
from kb.config import REPO_ROOT, Config
from kb.extract import ProgrammingTypes, Records, Sessions

Log = Callable[[str], None]

PROMPT_VERSION = 1
PROMPTS_DIR = REPO_ROOT / "prompts"
HALF_LIFE_YEARS = 2.0  # feedback from two years ago counts half as much when ranking
MERGE_SAMPLES = 3  # sample records shown per type name in the merge step
OUTPUT_TOKENS_ESTIMATE = (1_000, 0.25)  # rough output per unit: base + 25% of its input


def _prompt(name: str) -> str:
    common = (PROMPTS_DIR / f"synth_common_v{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    task = (PROMPTS_DIR / f"synth_{name}_v{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    return common.rstrip() + "\n\n" + task


def _norm(name: str) -> str:
    return " ".join(str(name).lower().split())


def _hash(*parts: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


STR = {"type": "string"}
IDS = {"type": "array", "items": STR}
POINTS = {"type": "array", "items": _obj({"text": STR, "record_ids": IDS})}
RESOLVED = _nullable(_obj({"change": STR, "record_ids": IDS}))
THEMES = {
    "type": "array",
    "items": _obj(
        {"name": STR, "description": STR, "record_ids": IDS, "likely_resolved": RESOLVED}
    ),
}
REQUESTS = {
    "type": "array",
    "items": _obj(
        {
            "category": {"type": "string", "enum": ["feature_request", "bug"]},
            "text": STR,
            "record_ids": IDS,
            "likely_resolved": RESOLVED,
        }
    ),
}

MERGE_SCHEMA = _obj(
    {"merges": {"type": "array", "items": _obj({"alias": STR, "canonical": STR, "reason": STR})}}
)
TYPE_SCHEMA = _obj(
    {
        "summary": STR,
        "working": POINTS,
        "improve": POINTS,
        "themes": THEMES,
        "requests": REQUESTS,
        "open_questions": POINTS,
        "description": POINTS,
        "history": {
            "type": "array",
            "items": _obj(
                {
                    "period": STR,
                    "text": STR,
                    "basis": {"type": "string", "enum": ["stated", "found", "conflict"]},
                    "record_ids": IDS,
                }
            ),
        },
        "guidance": POINTS,
        "feedback_by_period": {
            "type": "array",
            "items": _obj({"period": STR, "text": STR, "record_ids": IDS}),
        },
    }
)
SESSIONS_SCHEMA = _obj(
    {
        "sessions": {
            "type": "array",
            "items": _obj(
                {
                    "name": STR,
                    "covered": POINTS,
                    "guidance": POINTS,
                    "feedback_summary": _nullable(STR),
                    "feedback_record_ids": IDS,
                }
            ),
        },
        "unmatched": _obj({"summary": _nullable(STR), "record_ids": IDS}),
        "recurring": {
            "type": "array",
            "items": _obj(
                {
                    "text": STR,
                    "kind": {"type": "string", "enum": ["guidance", "feedback"]},
                    "record_ids": IDS,
                }
            ),
        },
    }
)
THEMES_SCHEMA = _obj({"themes": THEMES, "requests": REQUESTS, "open_questions": POINTS})
SUMMARY_SCHEMA = _obj(
    {"takeaways": {"type": "array", "items": _obj({"text": STR, "theme_refs": IDS})}}
)


# ---------------------------------------------------------------------------
# Saved synthesis state (_state/synthesis.json)
# ---------------------------------------------------------------------------


@dataclass
class SynthState:
    """What synthesis and publishing keep between runs.

    - `units`: unit key → {"hash", "output", "model"}: the last answer for each unit.
    - `docs`: "feedback" / "programs" / "citations" → Drive file ID.
    - `fingerprint`: a hash of everything the docs are built from, at the last publish.
    - `last_published_at`: start time of the run that last published (records first
      seen after it are "new since last update").
    """

    units: dict[str, dict[str, Any]] = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)
    fingerprint: str | None = None
    last_published_at: str | None = None
    published_by: str | None = None

    @classmethod
    def load(cls, path: Path) -> SynthState:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            units=raw.get("units") or {},
            docs=raw.get("docs") or {},
            fingerprint=raw.get("fingerprint"),
            last_published_at=raw.get("last_published_at"),
            published_by=raw.get("published_by"),
        )

    def save(self, path: Path) -> None:
        data = {
            "docs": self.docs,
            "fingerprint": self.fingerprint,
            "last_published_at": self.last_published_at,
            "published_by": self.published_by,
            "units": self.units,
        }
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    def theme_names(self, key: str) -> list[str]:
        out = (self.units.get(key) or {}).get("output") or {}
        return [t["name"] for t in out.get("themes") or []]


def fingerprint(
    records: Records, citations: Citations, types: ProgrammingTypes, cfg: Config
) -> str:
    """Changes whenever the docs would: records, citations, merges, model, or prompts."""
    cites = [
        [c["id"], c.get("status"), c.get("link"), c.get("author"), c.get("date"), c.get("context")]
        for c in citations.rows
    ]
    return _hash(
        PROMPT_VERSION,
        cfg.llm_synthesis_model,
        cfg.per_session_types,
        records.rows,
        cites,
        types.merges,
    )


# ---------------------------------------------------------------------------
# Corpus: the records synthesis works from
# ---------------------------------------------------------------------------


@dataclass
class Corpus:
    records: dict[str, dict[str, Any]]  # record ID → record (canonical type), in saved order
    citations: Citations
    per_session: list[str]
    sessions: Sessions
    this_year: int

    def is_per_session(self, ptype: str | None) -> bool:
        return bool(ptype) and _norm(ptype or "") in {_norm(t) for t in self.per_session}

    def get(self, rid: str) -> dict[str, Any] | None:
        return self.records.get(rid)

    def of_type(self, ptype: str | None) -> list[dict[str, Any]]:
        return [r for r in self.records.values() if r.get("programming_type") == ptype]

    def types(self) -> list[str]:
        return sorted(
            {r["programming_type"] for r in self.records.values() if r.get("programming_type")},
            key=str.lower,
        )

    def citation_ids(self, rids: Iterable[str]) -> list[str]:
        """The citations of these records, oldest citation ID first, no repeats."""
        seen: dict[str, None] = {}
        for rid in rids:
            for cid in (self.records.get(rid) or {}).get("citations") or []:
                seen.setdefault(cid, None)
        return sorted(seen, key=lambda c: int(c.split("-")[1]) if "-" in c else 0)

    def weight(self, rid: str) -> float:
        """How much a record counts when ranking: recent ones count more."""
        r = self.records.get(rid) or {}
        try:
            age = max(0, self.this_year - int(str(r.get("applies_to"))[:4]))
        except ValueError:
            age = 0
        return 0.5 ** (age / HALF_LIFE_YEARS)

    def score(self, rids: Iterable[str]) -> float:
        return sum(self.weight(r) for r in set(rids))

    def years(self, rids: Iterable[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for rid in set(rids):
            r = self.records.get(rid)
            if r:
                y = str(r.get("applies_to"))[:4]
                out[y] = out.get(y, 0) + 1
        return dict(sorted(out.items()))

    def last_mentioned(self, rids: Iterable[str]) -> str | None:
        """The latest date any of these records' sources was written (message or file)."""
        dates = []
        for cid in self.citation_ids(rids):
            c = self.citations.get(cid)
            if c and c.get("date"):
                dates.append(str(c["date"])[:10])
        return max(dates) if dates else None

    def quotes(self, rids: Iterable[str], n: int = 3) -> list[dict[str, Any]]:
        """Up to `n` quotes from these records, recent ones first, one per author."""
        rows = [self.records[r] for r in set(rids) if r in self.records]
        rows = [r for r in rows if r.get("quote")]
        rows.sort(key=lambda r: (str(r.get("applies_to")), r["record_id"]), reverse=True)
        out: list[dict[str, Any]] = []
        authors: set[str] = set()
        for r in rows:
            cid = (r.get("citations") or [None])[0]
            c = self.citations.get(cid) if cid else None
            author = (c or {}).get("author") or "unknown"
            if author in authors and len(rows) > n:
                continue
            authors.add(author)
            out.append(
                {
                    "text": r["quote"],
                    "author": author,
                    "applies_to": r.get("applies_to"),
                    "citations": r.get("citations") or [],
                }
            )
            if len(out) == n:
                break
        return out


def match_session(record: dict[str, Any], sessions: Sessions, ptype: str) -> str | None:
    """The session held on the record's exact date, when there was exactly one."""
    day = str(record.get("applies_to") or "")
    if len(day) != 10:
        return None
    found = {
        s["name"]
        for s in sessions.rows
        if _norm(s["programming_type"]) == _norm(ptype) and s.get("date") == day
    }
    return found.pop() if len(found) == 1 else None


def prepare(
    records: Records,
    citations: Citations,
    types: ProgrammingTypes,
    sessions: Sessions,
    per_session: Iterable[str],
    this_year: int,
) -> Corpus:
    """The records to synthesize, with merged type names and dated sessions applied."""
    corpus = Corpus({}, citations, list(per_session), sessions, this_year)
    for r in records.rows:
        cites = r.get("citations") or []
        status = [(citations.get(c) or {}).get("status", "active") for c in cites]
        if cites and all(s == "removed" for s in status):
            continue  # every source is gone
        r = dict(r)
        r["programming_type"] = types.canonical(r.get("programming_type"))
        ptype = r["programming_type"]
        if (
            r["kind"] == "feedback"
            and ptype
            and not r.get("session")
            and corpus.is_per_session(ptype)
        ):
            name = match_session(r, sessions, ptype)
            if name:
                r["session"] = name
                r["session_matched"] = True
        corpus.records[r["record_id"]] = r
    return corpus


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def record_line(r: dict[str, Any], show_session: bool = False) -> str:
    basis = {"stated": "stated", "inferred": "inferred"}.get(r.get("date_basis"), "approx.")
    head = f"[{r['record_id']}] {r.get('applies_to')} ({basis})"
    if show_session and r.get("session"):
        head += f' session "{r["session"]}"'
    if r["kind"] == "feedback":
        line = f"{head} feedback · {r.get('category')} · {r.get('sentiment')}: {r.get('feedback')}"
    else:
        line = f"{head} {r.get('info_type')}: {r.get('fact')}"
    if r.get("quote"):
        line += f' | quote: "{r["quote"]}"'
    return line


def _sorted(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (str(r.get("applies_to")), r["record_id"]))


@dataclass
class Unit:
    key: str
    kind: str  # merge | type | sessions | themes | summary
    system: str
    user: str
    schema: dict[str, Any]
    hash: str
    record_ids: list[str] = field(default_factory=list)
    ptype: str | None = None
    year: str | None = None

    @property
    def tokens(self) -> int:
        return llm_mod.estimate_tokens(self.system + self.user)


def _previous(names: list[str]) -> str:
    if not names:
        return ""
    return (
        "Theme names from the previous run (reuse where they still fit): "
        + "; ".join(names)
        + "\n\n"
    )


def merge_unit(
    store_types: ProgrammingTypes, records: Records, per_session: list[str], model: str
) -> Unit:
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for r in records.rows:
        name = r.get("programming_type")
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        if len(samples.setdefault(name, [])) < MERGE_SAMPLES:
            samples[name].append(r.get("feedback") or r.get("fact") or "")
    per = {_norm(t) for t in per_session}
    names = sorted(set(store_types.names) | set(counts), key=str.lower)
    blocks = []
    for name in names:
        mark = " (per-session)" if _norm(name) in per else ""
        lines = [f"- {name}{mark}: {counts.get(name, 0)} records"]
        lines += [f"    e.g. {s}" for s in samples.get(name, [])]
        blocks.append("\n".join(lines))
    prev = store_types.merges
    prev_text = "\n".join(f"- {a} → {c}" for a, c in sorted(prev.items())) or "(none)"
    user = (
        "Programming type names:\n"
        + "\n".join(blocks)
        + f"\n\nMerges made by the previous run:\n{prev_text}\n"
    )
    system = _prompt("merge")
    # Reused while the names stay the same (counts don't matter). The previous merges are
    # in the prompt but not the hash: they're this unit's own last answer.
    h = _hash("merge", PROMPT_VERSION, model, names, sorted(per))
    return Unit("merge", "merge", system, user, MERGE_SCHEMA, h)


def build_units(corpus: Corpus, saved: SynthState, model: str) -> list[Unit]:
    """Every type, sessions, and themes unit for this corpus."""
    units: list[Unit] = []
    for ptype in corpus.types():
        rows = corpus.of_type(ptype)
        per = corpus.is_per_session(ptype)
        general = [r for r in rows if not r.get("session")] if per else rows
        if general:
            units.append(_type_unit(ptype, general, per, saved, model))
        if per:
            units.extend(_session_units(corpus, ptype, rows, model))
    loose = [r for r in corpus.records.values() if not r.get("programming_type")]
    loose = [r for r in loose if r["kind"] == "feedback"]
    if loose:
        lines = [record_line(r) for r in _sorted(loose)]
        user = (
            _previous(saved.theme_names("themes"))
            + f"Feedback not tied to a programming type ({len(lines)} records, oldest first):\n"
            + "\n".join(lines)
            + "\n"
        )
        units.append(
            Unit(
                "themes",
                "themes",
                _prompt("themes"),
                user,
                THEMES_SCHEMA,
                _hash("themes", PROMPT_VERSION, model, lines),
                [r["record_id"] for r in loose],
            )
        )
    return units


def _type_unit(
    ptype: str, rows: list[dict[str, Any]], per: bool, saved: SynthState, model: str
) -> Unit:
    key = f"type:{ptype}"
    facts = [record_line(r) for r in _sorted(x for x in rows if x["kind"] == "program_info")]
    feedback = [record_line(r) for r in _sorted(x for x in rows if x["kind"] == "feedback")]
    head = f"Programming type: {ptype}"
    if per:
        head += (
            " (per-session: these are its general records; each session's records are "
            "handled separately)"
        )
    user = (
        head
        + "\n\n"
        + _previous(saved.theme_names(key))
        + f"Program facts ({len(facts)}, oldest first):\n"
        + ("\n".join(facts) or "(none)")
        + f"\n\nFeedback ({len(feedback)}, oldest first):\n"
        + ("\n".join(feedback) or "(none)")
        + "\n"
    )
    h = _hash("type", PROMPT_VERSION, model, ptype, per, facts, feedback)
    return Unit(
        key, "type", _prompt("type"), user, TYPE_SCHEMA, h, [r["record_id"] for r in rows], ptype
    )


def _session_units(
    corpus: Corpus, ptype: str, rows: list[dict[str, Any]], model: str
) -> list[Unit]:
    by_year: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in rows:
        if r.get("session"):
            by_year.setdefault(str(r["applies_to"])[:4], {}).setdefault(r["session"], []).append(r)
    units = []
    for year, named in sorted(by_year.items()):
        # Feedback naming no session, dated to a day of this year: maybe one of these.
        loose = [
            r
            for r in rows
            if not r.get("session")
            and r["kind"] == "feedback"
            and len(str(r.get("applies_to"))) == 10
            and str(r["applies_to"]).startswith(year)
        ]
        dates = {
            _norm(s["name"]): s.get("date")
            for s in corpus.sessions.rows
            if _norm(s["programming_type"]) == _norm(ptype) and s.get("year") == year
        }
        blocks = []
        for name in sorted(named, key=lambda n: (dates.get(_norm(n)) or year, n.lower())):
            recs = _sorted(named[name])
            when = dates.get(_norm(name)) or year
            facts = [record_line(r) for r in recs if r["kind"] == "program_info"]
            fb = [record_line(r) for r in recs if r["kind"] == "feedback"]
            blocks.append(
                f'Session "{name}" ({when})\n'
                + "Facts:\n"
                + ("\n".join(facts) or "(none)")
                + "\nFeedback:\n"
                + ("\n".join(fb) or "(none)")
            )
        loose_lines = [record_line(r) for r in _sorted(loose)]
        user = (
            f"Programming type: {ptype} (per-session)\nYear: {year}\n\n"
            + "\n\n".join(blocks)
            + f"\n\nFeedback about a {year} {ptype.lower()} that names no session "
            "(match to a session by date if you can):\n"
            + ("\n".join(loose_lines) or "(none)")
            + "\n"
        )
        ids = [r["record_id"] for recs in named.values() for r in recs] + [
            r["record_id"] for r in loose
        ]
        h = _hash("sessions", PROMPT_VERSION, model, ptype, year, blocks, loose_lines)
        units.append(
            Unit(
                f"sessions:{ptype}:{year}",
                "sessions",
                _prompt("sessions"),
                user,
                SESSIONS_SCHEMA,
                h,
                ids,
                ptype,
                year,
            )
        )
    return units


# ---------------------------------------------------------------------------
# Checking answers
# ---------------------------------------------------------------------------


class Checker:
    """Keeps only record IDs that belong to the unit (and to the right kind)."""

    def __init__(self, corpus: Corpus, allowed: Iterable[str]) -> None:
        self.corpus = corpus
        self.allowed = set(allowed)
        self.problems: list[str] = []

    def ids(self, raw: Any, kind: str | None = None, where: str = "") -> list[str]:
        out: list[str] = []
        for x in raw or []:
            rid = str(x).strip().strip("[]").strip()
            r = self.corpus.get(rid)
            if rid not in self.allowed or r is None:
                self.problems.append(f"{where}: unknown record {rid!r} dropped")
                continue
            if kind and r["kind"] != kind:
                continue
            if rid not in out:
                out.append(rid)
        return out

    def points(self, raw: Any, kind: str | None = None, where: str = "") -> list[dict[str, Any]]:
        out = []
        for p in raw or []:
            text = " ".join(str(p.get("text") or "").split())
            ids = self.ids(p.get("record_ids"), kind, where)
            if text and ids:
                out.append({"text": text, "record_ids": ids})
        return out

    def resolved(self, raw: Any, feedback_ids: list[str], where: str) -> dict[str, Any] | None:
        if not raw:
            return None
        ids = self.ids(raw.get("record_ids"), "program_info", where)
        change = " ".join(str(raw.get("change") or "").split())
        if not ids or not change:
            return None
        change_year = max(str(self.corpus.records[r]["applies_to"])[:4] for r in ids)
        last = max(
            (str(self.corpus.records[r]["applies_to"])[:4] for r in feedback_ids), default=""
        )
        if last > change_year:  # feedback continued after the change
            self.problems.append(
                f"{where}: 'likely resolved' dropped (feedback after {change_year})"
            )
            return None
        return {"change": change, "record_ids": ids}

    def themes(self, raw: Any) -> list[dict[str, Any]]:
        out = []
        for t in raw or []:
            name = " ".join(str(t.get("name") or "").split())
            where = f"theme {name!r}"
            ids = self.ids(t.get("record_ids"), "feedback", where)
            if not name or not ids:
                continue
            out.append(
                {
                    "name": name,
                    "description": " ".join(str(t.get("description") or "").split()),
                    "record_ids": ids,
                    "likely_resolved": self.resolved(t.get("likely_resolved"), ids, where),
                }
            )
        return out

    def requests(self, raw: Any) -> list[dict[str, Any]]:
        out = []
        for q in raw or []:
            text = " ".join(str(q.get("text") or "").split())
            ids = self.ids(q.get("record_ids"), "feedback", "request")
            if not text or not ids:
                continue
            cat = (
                q.get("category")
                if q.get("category") in ("bug", "feature_request")
                else "feature_request"
            )
            out.append(
                {
                    "category": cat,
                    "text": text,
                    "record_ids": ids,
                    "likely_resolved": self.resolved(q.get("likely_resolved"), ids, "request"),
                }
            )
        return out


def check(unit: Unit, raw: Any, corpus: Corpus) -> tuple[dict[str, Any], list[str]]:
    """The checked answer for a type, sessions, or themes unit, and any problems."""
    if not isinstance(raw, dict):
        raise ValueError("answer isn't a JSON object")
    c = Checker(corpus, unit.record_ids)
    if unit.kind == "themes":
        out: dict[str, Any] = {
            "themes": c.themes(raw.get("themes")),
            "requests": c.requests(raw.get("requests")),
            "open_questions": c.points(raw.get("open_questions"), "feedback", "open question"),
        }
    elif unit.kind == "type":
        history = []
        for h in raw.get("history") or []:
            text = " ".join(str(h.get("text") or "").split())
            ids = c.ids(h.get("record_ids"), "program_info", "history")
            if text and ids:
                basis = (
                    h.get("basis")
                    if h.get("basis") in ("stated", "found", "conflict")
                    else "stated"
                )
                period = " ".join(str(h.get("period") or "").split())
                history.append({"period": period, "text": text, "basis": basis, "record_ids": ids})
        periods = []
        for p in raw.get("feedback_by_period") or []:
            text = " ".join(str(p.get("text") or "").split())
            ids = c.ids(p.get("record_ids"), "feedback", "feedback by period")
            if text and ids:
                periods.append(
                    {
                        "period": " ".join(str(p.get("period") or "").split()),
                        "text": text,
                        "record_ids": ids,
                    }
                )
        out = {
            "summary": " ".join(str(raw.get("summary") or "").split()),
            "working": c.points(raw.get("working"), "feedback", "working"),
            "improve": c.points(raw.get("improve"), "feedback", "improve"),
            "themes": c.themes(raw.get("themes")),
            "requests": c.requests(raw.get("requests")),
            "open_questions": c.points(raw.get("open_questions"), "feedback", "open question"),
            "description": c.points(raw.get("description"), "program_info", "description"),
            "history": history,
            "guidance": c.points(raw.get("guidance"), "program_info", "guidance"),
            "feedback_by_period": periods,
        }
    elif unit.kind == "sessions":
        known = {}
        for rid in unit.record_ids:
            r = corpus.records[rid]
            if r.get("session"):
                known.setdefault(_norm(r["session"]), r["session"])
        sessions: list[dict[str, Any]] = []
        claimed: set[str] = set()
        for s in raw.get("sessions") or []:
            name = known.get(_norm(s.get("name") or ""))
            if name is None:
                c.problems.append(f"unknown session {s.get('name')!r} dropped")
                continue
            same = next((x for x in sessions if x["name"] == name), None)
            if same is not None:  # listed twice: fold the second entry into the first
                c.problems.append(f"session {name!r} listed twice; merged")
                fb = [
                    r
                    for r in c.ids(s.get("feedback_record_ids"), "feedback", name)
                    if r not in claimed
                ]
                claimed.update(fb)
                same["covered"] += c.points(s.get("covered"), "program_info", name)
                same["guidance"] += c.points(s.get("guidance"), "program_info", name)
                same["feedback_record_ids"] += fb
                if fb and not same["feedback_summary"]:
                    summary = " ".join(str(s.get("feedback_summary") or "").split())
                    same["feedback_summary"] = summary or None
                continue
            fb = [
                r for r in c.ids(s.get("feedback_record_ids"), "feedback", name) if r not in claimed
            ]
            claimed.update(fb)
            summary = " ".join(str(s.get("feedback_summary") or "").split()) or None
            sessions.append(
                {
                    "name": name,
                    "covered": c.points(s.get("covered"), "program_info", name),
                    "guidance": c.points(s.get("guidance"), "program_info", name),
                    "feedback_summary": summary if fb else None,
                    "feedback_record_ids": fb,
                }
            )
        um = raw.get("unmatched") or {}
        um_ids = [
            r for r in c.ids(um.get("record_ids"), "feedback", "unmatched") if r not in claimed
        ]
        out = {
            "sessions": sessions,
            "unmatched": {
                "summary": (" ".join(str(um.get("summary") or "").split()) or None)
                if um_ids
                else None,
                "record_ids": um_ids,
            },
            "recurring": [
                {**p, "kind": k}
                for k in ("guidance", "feedback")
                for p in c.points(
                    [x for x in raw.get("recurring") or [] if x.get("kind") == k],
                    "program_info" if k == "guidance" else "feedback",
                    "recurring",
                )
            ],
        }
    else:
        raise ValueError(f"unknown unit kind {unit.kind}")
    return out, c.problems


def check_merges(raw: Any, types: ProgrammingTypes, per_session: list[str]) -> dict[str, str]:
    """alias → canonical, with unknown names, per-session aliases, and loops left out."""
    if not isinstance(raw, dict):
        raise ValueError("answer isn't a JSON object")
    by_norm = {_norm(n): n for n in types.names}
    per = {_norm(t) for t in per_session}
    merges: dict[str, str] = {}
    for m in raw.get("merges") or []:
        alias = by_norm.get(_norm(m.get("alias") or ""))
        canonical = by_norm.get(_norm(m.get("canonical") or ""))
        if not alias or not canonical or alias == canonical or _norm(alias) in per:
            continue
        merges[alias] = canonical
    # Follow chains (A → B → C becomes A → C) and drop loops.
    out: dict[str, str] = {}
    for alias in merges:
        target, seen = merges[alias], {alias}
        while target in merges and target not in seen:
            seen.add(target)
            target = merges[target]
        if target != alias and target not in merges:
            out[alias] = target
    return out


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Synthesis:
    corpus: Corpus
    outputs: dict[str, dict[str, Any]]  # unit key → checked answer ({} if it failed)
    summary: list[dict[str, Any]]  # executive summary takeaways ({text, themes: [ref]})
    theme_refs: dict[str, tuple[str, str]]  # "T3" → (unit key, theme name)
    merges: dict[str, str]
    usage: llm_mod.Usage
    called: list[str] = field(default_factory=list)  # unit keys answered by the model
    reused: list[str] = field(default_factory=list)  # unit keys reused from the last run
    failed: dict[str, str] = field(default_factory=dict)  # unit key → error
    stale: list[str] = field(default_factory=list)  # failed units shown from an older answer
    problems: dict[str, list[str]] = field(default_factory=dict)

    # -- views used by rendering --------------------------------------------

    def type_output(self, ptype: str) -> dict[str, Any]:
        return self.outputs.get(f"type:{ptype}") or {}

    def session_outputs(self, ptype: str) -> list[tuple[str, dict[str, Any]]]:
        prefix = f"sessions:{ptype}:"
        return sorted(
            ((k.removeprefix(prefix), v) for k, v in self.outputs.items() if k.startswith(prefix)),
            reverse=True,
        )

    def session_claimed(self, ptype: str) -> set[str]:
        """Feedback records placed in a session (or that year's unmatched list)."""
        out: set[str] = set()
        for _, o in self.session_outputs(ptype):
            for s in o.get("sessions") or []:
                out.update(s["feedback_record_ids"])
            out.update((o.get("unmatched") or {}).get("record_ids") or [])
        return out

    def themes(self, key: str) -> list[dict[str, Any]]:
        """A unit's themes, current ones first by weighted score, then likely resolved.

        For a per-session type, records placed in a session are left out, and points
        recurring across sessions are added as themes.
        """
        out = self.outputs.get(key) or {}
        themes = [dict(t) for t in out.get("themes") or []]
        if key.startswith("type:"):
            ptype = key.removeprefix("type:")
            if self.corpus.is_per_session(ptype):
                claimed = self.session_claimed(ptype)
                for t in themes:
                    t["record_ids"] = [r for r in t["record_ids"] if r not in claimed]
                themes = [t for t in themes if t["record_ids"]]
                for year, o in self.session_outputs(ptype):
                    for p in o.get("recurring") or []:
                        if p["kind"] == "feedback":
                            themes.append(
                                {
                                    "name": f"Across {year} sessions: {p['text']}",
                                    "description": "",
                                    "record_ids": p["record_ids"],
                                    "likely_resolved": None,
                                    "recurring": True,
                                }
                            )
        themes.sort(
            key=lambda t: (
                t.get("likely_resolved") is not None,
                -self.corpus.score(t["record_ids"]),
            )
        )
        return themes

    def unthemed(self, key: str, rids: Iterable[str]) -> list[str]:
        """Feedback records of a unit that no theme (or session) covers."""
        covered: set[str] = set()
        for t in self.themes(key):
            covered.update(t["record_ids"])
        if key.startswith("type:"):
            covered |= self.session_claimed(key.removeprefix("type:"))
        return [
            r for r in rids if r not in covered and self.corpus.records[r]["kind"] == "feedback"
        ]

    def feedback_types(self) -> list[str]:
        """Programming types with feedback, most (recency-weighted) feedback first."""
        scores = {}
        for ptype in self.corpus.types():
            fb = [r["record_id"] for r in self.corpus.of_type(ptype) if r["kind"] == "feedback"]
            if fb:
                scores[ptype] = self.corpus.score(fb)
        return sorted(scores, key=lambda t: (-scores[t], t.lower()))

    def program_types(self) -> list[str]:
        """Programming types with program facts, alphabetically."""
        return [
            t
            for t in self.corpus.types()
            if any(r["kind"] == "program_info" for r in self.corpus.of_type(t))
        ]

    def guidance(self, ptype: str) -> list[dict[str, Any]]:
        out = list(self.type_output(ptype).get("guidance") or [])
        for year, o in self.session_outputs(ptype):
            for p in o.get("recurring") or []:
                if p["kind"] == "guidance":
                    out.append(
                        {
                            "text": f"{p['text']} (across {year} sessions)",
                            "record_ids": p["record_ids"],
                        }
                    )
        return out

    def requests(self) -> list[dict[str, Any]]:
        """Feature requests and bugs from every unit, each with its programming type."""
        out = []
        for key, o in self.outputs.items():
            if key.startswith("sessions:"):
                continue
            ptype = key.removeprefix("type:") if key.startswith("type:") else None
            for q in o.get("requests") or []:
                out.append({**q, "programming_type": ptype})
        out.sort(
            key=lambda q: (
                q.get("likely_resolved") is not None,
                -self.corpus.score(q["record_ids"]),
            )
        )
        return out

    def open_questions(self) -> list[dict[str, Any]]:
        out = []
        for key, o in self.outputs.items():
            if key.startswith("sessions:"):
                continue
            ptype = key.removeprefix("type:") if key.startswith("type:") else None
            for q in o.get("open_questions") or []:
                out.append({**q, "programming_type": ptype})
        return out


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """What a run would do, for `--dry-run`."""

    units: list[Unit]
    todo: list[Unit]  # units the model would be asked (the rest are reused)
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def _out_tokens(tokens: int) -> int:
    base, ratio = OUTPUT_TOKENS_ESTIMATE
    return int(base + ratio * tokens)


def plan(
    records: Records,
    citations: Citations,
    types: ProgrammingTypes,
    sessions: Sessions,
    saved: SynthState,
    cfg: Config,
    this_year: int,
) -> Plan:
    """Which units would be called, using the saved merges (the merge step may change them)."""
    model = cfg.llm_synthesis_model
    corpus = prepare(records, citations, types, sessions, cfg.per_session_types, this_year)
    units = build_units(corpus, saved, model)
    merge = merge_unit(types, records, cfg.per_session_types, model)
    if not types.names:
        merge = None
    todo = [
        u for u in [merge, *units] if u and (saved.units.get(u.key) or {}).get("hash") != u.hash
    ]
    tin = sum(u.tokens for u in todo)
    tout = sum(_out_tokens(u.tokens) for u in todo)
    if todo:  # the summary is asked whenever anything else changes: ~300 tokens a unit
        summary_in = llm_mod.estimate_tokens(_prompt("summary")) + 300 * len(units)
        tin += summary_in
        tout += 1_500
    batch = len(todo) >= cfg.llm_batch_min_requests
    return Plan(units, todo, tin, tout, llm_mod.cost(model, tin, tout, batch=batch))


def _ask(
    client: llm_mod.LLM,
    units: list[Unit],
    usage: llm_mod.Usage,
    log: Log,
    effort: str | None,
    parse: Callable[[Unit, Any], Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Ask each unit, retrying failures once. Returns (answers, errors) by unit key."""
    answers: dict[str, Any] = {}
    errors: dict[str, str] = {}
    by_rid: dict[str, Unit] = {}
    reqs: dict[str, llm_mod.Request] = {}
    for u in units:
        rid = llm_mod.request_id(usage.model, effort, u.system, u.user, u.schema)
        by_rid[rid] = u
        reqs[rid] = llm_mod.Request(rid, u.system, u.user, u.schema)

    def once(rids: list[str]) -> None:
        for rid, resp in client.run([reqs[r] for r in rids], log).items():
            u = by_rid[rid]
            if resp.usage:
                usage.add(resp.usage, resp.batch)
            try:
                if resp.error:
                    raise ValueError(resp.error)
                if resp.text is None:
                    raise ValueError("no answer")
                answers[u.key] = parse(u, json.loads(resp.text))
                errors.pop(u.key, None)
            except (ValueError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                errors[u.key] = (
                    f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e)
                )
                forget = getattr(client, "forget", None)
                if forget is not None:
                    forget(rid)

    once(list(reqs))
    if errors:
        log(f"retrying {len(errors)} failed synthesis request{'s' if len(errors) != 1 else ''}")
        once([rid for rid, u in by_rid.items() if u.key in errors])
    return answers, errors


def run(
    records: Records,
    citations: Citations,
    types: ProgrammingTypes,
    sessions: Sessions,
    saved: SynthState,
    client: llm_mod.LLM | None,
    cfg: Config,
    this_year: int,
    log: Log = lambda _: None,
) -> Synthesis:
    """Synthesize every record. Updates `types` (merges) and `saved.units` in place."""
    model = cfg.llm_synthesis_model
    effort = cfg.llm_synthesis_effort
    usage = llm_mod.Usage(model=model)
    called: list[str] = []
    reused: list[str] = []
    failed: dict[str, str] = {}
    problems: dict[str, list[str]] = {}

    def need(u: Unit) -> bool:
        return (saved.units.get(u.key) or {}).get("hash") != u.hash

    def require_client() -> llm_mod.LLM:
        if client is None:
            raise RuntimeError("Synthesis needs the Anthropic client (ANTHROPIC_API_KEY).")
        return client

    # 1. Merge duplicate type names.
    merge = merge_unit(types, records, cfg.per_session_types, model)
    if types.names and need(merge):
        log(f"checking {len(types.names)} programming type names for duplicates")
        answers, errors = _ask(
            require_client(),
            [merge],
            usage,
            log,
            effort,
            lambda u, raw: check_merges(raw, types, cfg.per_session_types),
        )
        if "merge" in answers:
            types.set_merges(answers["merge"])
            saved.units["merge"] = {"hash": merge.hash, "output": answers["merge"], "model": model}
            called.append("merge")
        else:
            failed["merge"] = errors.get("merge", "no answer")
            log(f"merge step failed ({failed['merge']}); keeping the previous merges")
    elif types.names:
        reused.append("merge")
    merges = types.merges

    # 2–3. Prepare and ask every unit whose records changed.
    corpus = prepare(records, citations, types, sessions, cfg.per_session_types, this_year)
    units = build_units(corpus, saved, model)
    todo = [u for u in units if need(u)]
    outputs: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for u in units:
        if not need(u):
            # Checked again, so fixes to the checks apply to saved answers too.
            outputs[u.key], _ = check(u, saved.units[u.key]["output"], corpus)
            reused.append(u.key)
    if todo:
        log(
            f"{len(todo)} of {len(units)} sections to synthesize ({model}); "
            f"{len(units) - len(todo)} unchanged since the last run"
        )

        def parse(u: Unit, raw: Any) -> Any:
            out, probs = check(u, raw, corpus)
            if probs:
                problems[u.key] = probs
            return out

        answers, errors = _ask(require_client(), todo, usage, log, effort, parse)
        for u in todo:
            if u.key in answers:
                outputs[u.key] = answers[u.key]
                saved.units[u.key] = {"hash": u.hash, "output": answers[u.key], "model": model}
                called.append(u.key)
                continue
            failed[u.key] = errors.get(u.key, "no answer")
            old = (saved.units.get(u.key) or {}).get("output")
            if old is not None:  # show the last good answer, checked against today's records
                try:
                    outputs[u.key], _ = check(u, old, corpus)
                    stale.append(u.key)
                except ValueError:
                    outputs[u.key] = {}
            else:
                outputs[u.key] = {}
            log(f"synthesis failed for {u.key}: {failed[u.key]}")
    # Forget units that no longer exist (a merged type, a year with no sessions left).
    keep = {u.key for u in units} | {"merge", "summary"}
    for key in list(saved.units):
        if key not in keep:
            del saved.units[key]

    syn = Synthesis(corpus, outputs, [], {}, merges, usage, called, reused, failed, stale, problems)

    # 4. Executive summary.
    summary_unit, refs = _summary_unit(syn, model)
    syn.theme_refs = refs
    if refs:
        if need(summary_unit):
            answers, errors = _ask(
                require_client(),
                [summary_unit],
                usage,
                log,
                effort,
                lambda u, raw: _check_summary(raw, refs),
            )
            if "summary" in answers:
                syn.summary = answers["summary"]
                saved.units["summary"] = {
                    "hash": summary_unit.hash,
                    "output": {"takeaways": syn.summary, "refs": refs},
                    "model": model,
                }
                called.append("summary")
            else:
                failed["summary"] = errors.get("summary", "no answer")
        else:
            syn.summary = saved.units["summary"]["output"]["takeaways"]
            reused.append("summary")
    return syn


def _summary_unit(syn: Synthesis, model: str) -> tuple[Unit, dict[str, tuple[str, str]]]:
    corpus = syn.corpus
    refs: dict[str, tuple[str, str]] = {}
    blocks = []

    def theme_lines(key: str) -> list[str]:
        lines = []
        for t in syn.themes(key):
            ref = f"T{len(refs) + 1}"
            refs[ref] = (key, t["name"])
            years = " · ".join(f"{y}: {n}" for y, n in corpus.years(t["record_ids"]).items())
            line = (
                f"- [{ref}] {t['name']} ({len(t['record_ids'])} records; {years}; "
                f"last mentioned {corpus.last_mentioned(t['record_ids']) or '?'})"
            )
            if t.get("likely_resolved"):
                line += f" LIKELY RESOLVED after: {t['likely_resolved']['change']}"
            if t.get("description"):
                line += f": {t['description']}"
            lines.append(line)
        return lines

    for ptype in syn.feedback_types():
        key = f"type:{ptype}"
        o = syn.outputs.get(key) or {}
        lines = theme_lines(key)
        if lines:
            blocks.append(
                f"## {ptype}\nSummary: {o.get('summary') or '(none)'}\n" + "\n".join(lines)
            )
    lines = theme_lines("themes")
    if lines:
        blocks.append("## Other themes (not tied to one programming type)\n" + "\n".join(lines))
    user = f"This year: {corpus.this_year}\n\n" + "\n\n".join(blocks) + "\n"
    system = _prompt("summary")
    return Unit(
        "summary",
        "summary",
        system,
        user,
        SUMMARY_SCHEMA,
        _hash("summary", PROMPT_VERSION, model, user),
    ), refs


def _check_summary(raw: Any, refs: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("answer isn't a JSON object")
    out = []
    for t in raw.get("takeaways") or []:
        text = " ".join(str(t.get("text") or "").split())
        themes = [
            r
            for r in dict.fromkeys(str(x).strip("[] ") for x in t.get("theme_refs") or [])
            if r in refs
        ]
        if text:
            out.append({"text": text, "themes": themes})
    return out
