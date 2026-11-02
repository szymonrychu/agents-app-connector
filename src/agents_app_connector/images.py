"""Validation of untrusted sketch uploads: PNG images and stroke JSON."""

import io
import json
import warnings

from PIL import Image, UnidentifiedImageError

from .api.errors import ApiError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 8000 * 8000

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def validate_png(
    data: bytes, *, max_long_edge: int, min_edge: int, max_bytes: int
) -> tuple[int, int]:
    """Return (width, height) of a fully decodable PNG, or raise ApiError."""
    if len(data) > max_bytes:
        raise ApiError(413, "too_large", f"png exceeds {max_bytes} bytes")
    if not data.startswith(PNG_MAGIC):
        raise ApiError(415, "invalid_png", "not a PNG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
            with Image.open(io.BytesIO(data)) as img:
                if img.format != "PNG":
                    raise ApiError(415, "invalid_png", "not a PNG")
                width, height = img.size
                if width * height > MAX_PIXELS:
                    raise ApiError(415, "invalid_png", "image has too many pixels")
                img.load()
    except ApiError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise ApiError(415, "invalid_png", "PNG is corrupt, truncated or too large") from None
    if max(width, height) > max_long_edge:
        raise ApiError(422, "bad_dimensions", f"long edge exceeds {max_long_edge} px")
    if min(width, height) < min_edge:
        raise ApiError(422, "bad_dimensions", f"short edge below {min_edge} px")
    return width, height


def validate_strokes(data: bytes, *, max_bytes: int) -> dict:
    """Return the parsed stroke document, or raise ApiError."""
    if len(data) > max_bytes:
        raise ApiError(413, "too_large", f"strokes exceed {max_bytes} bytes")
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        raise ApiError(422, "invalid_strokes", "strokes are not valid JSON") from None
    if not isinstance(doc, dict):
        raise ApiError(422, "invalid_strokes", "strokes must be a JSON object")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ApiError(422, "invalid_strokes", "version must be an integer")
    canvas = doc.get("canvas")
    if not isinstance(canvas, dict) or not all(k in canvas for k in ("w", "h", "dpi")):
        raise ApiError(422, "invalid_strokes", "canvas needs w, h and dpi")
    if not isinstance(doc.get("strokes"), list):
        raise ApiError(422, "invalid_strokes", "strokes must be a list")
    return doc
