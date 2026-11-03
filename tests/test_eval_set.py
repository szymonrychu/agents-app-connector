"""Synthetic eval set: regeneration, upload validity and the scoring function."""

import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image

from agents_app_connector.images import validate_png

ROOT = Path(__file__).resolve().parent.parent
NAMES = [
    "01-notes",
    "02-flowchart",
    "03-table",
    "04-wireframe",
    "05-todo",
    "06-sequence",
    "07-rotated",
    "08-mixed",
]


def load(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


make = load("make_eval_set")
ev = load("eval_sketches")


def test_eval_set_regenerates_identically(tmp_path):
    make.generate(tmp_path)
    assert sorted(p.stem for p in (tmp_path / "sketches").glob("*.png")) == NAMES
    for name in NAMES:
        # Pixels, not file bytes: the PNG encoder output may differ between zlib builds.
        with (
            Image.open(tmp_path / "sketches" / f"{name}.png") as new,
            Image.open(ROOT / "eval" / "sketches" / f"{name}.png") as old,
        ):
            assert new.mode == old.mode == "L"
            assert new.size == old.size
            assert new.tobytes() == old.tobytes()
        assert (tmp_path / "expected" / f"{name}.json").read_bytes() == (
            ROOT / "eval" / "expected" / f"{name}.json"
        ).read_bytes()


def test_items_valid_for_upload():
    for name in NAMES:
        data = (ROOT / "eval" / "sketches" / f"{name}.png").read_bytes()
        w, h = validate_png(data, max_long_edge=1568, min_edge=200, max_bytes=6291456)
        assert max(w, h) == 1568
        expected = json.loads((ROOT / "eval" / "expected" / f"{name}.json").read_text())
        assert expected["words"]
        assert expected["diagram"] in ("flowchart", "table", "sequenceDiagram", None)
        assert expected["min_word_recall"] == 0.8


FRONT = (
    "---\nid: abc\nsource_image: sketch-abc.png\ncreated: 2026-11-02T10:00:00Z\n"
    "confidence: high\nunreadable_count: 0\n---\n# Title\n\n"
)


def test_score_spec_recall_and_markers():
    exp = {"words": ["buy", "milk", "eggs", "bread"], "diagram": None, "min_word_recall": 0.8}
    s = ev.score(FRONT + "Buy [[?milk]] and [[?egg|eggs]]\n", exp)
    assert s.recall == 0.75
    assert s.diagram_ok is True
    assert s.front_matter is True


def test_score_spec_diagrams():
    mer = FRONT + "```mermaid\nflowchart TD\n  A --> B\n```\n"
    seq = FRONT + "```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"
    tab = FRONT + "| a | b |\n|---|---|\n| 1 | 2 |\n"
    e = {"words": [], "min_word_recall": 0.8}
    assert ev.score(mer, {**e, "diagram": "flowchart"}).diagram_ok
    assert not ev.score(seq, {**e, "diagram": "flowchart"}).diagram_ok
    assert ev.score(seq, {**e, "diagram": "sequenceDiagram"}).diagram_ok
    assert ev.score(tab, {**e, "diagram": "table"}).diagram_ok
    assert not ev.score(mer, {**e, "diagram": "table"}).diagram_ok


def test_score_spec_front_matter_missing_key():
    bad = "---\nid: abc\n---\n# T\n"
    s = ev.score(bad, {"words": ["t"], "diagram": None, "min_word_recall": 0.8})
    assert s.front_matter is False
    assert (
        ev.score("# T\n", {"words": [], "diagram": None, "min_word_recall": 0.8}).front_matter
        is False
    )


def _item_client(events, fail_get=False):
    import httpx

    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(202, json={"conversation_id": "c1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/events"):
            if fail_get:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"cursor": 1, "events": events})
        return httpx.Response(200, text="# T")

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_run_item_deletes_conversation_when_done():
    events = [{"type": "sketch_status", "status": "done"}]
    client, calls = _item_client(events)
    ev.run_item(client, "http://x", "01-notes", 5)
    assert ("DELETE", "/conversations/c1") in calls


def test_run_item_deletes_conversation_on_timeout():
    client, calls = _item_client([])
    ev.run_item(client, "http://x", "01-notes", 0)
    assert ("DELETE", "/conversations/c1") in calls


def test_run_item_deletes_conversation_on_failure():
    client, calls = _item_client([], fail_get=True)
    result = ev.run_item(client, "http://x", "01-notes", 5)
    assert result["error"] == "ConnectError"
    assert ("DELETE", "/conversations/c1") in calls
