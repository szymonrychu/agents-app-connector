"""Sketch plugin (Boox sketch translator): prompt, attachments and outbound cleanup."""

import json
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


def _strip_to_front_matter(text: str) -> str:
    lines = text.strip().splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            fenced = i > 0 and lines[i - 1].lstrip().startswith("```")
            lines = lines[i:]
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
            png = next((f for f in turn.files if f.kind == "sketch_png"), None)
            if png is None:
                parts.append(MISSING_FILE)
            else:
                name = _name("sketch_png", png.attachment_id)
                parts.append(
                    f"Image 1: {name} (the exact path follows in the Attached files line; "
                    f'find it with Glob ".attachments/**/{name}" and Read it)'
                )
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
