from uuid import uuid4

from test_plugins import make_turn

from agents_app_connector.plugins import TurnFile
from agents_app_connector.plugins.sketch import PLUGIN


def test_prompt_image_before_caption_and_instruction():
    t = make_turn()
    p = PLUGIN.render_prompt(t)
    aid = t.files[0].attachment_id
    img = p.index(f"sketch-{aid}.png")
    assert img < p.index("login screen")
    assert img < p.index("Reply with only the markdown spec.")
    assert f"Image 1: sketch-{aid}.png (page 0)" in p
    assert "<this message>" not in p
    assert p.rstrip().endswith("Reply with only the markdown spec.")


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
    assert p.rstrip().endswith("Reply with only the markdown spec.")
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
