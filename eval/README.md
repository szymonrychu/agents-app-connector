# Eval set

Sixteen synthetic handwriting sketches (`sketches/*.png`, 8-bit grayscale except `09-colour-shapes` and `10-annotated-spec`, which are RGB with black, blue and red ink; long edge 1568 px) with
expected words and diagram kind (`expected/*.json`). They contain no personal data.

Regenerate (deterministic, seed 7, no platform fonts: glyphs are drawn from a stroke table with
Pillow primitives, with jitter and baseline wobble):

    uv run python scripts/make_eval_set.py

Measure a deployment (a measurement, not a gate: exit code is always 0). The device token is
read from the environment variable `APPCONN_EVAL_TOKEN`:

    APPCONN_EVAL_TOKEN=... uv run python scripts/eval_sketches.py \
        --base-url https://<host>/app/v1/sketch --out eval-result.json

Per item it uploads the sketch into a new conversation, long-polls until `done` or `failed`
(timeout 600 s), fetches `spec.md` and the translator feedback, and prints `item, recall,
precision, diagram_ok, feedback, seconds`. Recall is case-insensitive word recall against `words`
(`[[?x]]` counts as x); `min_word_recall` is 0.8. Precision is the share of the document's word
tokens that are drawn on the page (`words` plus `must_keep`), counted after dropping fence lines,
mermaid syntax (only node, edge and participant labels count), table and markdown markers, and
`[[...]]` markers (`[[?a|b]]` counts as a); `min_precision` is 0.9, 0.95 for
`11-bare-wireframe`. The JSON output also lists the `invented` words and the `added_structure` (front matter or markdown headings; the document must have none). `[illegible]` markers carry no word. `diagram_ok` checks a
mermaid block of the expected kind or a GFM table. `feedback` counts the items the translator
sent with `feedback_add`; they never count against precision because they are not in the
document.

`11-bare-wireframe` is a login screen with only four labels (LOGIN, EMAIL, PASSWORD, SIGN IN):
the shape that invites invented requirements, so it expects precision of at least 0.95.

`10-annotated-spec` is an annotation: a printed spec page with blue notes (one line struck out, two
lines added in the margin). `specs/10-annotated-spec.png` is the same page without notes and
`specs/10-annotated-spec.md` its text. The runner uploads the base page first and waits for spec
v1, then uploads the annotated page into the same conversation with `meta.annotates` (spec v1,
document page 0, the whole of v1 as source lines) and scores the result. `expected.words` are the
added words; `must_keep` lists words of the unannotated parts that must survive (reported as
`kept` and `lost`).

`12-notebook-lines` to `15-image-region` are notebook pages (page canvas 1404 x 1872 px, rows of
106 px from y 106, text from x 53, margin from x 983; the PNG is the page scaled to 1176 x 1568,
grayscale, no grid). Each has `layouts/<item>.json` (the layout the app sends) and
`strokes/<item>.json` (its pen strokes in page px), and its expected JSON adds `lines` (the
expected provenance per source line) and `ops` (the expected status per op id), plus `must_not`
(words that must not be written) and `images` (region ids the document must reference) where
they apply.

- `12-notebook-lines`: five segments of new handwriting on rows 1, 2 (two segments, a gap of
  more than two cells), 3 and 5; each becomes its own source line. No base.
- `13-replace-and-strike`: base v1 "Buy milk / Buy eggs / Call bob" printed on rows 1-3, BREAD
  written above "milk" (replace) and "Call bob" struck out (delete).
- `14-margin-insert`: base v1 "Buy milk / Call bob", a boxed margin note AND JAM with an arrow
  to the end of "Buy milk" (insert inline).
- `15-image-region`: base v1 "Garden plan / Plant roses", an image region below it holding a
  drawn shed labelled SHED (not to be transcribed); `regions/15-image-region.0.0.png` is the
  app's crop.

- `16-picture-and-boxes`: a plain page (no layout) with boxes LOGIN and HOME joined by an arrow
  (a labelled flowchart) and a dashed frame holding a drawn shed labelled SHED. The runner sends
  the frame as `meta.pages[0].regions` (`expected.pictures`) with `regions/16-picture-and-boxes.0.0.png`
  and checks that the document references the picture (`images_ok`) and does not write SHED.

Items 13-15 have a base page: `specs/<item>.png` with `layouts/<item>.base.json` and
`strokes/<item>.base.json` (one segment per row), and `specs/<item>.md`, the text it should
become. The runner uploads the base page first and waits for spec v1, then rebases the item
layout onto v1 (each `L<k>` of the base text maps to the v1 line with the same words; target
quotes are read again from that line) and uploads the page with `base_version` 1. The result
adds `ops_ok`, `lines_ok`, `images_ok` and `forbidden`; image references carry no word in
precision.
