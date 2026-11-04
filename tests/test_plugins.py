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


def test_plugin_has_no_reply_parser():
    assert not hasattr(PLUGIN, "outbound")
    assert "outbound" not in plugins.ClientPlugin.__dict__


def test_preset_model_and_prompt():
    preset = load_preset()
    spec = preset["spec"]
    assert spec["model"] == "claude-sonnet-5-5"
    text = spec["wrapper"]["system_append"]
    assert len(text) <= 16384
    assert "[[?word]]" in text and "mermaid" in text
    assert preset["superleader_manageable"] is True
    assert "allow" not in spec["wrapper"]["permissions"]
    assert "discard_after_idle_seconds" not in spec
    assert spec["inactivity"] == {"close_seconds": 900}
    assert "crop" not in text.lower()


def test_preset_keys_valid_for_session_spec():
    spec = load_preset()["spec"]
    assert set(spec) <= {
        "model",
        "replicas",
        "reuse_running_agent",
        "inactivity",
        "peers",
        "wrapper",
    }
    assert set(spec["wrapper"]) <= {"system_append", "permissions", "mcp_servers"}


def test_text_is_ascii():
    root = Path(plugins.__file__).parent / "sketch"
    for f in root.iterdir():
        if f.is_file() and f.suffix in {".py", ".md", ".json"}:
            f.read_bytes().decode("ascii")


def test_preset_is_read_only_without_network():
    perms = load_preset()["spec"]["wrapper"]["permissions"]
    assert "allow" not in perms
    for tool in (
        "Bash",
        "Write",
        "Edit",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "Task",
        "Agent",
        "mcp__any-mcp__*",
        "mcp__ccw__*",
        "mcp__plugin_*",
        "mcp__claude_ai_*",
    ):
        assert tool in perms["deny"]
    assert "mcp__*" not in perms["deny"]
    assert not any(d.startswith("mcp__appconn") for d in perms["deny"])
    assert load_preset()["spec"]["peers"] is False


def test_preset_points_the_translator_at_the_connector_mcp_server():
    servers = load_preset()["spec"]["wrapper"]["mcp_servers"]
    assert list(servers) == ["appconn"]
    assert servers["appconn"] == {
        "type": "http",
        "url": "@mcp_url",
        "headers": {"Authorization": "Bearer ${ORCH_AGENT_TOKEN}"},
    }
