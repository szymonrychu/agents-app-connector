import asyncio
import io
import json
import uuid

import httpx
from PIL import Image

from agents_app_connector.plugins.sketch import router as sketch_router

PREFIX = "/app/v1/sketch"


class Sketch:
    id = "sketch"
    prefix = PREFIX

    def router(self, deps):
        return sketch_router.build_router(deps)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def png(w=400, h=300):
    buf = io.BytesIO()
    Image.new("L", (w, h), 255).save(buf, "PNG")
    return buf.getvalue()


def strokes():
    return json.dumps({"version": 1, "canvas": {"w": 400, "h": 300, "dpi": 300}, "strokes": []})


CANVAS = {"w": 400, "h": 300, "dpi": 300}


def pages(*indices):
    return [{"page": i, "canvas": CANVAS} for i in indices]


def meta(**over):
    base = {"client_id": str(uuid.uuid4()), "pages": pages(0)}
    return json.dumps({**base, **over})


def files(png_bytes=None, with_strokes=False, count=1):
    """Parts sketch.<i>.png (and strokes.<i>.json) for i in range(count)."""
    out = {}
    for i in range(count):
        out[f"sketch.{i}.png"] = (f"sketch.{i}.png", png_bytes or png(), "image/png")
        if with_strokes:
            out[f"strokes.{i}.json"] = (f"strokes.{i}.json", strokes().encode(), "application/json")
    return out


def stored_files(store):
    return [p for p in store.root.rglob("*") if p.is_file()] if store.root.exists() else []


async def post(api, token, m=None, fl=None):
    return await api.post(
        "/sketches", data={"meta": m or meta()}, files=fl or files(), headers=bearer(token)
    )


async def count(conn, table):
    return (await (await conn.execute(f"SELECT count(*) FROM {table}")).fetchone())[0]


def only(values):
    values = list(values)
    assert len(values) == 1
    return values[0]


async def rows(conn, sql, *params):
    return await (await conn.execute(sql, params)).fetchall()


def event(key: str) -> bytes:
    return f'event: task_changed\ndata: {{"key": "{key}"}}\n\n'.encode()


class FakeSse:
    """Each upstream connection reads chunks from its own queue; None ends it."""

    def __init__(self):
        self.connections: list[asyncio.Queue] = []
        self.closed: list[asyncio.Event] = []
        self.requests: list[httpx.Request] = []
        self.fail_connects = 0
        self.connected = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_connects:
            self.fail_connects -= 1
            raise httpx.ConnectError("refused")
        chunks: asyncio.Queue = asyncio.Queue()
        closed = asyncio.Event()
        self.connections.append(chunks)
        self.closed.append(closed)
        self.connected.set()

        async def body():
            try:
                while True:
                    chunk = await chunks.get()
                    if chunk is None:
                        return
                    yield chunk
            finally:
                closed.set()

        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    def send(self, chunk: bytes, conn: int = -1) -> None:
        self.connections[conn].put_nowait(chunk)
