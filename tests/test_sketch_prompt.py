from uuid import uuid4

from test_plugins import make_turn

from agents_app_connector.plugins import TurnFile
from agents_app_connector.plugins.sketch import PLUGIN

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
    assert PLUGIN.render_prompt(m) == "User feedback: x\n\n" + FINAL


def test_system_append_has_annotated_spec_rule():
    from agents_app_connector.plugins.sketch import load_preset

    text = load_preset()["spec"]["wrapper"]["system_append"]
    assert "Annotated spec" in text and "strike-through" in text and "Open questions" in text
    text.encode("ascii")


def test_prompts_point_at_the_tools_never_at_the_reply():
    for turn in (make_turn(), make_turn(kind="message", first=False, files=[], text="x")):
        p = PLUGIN.render_prompt(turn)
        assert "document_write" in p and "feedback_add" in p
        assert "Reply with" not in p and "markdown spec" not in p
        assert p.isascii()
