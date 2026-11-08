"""Sketch plugin (Boox sketch translator): prompt and attachments. The translator writes the
document and its feedback with its MCP tools (`translator_tools`); its reply text is discarded."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from agents_common.orchestrator import Attachment

if TYPE_CHECKING:
    from fastapi import APIRouter

    from agents_app_connector.plugins import NotebookPage, PluginDeps, TurnInput
    from agents_app_connector.plugins.sketch.layout import EditOp, SpecLayout

HERE = Path(__file__).parent
FINAL_LINE = (
    "Write the document with document_write and anything else with feedback_add. "
    "Your reply text is discarded."
)
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


def _image_detail(annotated, note, page: int) -> str:
    if annotated is None or note is None:
        return f"page {page}"
    first, last = note.lines
    return f"spec v{annotated[0]}, document page {note.doc_page}, source lines {first}-{last}"


def _num(value: float) -> str:
    return str(round(value))


def _rect(rect) -> str:
    return "rect " + ",".join(_num(v) for v in rect)


def _short(op_id) -> str:
    return op_id.hex[:4]


def _op_line(op: "EditOp") -> str:
    head = f"op {_short(op.id)} "
    rect = "" if op.rect is None else f" ({_rect(op.rect)})"
    if op.kind in ("replace", "delete") and op.target is not None:
        t = op.target
        where = f'{t.line_id}[{t.from_}:{t.to}] "{t.quote}"'
        if op.kind == "delete":
            return head + f"delete {where}"
        return head + f"replace {where} with the handwriting above it{rect}"
    if op.kind == "insert" and op.at is not None:
        at = op.at
        where = (
            f"at {at.line_id} offset {at.offset} inline"
            if at.mode == "inline"
            else f"after {at.line_id}"
        )
        return head + f"insert {where} from the margin note{rect}"
    if op.kind == "note":
        return head + (
            f"note from the margin ink{rect}: apply it to the line its arrow points at, "
            "or to the nearest lines it annotates; reject it when you cannot place it"
        )
    region = op.region
    after = "" if region is None or region.after_line is None else f" after {region.after_line}"
    return (
        head + f"image region {region.id if region else ''}{rect}{after}: do not transcribe inside"
    )


def _notebook_lines(page: "NotebookPage", base: "tuple[int, str, SpecLayout] | None") -> list[str]:
    g = page.layout.geometry
    out = [
        f"Notebook geometry: cell {g.cell}, rows {g.rows} from y {g.top}, pitch {g.row_pitch}, "
        f"text x {g.text_left}-{g.margin_left}, margin x {g.margin_left}-{page.width}"
    ]
    source: dict[str, str] = {}
    if base is not None:
        text = base[1].splitlines()
        for line in base[2].lines:
            if line.source_line <= len(text):
                source[line.line_id] = text[line.source_line - 1]
    for run in page.layout.runs:
        out.append(f'{run.line_id} row {run.row}: "{source.get(run.line_id, "")}"')
    for seg in page.layout.segments:
        out.append(f"{seg.id} row {seg.row} x {_num(seg.x0)}-{_num(seg.x1)}")
    out += [_op_line(op) for op in page.layout.ops]
    return out


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
        annotated = turn.annotated_spec
        if turn.kind == "sketch" or any(f.kind == "sketch_png" for f in turn.files):
            pngs = [f for f in turn.files if f.kind == "sketch_png"]
            if not pngs:
                parts.append(MISSING_FILE)
            else:
                notes = {p.page: p for p in turn.annotated_pages}
                notebook = {p.page: p for p in turn.layouts}
                lines = []
                for i, f in enumerate(pngs, 1):
                    page = f.page or 0
                    lines.append(
                        f"Image {i}: {_name('sketch_png', f.attachment_id)} "
                        f"({_image_detail(annotated, notes.get(page), page)})"
                    )
                    if page in notebook:
                        lines += _notebook_lines(notebook[page], turn.base)
                parts.append("\n".join(lines))
                if len(pngs) > 1 and annotated is None:
                    parts.append("The images are pages of one sketch; treat them together.")
        if annotated is not None:
            n = annotated[0]
            parts.append(
                f"These images are spec version {n} rendered as pages with the user's "
                "handwritten notes over them. Printed text is the spec; handwriting, marks and "
                f"shapes are the notes. Apply the notes to spec version {n} and call "
                "document_write with the full revised document."
            )
        if turn.layouts:
            parts.append(self._notebook_instruction(turn))
        if turn.caption:
            parts.append(f"Caption: {turn.caption}")
        if turn.text:
            parts.append(f"User notes: {turn.text}")
        if annotated is not None:
            version, markdown = annotated
            parts.append(f"Spec version {version}:\n```markdown\n{markdown.strip()}\n```")
        elif turn.layouts and turn.base is not None:
            version, markdown, base_layout = turn.base
            ids = "\n".join(f"{x.line_id}: line {x.source_line}" for x in base_layout.lines)
            parts.append(
                f"Spec version {version}:\n```markdown\n{markdown.strip()}\n```\n"
                f"Line ids of spec version {version}:\n{ids}"
            )
        elif turn.previous_spec is not None:
            version, markdown = turn.previous_spec
            parts.append(f"Previous spec version {version}:\n```markdown\n{markdown.strip()}\n```")
        ops = [op for page in turn.layouts for op in page.layout.ops]
        if ops:
            parts.append("Op ids: " + ", ".join(f"{_short(op.id)}={op.id}" for op in ops))
        parts.append(FINAL_LINE)
        return "\n\n".join(parts)

    @staticmethod
    def _notebook_instruction(turn: "TurnInput") -> str:
        if turn.base is None:
            return (
                "These images are notebook pages with new handwriting: transcribe each segment "
                "and call document_write with the full document, lines and ops."
            )
        n = turn.base[0]
        return (
            f"These images are notebook pages. The printed rows are spec version {n} (their "
            "line ids are given above), the handwriting is new: transcribe each segment, apply "
            f"each op to spec version {n} and call document_write with the full revised "
            "document, lines and ops."
        )


PLUGIN = SketchPlugin()
