"""Generate the synthetic handwriting eval set into eval/sketches and eval/expected.

    uv run python scripts/make_eval_set.py [--out eval]

Deterministic (random seed 7) and independent of platform fonts: every glyph is drawn from a
built-in stroke table with Pillow ImageDraw primitives only, with per-glyph jitter and a slight
baseline wobble to look hand drawn. Images are 8-bit grayscale `L`, long edge 1568, except
09-colour-shapes and 10-annotated-spec, which are `RGB` (black, blue and red pens).

10-annotated-spec also writes specs/10-annotated-spec.png (the page without notes, uploaded
first to get spec v1) and specs/10-annotated-spec.md (the text it prints).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw

SEED = 7
W, H = 1568, 1176
PEN = 4
BLACK, BLUE, RED = (0, 0, 0), (0, 0, 255), (220, 0, 0)
MIN_RECALL = 0.8

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
        doc = {"words": words, "diagram": diagram, "min_word_recall": MIN_RECALL, **sheet.extra}
        (out / "expected" / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n")
        if sheet.base is not None:
            (out / "specs").mkdir(parents=True, exist_ok=True)
            sheet.base.save(out / "specs" / f"{name}.png", "PNG")
            (out / "specs" / f"{name}.md").write_text(sheet.base_md)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "eval")
    generate(parser.parse_args().out)


if __name__ == "__main__":
    main()
