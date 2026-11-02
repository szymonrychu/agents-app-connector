"""scripts/ensure_preset.py: create, update or leave the sketch-translator preset."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from agents_app_connector.plugins.sketch import load_preset

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ensure_preset.py"
spec = importlib.util.spec_from_file_location("ensure_preset", SCRIPT)
ensure = importlib.util.module_from_spec(spec)
sys.modules["ensure_preset"] = ensure
spec.loader.exec_module(ensure)

BASE = "http://orch.test/agent/v1/admin"


def make(handler):
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), calls


def stored(preset: dict, **over) -> dict:
    return {
        "id": "1",
        "name": preset["name"],
        "spec": preset["spec"],
        "superleader_manageable": True,
        **over,
    }


def test_create_on_404_sends_manageable_true():
    preset = load_preset()

    def handler(req):
        if req.method == "GET":
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(201, json=stored(preset))

    client, calls = make(handler)
    assert ensure.ensure_preset(client, BASE, preset) == "created"
    assert [c.method for c in calls] == ["GET", "POST"]
    assert calls[0].url.path == "/agent/v1/admin/presets/sketch-translator"
    assert calls[1].url.path == "/agent/v1/admin/presets"
    body = json.loads(calls[1].content)
    assert body["superleader_manageable"] is True
    assert body["spec"] == preset["spec"]


def test_update_on_differing_spec():
    preset = load_preset()
    old = {**preset["spec"], "model": "other"}

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=stored(preset, spec=old))
        return httpx.Response(200, json=stored(preset))

    client, calls = make(handler)
    assert ensure.ensure_preset(client, BASE, preset) == "updated"
    assert calls[1].method == "PUT"
    assert calls[1].url.path == "/agent/v1/admin/presets/sketch-translator"
    assert json.loads(calls[1].content)["superleader_manageable"] is True


def test_update_when_not_manageable():
    preset = load_preset()

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=stored(preset, superleader_manageable=False))
        return httpx.Response(200, json=stored(preset))

    client, calls = make(handler)
    assert ensure.ensure_preset(client, BASE, preset) == "updated"


def test_unchanged_on_equal():
    preset = load_preset()
    client, calls = make(lambda req: httpx.Response(200, json=stored(preset)))
    assert ensure.ensure_preset(client, BASE, preset) == "unchanged"
    assert len(calls) == 1


def test_second_run_is_idempotent():
    preset = load_preset()
    state = {}

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=state["p"]) if state else httpx.Response(404, json={})
        body = json.loads(req.content)
        state["p"] = {"id": "1", **body}
        return httpx.Response(201 if req.method == "POST" else 200, json=state["p"])

    client, _ = make(handler)
    assert ensure.ensure_preset(client, BASE, preset) == "created"
    assert ensure.ensure_preset(client, BASE, preset) == "unchanged"


def test_non_2xx_raises_with_status_and_code_only():
    preset = load_preset()
    client, _ = make(lambda req: httpx.Response(403, json={"error": "forbidden", "detail": "x"}))
    with pytest.raises(ensure.EnsureError) as exc:
        ensure.ensure_preset(client, BASE, preset)
    assert str(exc.value) == "HTTP 403 forbidden"


def test_main_exits_1_without_leaking_token(monkeypatch, capsys):
    monkeypatch.setenv("ORCH_AGENT_TOKEN", "s3cret-value")
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    code = ensure.main(["--base-url", BASE], transport=transport)
    out = capsys.readouterr()
    assert code == 1
    assert seen["auth"] == "Bearer s3cret-value"
    assert "s3cret-value" not in out.out + out.err
    assert "401" in out.err and "unauthorized" in out.err


def test_main_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("ORCH_AGENT_TOKEN", raising=False)
    assert ensure.main(["--base-url", BASE]) == 1
    assert "ORCH_AGENT_TOKEN" in capsys.readouterr().err
