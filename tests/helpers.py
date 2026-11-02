import io
import json
import uuid

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


def meta(**over):
    base = {"client_id": str(uuid.uuid4()), "canvas": {"w": 400, "h": 300, "dpi": 300}}
    return json.dumps({**base, **over})


def files(png_bytes=None, with_strokes=False):
    out = {"sketch.png": ("sketch.png", png_bytes or png(), "image/png")}
    if with_strokes:
        out["strokes.json"] = ("strokes.json", strokes().encode(), "application/json")
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
