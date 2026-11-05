import yaml

from kb.sources.drive import (
    SHEET_MIME,
    _a1_sheet,
    file_metadata,
    render_file,
    render_presentation,
    render_table,
    strip_embedded_images,
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
