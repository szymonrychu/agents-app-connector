"""Generate the synthetic handwriting eval set into eval/sketches and eval/expected.

    uv run python scripts/make_eval_set.py [--out eval]

Deterministic (random seed 7) and independent of platform fonts: every glyph is drawn from a
built-in stroke table with Pillow ImageDraw primitives only, with per-glyph jitter and a slight
baseline wobble to look hand drawn. Images are 8-bit grayscale `L`, long edge 1568, except
09-colour-shapes and 10-annotated-spec, which are `RGB` (black, blue and red pens).

11-bare-wireframe is a login wireframe with only four labels, the shape that invites invented
requirements; its expected precision is stricter (0.95 instead of 0.9).

10-annotated-spec also writes specs/10-annotated-spec.png (the page without notes, uploaded
first to get spec v1) and specs/10-annotated-spec.md (the text it prints).

12-15 are notebook pages (page canvas 1404 x 1872 px, rows of 106 px from y 106, margin from
x 983; the PNG is the page scaled to a 1568 px long edge, no grid). Each also writes
layouts/<item>.json (the page layout the app would send), strokes/<item>.json (its recorded
strokes in page px) and, for 13-15, a base page: specs/<item>.png with its own
layouts/<item>.base.json and strokes/<item>.base.json, and specs/<item>.md, the text the base
should become (spec v1, one source line per row). The annotated page shows that text printed on
its rows. 15-image-region also writes regions/15-image-region.0.0.png, the app's crop.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

SEED = 7
W, H = 1568, 1176
PEN = 4
BLACK, BLUE, RED = (0, 0, 0), (0, 0, 255), (220, 0, 0)
MIN_RECALL = 0.8
MIN_PRECISION = 0.9

_P = [(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5)]
_O = [(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0)]
_PP = [(0, 6), (0, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]

# Glyph polylines on a 4 x 6 grid (x right, y down).
GLYPHS: dict[str, list[list[tuple[float, float]]]] = {
    "A": [[(0, 6), (2, 0), (4, 6)], [(0.8, 4), (3.2, 4)]],
    "B": [[(0, 6), (0, 0), (3, 0), (4, 1), (3, 3), (0, 3)], [(3, 3), (4, 4.5), (3, 6), (0, 6)]],
    "C": [_P],
    "D": [[(0, 0), (0, 6), (2.5, 6), (4, 4.5), (4, 1.5), (2.5, 0), (0, 0)]],
    "E": [[(4, 0), (0, 0), (0, 6), (4, 6)], [(0, 3), (3, 3)]],
    "F": [[(4, 0), (0, 0), (0, 6)], [(0, 3), (3, 3)]],
    "G": [_P, [(4, 5), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 0), (3, 0)], [(2, 0), (2, 6)], [(1, 6), (3, 6)]],
    "J": [[(4, 0), (4, 5), (3, 6), (1, 6), (0, 5)]],
    "K": [[(0, 0), (0, 6)], [(4, 0), (0, 3.5)], [(1.5, 2.5), (4, 6)]],
    "L": [[(0, 0), (0, 6), (4, 6)]],
    "M": [[(0, 6), (0, 0), (2, 3), (4, 0), (4, 6)]],
    "N": [[(0, 6), (0, 0), (4, 6), (4, 0)]],
    "O": [_O],
    "P": [_PP],
    "Q": [_O, [(2.5, 4.5), (4, 6.5)]],
    "R": [_PP, [(2, 3), (4, 6)]],
    "S": [
        [
            (4, 1),
            (3, 0),
            (1, 0),
            (0, 1),
            (0, 2),
            (1, 3),
            (3, 3),
            (4, 4),
            (4, 5),
            (3, 6),
            (1, 6),
            (0, 5),
        ]
    ],
    "T": [[(0, 0), (4, 0)], [(2, 0), (2, 6)]],
    "U": [[(0, 0), (0, 5), (1, 6), (3, 6), (4, 5), (4, 0)]],
    "V": [[(0, 0), (2, 6), (4, 0)]],
    "W": [[(0, 0), (1, 6), (2, 3), (3, 6), (4, 0)]],
    "X": [[(0, 0), (4, 6)], [(4, 0), (0, 6)]],
    "Y": [[(0, 0), (2, 3), (4, 0)], [(2, 3), (2, 6)]],
    "Z": [[(0, 0), (4, 0), (0, 6), (4, 6)]],
    "0": [_O],
    "1": [[(1, 1), (2, 0), (2, 6)], [(1, 6), (3, 6)]],
    "2": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (0, 6), (4, 6)]],
    "3": [[(0, 0), (4, 0), (2, 2.5), (4, 4), (4, 5), (3, 6), (1, 6), (0, 5)]],
    "4": [[(3, 6), (3, 0), (0, 4), (4, 4)]],
    "5": [[(4, 0), (0, 0), (0, 3), (3, 3), (4, 4), (4, 5), (3, 6), (0, 6)]],
    "6": [[(4, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]],
    "7": [[(0, 0), (4, 0), (1.5, 6)]],
    "8": [
        [(1, 0), (3, 0), (4, 1), (3, 3), (1, 3), (0, 1), (1, 0)],
        [(3, 3), (4, 4.5), (3, 6), (1, 6), (0, 4.5), (1, 3)],
    ],
    "9": [[(4, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (0, 6)]],
    "-": [[(0.5, 3), (3.5, 3)]],
    "?": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (2, 3), (2, 4)], [(2, 5.6), (2, 6)]],
    ".": [[(2, 5.6), (2, 6)]],
    ":": [[(2, 1.6), (2, 2)], [(2, 4.6), (2, 5)]],
}
ADVANCE = 5.6  # glyph pitch in grid units


class Sheet:
    """A page with hand-drawn looking primitives; records the words it writes.

    Grayscale `L` by default; `rgb=True` makes an `RGB` page whose pen is set with `ink`.
    """

    def __init__(self, rng: random.Random, size: tuple[int, int] = (W, H), *, rgb: bool = False):
        self.rng = rng
        self.rgb = rgb
        self.ink: int | tuple[int, int, int] = BLACK if rgb else 0
        self.img = Image.new("RGB" if rgb else "L", size, (255, 255, 255) if rgb else 255)
        self.d = ImageDraw.Draw(self.img)
        self.words: list[str] = []
        self.base: Image.Image | None = None  # the page before the notes (annotated item)
        self.base_md = ""
        self.extra: dict[str, list[str]] = {}  # extra keys of the expected JSON
        self.min_precision = MIN_PRECISION

    def _j(self, amount: float) -> float:
        return self.rng.uniform(-amount, amount)

    def _stroke(self, pts: list[tuple[float, float]], jitter: float = 1.0) -> None:
        pts = [(x + self._j(jitter), y + self._j(jitter)) for x, y in pts]
        self.d.line(pts, fill=self.ink, width=PEN, joint="curve")

    def text_width(self, s: str, h: float) -> float:
        return len(s) * ADVANCE * h / 6

    def text(self, x: float, y: float, s: str, h: float = 44, *, center: bool = False) -> None:
        self.words.extend(re.findall(r"[a-z0-9]+", s.lower()))
        if center:
            x -= self.text_width(s, h) / 2
        k = h / 6
        phase = self.rng.uniform(0, math.tau)
        for i, ch in enumerate(s.upper()):
            gx = x + i * ADVANCE * k + self._j(2.0)
            gy = y + 3 * math.sin(phase + i * 0.7) + self._j(1.5)
            for line in GLYPHS.get(ch, []):
                self._stroke([(gx + px * k, gy + py * k) for px, py in line], 0.8)

    def line(self, a: tuple[float, float], b: tuple[float, float]) -> None:
        mid = ((a[0] + b[0]) / 2 + self._j(3), (a[1] + b[1]) / 2 + self._j(3))
        self._stroke([a, mid, b], 1.5)

    def dashed(self, a: tuple[float, float], b: tuple[float, float], dash: float = 24) -> None:
        n = max(1, int(math.dist(a, b) // (dash * 2)))
        for i in range(n):
            t0, t1 = 2 * i / (2 * n), (2 * i + 1) / (2 * n)
            self._stroke(
                [
                    (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                    (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1),
                ],
                1.0,
            )

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        for a, b in (
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        ):
            self.line(a, b)

    def diamond(self, cx: float, cy: float, hw: float, hh: float) -> None:
        pts = [(cx, cy - hh), (cx + hw, cy), (cx, cy + hh), (cx - hw, cy), (cx, cy - hh)]
        for a, b in zip(pts, pts[1:], strict=False):
            self.line(a, b)

    def circle(self, cx: float, cy: float, r: float) -> None:
        j = self._j
        self.d.ellipse(
            (cx - r + j(2), cy - r + j(2), cx + r + j(2), cy + r + j(2)), outline=0, width=PEN
        )

    def arrow(self, a: tuple[float, float], b: tuple[float, float], head: float = 22) -> None:
        self.line(a, b)
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        for da in (2.6, -2.6):
            self._stroke([b, (b[0] + head * math.cos(ang + da), b[1] + head * math.sin(ang + da))])

    def labeled_box(
        self, x0: float, y0: float, x1: float, y1: float, s: str, h: float = 44
    ) -> None:
        self.rect(x0, y0, x1, y1)
        self.text((x0 + x1) / 2, (y0 + y1) / 2 - h / 2, s, h, center=True)


def notes(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.text(100, 80, "MEETING NOTES", 64)
    s.line((100, 170), (900, 170))
    for i, item in enumerate(["BUY MILK", "CALL BOB FRIDAY", "SHIP RELEASE 2", "REVIEW DOCS"]):
        y = 260 + i * 140
        s.circle(130, y + 22, 12)
        s.text(190, y, item, 52)
    return s


def flowchart(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.labeled_box(100, 80, 420, 190, "START")
    s.arrow((260, 190), (260, 300))
    s.diamond(260, 460, 220, 160)
    s.text(260, 440, "VALID?", 44, center=True)
    s.arrow((480, 460), (860, 460))
    s.text(670, 400, "YES", 36, center=True)
    s.labeled_box(860, 400, 1180, 520, "SAVE")
    s.arrow((260, 620), (260, 800))
    s.text(330, 690, "NO", 36, center=True)
    s.labeled_box(100, 800, 420, 920, "REJECT")
    return s


def table(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    x0, y0, cw, rh = 140, 160, 430, 190
    for c in range(4):
        s.line((x0 + c * cw, y0), (x0 + c * cw, y0 + 4 * rh))
    for r in range(5):
        s.line((x0, y0 + r * rh), (x0 + 3 * cw, y0 + r * rh))
    rows = [
        ["NAME", "ROLE", "STATUS"],
        ["ALICE", "DEV", "ACTIVE"],
        ["BOB", "OPS", "IDLE"],
        ["CARA", "QA", "ACTIVE"],
    ]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            s.text(x0 + c * cw + cw / 2, y0 + r * rh + rh / 2 - 24, cell, 48, center=True)
    return s


def wireframe(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.rect(80, 80, 1488, 1096)
    s.line((480, 80), (480, 1096))
    s.text(280, 120, "MENU", 52, center=True)
    for i, item in enumerate(["HOME", "USERS", "SETTINGS"]):
        s.text(140, 280 + i * 140, item, 44)
    s.rect(560, 140, 1100, 240)
    s.text(600, 170, "SEARCH", 40)
    for i in range(3):
        y = 330 + i * 150
        s.rect(560, y, 1400, y + 100)
        s.line((600, y + 50), (900, y + 50))
    s.labeled_box(1180, 940, 1400, 1040, "SAVE", 40)
    return s


def todo(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.text(100, 80, "TODO", 72)
    for i, (item, done) in enumerate(
        [("WRITE TESTS", True), ("FIX LINT", False), ("UPDATE DOCS", True), ("TAG RELEASE", False)]
    ):
        y = 280 + i * 160
        s.rect(110, y, 190, y + 80)
        if done:
            s.line((120, y + 40), (150, y + 70))
            s.line((150, y + 70), (200, y - 20))
        s.text(250, y + 10, item, 52)
    return s


def sequence(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.labeled_box(160, 80, 520, 190, "CLIENT")
    s.labeled_box(1000, 80, 1360, 190, "SERVER")
    s.dashed((340, 190), (340, 1090))
    s.dashed((1180, 190), (1180, 1090))
    s.arrow((340, 380), (1180, 380))
    s.text(760, 320, "LOGIN", 40, center=True)
    s.arrow((1180, 620), (340, 620))
    s.text(760, 560, "TOKEN", 40, center=True)
    s.arrow((340, 860), (1180, 860))
    s.text(760, 800, "FETCH", 40, center=True)
    return s


def rotated(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.text(100, 100, "ROTATED NOTES", 64)
    for i, item in enumerate(["CHECK ORIENTATION", "SCAN PAGE SIDEWAYS", "READ ANYWAY"]):
        y = 300 + i * 170
        s.circle(130, y + 22, 12)
        s.text(190, y, item, 52)
    s.img = s.img.transpose(Image.Transpose.ROTATE_90)
    return s


def mixed(rng: random.Random) -> Sheet:
    s = Sheet(rng)
    s.text(100, 80, "OVERVIEW", 72)
    s.text(100, 240, "THE SERVICE STORES SKETCHES", 48)
    s.text(100, 330, "AND RETURNS A SPEC", 48)
    for x0, label in ((100, "APP"), (600, "API"), (1100, "DB")):
        s.labeled_box(x0, 620, x0 + 320, 740, label)
    s.arrow((420, 680), (600, 680))
    s.arrow((920, 680), (1100, 680))
    return s


def colour_shapes(rng: random.Random) -> Sheet:
    s = Sheet(rng, rgb=True)
    for x0, label in ((100, "INPUT"), (600, "PARSE"), (1100, "OUTPUT")):
        s.labeled_box(x0, 500, x0 + 320, 620, label)
    s.ink = BLUE
    s.arrow((420, 560), (600, 560))
    s.arrow((920, 560), (1100, 560))
    s.ink = RED
    s.text(784, 760, "CHECK THIS", 48, center=True)
    s.ink = BLACK
    return s


BASE_LINES = ["BUY MILK", "BUY EGGS", "CALL BOB", "SHIP PARCEL"]


def annotated_spec(rng: random.Random) -> Sheet:
    """A printed spec page with blue notes: one line struck out, two added in the margin."""
    s = Sheet(rng, rgb=True)
    s.text(100, 80, "SHOPPING LIST", 64)
    ys = []
    for i, item in enumerate(BASE_LINES):
        ys.append(260 + i * 150)
        s.text(100, ys[-1], item, 52)
    kept = list(
        dict.fromkeys(w for item in BASE_LINES if item != "CALL BOB" for w in item.lower().split())
    )
    title_words = ["shopping", "list"]
    s.base = s.img.copy()
    s.base_md = "# Shopping list\n\n## Transcription\n\n" + "\n".join(
        item.capitalize() + "\n" for item in BASE_LINES
    )
    s.words = []
    s.ink = BLUE
    strike_y = ys[2] + 26
    s.line((90, strike_y), (90 + s.text_width("CALL BOB", 52) + 20, strike_y))
    s.text(760, ys[0], "ADD BREAD", 48)
    s.text(760, ys[1] + 70, "ADD BUTTER", 48)
    s.arrow((740, ys[0] + 30), (560, ys[1] + 30))
    s.ink = BLACK
    s.extra = {"must_keep": title_words + kept}
    return s


def bare_wireframe(rng: random.Random) -> Sheet:
    """A login screen with four labels and nothing else: no requirements are written down."""
    s = Sheet(rng)
    s.rect(420, 60, 1148, 1116)
    s.text(784, 160, "LOGIN", 64, center=True)
    for i, label in enumerate(["EMAIL", "PASSWORD"]):
        y = 360 + i * 260
        s.text(500, y, label, 40)
        s.rect(500, y + 70, 1068, y + 150)
    s.labeled_box(600, 900, 968, 1010, "SIGN IN", 44)
    s.min_precision = 0.95
    return s


NB_W, NB_H = 1404, 1872
NB_SCALE = 1568 / NB_H
TOP, PITCH, MARGIN_LEFT, TEXT_LEFT = 106, 106, 983, 53
GEOMETRY = {
    "cell": 53,
    "row_pitch": PITCH,
    "top": TOP,
    "rows": 16,
    "text_left": TEXT_LEFT,
    "margin_left": MARGIN_LEFT,
}
PRINT_H = 30  # printed glyph height: 40 px text, baseline 11 px above the row line
PRINT_ADVANCE = ADVANCE * PRINT_H / 6


def _id(name: str) -> str:
    """A stable uuid for an op or a region of an eval item."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agents-app-connector/eval/{name}"))


class NotebookSheet(Sheet):
    """A notebook page in page px, drawn scaled to the upload size, recording its pen strokes
    (page px) and the layout the app would send."""

    def __init__(self, rng: random.Random):
        super().__init__(rng, (round(NB_W * NB_SCALE), round(NB_H * NB_SCALE)))
        self.strokes: list[dict] = []
        self.layout: dict = {
            "version": 1,
            "geometry": GEOMETRY,
            "segments": [],
            "runs": [],
            "ops": [],
        }
        self.regions: dict[str, Image.Image] = {}
        self.base_sheet: NotebookSheet | None = None

    def _stroke(self, pts: list[tuple[float, float]], jitter: float = 1.0) -> None:
        pts = [(x + self._j(jitter), y + self._j(jitter)) for x, y in pts]
        self.strokes.append(
            {
                "tool": "pen",
                "color": "black",
                "points": [[round(x, 1), round(y, 1)] for x, y in pts],
            }
        )
        scaled = [(x * NB_SCALE, y * NB_SCALE) for x, y in pts]
        self.d.line(scaled, fill=self.ink, width=PEN, joint="curve")

    def mark(self) -> int:
        return len(self.strokes)

    def since(self, first: int) -> list[int]:
        return list(range(first, len(self.strokes)))

    def printed(self, row: int, s: str, line_id: str) -> None:
        """A rendered spec line on its row (no pen strokes, no jitter)."""
        k = PRINT_H / 6
        y = TOP + (row + 1) * PITCH - 11 - PRINT_H
        for i, ch in enumerate(s.upper()):
            for line in GLYPHS.get(ch, []):
                pts = [
                    ((TEXT_LEFT + i * PRINT_ADVANCE + px * k) * NB_SCALE, (y + py * k) * NB_SCALE)
                    for px, py in line
                ]
                self.d.line(pts, fill=self.ink, width=3, joint="curve")
        self.layout["runs"].append({"line_id": line_id, "row": row, "x0": TEXT_LEFT})

    @staticmethod
    def char_x(i: int) -> float:
        """Left edge of printed character i of a run."""
        return TEXT_LEFT + i * PRINT_ADVANCE

    def write_row(self, row: int, x: float, s: str, h: float = 44) -> str:
        """Handwriting on a row's text band; returns its segment id."""
        first = self.mark()
        self.text(x, TOP + (row + 1) * PITCH - h - 8, s, h)
        n = sum(1 for seg in self.layout["segments"] if seg["row"] == row)
        seg_id = f"p0-r{row}-{n}"
        self.layout["segments"].append(
            {
                "id": seg_id,
                "row": row,
                "x0": round(x, 1),
                "x1": round(x + self.text_width(s, h), 1),
                "strokes": self.since(first),
            }
        )
        return seg_id

    def op(self, **fields) -> str:
        self.layout["ops"].append(fields)
        return fields["id"]


def _base_page(rng: random.Random, lines: list[str]) -> NotebookSheet:
    """The handwritten page that becomes spec v1: one line per row from row 1."""
    b = NotebookSheet(rng)
    for row, text in enumerate(lines, 1):
        b.write_row(row, TEXT_LEFT + 7, text)
    b.base_md = "".join(t.capitalize() + "\n" for t in lines)
    return b


def _with_base(s: NotebookSheet, base: NotebookSheet) -> None:
    s.base_sheet = base
    s.base_md = base.base_md
    for row, text in enumerate(base.base_md.splitlines(), 1):
        s.printed(row, text, f"L{row}")


def notebook_lines(rng: random.Random) -> Sheet:
    """New handwriting on rows; row 2 holds two segments (gap over two cells)."""
    s = NotebookSheet(rng)
    ids = [
        s.write_row(1, 60, "PLAN FOR MONDAY"),
        s.write_row(2, 60, "CALL THE BANK"),
        s.write_row(2, 640, "PAY RENT"),
        s.write_row(3, 60, "FIX THE BIKE"),
        s.write_row(5, 60, "BOOK FLIGHTS"),
    ]
    s.extra = {
        "lines": [{"n": n, "segments": [i]} for n, i in enumerate(ids, 1)],
        "ops": [],
    }
    return s


def replace_and_strike(rng: random.Random) -> Sheet:
    """Base v1 (three rows); BREAD written above "milk", "call bob" struck out."""
    base = _base_page(rng, ["BUY MILK", "BUY EGGS", "CALL BOB"])
    s = NotebookSheet(rng)
    _with_base(s, base)
    s.words = []
    first = s.mark()
    x, y = s.char_x(4), TOP + PITCH + 6
    s.text(x, y, "BREAD", 36)
    replace = s.op(
        id=_id("13-replace"),
        kind="replace",
        target={"line_id": "L1", "from": 4, "to": 8, "quote": "milk"},
        strokes=s.since(first),
        rect=[round(x, 1), y, round(s.text_width("BREAD", 36), 1), 40],
    )
    first = s.mark()
    mid = TOP + 3 * PITCH + PITCH * 3 / 4 - 4
    s.line((s.char_x(0) - 6, mid), (s.char_x(8) + 6, mid))
    strike = s.op(
        id=_id("13-strike"),
        kind="delete",
        target={"line_id": "L3", "from": 0, "to": 8, "quote": "Call bob"},
        strokes=s.since(first),
    )
    s.words = ["buy", "bread", "eggs"]
    s.extra = {
        "must_not": ["milk", "call", "bob"],
        "lines": [{"n": 1, "kept": "L1", "op": replace}, {"n": 2, "kept": "L2"}],
        "ops": [{"id": replace, "status": "applied"}, {"id": strike, "status": "applied"}],
    }
    return s


def margin_insert(rng: random.Random) -> Sheet:
    """Base v1 (two rows); a boxed margin note with an arrow to the end of "buy milk"."""
    base = _base_page(rng, ["BUY MILK", "CALL BOB"])
    s = NotebookSheet(rng)
    _with_base(s, base)
    s.words = []
    first = s.mark()
    box = (1010.0, TOP + 2 * PITCH + 10.0, 1370.0, TOP + 3 * PITCH - 10.0)
    s.rect(*box)
    s.text(1040, box[1] + 22, "AND JAM", 40)
    tip = (s.char_x(8) + 4, TOP + PITCH + PITCH * 3 / 4)
    s.arrow((box[0], (box[1] + box[3]) / 2), tip)
    insert = s.op(
        id=_id("14-insert"),
        kind="insert",
        at={"line_id": "L1", "offset": 8, "mode": "inline"},
        strokes=s.since(first),
        rect=[box[0], box[1], box[2] - box[0], box[3] - box[1]],
    )
    s.words = ["buy", "milk", "and", "jam", "call", "bob"]
    s.extra = {
        "lines": [{"n": 1, "kept": "L1", "op": insert}, {"n": 2, "kept": "L2"}],
        "ops": [{"id": insert, "status": "applied"}],
    }
    return s


def image_region(rng: random.Random) -> Sheet:
    """Base v1 (two rows); an image region framed below it holds a drawn shed with a label that
    must not be transcribed."""
    base = _base_page(rng, ["GARDEN PLAN", "PLANT ROSES"])
    s = NotebookSheet(rng)
    _with_base(s, base)
    s.words = []
    first = s.mark()
    frame = (120.0, TOP + 3 * PITCH + 20.0, 760.0, TOP + 8 * PITCH - 20.0)
    for a, b in (
        ((frame[0], frame[1]), (frame[2], frame[1])),
        ((frame[2], frame[1]), (frame[2], frame[3])),
        ((frame[2], frame[3]), (frame[0], frame[3])),
        ((frame[0], frame[3]), (frame[0], frame[1])),
    ):
        s.dashed(a, b)
    s.rect(300, 560, 580, 820)
    s.line((280, 570), (440, 440))
    s.line((440, 440), (600, 570))
    s.text(360, 660, "SHED", 44)
    region_id = _id("15-region")
    rect = [frame[0], frame[1], frame[2] - frame[0], frame[3] - frame[1]]
    op = s.op(
        id=_id("15-image"),
        kind="image",
        strokes=s.since(first),
        rect=rect,
        region={"id": region_id, "part": "region.0.0.png", "after_line": "L2"},
    )
    crop = tuple(round(v * NB_SCALE) for v in frame)
    s.regions["region.0.0.png"] = s.img.crop(crop)
    s.words = ["garden", "plan", "plant", "roses"]
    s.extra = {
        "must_not": ["shed"],
        "images": [region_id],
        "lines": [{"n": 1, "kept": "L1"}, {"n": 2, "kept": "L2"}, {"n": 3, "op": op}],
        "ops": [{"id": op, "status": "applied"}],
    }
    return s


ITEMS = [
    ("01-notes", notes, None),
    ("02-flowchart", flowchart, "flowchart"),
    ("03-table", table, "table"),
    ("04-wireframe", wireframe, None),
    ("05-todo", todo, None),
    ("06-sequence", sequence, "sequenceDiagram"),
    ("07-rotated", rotated, None),
    ("08-mixed", mixed, "flowchart"),
    ("09-colour-shapes", colour_shapes, "flowchart"),
    ("10-annotated-spec", annotated_spec, None),
    ("11-bare-wireframe", bare_wireframe, None),
    ("12-notebook-lines", notebook_lines, None),
    ("13-replace-and-strike", replace_and_strike, None),
    ("14-margin-insert", margin_insert, None),
    ("15-image-region", image_region, None),
]


def generate(out: Path) -> None:
    """Write sketches/<item>.png and expected/<item>.json under `out`."""
    rng = random.Random(SEED)
    (out / "sketches").mkdir(parents=True, exist_ok=True)
    (out / "expected").mkdir(parents=True, exist_ok=True)
    for name, build, diagram in ITEMS:
        sheet = build(rng)
        sheet.img.save(out / "sketches" / f"{name}.png", "PNG")
        words = list(dict.fromkeys(sheet.words))
        doc = {
            "words": words,
            "diagram": diagram,
            "min_word_recall": MIN_RECALL,
            "min_precision": sheet.min_precision,
            **sheet.extra,
        }
        (out / "expected" / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n")
        if sheet.base is not None:
            (out / "specs").mkdir(parents=True, exist_ok=True)
            sheet.base.save(out / "specs" / f"{name}.png", "PNG")
            (out / "specs" / f"{name}.md").write_text(sheet.base_md)
        if isinstance(sheet, NotebookSheet):
            _write_notebook(out, name, sheet)


def _json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _strokes_doc(sheet: NotebookSheet) -> dict:
    return {"version": 1, "canvas": {"w": NB_W, "h": NB_H, "dpi": 227}, "strokes": sheet.strokes}


def _write_notebook(out: Path, name: str, sheet: NotebookSheet) -> None:
    _json(out / "layouts" / f"{name}.json", sheet.layout)
    _json(out / "strokes" / f"{name}.json", _strokes_doc(sheet))
    for part, img in sheet.regions.items():
        (out / "regions").mkdir(parents=True, exist_ok=True)
        img.save(out / "regions" / f"{name}.{part.removeprefix('region.')}", "PNG")
    base = sheet.base_sheet
    if base is not None:
        (out / "specs").mkdir(parents=True, exist_ok=True)
        base.img.save(out / "specs" / f"{name}.png", "PNG")
        (out / "specs" / f"{name}.md").write_text(base.base_md)
        _json(out / "layouts" / f"{name}.base.json", base.layout)
        _json(out / "strokes" / f"{name}.base.json", _strokes_doc(base))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "eval")
    generate(parser.parse_args().out)


if __name__ == "__main__":
    main()
