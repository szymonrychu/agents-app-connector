"""Wire models of notebook pages: layout.<i>.json, ops, regions and the stored spec layout."""

import uuid

import pytest
from helpers import pages
from pydantic import ValidationError

from agents_app_connector.api.schemas import SketchMeta
from agents_app_connector.plugins.sketch import layout

GEOMETRY = {
    "cell": 53,
    "row_pitch": 106,
    "top": 106,
    "rows": 16,
    "text_left": 53,
    "margin_left": 983,
}


def op(kind, **over):
    base = {"id": str(uuid.uuid4()), "kind": kind, "strokes": [0]}
    fields = {
        "replace": {"target": {"line_id": "L1", "from": 0, "to": 3, "quote": "buy"}},
        "delete": {"target": {"line_id": "L1", "from": 4, "to": 8, "quote": "milk"}},
        "insert": {
            "at": {"line_id": "L1", "offset": 8, "mode": "inline"},
            "rect": [990, 300, 200, 90],
        },
        "image": {
            "region": {"id": str(uuid.uuid4()), "part": "region.0.0.png", "after_line": "L1"},
            "rect": [60, 600, 400, 300],
        },
    }[kind]
    return {**base, **fields, **over}


def page_layout(**over):
    base = {
        "version": 1,
        "geometry": GEOMETRY,
        "segments": [{"id": "p0-r4-0", "row": 4, "x0": 60.0, "x1": 900.5, "strokes": [1, 2]}],
        "runs": [{"line_id": "L1", "row": 1, "x0": 53.0}],
        "ops": [op("replace"), op("delete"), op("insert"), op("image")],
    }
    return {**base, **over}


def test_page_layout_round_trip():
    parsed = layout.PageLayout.model_validate(page_layout())
    dumped = parsed.model_dump(mode="json", by_alias=True)
    assert dumped["ops"][0]["target"]["from"] == 0
    assert layout.PageLayout.model_validate(dumped) == parsed
    assert parsed.ops[3].region.part == "region.0.0.png"
    assert layout.layout_field(2) == "layout.2.json"
    assert layout.region_field(1, 3) == "region.1.3.png"


@pytest.mark.parametrize(
    ("kind", "missing"),
    [
        ("replace", "target"),
        ("delete", "target"),
        ("insert", "at"),
        ("insert", "rect"),
        ("image", "region"),
        ("image", "rect"),
    ],
)
def test_op_kind_requires_its_fields(kind, missing):
    raw = op(kind)
    raw.pop(missing)
    with pytest.raises(ValidationError, match=missing):
        layout.EditOp.model_validate(raw)


@pytest.mark.parametrize("bad", ["p0-4-0", "x0-r4-0", "p0-r4", "p0-r4-0 ", "p-1-r4-0"])
def test_segment_id_pattern(bad):
    with pytest.raises(ValidationError):
        layout.Segment.model_validate({"id": bad, "row": 4, "x0": 0, "x1": 1, "strokes": []})
    assert layout.Segment.model_validate(
        {"id": "p12-r15-3", "row": 15, "x0": 0, "x1": 1, "strokes": []}
    )


@pytest.mark.parametrize("where", ["top", "geometry", "segment", "op", "target"])
def test_unknown_key_rejected(where):
    raw = page_layout()
    target = {
        "top": raw,
        "geometry": raw["geometry"],
        "segment": raw["segments"][0],
        "op": raw["ops"][0],
        "target": raw["ops"][0]["target"],
    }[where]
    target["extra"] = 1
    with pytest.raises(ValidationError):
        layout.PageLayout.model_validate(raw)


def test_line_id_and_region_part_patterns():
    with pytest.raises(ValidationError):
        layout.VisibleRun.model_validate({"line_id": "12", "row": 0, "x0": 0})
    with pytest.raises(ValidationError):
        layout.EditOp.model_validate(op("image", region={"id": str(uuid.uuid4()), "part": "r.png"}))


def test_spec_layout_defaults():
    stored = layout.SpecLayout.model_validate(
        {
            "lines": [{"line_id": "L1", "source_line": 1, "origin": None}],
            "ops": [],
            "base_version": None,
        }
    )
    assert stored.reverted_from is None


def _meta(**over):
    base = {"client_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4()), "pages": pages(0)}
    return SketchMeta.model_validate({**base, **over})


def test_base_version_requires_conversation_id():
    assert _meta(base_version=3).base_version == 3
    with pytest.raises(ValidationError, match="base_version requires conversation_id"):
        _meta(base_version=3, conversation_id=None)
    with pytest.raises(ValidationError):
        _meta(base_version=0)


def test_base_version_excludes_annotates():
    ann = {"spec_version": 1, "pages": [{"page": 0, "doc_page": 0, "lines": [0, 1]}]}
    with pytest.raises(ValidationError, match="base_version cannot be combined with annotates"):
        _meta(base_version=1, annotates=ann)
