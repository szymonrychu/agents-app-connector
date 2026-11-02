"""Measure the sketch translator against the synthetic eval set on a live deployment.

    APPCONN_EVAL_TOKEN=<device token> python scripts/eval_sketches.py \
        --base-url https://<host>/app/v1/sketch --out eval-result.json

The device token is read from the environment variable APPCONN_EVAL_TOKEN only (never from
argv) and is never printed. This is a measurement, not a gate: the exit code is always 0.
"""

from __future__ import annotations

import argparse
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
FRONT_KEYS = ("id", "source_image", "created", "confidence", "unreadable_count")
POLL_WAIT = 25


@dataclass
class Score:
    recall: float
    diagram_ok: bool
    front_matter: bool
    missing: list[str]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _front_matter_ok(md: str) -> bool:
    lines = md.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return False
    keys = {ln.split(":", 1)[0].strip() for ln in lines[1:end] if ":" in ln}
    return all(k in keys for k in FRONT_KEYS)


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


def score(spec_md: str, expected: dict) -> Score:
    """Word recall, diagram kind presence and front matter validity of one spec."""
    # `[[?a|b]]` counts as both a and b; `[[illegible]]` carries no word.
    text = re.sub(r"\[\[\?([^\]]*)\]\]", lambda m: m.group(1).replace("|", " "), spec_md)
    text = text.replace("[[illegible]]", " ")
    got = _tokens(text)
    words = list(dict.fromkeys(w.lower() for w in expected["words"]))
    missing = [w for w in words if w not in got]
    recall = 1.0 if not words else (len(words) - len(missing)) / len(words)
    return Score(
        recall=recall,
        diagram_ok=_diagram_ok(spec_md, expected.get("diagram")),
        front_matter=_front_matter_ok(spec_md),
        missing=missing,
    )


def _wait_done(client: httpx.Client, base: str, conversation: str, timeout: float) -> str:
    """Long-poll events until a terminal sketch_status; returns 'done', 'failed' or 'timeout'."""
    deadline = time.monotonic() + timeout
    cursor = 0
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
                return ev["status"]
    return "timeout"


def run_item(client: httpx.Client, base: str, name: str, timeout: float) -> dict:
    png = (ROOT / "eval" / "sketches" / f"{name}.png").read_bytes()
    expected = json.loads((ROOT / "eval" / "expected" / f"{name}.json").read_text())
    started = time.monotonic()
    result = {
        "item": name,
        "status": "error",
        "recall": 0.0,
        "diagram_ok": False,
        "front_matter": False,
    }
    try:
        meta = {
            "client_id": str(uuid.uuid4()),
            "conversation": "new",
            "canvas": {"w": 1568, "h": 1176, "dpi": 300},
        }
        up = client.post(
            f"{base}/sketches",
            data={"meta": json.dumps(meta)},
            files={"sketch.png": ("sketch.png", png, "image/png")},
        )
        up.raise_for_status()
        conversation = up.json()["conversation_id"]
        result["status"] = _wait_done(client, base, conversation, timeout)
        if result["status"] == "done":
            md = client.get(f"{base}/conversations/{conversation}/spec.md")
            md.raise_for_status()
            sc = score(md.text, expected)
            result.update(
                recall=round(sc.recall, 3),
                diagram_ok=sc.diagram_ok,
                front_matter=sc.front_matter,
                missing=sc.missing,
            )
    except httpx.HTTPError as exc:
        result["error"] = type(exc).__name__
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def format_table(rows: list[dict]) -> str:
    head = f"{'item':<16}{'recall':>8}  {'diagram_ok':<11}{'front_matter':<13}{'seconds':>8}"
    lines = [head]
    for r in rows:
        lines.append(
            f"{r['item']:<16}{r['recall']:>8.2f}  {r['diagram_ok']!s:<11}"
            f"{r['front_matter']!s:<13}{r['seconds']:>8.1f}"
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
