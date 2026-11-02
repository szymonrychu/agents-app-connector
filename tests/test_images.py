import io
import json
import struct
import zlib

import pytest
from PIL import Image

from agents_app_connector.api.errors import ApiError
from agents_app_connector.images import validate_png, validate_strokes

KW = {"max_long_edge": 1568, "min_edge": 200, "max_bytes": 6291456}


def png(w, h, mode="L"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), 255 if mode == "L" else (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def header_only(w, h):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def code(exc):
    return exc.value.status, exc.value.error


def test_valid_png_dims():
    assert validate_png(png(1568, 1176), **KW) == (1568, 1176)


def test_rgb_png_accepted():
    assert validate_png(png(300, 300, "RGB"), **KW) == (300, 300)


def test_long_edge_over_limit_422():
    with pytest.raises(ApiError) as exc:
        validate_png(png(1569, 800), **KW)
    assert code(exc) == (422, "bad_dimensions")


def test_tiny_422():
    with pytest.raises(ApiError) as exc:
        validate_png(png(150, 150), **KW)
    assert code(exc) == (422, "bad_dimensions")


def test_short_edge_422():
    with pytest.raises(ApiError) as exc:
        validate_png(png(1000, 199), **KW)
    assert code(exc) == (422, "bad_dimensions")


def test_over_max_bytes_413():
    with pytest.raises(ApiError) as exc:
        validate_png(png(800, 600), **{**KW, "max_bytes": 100})
    assert code(exc) == (413, "too_large")


def test_bad_magic_415():
    with pytest.raises(ApiError) as exc:
        validate_png(b"GIF89a" + b"\x00" * 100, **KW)
    assert code(exc) == (415, "invalid_png")


def test_truncated_415():
    with pytest.raises(ApiError) as exc:
        validate_png(png(800, 600)[:200], **KW)
    assert code(exc) == (415, "invalid_png")


def test_truncated_pixel_data_415():
    data = png(800, 600)
    with pytest.raises(ApiError) as exc:
        validate_png(data[: len(data) // 2] + data[-12:], **KW)
    assert code(exc) == (415, "invalid_png")


def test_bomb_415():
    with pytest.raises(ApiError) as exc:
        validate_png(header_only(20000, 20000), **KW)
    assert code(exc) == (415, "invalid_png")


def test_bomb_between_one_and_two_times_limit_415():
    with pytest.raises(ApiError) as exc:
        validate_png(header_only(9000, 9000), **KW)
    assert code(exc) == (415, "invalid_png")


GOOD = {"version": 1, "canvas": {"w": 100, "h": 100, "dpi": 300}, "strokes": []}


def test_strokes_shape():
    assert validate_strokes(json.dumps(GOOD).encode(), max_bytes=1000) == GOOD


@pytest.mark.parametrize(
    "doc",
    [
        b"not json",
        b"[]",
        json.dumps({**GOOD, "version": "1"}).encode(),
        json.dumps({**GOOD, "version": True}).encode(),
        json.dumps({**GOOD, "canvas": {"w": 1, "h": 1}}).encode(),
        json.dumps({**GOOD, "strokes": {}}).encode(),
        json.dumps({"version": 1, "canvas": GOOD["canvas"]}).encode(),
    ],
)
def test_strokes_invalid_422(doc):
    with pytest.raises(ApiError) as exc:
        validate_strokes(doc, max_bytes=1000)
    assert code(exc) == (422, "invalid_strokes")


def test_strokes_too_large_413():
    with pytest.raises(ApiError) as exc:
        validate_strokes(json.dumps(GOOD).encode(), max_bytes=10)
    assert code(exc) == (413, "too_large")
