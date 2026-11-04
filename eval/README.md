# Eval set

Ten synthetic handwriting sketches (`sketches/*.png`, 8-bit grayscale except `09-colour-shapes` and `10-annotated-spec`, which are RGB with black, blue and red ink; long edge 1568 px) with
expected words and diagram kind (`expected/*.json`). They contain no personal data.

Regenerate (deterministic, seed 7, no platform fonts: glyphs are drawn from a stroke table with
Pillow primitives, with jitter and baseline wobble):

    uv run python scripts/make_eval_set.py

Measure a deployment (a measurement, not a gate: exit code is always 0). The device token is
read from the environment variable `APPCONN_EVAL_TOKEN`:

    APPCONN_EVAL_TOKEN=... uv run python scripts/eval_sketches.py \
        --base-url https://<host>/app/v1/sketch --out eval-result.json

Per item it uploads the sketch into a new conversation, long-polls until `done` or `failed`
(timeout 600 s), fetches `spec.md` and prints `item, recall, diagram_ok, front_matter, seconds`.
Recall is case-insensitive word recall against `words` (`[[?x]]` counts as x); `min_word_recall`
is 0.8. `diagram_ok` checks a mermaid block of the expected kind or a GFM table.

`10-annotated-spec` is an annotation: a printed spec page with blue notes (one line struck out, two
lines added in the margin). `specs/10-annotated-spec.png` is the same page without notes and
`specs/10-annotated-spec.md` its text. The runner uploads the base page first and waits for spec
v1, then uploads the annotated page into the same conversation with `meta.annotates` (spec v1,
document page 0, the whole of v1 as source lines) and scores the result. `expected.words` are the
added words; `must_keep` lists words of the unannotated parts that must survive (reported as
`kept` and `lost`).
