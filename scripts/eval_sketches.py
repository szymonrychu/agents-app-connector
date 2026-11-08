"""Measure the sketch translator against the synthetic eval set on a live deployment.

    APPCONN_EVAL_TOKEN=<device token> python scripts/eval_sketches.py \
        --base-url https://<host>/app/v1/sketch --out eval-result.json

The device token is read from the environment variable APPCONN_EVAL_TOKEN only (never from
argv) and is never printed. This is a measurement, not a gate: the exit code is always 0.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

TOKEN_ENV = "APPCONN_EVAL_TOKEN"
ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
POLL_WAIT = 25
NOTEBOOK_CANVAS = {"w": 1404, "h": 1872, "dpi": 227}


@dataclass
class Score:
    recall: float
    diagram_ok: bool
    missing: list[str]
    precision: float = 1.0
    invented: list[str] | None = None
    added_structure: list[str] | None = None
    kept: float = 1.0
    lost: list[str] | None = None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


_MERMAID_LABEL = re.compile(r'\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}|"([^"]*)"|\|([^|]*)\||:\s*(.*)$')
_PARTICIPANT = re.compile(r"^\s*(?:participant|actor)\s+(.+)$")


def _mermaid_labels(line: str) -> str:
    """Only the label text of a mermaid line: node ids, keywords and arrows are syntax."""
    named = _PARTICIPANT.match(line)
    if named:
        return named.group(1).split(" as ", 1)[-1]
    return " ".join(g for m in _MERMAID_LABEL.finditer(line) for g in m.groups() if g)


def _document_words(md: str) -> list[str]:
    """The word tokens the document writes, in order: reading markers reduced to their first
    reading (`[[?a|b]]` is a, other `[[...]]` markers carry no word), fence lines dropped and
    mermaid blocks reduced to their labels."""
    text = re.sub(r"\[\[\?([^\]|]*)[^\]]*\]\]", r" \1 ", md)
    text = re.sub(r"\[\[[^\]]*\]\]", " ", text).replace("[illegible]", " ")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # an image reference carries no word
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip()[3:].strip().lower()
            continue
        kept.append(_mermaid_labels(line) if fence == "mermaid" else line)
    return re.findall(r"[a-z0-9]+", "\n".join(kept).lower())


def _diagram_ok(md: str, kind: str | None) -> bool:
    if kind is None:
        return True
    if kind == "table":
        lines = md.splitlines()
        sep = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
        return any(
            "|" in a and "|" in b and sep.match(b) for a, b in zip(lines, lines[1:], strict=False)
        )
    return re.search(rf"```mermaid\s*\n\s*{kind}\b", md) is not None


def _added_structure(md: str) -> list[str]:
    """Front matter and markdown headings: the translator must add neither, so each one found
    is listed (headings outside fences only)."""
    found = ["front matter"] if md.startswith("---\n") else []
    fenced = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6}\s", line):
            found.append(line.strip())
    return found


def score(spec_md: str, expected: dict) -> Score:
    """Word recall, precision (share of the document's words that are drawn on the page),
    diagram kind presence and kept words of one document."""
    # `[[?a|b]]` counts as both a and b; `[illegible]` carries no word.
    text = re.sub(r"\[\[\?([^\]]*)\]\]", lambda m: m.group(1).replace("|", " "), spec_md)
    text = text.replace("[illegible]", " ")
    got = _tokens(text)
    words = list(dict.fromkeys(w.lower() for w in expected["words"]))
    missing = [w for w in words if w not in got]
    recall = 1.0 if not words else (len(words) - len(missing)) / len(words)
    keep = list(dict.fromkeys(w.lower() for w in expected.get("must_keep", [])))
    lost = [w for w in keep if w not in got]
    kept = 1.0 if not keep else (len(keep) - len(lost)) / len(keep)
    drawn = set(words) | set(keep)
    written = _document_words(spec_md)
    extra = [w for w in written if w not in drawn]
    precision = 1.0 if not written else (len(written) - len(extra)) / len(written)
    return Score(
        recall=recall,
        diagram_ok=_diagram_ok(spec_md, expected.get("diagram")),
        missing=missing,
        precision=precision,
        invented=list(dict.fromkeys(extra)),
        added_structure=_added_structure(spec_md),
        kept=kept,
        lost=lost,
    )


def _wait_done(
    client: httpx.Client, base: str, conversation: str, timeout: float, runs: int = 1
) -> str:
    """Long-poll events until `runs` terminal sketch_status events (the last one decides);
    returns 'done', 'failed' or 'timeout'."""
    deadline = time.monotonic() + timeout
    cursor = 0
    seen = 0
    while time.monotonic() < deadline:
        wait = int(min(POLL_WAIT, max(0, deadline - time.monotonic())))
        r = client.get(
            f"{base}/conversations/{conversation}/events",
            params={"after": cursor, "wait": wait},
            timeout=wait + 30,
        )
        r.raise_for_status()
        body = r.json()
        cursor = body["cursor"]
        for ev in body["events"]:
            if ev["type"] == "sketch_status" and ev.get("status") in ("done", "failed"):
                seen += 1
                if seen >= runs or ev["status"] == "failed":
                    return ev["status"]
    return "timeout"


def _delete_conversation(client: httpx.Client, base: str, conversation: str) -> None:
    """Best effort: a failed delete must not hide the item's result."""
    with contextlib.suppress(httpx.HTTPError):
        client.delete(f"{base}/conversations/{conversation}")


def _upload(
    client: httpx.Client,
    base: str,
    png: bytes,
    pictures: list[dict] | None = None,
    regions: dict[str, bytes] | None = None,
    **meta_extra,
) -> str:
    """Upload one single-page sketch, with its picture regions and their crops (by part name)
    when it has any; returns the conversation id."""
    page = {"page": 0, "canvas": {"w": 1568, "h": 1176, "dpi": 300}}
    if pictures:
        page["regions"] = pictures
    meta = {"client_id": str(uuid.uuid4()), "pages": [page], **meta_extra}
    files = {"sketch.0.png": ("sketch.0.png", png, "image/png")}
    for part, data in (regions or {}).items():
        files[part] = (part, data, "image/png")
    up = client.post(f"{base}/sketches", data={"meta": json.dumps(meta)}, files=files)
    up.raise_for_status()
    return up.json()["conversation_id"]


def _notebook_upload(
    client: httpx.Client, base: str, name: str, suffix: str = "", **meta_extra
) -> str:
    """Upload a notebook page with its strokes, layout and region parts; returns the
    conversation id. `suffix` ".base" picks the base page of an item."""
    png = (EVAL / ("specs" if suffix else "sketches") / f"{name}.png").read_bytes()
    layout = meta_extra.pop("layout", None)
    if layout is None:
        layout = json.loads((EVAL / "layouts" / f"{name}{suffix}.json").read_text())
    meta = {
        "client_id": str(uuid.uuid4()),
        "pages": [{"page": 0, "canvas": NOTEBOOK_CANVAS}],
        **meta_extra,
    }
    files = {
        "sketch.0.png": ("sketch.0.png", png, "image/png"),
        "strokes.0.json": (
            "strokes.0.json",
            (EVAL / "strokes" / f"{name}{suffix}.json").read_bytes(),
            "application/json",
        ),
        "layout.0.json": ("layout.0.json", json.dumps(layout).encode(), "application/json"),
    }
    if not suffix:
        for path in sorted((EVAL / "regions").glob(f"{name}.*.png")):
            part = "region." + path.name.removeprefix(f"{name}.")
            files[part] = (part, path.read_bytes(), "image/png")
    up = client.post(f"{base}/sketches", data={"meta": json.dumps(meta)}, files=files)
    up.raise_for_status()
    return up.json()["conversation_id"]


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def rebase_layout(layout: dict, base_md: str, spec: dict) -> dict:
    """The item layout with the line ids of the model-written base version: L<k> of the eval
    base text (source line k of specs/<item>.md) maps to the version's line whose words match,
    and each target's quote is read again from that line."""
    ids = {x["source_line"]: x["line_id"] for x in (spec.get("layout") or {}).get("lines", [])}
    written = spec["markdown"].splitlines()
    found: dict[str, tuple[str, str, str]] = {}
    for k, text in enumerate(base_md.splitlines(), 1):
        for n, got in enumerate(written, 1):
            if n in ids and _norm(got) == _norm(text):
                found[f"L{k}"] = (ids[n], got, text)
                break
    out = json.loads(json.dumps(layout))

    def line_id(old: str) -> str:
        return found[old][0] if old in found else old

    for run in out["runs"]:
        run["line_id"] = line_id(run["line_id"])
    for op in out["ops"]:
        target, at, region = op.get("target"), op.get("at"), op.get("region")
        if target and target["line_id"] in found:
            _, got, _ = found[target["line_id"]]
            i = got.lower().find(target["quote"].lower())
            if i >= 0:
                target.update({"from": i, "to": i + len(target["quote"])})
                target["quote"] = got[i : target["to"]]
        if at and at["line_id"] in found:
            _, got, text = found[at["line_id"]]
            at["offset"] = len(got) if at["offset"] >= len(text) else min(at["offset"], len(got))
        for item in (target, at):
            if item:
                item["line_id"] = line_id(item["line_id"])
        if region and region.get("after_line"):
            region["after_line"] = line_id(region["after_line"])
    return out


def _spec_json(client: httpx.Client, base: str, conversation: str) -> dict:
    r = client.get(f"{base}/conversations/{conversation}/spec")
    r.raise_for_status()
    return r.json()


def picture_checks(markdown: str, expected: dict) -> dict:
    """The expected image references present and no forbidden word written."""
    words = set(_document_words(markdown))
    return {
        "images_ok": all(f"images/{i}.png" in markdown for i in expected.get("images", [])),
        "forbidden": [w for w in expected.get("must_not", []) if w in words],
    }


def notebook_checks(spec: dict, expected: dict) -> dict:
    """Op statuses as expected, every expected segment id in the line provenance, plus the
    picture checks."""
    layout = spec.get("layout") or {}
    got_ops = {o["id"]: o["status"] for o in layout.get("ops", [])}
    segments = {
        s
        for line in layout.get("lines", [])
        for s in (line.get("origin") or {}).get("segments", [])
    }
    want_segments = {s for line in expected.get("lines", []) for s in line.get("segments", [])}
    return {
        "ops_ok": all(got_ops.get(o["id"]) == o["status"] for o in expected.get("ops", [])),
        "lines_ok": want_segments <= segments,
        **picture_checks(spec["markdown"], expected),
    }


def _spec_md(client: httpx.Client, base: str, conversation: str) -> str:
    md = client.get(f"{base}/conversations/{conversation}/spec.md")
    md.raise_for_status()
    return md.text


def _feedback_count(client: httpx.Client, base: str, conversation: str) -> int:
    """Translator feedback items of the conversation (kept apart from the document)."""
    total, cursor = 0, 0
    while True:
        r = client.get(f"{base}/conversations/{conversation}/feedback", params={"after": cursor})
        r.raise_for_status()
        body = r.json()
        if not body["items"]:
            return total
        total += len(body["items"])
        cursor = body["cursor"]


def run_item(client: httpx.Client, base: str, name: str, timeout: float) -> dict:
    """Upload the sketch into a new conversation and score the spec. An item with a base page in
    eval/specs/ is an annotation: the base page is uploaded and translated first (spec v1), then
    the annotated page follows with meta.annotates over the whole of spec v1. A notebook item
    (eval/layouts/<item>.json) uploads its layout and strokes; with a base it first uploads the
    base page (its own layout) for spec v1, then the page on base_version v1 with the layout's
    line ids and quotes rebased onto that version, and adds the notebook checks."""
    png = (ROOT / "eval" / "sketches" / f"{name}.png").read_bytes()
    base_page = ROOT / "eval" / "specs" / f"{name}.png"
    expected = json.loads((ROOT / "eval" / "expected" / f"{name}.json").read_text())
    started = time.monotonic()
    result = {
        "item": name,
        "status": "error",
        "recall": 0.0,
        "precision": 0.0,
        "diagram_ok": False,
        "feedback": 0,
    }
    conversation = None
    try:
        runs = 1
        notebook = (EVAL / "layouts" / f"{name}.json").exists()
        base_md = EVAL / "specs" / f"{name}.md"
        if notebook and base_md.exists():
            conversation = _notebook_upload(client, base, name, ".base", conversation="new")
            status = _wait_done(client, base, conversation, timeout)
            if status != "done":
                result["status"] = status
                return result
            v1 = _spec_json(client, base, conversation)
            layout = json.loads((EVAL / "layouts" / f"{name}.json").read_text())
            _notebook_upload(
                client,
                base,
                name,
                conversation_id=conversation,
                base_version=v1["version"],
                layout=rebase_layout(layout, base_md.read_text(), v1),
            )
            runs = 2
        elif notebook:
            conversation = _notebook_upload(client, base, name, conversation="new")
        elif base_page.exists():
            conversation = _upload(client, base, base_page.read_bytes(), conversation="new")
            status = _wait_done(client, base, conversation, timeout)
            if status != "done":
                result["status"] = status
                return result
            lines = len(_spec_md(client, base, conversation).splitlines())
            annotates = {
                "spec_version": 1,
                "pages": [{"page": 0, "doc_page": 0, "lines": [0, max(0, lines - 1)]}],
            }
            _upload(
                client,
                base,
                png,
                conversation_id=conversation,
                caption="Handwritten notes over spec v1",
                annotates=annotates,
            )
            runs = 2
        else:
            pictures = expected.get("pictures", [])
            regions = {
                "region." + path.name.removeprefix(f"{name}."): path.read_bytes()
                for path in sorted((EVAL / "regions").glob(f"{name}.*.png"))
            }
            conversation = _upload(client, base, png, pictures, regions, conversation="new")
        result["status"] = _wait_done(client, base, conversation, timeout, runs)
        if result["status"] == "done":
            md = _spec_md(client, base, conversation)
            sc = score(md, expected)
            result.update(
                recall=round(sc.recall, 3),
                precision=round(sc.precision, 3),
                min_precision=expected.get("min_precision"),
                diagram_ok=sc.diagram_ok,
                feedback=_feedback_count(client, base, conversation),
                missing=sc.missing,
                invented=sc.invented,
                added_structure=sc.added_structure,
                kept=round(sc.kept, 3),
                lost=sc.lost,
            )
            if notebook:
                result.update(notebook_checks(_spec_json(client, base, conversation), expected))
            elif expected.get("pictures"):
                result.update(picture_checks(md, expected))
    except httpx.HTTPError as exc:
        result["error"] = type(exc).__name__
    finally:
        if conversation is not None:
            _delete_conversation(client, base, conversation)
        result["seconds"] = round(time.monotonic() - started, 1)
    return result


def format_table(rows: list[dict]) -> str:
    head = (
        f"{'item':<20}{'recall':>8}{'precision':>11}  {'diagram_ok':<11}"
        f"{'feedback':>9}{'seconds':>9}"
    )
    lines = [head]
    for r in rows:
        lines.append(
            f"{r['item']:<20}{r['recall']:>8.2f}{r['precision']:>11.2f}  "
            f"{r['diagram_ok']!s:<11}{r['feedback']:>9}{r['seconds']:>9.1f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", type=Path, default=Path("eval-result.json"))
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"{TOKEN_ENV} is not set", file=sys.stderr)
        return 0
    names = sorted(p.stem for p in (ROOT / "eval" / "sketches").glob("*.png"))
    with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=60) as client:
        rows = [run_item(client, args.base_url.rstrip("/"), n, args.timeout) for n in names]
    print(format_table(rows))
    args.out.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
