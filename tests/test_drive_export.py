import yaml

from kb.sources.drive import (
    SHEET_MIME,
    _a1_sheet,
    file_metadata,
    render_file,
    render_presentation,
    render_table,
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
