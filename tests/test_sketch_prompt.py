from uuid import UUID, uuid4

from test_plugins import make_turn

from agents_app_connector.plugins import NotebookPage, TurnFile
from agents_app_connector.plugins.sketch import MARGIN_INK, PLUGIN
from agents_app_connector.plugins.sketch.layout import PageLayout, SpecLayout

FINAL = (
    "Write the document with document_write and anything else with feedback_add. "
    "Your reply text is discarded."
)


def test_prompt_image_before_caption_and_instruction():
    t = make_turn()
    p = PLUGIN.render_prompt(t)
    aid = t.files[0].attachment_id
    img = p.index(f"sketch-{aid}.png")
    assert img < p.index("login screen")
    assert img < p.index(FINAL)
    assert f"Image 1: sketch-{aid}.png (page 0)" in p
    assert "<this message>" not in p
    assert p.rstrip().endswith(FINAL)


def test_prompt_follow_includes_previous_spec_and_feedback():
    t = make_turn(
        kind="message",
        first=False,
        files=[],
        caption=None,
        text="rename the button",
        previous_spec=(2, "# Old\n"),
    )
    p = PLUGIN.render_prompt(t)
    assert "Previous spec version 2:" in p
    assert "```markdown\n# Old\n```" in p
    assert "rename the button" in p
    assert p.rstrip().endswith(FINAL)
    assert "Image 1" not in p


def test_prompt_missing_file_noted():
    t = make_turn(files=[])
    assert "(sketch file no longer stored)" in PLUGIN.render_prompt(t)


def test_prompt_follow_with_new_image_orders_image_first():
    t = make_turn(kind="message", first=False, text="see new", previous_spec=(1, "# A"))
    p = PLUGIN.render_prompt(t)
    assert p.index("Image 1") < p.index("see new") < p.index("Previous spec version 1")


def _page_files(*pages):
    out = []
    for n in pages:
        out.append(TurnFile(uuid4(), "sketch_png", "x.png", "image/png", b"P", n))
        out.append(TurnFile(uuid4(), "sketch_strokes", "x.json", "application/json", b"{}", n))
    return out


def test_prompt_lists_every_page_in_order():
    files = _page_files(0, 1, 2)
    p = PLUGIN.render_prompt(make_turn(files=files))
    names = [f"sketch-{f.attachment_id}.png" for f in files if f.kind == "sketch_png"]
    pos = [p.index(f"Image {i + 1}: {n} (page {i})") for i, n in enumerate(names)]
    assert pos == sorted(pos)
    assert "The images are pages of one sketch; treat them together." in p


def test_single_page_prompt_lists_page_index():
    t = make_turn()
    p = PLUGIN.render_prompt(t)
    assert f"Image 1: sketch-{t.files[0].attachment_id}.png (page 0)" in p
    assert "pages of one sketch" not in p
    assert "the exact path follows" not in p


def test_system_append_explains_colours_and_shapes():
    from agents_app_connector.plugins.sketch import load_preset

    text = load_preset()["spec"]["wrapper"]["system_append"]
    for word in (
        "black",
        "red",
        "blue",
        "green",
        "orange",
        "purple",
        "rectangle",
        "arrow",
        "mermaid",
    ):
        assert word in text
    text.encode("ascii")


def _annotated_turn(files, **kw):
    from agents_app_connector.plugins import AnnotatedPage

    base = dict(
        annotated_spec=(2, "# Login\n\nBody text\n"),
        annotated_pages=[
            AnnotatedPage(page=0, doc_page=3, lines=(4, 9)),
            AnnotatedPage(page=1, doc_page=4, lines=(10, 15)),
        ],
        files=files,
        caption="Handwritten notes over spec v2",
        previous_spec=None,
    )
    base.update(kw)
    return make_turn(**base)


def test_annotation_prompt_snapshot():
    files = _page_files(0, 1)
    ids = [f.attachment_id for f in files if f.kind == "sketch_png"]
    p = PLUGIN.render_prompt(_annotated_turn(files))
    assert p == (
        f"Image 1: sketch-{ids[0]}.png (spec v2, document page 3, source lines 4-9)\n"
        f"Image 2: sketch-{ids[1]}.png (spec v2, document page 4, source lines 10-15)\n\n"
        "These images are spec version 2 rendered as pages with the user's handwritten notes "
        "over them. Printed text is the spec; handwriting, marks and shapes are the notes. "
        "Apply the notes to spec version 2 and call document_write with the full revised "
        "document.\n\n"
        "Caption: Handwritten notes over spec v2\n\n"
        "Spec version 2:\n```markdown\n# Login\n\nBody text\n```\n\n" + FINAL
    )


def test_annotation_prompt_omits_previous_spec_block():
    p = PLUGIN.render_prompt(_annotated_turn(_page_files(0, 1), previous_spec=(3, "# Newer")))
    assert "Previous spec version" not in p and "# Newer" not in p
    assert "Spec version 2:" in p


def test_annotation_prompt_follow_turn_still_carries_spec():
    p = PLUGIN.render_prompt(_annotated_turn(_page_files(0, 1), first=False))
    assert "Spec version 2:" in p and "source lines 4-9" in p


def test_annotation_prompt_page_without_mapping_falls_back_to_page_index():
    files = _page_files(0, 5)
    p = PLUGIN.render_prompt(_annotated_turn(files))
    assert "(page 5)" in p and "source lines 4-9" in p


def test_plain_prompts_unchanged_by_annotation_support():
    t = make_turn()
    aid = t.files[0].attachment_id
    assert PLUGIN.render_prompt(t) == (
        f"Image 1: sketch-{aid}.png (page 0)\n\nCaption: login screen\n\n" + FINAL
    )
    m = make_turn(kind="message", first=False, files=[], caption=None, text="x")
    assert PLUGIN.render_prompt(m) == "User notes: x\n\n" + FINAL


def test_system_append_has_annotated_spec_rule():
    from agents_app_connector.plugins.sketch import load_preset

    text = load_preset()["spec"]["wrapper"]["system_append"]
    assert "Annotated spec" in text and "strike-through" in text and "feedback_add" in text
    text.encode("ascii")


def test_system_append_is_a_faithful_transcription_contract():
    from agents_app_connector.plugins.sketch import load_preset

    text = load_preset()["spec"]["wrapper"]["system_append"]
    for phrase in (
        "Your job is to recreate",
        "document_write",
        "exactly once",
        "feedback_add",
        "[illegible]",
        "[[red]]",
        "[[/red]]",
        "never block",
        "No title",
        "front matter",
        "mermaid",
    ):
        assert phrase in text, phrase
    # the old template and its added sections are gone
    for gone in ("## Transcription", "## Diagrams", "## Requirements", "unreadable_count"):
        assert gone not in text, gone
    assert "[[illegible]]" not in text


def test_prompts_point_at_the_tools_never_at_the_reply():
    for turn in (make_turn(), make_turn(kind="message", first=False, files=[], text="x")):
        p = PLUGIN.render_prompt(turn)
        assert "document_write" in p and "feedback_add" in p
        assert "Reply with" not in p and "markdown spec" not in p
        assert p.isascii()


def _system_append():
    from agents_app_connector.plugins.sketch import load_preset

    return load_preset()["spec"]["wrapper"]["system_append"]


def test_prompt_quotes_mermaid_labels_with_colour_markers():
    text = _system_append()
    assert 'A["[[red]]Login[[/red]]"]' in text
    assert "subroutine" in text


def test_prompt_is_second_person_and_keeps_feedback_for_the_tool():
    text = _system_append()
    assert "the user's feedback" not in text
    assert "the user's notes" in text
    assert "You recreate" in text


# notebook pages

OP_IDS = [
    UUID("3f2a0000-0000-4000-8000-000000000001"),
    UUID("9c1d0000-0000-4000-8000-000000000002"),
    UUID("7b7b0000-0000-4000-8000-000000000003"),
    UUID("a1a10000-0000-4000-8000-000000000004"),
]
REGION = UUID("5e5e0000-0000-4000-8000-000000000005")
NB_LAYOUT = PageLayout.model_validate(
    {
        "version": 1,
        "geometry": {
            "cell": 53,
            "row_pitch": 106,
            "top": 106,
            "rows": 16,
            "text_left": 53,
            "margin_left": 983,
        },
        "segments": [{"id": "p0-r4-0", "row": 4, "x0": 60, "x1": 900.4, "strokes": [5]}],
        "runs": [{"line_id": "L11", "row": 1, "x0": 53}, {"line_id": "L12", "row": 2, "x0": 53}],
        "ops": [
            {
                "id": str(OP_IDS[0]),
                "kind": "replace",
                "target": {"line_id": "L12", "from": 4, "to": 8, "quote": "milk"},
                "strokes": [0],
                "rect": [200, 280, 150, 50],
            },
            {
                "id": str(OP_IDS[1]),
                "kind": "delete",
                "target": {"line_id": "L11", "from": 0, "to": 4, "quote": "Shop"},
                "strokes": [1],
            },
            {
                "id": str(OP_IDS[2]),
                "kind": "insert",
                "at": {"line_id": "L12", "offset": 8, "mode": "inline"},
                "strokes": [2, 3],
                "rect": [990, 300, 300, 90.6],
            },
            {
                "id": str(OP_IDS[3]),
                "kind": "image",
                "strokes": [4],
                "rect": [60, 600, 400, 300],
                "region": {"id": str(REGION), "part": "region.0.0.png", "after_line": "L11"},
            },
        ],
    }
)
BASE_MD = "Shop list\nbuy milk\n"
BASE_SPEC = (
    3,
    BASE_MD,
    SpecLayout.model_validate(
        {"lines": [{"line_id": "L11", "source_line": 1}, {"line_id": "L12", "source_line": 2}]}
    ),
)


def _notebook_turn(**kw):
    files = _page_files(0)
    base = {
        "files": files,
        "caption": None,
        "layouts": [NotebookPage(0, NB_LAYOUT, 1404)],
        "base": BASE_SPEC,
        "previous_spec": (3, BASE_MD),
    }
    return make_turn(**{**base, **kw})


def test_notebook_prompt_snapshot():
    t = _notebook_turn()
    aid = t.files[0].attachment_id
    assert PLUGIN.render_prompt(t) == (
        f"Image 1: sketch-{aid}.png (page 0)\n"
        "Notebook geometry: cell 53, rows 16 from y 106, pitch 106, text x 53-983, "
        "margin x 983-1404\n"
        'L11 row 1: "Shop list"\n'
        'L12 row 2: "buy milk"\n'
        "p0-r4-0 row 4 x 60-900\n"
        'op 3f2a replace L12[4:8] "milk" with the handwriting above it (rect 200,280,150,50)\n'
        'op 9c1d delete L11[0:4] "Shop"\n'
        "op 7b7b insert at L12 offset 8 inline from the margin note (rect 990,300,300,91)\n"
        f"op a1a1 image region {REGION} (rect 60,600,400,300) after L11: "
        "do not transcribe inside\n\n"
        "These images are notebook pages. The printed rows are spec version 3 (their line ids "
        "are given above), the handwriting is new: transcribe each segment, apply each op to "
        "spec version 3 and call document_write with the full revised document, lines and "
        "ops.\n\n"
        f"{MARGIN_INK}\n\n"
        "Spec version 3:\n```markdown\nShop list\nbuy milk\n```\n"
        "Line ids of spec version 3:\nL11: line 1\nL12: line 2\n\n"
        f"Op ids: 3f2a={OP_IDS[0]}, 9c1d={OP_IDS[1]}, 7b7b={OP_IDS[2]}, a1a1={OP_IDS[3]}\n\n"
        + FINAL
    )


def test_notebook_prompt_insert_after_line_and_image_without_anchor():
    raw = NB_LAYOUT.model_dump(mode="json", by_alias=True)
    raw["ops"][2]["at"]["mode"] = "after_line"
    raw["ops"][3]["region"]["after_line"] = None
    t = _notebook_turn(layouts=[NotebookPage(0, PageLayout.model_validate(raw), 1404)])
    p = PLUGIN.render_prompt(t)
    assert "op 7b7b insert after L12 from the margin note (rect 990,300,300,91)\n" in p
    assert f"op a1a1 image region {REGION} (rect 60,600,400,300): do not transcribe inside" in p


def test_notebook_prompt_carries_base_in_full():
    p = PLUGIN.render_prompt(_notebook_turn(first=False, previous_spec=None))
    assert "Spec version 3:\n```markdown\nShop list\nbuy milk\n```" in p
    assert "Previous spec version" not in p
    assert "L11: line 1\nL12: line 2" in p


def test_notebook_prompt_without_base_keeps_previous_spec():
    raw = NB_LAYOUT.model_dump(mode="json", by_alias=True)
    raw["ops"], raw["runs"] = [], []
    t = _notebook_turn(layouts=[NotebookPage(0, PageLayout.model_validate(raw), 1404)], base=None)
    p = PLUGIN.render_prompt(t)
    assert "p0-r4-0 row 4 x 60-900" in p and "Op ids" not in p
    assert "Previous spec version 3:" in p and "Line ids" not in p


def test_plain_prompts_unchanged_by_notebook_support():
    t = make_turn()
    aid = t.files[0].attachment_id
    p = PLUGIN.render_prompt(t)
    assert p == f"Image 1: sketch-{aid}.png (page 0)\n\nCaption: login screen\n\n" + FINAL
    assert "Notebook" not in p and MARGIN_INK not in p


def test_notebook_layout_and_region_files_never_attached():
    files = [
        *_page_files(0),
        TurnFile(uuid4(), "sketch_layout", "l.json", "application/json", b"{}", 0),
        TurnFile(uuid4(), "spec_image", "r.png", "image/png", b"P", 0),
    ]
    names = [a.name for a in PLUGIN.turn_attachments(_notebook_turn(files=files))]
    assert len(names) == 2 and all(n.startswith(("sketch-", "strokes-")) for n in names)


def test_system_append_has_notebook_pages_section():
    text = _system_append()
    assert "Notebook pages" in text
    section = text[text.index("Notebook pages") :]
    for word in (
        "segment",
        "its own markdown source line",
        "fenced block",
        "kept",
        "![image](images/<region id>.png)",
        "do not transcribe",
        "applied or rejected",
        "every op",
        "lines and ops",
    ):
        assert word in section, word
    assert text.isascii()
