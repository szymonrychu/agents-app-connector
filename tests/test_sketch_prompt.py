from test_plugins import make_turn

from agents_app_connector.plugins.sketch import PLUGIN


def test_prompt_image_before_caption_and_instruction():
    t = make_turn()
    p = PLUGIN.render_prompt(t)
    aid = t.files[0].attachment_id
    img = p.index(f"sketch-{aid}.png")
    assert img < p.index("login screen")
    assert img < p.index("Reply with only the markdown spec.")
    assert f'Glob ".attachments/**/sketch-{aid}.png"' in p
    assert "Attached files" in p
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
