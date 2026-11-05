"""Fakes for synthesis and publishing: a model that answers each unit from its record
lines, and a publisher that keeps docs and sheets in memory."""

import json
import re

from kb import llm as llm_mod

REC = re.compile(
    r'^\[(R-\d+)\] (\S+) \(([\w.]+)\)(?: session "[^"]+")? (feedback|description|history|guidance)',
    re.M,
)


def _records(text):
    """[(record ID, applies_to, kind or info type)] in the text."""
    return [(m.group(1), m.group(2), m.group(4)) for m in REC.finditer(text)]


def default_answer(system, user, merges=()):
    if "## Task: find programming type names" in system:
        return {"merges": [{"alias": a, "canonical": c, "reason": "same"} for a, c in merges]}
    if "## Task: write the executive summary" in system:
        refs = re.findall(r"\[(T\d+)\]", user)
        return {"takeaways": [{"text": "The main takeaway", "theme_refs": refs[:2]}]}
    recs = _records(user)
    fb = [r for r, _, k in recs if k == "feedback"]
    facts = [(r, d, k) for r, d, k in recs if k != "feedback"]
    if "## Task: write one programming type's sections" in system:
        ptype = re.search(r"^Programming type: ([^\n(]+)", user, re.M).group(1).strip()
        desc = [r for r, _, k in facts if k == "description"]
        return {
            "summary": f"People talk about {ptype}.",
            "working": [{"text": "It works", "record_ids": fb[:1]}] if fb else [],
            "improve": [{"text": "It could improve", "record_ids": fb[-1:]}] if fb else [],
            "themes": [
                {
                    "name": "Main Theme",
                    "description": "What people said.",
                    "record_ids": fb,
                    "likely_resolved": None,
                }
            ]
            if fb
            else [],
            "requests": [],
            "open_questions": [],
            "description": [{"text": f"{ptype} as it runs now", "record_ids": desc[-1:]}]
            if desc
            else [],
            "history": [
                {"period": d, "text": f"Setup in {d}", "basis": "stated", "record_ids": [r]}
                for r, d, k in facts
                if k != "guidance"
            ],
            "guidance": [
                {"text": "Prepare early", "record_ids": [r]} for r, _, k in facts if k == "guidance"
            ],
            "feedback_by_period": [{"period": "all", "text": "Mixed.", "record_ids": fb}]
            if fb
            else [],
        }
    if "## Task: write one year of a per-session" in system:
        sessions = []
        blocks = re.split(r'^Session "', user, flags=re.M)[1:]
        loose = user.split("names no session", 1)[1]
        for block in blocks:
            name = block.split('"', 1)[0]
            body = block.split("names no session", 1)[0]
            recs = _records(body)
            sfb = [r for r, _, k in recs if k == "feedback"]
            sessions.append(
                {
                    "name": name,
                    "covered": [
                        {"text": f"{name} content", "record_ids": [r]}
                        for r, _, k in recs
                        if k == "description"
                    ],
                    "guidance": [],
                    "feedback_summary": "Liked it." if sfb else None,
                    "feedback_record_ids": sfb,
                }
            )
        lfb = [r for r, _, k in _records(loose) if k == "feedback"]
        return {
            "sessions": sessions,
            "unmatched": {"summary": "Unplaced." if lfb else None, "record_ids": lfb},
            "recurring": [],
        }
    if "## Task: group feedback that isn't about one programming type" in system:
        return {
            "themes": [
                {
                    "name": "Loose Theme",
                    "description": "d",
                    "record_ids": fb,
                    "likely_resolved": None,
                }
            ],
            "requests": [],
            "open_questions": [],
        }
    raise AssertionError("unknown synthesis task")


class FakeSynthLLM:
    model = "claude-opus-5-5"

    def __init__(self):
        self.requests = []
        self.merges = []  # (alias, canonical) pairs the merge step returns
        self.custom = None  # (system, user) → answer dict, or None for the default
        self.fail = []  # substrings of the user message: those requests fail

    def run(self, requests, log):
        out = {}
        for r in requests:
            self.requests.append(r)
            usage = {"input_tokens": 500, "output_tokens": 100}
            if any(f in r.user for f in self.fail):
                out[r.custom_id] = llm_mod.Response(r.custom_id, error="boom", usage=usage)
                continue
            answer = self.custom(r.system, r.user) if self.custom else None
            if answer is None:
                answer = default_answer(r.system, r.user, self.merges)
            out[r.custom_id] = llm_mod.Response(r.custom_id, text=json.dumps(answer), usage=usage)
        return out

    def keys(self):
        """Which kind of unit each request was, in order."""
        out = []
        for r in self.requests:
            for marker, kind in (
                ("find programming type names", "merge"),
                ("executive summary", "summary"),
                ("one programming type's sections", "type"),
                ("one year of a per-session", "sessions"),
                ("isn't about one programming type", "themes"),
            ):
                if "## Task: " in r.system and marker in r.system.split("## Task: ", 1)[1][:80]:
                    out.append(kind)
                    break
        return out


LINK = re.compile(r"\[((?:\\.|[^\]\\])*)\]\(([^)\s]+)\)")


def markdown_to_doc(text):
    """A rough Docs API `documents.get` body for uploaded markdown: one paragraph per
    line, headings with IDs, and a text run per link."""
    content = []
    index = 1
    n = 0
    for line in text.split("\n"):
        m = re.match(r"(#{1,6}) (.*)", line)
        style = {"namedStyleType": "NORMAL_TEXT"}
        body = line
        if m:
            n += 1
            style = {"namedStyleType": f"HEADING_{len(m.group(1))}", "headingId": f"h.{n}"}
            body = m.group(2)
        elements = []
        pos = 0
        for lm in LINK.finditer(body):
            before = re.sub(r"\\(.)", r"\1", body[pos : lm.start()])
            if before:
                elements.append(
                    {
                        "startIndex": index,
                        "endIndex": index + len(before),
                        "textRun": {"content": before, "textStyle": {}},
                    }
                )
                index += len(before)
            label = re.sub(r"\\(.)", r"\1", lm.group(1))
            elements.append(
                {
                    "startIndex": index,
                    "endIndex": index + len(label),
                    "textRun": {"content": label, "textStyle": {"link": {"url": lm.group(2)}}},
                }
            )
            index += len(label)
            pos = lm.end()
        rest = re.sub(r"\\(.)", r"\1", body[pos:]) + "\n"
        elements.append(
            {
                "startIndex": index,
                "endIndex": index + len(rest),
                "textRun": {"content": rest, "textStyle": {}},
            }
        )
        index += len(rest)
        content.append({"paragraph": {"paragraphStyle": style, "elements": elements}})
    return {"body": {"content": content}}


class FakePublisher:
    def __init__(self):
        self.files = {}  # id → {"name", "mime", "trashed", "markdown", "doc", "tabs"}
        self.n = 0
        self.fail_sheet = False
        self.uploads = []  # doc IDs, in upload order

    def exists(self, file_id):
        f = self.files.get(file_id)
        return f is not None and not f["trashed"]

    def find(self, name, mime):
        for fid, f in self.files.items():
            if f["name"] == name and f["mime"] == mime and not f["trashed"]:
                return fid
        return None

    def create(self, name, mime):
        self.n += 1
        fid = f"file{self.n}"
        self.files[fid] = {
            "name": name,
            "mime": mime,
            "trashed": False,
            "markdown": None,
            "doc": None,
            "tabs": {},
        }
        return fid

    def upload_markdown(self, doc_id, text):
        self.uploads.append(doc_id)
        self.files[doc_id]["markdown"] = text
        self.files[doc_id]["doc"] = markdown_to_doc(text)

    def get_doc(self, doc_id):
        return self.files[doc_id]["doc"]

    def update_doc(self, doc_id, requests):
        doc = self.files[doc_id]["doc"]
        for req in requests:
            u = req["updateTextStyle"]
            for el in doc["body"]["content"]:
                for e in el["paragraph"]["elements"]:
                    if e["startIndex"] == u["range"]["startIndex"]:
                        e["textRun"]["textStyle"]["link"] = u["textStyle"]["link"]

    def write_sheet(self, sheet_id, tabs, fresh):
        if self.fail_sheet:
            raise RuntimeError("sheet write failed")
        self.files[sheet_id]["tabs"] = tabs

    # -- helpers for tests ---------------------------------------------------

    def by_name(self, name):
        return next(f for f in self.files.values() if f["name"] == name and not f["trashed"])

    def links(self, name):
        """Every link URL in a doc, after heading links were fixed."""
        out = []
        for el in self.by_name(name)["doc"]["body"]["content"]:
            for e in el["paragraph"]["elements"]:
                link = e["textRun"]["textStyle"].get("link")
                if link:
                    out.append((e["textRun"]["content"], link["url"]))
        return out
