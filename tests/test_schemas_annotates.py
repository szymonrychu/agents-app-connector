import uuid

import pytest
from helpers import count, files, meta, pages, post
from pydantic import ValidationError

from agents_app_connector.api.schemas import SketchMeta


def _ann(**over):
    base = {"spec_version": 2, "pages": [{"page": 0, "doc_page": 3, "lines": [4, 9]}]}
    return {**base, **over}


def _model(**over):
    base = {
        "client_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "pages": pages(0),
        "annotates": _ann(),
    }
    return SketchMeta.model_validate({**base, **over})


def test_annotates_valid_roundtrip():
    m = _model()
    assert m.annotates.spec_version == 2
    assert m.annotates.pages[0].lines == (4, 9) and m.annotates.pages[0].doc_page == 3
    again = SketchMeta.model_validate_json(m.model_dump_json())
    assert again == m
    assert _model(annotates=None).annotates is None


async def test_annotates_requires_conversation_id(device_api, token, conn):
    raw = meta(annotates=_ann())
    r = await post(device_api, token, raw)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert "annotates" in r.json()["detail"]
    assert await count(conn, "inbound") == 0
    with pytest.raises(ValidationError):
        _model(conversation_id=None)


@pytest.mark.parametrize(
    "bad",
    [
        [{"page": 1, "doc_page": 0, "lines": [0, 1]}],
        [
            {"page": 0, "doc_page": 0, "lines": [0, 1]},
            {"page": 0, "doc_page": 1, "lines": [2, 3]},
        ],
        [],
    ],
)
async def test_annotates_page_must_match_meta_pages(device_api, token, conn, bad):
    cid = str(uuid.uuid4())
    r = await post(device_api, token, meta(conversation_id=cid, annotates=_ann(pages=bad)))
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert "annotates" in r.json()["detail"]
    assert await count(conn, "inbound") == 0
    with pytest.raises(ValidationError):
        _model(annotates=_ann(pages=bad))


@pytest.mark.parametrize("lines", [[5, 2], [-1, 3], [1, 2, 3], [1]])
def test_annotates_bad_lines_rejected(lines):
    with pytest.raises(ValidationError):
        _model(annotates=_ann(pages=[{"page": 0, "doc_page": 0, "lines": lines}]))


def test_annotates_negative_doc_page_rejected():
    with pytest.raises(ValidationError):
        _model(annotates=_ann(pages=[{"page": 0, "doc_page": -1, "lines": [0, 1]}]))


@pytest.mark.parametrize("version", [0, -1])
def test_annotates_spec_version_zero_rejected(version):
    with pytest.raises(ValidationError):
        _model(annotates=_ann(spec_version=version))


def test_annotates_unknown_field_rejected():
    with pytest.raises(ValidationError):
        _model(annotates=_ann(annotates_version=1))
    with pytest.raises(ValidationError):
        _model(annotates=_ann(pages=[{"page": 0, "doc_page": 0, "lines": [0, 1], "x": 1}]))


async def test_annotates_multi_page_valid_through_route_shape(device_api, token):
    # shape check only: the spec does not exist yet, so the route answers beyond 422
    cid = str(uuid.uuid4())
    ann = _ann(
        pages=[
            {"page": 0, "doc_page": 0, "lines": [0, 3]},
            {"page": 1, "doc_page": 2, "lines": [4, 9]},
        ]
    )
    r = await post(
        device_api,
        token,
        meta(conversation_id=cid, pages=pages(0, 1), annotates=ann),
        files(count=2),
    )
    assert r.status_code != 422
