import logging
import threading
import time

import pytest
import yaml

from kb.sources import drive as drive_mod
from kb.sources.drive import (
    SHEET_MIME,
    RetryCounter,
    Services,
    _a1_sheet,
    export_files,
    file_metadata,
    render_file,
    render_presentation,
    render_table,
    strip_embedded_images,
    threaded_export,
)


def text(s):
    return {"textElements": [{"paragraphMarker": {}}, {"textRun": {"content": s}}]}


def shape(s, object_id="x"):
    return {"objectId": object_id, "shape": {"text": text(s)}}


def test_presentation_includes_slide_text_tables_groups_and_speaker_notes():
    pres = {
        "slides": [
            {
                "pageElements": [
                    shape("Night market recap\n"),
                    {"objectId": "img", "image": {}},
                    {
                        "elementGroup": {
                            "children": [shape("Food sold out"), shape("Lines too long")]
                        }
                    },
                    {
                        "table": {
                            "tableRows": [
                                {"tableCells": [{"text": text("Stall")}, {"text": text("Rating")}]},
                                {"tableCells": [{"text": text("Dumplings")}, {"text": text("5")}]},
                            ]
                        }
                    },
                ],
                "slideProperties": {
                    "notesPage": {
                        "notesProperties": {"speakerNotesObjectId": "notes1"},
                        "pageElements": [
                            shape("slide image placeholder", "thumb"),
                            shape("Mention the vendor shortage", "notes1"),
                        ],
                    }
                },
            },
            {"pageElements": [], "slideProperties": {}},
        ]
    }
    md = render_presentation(pres)
    assert "## Slide 1" in md and "## Slide 2" in md
    for s in ["Night market recap", "Food sold out", "Lines too long", "Dumplings | 5"]:
        assert s in md
    assert "**Speaker notes:**\n\nMention the vendor shortage" in md
    assert "slide image placeholder" not in md
    assert "_(no text)_" in md.split("## Slide 2")[1]


def test_render_table_pads_rows_escapes_and_drops_blank_rows():
    md = render_table([["Name", "Comment"], [], ["", ""], ["Ann", "a|b\nc"], ["Bo"]])
    assert md.splitlines() == [
        "| Name | Comment |",
        "| --- | --- |",
        "| Ann | a\\|b<br>c |",
        "| Bo |  |",
    ]
    assert render_table([]) == "_(empty)_\n"


def test_a1_sheet_quotes_titles():
    assert _a1_sheet("Tab 1") == "'Tab 1'"
    assert _a1_sheet("Bob's") == "'Bob''s'"


def test_render_file_has_front_matter_and_owner_fallback():
    f = {
        "id": "abc",
        "name": "Feedback: Oct",
        "mimeType": SHEET_MIME,
        "modifiedTime": "2026-10-30T14:02:11Z",
        "webViewLink": "https://docs.google.com/spreadsheets/d/abc",
        "owners": [],
        "lastModifyingUser": {"displayName": "Dee"},
    }
    out = render_file(f, "body\n")
    _, front, rest = out.split("---\n", 2)
    meta = yaml.safe_load(front)
    assert meta == file_metadata(f)
    assert meta["id"] == "drive:abc"
    assert meta["source"] == "sheet"
    assert meta["owner"] == "Dee"
    assert rest == "\n# Feedback: Oct\n\nbody\n"


def test_listing_reads_subfolders_once_and_dedupes(monkeypatch):
    from kb.sources import drive as gdrive

    tree = {
        "root": [
            {"id": "sub", "mimeType": gdrive.FOLDER_MIME},
            {"id": "a", "mimeType": gdrive.DOC_MIME},
            {"id": "pdf", "mimeType": "application/pdf"},
        ],
        "sub": [
            {"id": "b", "mimeType": gdrive.SHEET_MIME},
            {"id": "root", "mimeType": gdrive.FOLDER_MIME},  # cycle via a shortcut-like link
            {"id": "a", "mimeType": gdrive.DOC_MIME},  # same file in two folders
        ],
        "other": [{"id": "sub", "mimeType": gdrive.FOLDER_MIME}],
    }
    calls = []

    def children(drive, folder_id, *, page_size=100, page_token=None):
        calls.append(folder_id)
        return {"files": tree[folder_id]}

    monkeypatch.setattr(gdrive, "list_children", children)
    files = gdrive.list_folder_files(None, ["root", "other"], [gdrive.DOC_MIME, gdrive.SHEET_MIME])
    assert [f["id"] for f in files] == ["a", "b"]
    assert sorted(calls) == ["other", "root", "sub"]


def test_strip_embedded_images_replaces_refs_and_drops_definitions():
    md = (
        "# Plan\n\n"
        "Intro ![][image1] text.\n\n"
        "![][image2]![][image2] ![][image3]\n\n"
        "| Bone | ![logo][image3] |\n\n"
        "Inline ![chart](data:image/jpeg;base64,/9j/AAAA) here.\n\n"
        "Linked ![photo](https://example.com/p.png) and ![][remote] stay.\n\n"
        "[image1]: <data:image/png;base64,iVBORw0KGgo=>\n\n"
        "[image2]: <data:image/png;base64,AAAA>\n"
        '[Image3]: <data:image/gif;base64,R0lGOD> "Screenshot 2026-07-01"\n'
        "[remote]: https://example.com/r.png\n"
    )
    out = strip_embedded_images(md)
    assert "data:" not in out
    assert "Intro [image] text." in out
    assert "\n[image]\n" in out  # a run of images collapses to one
    assert "| Bone | [image: logo] |" in out
    assert "Inline [image: chart] here." in out
    assert "![photo](https://example.com/p.png)" in out
    assert "![][remote]" in out and "[remote]: https://example.com/r.png" in out


def test_strip_embedded_images_leaves_plain_text_unchanged():
    md = "# Notes\n\nNo images here, just [a link](https://x.y) and [ref][1].\n\n[1]: https://x.y\n"
    assert strip_embedded_images(md) == md


# ---------------------------------------------------------------------------
# Exporting several files at once
# ---------------------------------------------------------------------------


def _files(n):
    return [{"id": f"f{i}", "name": f"File {i}", "mimeType": SHEET_MIME} for i in range(n)]


def _slow_export(f):
    """Later files finish first, so results come back out of order unless re-sorted."""
    i = int(f["id"][1:])
    time.sleep(0.002 * (20 - i))
    if i % 7 == 3:
        raise RuntimeError(f"boom {i}")
    return f"body {i}"


@pytest.mark.parametrize("workers", [2, 8])
def test_parallel_export_matches_one_at_a_time(workers):
    files = _files(20)
    one = [(r.file["id"], r.body, r.error) for r in export_files(files, _slow_export, 1)]
    many = [(r.file["id"], r.body, r.error) for r in export_files(files, _slow_export, workers)]
    assert many == one
    assert [fid for fid, _, _ in many] == [f["id"] for f in files]
    assert [e for _, _, e in many if e] == [
        "RuntimeError: boom 3",
        "RuntimeError: boom 10",
        "RuntimeError: boom 17",
    ]


def test_parallel_export_runs_files_at_the_same_time():
    running, peak, lock = 0, 0, threading.Lock()
    gate = threading.Barrier(4, timeout=5)

    def export(f):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        gate.wait()  # only passes once 4 exports are running together
        with lock:
            running -= 1
        return f["id"]

    assert [r.body for r in export_files(_files(8), export, 4)] == [f"f{i}" for i in range(8)]
    assert peak == 4


def test_threaded_export_builds_one_client_set_per_thread(monkeypatch):
    built = []

    def make_services():
        built.append(threading.get_ident())
        return Services(drive=threading.get_ident(), sheets=None, slides=None)

    # Each export must use the clients built on its own thread.
    monkeypatch.setattr(
        drive_mod, "export_text", lambda s, f: str(s.drive == threading.get_ident())
    )
    export = threaded_export(make_services)
    results = [r.body for r in export_files(_files(30), export, 4)]
    assert results == ["True"] * 30
    assert len(built) == len(set(built)) <= 4


def test_stopping_early_cancels_queued_exports():
    started = []

    def export(f):
        started.append(f["id"])
        time.sleep(0.01)
        return f["id"]

    results = export_files(_files(100), export, 2)
    next(results)
    results.close()
    assert len(started) < 10


def test_retry_counter_counts_googleapiclient_retries():
    log = logging.getLogger("googleapiclient.http")
    with RetryCounter() as retries:
        log.warning(
            "Sleeping %.2f seconds before retry %d of %d for %s: %s %s, after %s",
            1.0,
            1,
            5,
            "request",
            "GET",
            "https://x",
            429,
        )
        log.warning(
            "Sleeping %.2f seconds before retry %d of %d for %s: %s %s, after %s",
            2.0,
            2,
            5,
            "request",
            "GET",
            "https://x",
            503,
        )
        log.warning('Encountered 403 Forbidden with reason "%s"', "rateLimitExceeded")
    log.warning("Sleeping 1.00 seconds before retry 1 of 5 for request: GET x, after 429")
    assert (retries.retries, retries.rate_limited) == (2, 1)
    assert retries.summary() == ", 2 retries (1 rate-limited)"
    assert RetryCounter().summary() == ""


def test_parallel_listing_matches_one_folder_at_a_time(monkeypatch):
    # root → 12 subfolders, each with 3 files and a nested folder holding one more file;
    # every subfolder also links back to the first one, and "dup" is in every folder.
    tree = {"root": [{"id": f"s{i}", "mimeType": drive_mod.FOLDER_MIME} for i in range(12)]}
    for i in range(12):
        tree[f"s{i}"] = [
            *({"id": f"s{i}f{j}", "mimeType": drive_mod.DOC_MIME} for j in range(3)),
            {"id": f"n{i}", "mimeType": drive_mod.FOLDER_MIME},
            {"id": "s0", "mimeType": drive_mod.FOLDER_MIME},
            {"id": "dup", "mimeType": drive_mod.DOC_MIME},
        ]
        tree[f"n{i}"] = [{"id": f"n{i}f", "mimeType": drive_mod.SHEET_MIME}]
    calls = []

    def children(drive, folder_id, *, page_size=100, page_token=None):
        calls.append(folder_id)
        time.sleep(0.001 * (hash(folder_id) % 5))  # finish out of order
        return {"files": tree[folder_id]}

    monkeypatch.setattr(drive_mod, "list_children", children)

    def run(**kw):
        calls.clear()
        logs = []
        files = drive_mod.list_folder_files(None, ["root"], log=logs.append, **kw)
        return [f["id"] for f in files], logs, sorted(calls)

    one = run()
    many = run(make_drive=lambda: None, workers=4)
    assert many == one
    ids, logs, calls_made = one
    assert ids[:4] == ["s0f0", "s0f1", "s0f2", "dup"] and len(ids) == 12 * 4 + 1
    assert calls_made == sorted(tree)  # each folder read once
    assert logs[-1] == "folders read: 25 (0 queued), in-scope files found: 49"
