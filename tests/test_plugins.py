from pathlib import Path
from uuid import uuid4

from agents_common.orchestrator import Attachment

from agents_app_connector import plugins
from agents_app_connector.plugins import PLUGIN_IDS, REGISTRY, TurnFile, TurnInput
from agents_app_connector.plugins.sketch import PLUGIN, SketchPlugin, load_preset


def make_turn(**kw):
    aid = uuid4()
    base = dict(
        kind="sketch",
        first=True,
        conversation_id=uuid4(),
        inbound_id=aid,
        caption="login screen",
        text=None,
        files=[
            TurnFile(aid, "sketch_png", "ignored.png", "image/png", b"PNGDATA"),
            TurnFile(uuid4(), "sketch_strokes", "ignored.json", "application/json", b"{}"),
        ],
        previous_spec=None,
    )
    base.update(kw)
    return TurnInput(**base)


def test_registry_has_sketch():
    assert {"sketch"} == PLUGIN_IDS
    assert REGISTRY["sketch"] is PLUGIN
    assert isinstance(PLUGIN, SketchPlugin) and PLUGIN.prefix == "/app/v1/sketch"
    assert frozenset(REGISTRY) == plugins.PLUGIN_IDS


def test_attachments_are_agents_common_attachments():
    t = make_turn()
    out = PLUGIN.turn_attachments(t)
    png, strokes = t.files
    assert out[0] == Attachment(f"sketch-{png.attachment_id}.png", "image/png", b"PNGDATA")
    assert out[1] == Attachment(f"strokes-{strokes.attachment_id}.json", "application/json", b"{}")
    assert all(isinstance(a, Attachment) for a in out)


def test_outbound_strips_fence_and_prose():
    text = "Here it is\n```markdown\n---\nid: x\n---\n# T\n```"
    assert PLUGIN.outbound(text) == ("---\nid: x\n---\n# T", True)


def test_outbound_plain_fence_and_no_close():
    assert PLUGIN.outbound("```\n---\nid: x\n# T\n```") == ("---\nid: x\n# T", False)


def test_outbound_without_front_matter():
    assert PLUGIN.outbound("# T") == ("# T", False)


def test_outbound_keeps_inner_fences():
    text = "---\nid: x\n---\n# T\n```text\nbox\n```"
    assert PLUGIN.outbound(text) == (text, True)


def test_preset_model_and_prompt():
    preset = load_preset()
    spec = preset["spec"]
    assert spec["model"] == "claude-sonnet-5-5"
    text = spec["wrapper"]["system_append"]
    assert len(text) <= 16384
    assert "[[?word]]" in text and "mermaid" in text
    assert preset["superleader_manageable"] is True
    assert spec["wrapper"]["permissions"]["allow"] == ["Read", "Glob"]
    assert "crop" not in text.lower()


def test_preset_keys_valid_for_session_spec():
    spec = load_preset()["spec"]
    assert set(spec) <= {
        "model",
        "replicas",
        "reuse_running_agent",
        "discard_after_idle_seconds",
        "peers",
        "wrapper",
    }
    assert 60 <= spec["discard_after_idle_seconds"] <= 2592000
    assert set(spec["wrapper"]) <= {"system_append", "permissions"}


def test_text_is_ascii():
    root = Path(plugins.__file__).parent / "sketch"
    for f in root.iterdir():
        if f.is_file() and f.suffix in {".py", ".md", ".json"}:
            f.read_bytes().decode("ascii")


def test_preset_is_read_only_without_network():
    perms = load_preset()["spec"]["wrapper"]["permissions"]
    assert perms["allow"] == ["Read", "Glob"]
    for tool in (
        "Bash",
        "Write",
        "Edit",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "Task",
        "mcp__*",
    ):
        assert tool in perms["deny"]
    assert not set(perms["allow"]) & set(perms["deny"])
    assert load_preset()["spec"]["peers"] is False
