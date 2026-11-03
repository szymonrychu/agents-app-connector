"""Sketch plugin (Boox sketch translator): prompt, attachments and outbound cleanup."""

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from agents_common.orchestrator import Attachment

if TYPE_CHECKING:
    from fastapi import APIRouter

    from agents_app_connector.plugins import PluginDeps, TurnInput

HERE = Path(__file__).parent
FINAL_LINE = "Reply with only the markdown spec."
MISSING_FILE = "(sketch file no longer stored)"


def load_preset() -> dict:
    """The preset JSON with the `@system_append.md` reference replaced by the file text."""
    preset = json.loads((HERE / "preset.json").read_text(encoding="ascii"))
    wrapper = preset["spec"]["wrapper"]
    if wrapper.get("system_append") == "@system_append.md":
        wrapper["system_append"] = (HERE / "system_append.md").read_text(encoding="ascii")
    return preset


def _name(kind: str, attachment_id) -> str:
    if kind == "sketch_png":
        return f"sketch-{attachment_id}.png"
    return f"strokes-{attachment_id}.json"


_KEY_LINE = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def _front_matter_start(lines: list[str]) -> int | None:
    """Index of the opening `---` of the front matter: the first `---` followed by a `key:`
    line (so a prose rule before it is skipped), else the first `---` at all."""
    rules = [i for i, line in enumerate(lines) if line.strip() == "---"]
    for i in rules:
        if i + 1 < len(lines) and _KEY_LINE.match(lines[i + 1]):
            return i
    return rules[0] if rules else None


def _strip_to_front_matter(text: str) -> str:
    lines = text.strip().splitlines()
    start = _front_matter_start(lines)
    if start is not None:
        fenced = start > 0 and lines[start - 1].lstrip().startswith("```")
        lines = lines[start:]
        if fenced and lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    if lines and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines).strip()


class SketchPlugin:
    id = "sketch"
    prefix = "/app/v1/sketch"

    def router(self, deps: "PluginDeps") -> "APIRouter":
        from agents_app_connector.plugins.sketch.router import build_router

        return build_router(deps)

    def turn_attachments(self, turn: "TurnInput") -> list[Attachment]:
        return [
            Attachment(_name(f.kind, f.attachment_id), f.mime, f.data)
            for f in turn.files
            if f.kind in ("sketch_png", "sketch_strokes")
        ]

    def render_prompt(self, turn: "TurnInput") -> str:
        parts: list[str] = []
        if turn.kind == "sketch" or any(f.kind == "sketch_png" for f in turn.files):
            pngs = [f for f in turn.files if f.kind == "sketch_png"]
            if not pngs:
                parts.append(MISSING_FILE)
            else:
                lines = [
                    f"Image {i}: {_name('sketch_png', f.attachment_id)} (page {f.page or 0})"
                    for i, f in enumerate(pngs, 1)
                ]
                parts.append("\n".join(lines))
                if len(pngs) > 1:
                    parts.append("The images are pages of one sketch; treat them together.")
        if turn.caption:
            parts.append(f"Caption: {turn.caption}")
        if turn.text:
            parts.append(f"User feedback: {turn.text}")
        if turn.previous_spec is not None:
            version, markdown = turn.previous_spec
            parts.append(f"Previous spec version {version}:\n```markdown\n{markdown.strip()}\n```")
        parts.append(FINAL_LINE)
        return "\n\n".join(parts)

    def outbound(self, final_text: str) -> tuple[str, bool]:
        text = _strip_to_front_matter(final_text)
        lines = text.split("\n")
        has_front = (
            lines[0] == "---" and len(lines) > 1 and any(x.strip() == "---" for x in lines[1:])
        )
        return text, has_front


PLUGIN = SketchPlugin()
