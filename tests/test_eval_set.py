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
    "09-colour-shapes",
    "10-annotated-spec",
    "11-bare-wireframe",
]
COLOUR = "09-colour-shapes"
ANNOTATED = "10-annotated-spec"
BARE = "11-bare-wireframe"
RGB_ITEMS = (COLOUR, ANNOTATED)


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
            assert new.mode == old.mode == ("RGB" if name in RGB_ITEMS else "L")
            assert new.size == old.size
            assert new.tobytes() == old.tobytes()
        assert (tmp_path / "expected" / f"{name}.json").read_bytes() == (
            ROOT / "eval" / "expected" / f"{name}.json"
        ).read_bytes()


def test_colour_item_is_rgb_and_others_grayscale():
    for name in NAMES:
        with Image.open(ROOT / "eval" / "sketches" / f"{name}.png") as im:
            assert im.mode == ("RGB" if name in RGB_ITEMS else "L"), name
    with Image.open(ROOT / "eval" / "sketches" / f"{COLOUR}.png") as im:
        r, g, b = (im.getchannel(i).getextrema()[0] for i in range(3))
        assert min(r, g, b) < 100  # inked in red or blue, not only gray


def test_items_valid_for_upload():
    for name in NAMES:
        data = (ROOT / "eval" / "sketches" / f"{name}.png").read_bytes()
        w, h = validate_png(data, max_long_edge=1568, min_edge=200, max_bytes=6291456)
        assert max(w, h) == 1568
        expected = json.loads((ROOT / "eval" / "expected" / f"{name}.json").read_text())
        assert expected["words"]
        assert expected["diagram"] in ("flowchart", "table", "sequenceDiagram", None)
        assert expected["min_word_recall"] == 0.8
        assert expected["min_precision"] == (0.95 if name == BARE else 0.9)


def test_annotated_item_regenerates_identically_with_base_page_and_spec(tmp_path):
    make.generate(tmp_path)
    for rel in ("specs/10-annotated-spec.md", "specs/10-annotated-spec.png"):
        new = (tmp_path / rel).read_bytes() if rel.endswith(".md") else None
        if new is not None:
            assert new == (ROOT / "eval" / rel).read_bytes()
        else:
            with (
                Image.open(tmp_path / rel) as a,
                Image.open(ROOT / "eval" / rel) as b,
            ):
                assert a.mode == b.mode == "RGB" and a.size == b.size
                assert a.tobytes() == b.tobytes()
    assert not (tmp_path / "specs" / "01-notes.md").exists()
    # the annotated page is the base page plus ink
    with (
        Image.open(ROOT / "eval" / "specs" / "10-annotated-spec.png") as base,
        Image.open(ROOT / "eval" / "sketches" / "10-annotated-spec.png") as page,
    ):
        assert base.size == page.size and base.tobytes() != page.tobytes()


def test_annotated_item_expected_has_must_keep():
    exp = json.loads((ROOT / "eval" / "expected" / "10-annotated-spec.json").read_text())
    assert exp["must_keep"] and exp["words"]
    assert not set(exp["must_keep"]) & {"call", "bob"}  # struck out in the page
    for name in [n for n in NAMES if n != ANNOTATED]:
        other = json.loads((ROOT / "eval" / "expected" / f"{name}.json").read_text())
        assert "must_keep" not in other


FRONT = ""


def test_score_spec_recall_and_markers():
    exp = {"words": ["buy", "milk", "eggs", "bread"], "diagram": None, "min_word_recall": 0.8}
    s = ev.score(FRONT + "Buy [[?milk]] and [[?egg|eggs]]\n", exp)
    assert s.recall == 0.75
    assert s.diagram_ok is True


def test_score_must_keep_missing_is_reported():
    exp = {
        "words": ["bread"],
        "must_keep": ["milk", "eggs", "ship"],
        "diagram": None,
        "min_word_recall": 0.8,
    }
    s = ev.score(FRONT + "Buy milk\nAdd bread\nShip parcel\n", exp)
    assert s.recall == 1.0
    assert s.lost == ["eggs"]
    assert round(s.kept, 3) == 0.667
    clean = ev.score(FRONT + "milk eggs ship bread", exp)
    assert clean.lost == [] and clean.kept == 1.0
    none = ev.score(FRONT + "x", {"words": [], "diagram": None, "min_word_recall": 0.8})
    assert none.kept == 1.0 and none.lost == []


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


def test_score_precision_counts_only_drawn_words():
    exp = {"words": ["login", "email", "password", "sign", "in"], "diagram": None}
    assert ev.score("LOGIN\n\nEMAIL\n\nPASSWORD\n\n[ SIGN IN ]", exp).precision == 1.0
    invented = ev.score(
        "# Login\n\n## Requirements\n\n- Email field\n- Password field\n- Sign in", exp
    )
    assert invented.precision == 5 / 8
    assert invented.invented == ["requirements", "field"]
    assert ev.score("", exp).precision == 1.0


def test_score_precision_ignores_markers_and_diagram_syntax():
    exp = {"words": ["start", "valid", "yes", "no", "save", "reject"], "diagram": "flowchart"}
    md = (
        "```mermaid\nflowchart TD\n  A[START] --> B{VALID?}\n  B -->|YES| C[SAVE]\n"
        '  B -->|NO| D["REJECT"]\n  E[""]\n```\n\n[[?save|sove]] [illegible] [[red]]start[[/red]]\n'
    )
    assert ev.score(md, exp).precision == 1.0
    seq = (
        "```mermaid\nsequenceDiagram\n  participant C as CLIENT\n  participant SERVER\n"
        "  C->>SERVER: LOGIN\n```\n"
    )
    assert ev.score(seq, {"words": ["client", "server", "login"], "diagram": None}).precision == 1.0
    table = "| NAME | ROLE |\n|---|---|\n| ALICE | DEV |\n"
    exp_t = {"words": ["name", "role", "alice", "dev"], "diagram": "table"}
    assert ev.score(table, exp_t).precision == 1.0
    wire = "```text\n+--------+\n| MENU   |\n+--------+\n```\n"
    assert ev.score(wire, {"words": ["menu"], "diagram": None}).precision == 1.0


def test_score_precision_counts_must_keep_as_drawn():
    exp = {"words": ["bread"], "must_keep": ["milk"], "diagram": None}
    assert ev.score("milk bread call", exp).precision == 2 / 3


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
        if request.url.path.endswith("/feedback"):
            after = int(request.url.params.get("after", 0))
            items = [{"id": 1, "kind": "question", "text": "?"}] if after == 0 else []
            return httpx.Response(200, json={"items": items, "cursor": 1})
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


def test_run_item_annotated_uploads_base_then_annotation():
    import httpx

    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(202, json={"conversation_id": "c1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/events"):
            done = {"type": "sketch_status", "status": "done"}
            return httpx.Response(200, json={"cursor": 2, "events": [done, done]})
        if request.url.path.endswith("/feedback"):
            return httpx.Response(200, json={"items": [], "cursor": 0})
        return httpx.Response(
            200, text="SHOPPING LIST\n\nBUY MILK\n\nBUY EGGS\n\nCALL BOB\n\nSHIP PARCEL\n"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = ev.run_item(client, "http://x", ANNOTATED, 5)
    assert len(posts) == 2
    assert b"annotates" not in posts[0] and b'"conversation": "new"' in posts[0]
    assert b'"annotates"' in posts[1] and b'"spec_version": 1' in posts[1]
    assert b'"conversation_id": "c1"' in posts[1] and b'"lines": [0, 8]' in posts[1]
    assert result["status"] == "done" and "kept" in result


def test_run_item_plain_item_uploads_once():
    client, calls = _item_client([{"type": "sketch_status", "status": "done"}])
    ev.run_item(client, "http://x", "01-notes", 5)
    assert [c for c in calls if c[0] == "POST"] == [("POST", "/sketches")]


def test_run_item_reports_precision_and_feedback_count_without_front_matter():
    client, _ = _item_client([{"type": "sketch_status", "status": "done"}])
    result = ev.run_item(client, "http://x", "01-notes", 5)
    assert result["status"] == "done" and result["feedback"] == 1
    assert "precision" in result and "front_matter" not in result
    table = ev.format_table([result])
    assert "precision" in table and "feedback" in table and "front_matter" not in table


def test_bare_wireframe_item_has_four_labels_and_strict_precision():
    exp = json.loads((ROOT / "eval" / "expected" / f"{BARE}.json").read_text())
    assert exp["words"] == ["login", "email", "password", "sign", "in"]
    assert exp["diagram"] is None and exp["min_precision"] == 0.95


def test_score_flags_front_matter_and_added_headings():
    exp = {"words": ["login", "email", "password", "sign", "in"], "diagram": None}
    bad = (
        "---\nid: 1\nconfidence: high\n---\n# Login screen\n\n## Transcription\n\n"
        "LOGIN\n\n## Requirements\n\n- EMAIL\n"
    )
    sc = ev.score(bad, exp)
    assert sc.added_structure == [
        "front matter",
        "# Login screen",
        "## Transcription",
        "## Requirements",
    ]
    good = ev.score("LOGIN\n\nEMAIL\n\nPASSWORD\n\n[ SIGN IN ]", exp)
    assert good.added_structure == []


def test_bare_wireframe_item_fails_on_invented_sections_and_passes_on_the_bare_text():
    exp = json.loads((ROOT / "eval" / "expected" / "11-bare-wireframe.json").read_text())
    invented = ev.score(
        "# Login\n\n## Transcription\n\nLOGIN\nEMAIL\nPASSWORD\nSIGN IN\n\n"
        "## Requirements\n\n- The user can sign in\n- The password is hidden\n",
        exp,
    )
    assert invented.precision < exp["min_precision"]
    assert invented.added_structure
    bare = ev.score("LOGIN\n\nEMAIL\n\nPASSWORD\n\nSIGN IN\n", exp)
    assert bare.precision >= exp["min_precision"] and bare.recall == 1.0
    assert bare.added_structure == []


def test_illegible_marker_carries_no_word():
    exp = {"words": ["buy", "milk"], "diagram": None}
    sc = ev.score("BUY [illegible] MILK", exp)
    assert sc.recall == 1.0 and sc.precision == 1.0
