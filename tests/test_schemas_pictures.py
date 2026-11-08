import uuid

import pytest
from pydantic import ValidationError

from agents_app_connector.api.schemas import PageMeta, PictureRegion

CANVAS = {"w": 1568, "h": 1176, "dpi": 300}
U = "6f1c2a3e-1b2c-4d5e-8f90-123456789abc"


def test_page_meta_regions_roundtrip():
    meta = PageMeta.model_validate(
        {
            "page": 0,
            "canvas": CANVAS,
            "regions": [{"id": U, "rect": [10, 20, 300, 200], "part": "region.0.0.png"}],
        }
    )
    assert meta.regions[0].rect == (10.0, 20.0, 300.0, 200.0)
    assert meta.regions[0].id == uuid.UUID(U)


def test_regions_default_empty():
    assert PageMeta.model_validate({"page": 0, "canvas": CANVAS}).regions == []


@pytest.mark.parametrize("part", ["region.0.png", "sketch.0.png", "region.a.0.png"])
def test_picture_region_bad_part_rejected(part):
    with pytest.raises(ValidationError):
        PictureRegion.model_validate({"id": U, "rect": [0, 0, 5, 5], "part": part})


@pytest.mark.parametrize("rect", [[1, 2, 3], [0, 0, 0, 10], [-1, 0, 5, 5], [0, 0, "inf", 5]])
def test_picture_region_bad_rect_rejected(rect):
    with pytest.raises(ValidationError):
        PictureRegion.model_validate({"id": U, "rect": rect, "part": "region.0.0.png"})


def test_picture_region_unknown_field_rejected():
    with pytest.raises(ValidationError):
        PictureRegion.model_validate(
            {"id": U, "rect": [0, 0, 5, 5], "part": "region.0.0.png", "after_line": "L1"}
        )
