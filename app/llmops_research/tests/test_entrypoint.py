"""The AgentCore Runtime HTTP contract (POST /invocations, GET /ping) served by main.py, driven through Starlette's TestClient."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import main


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")  # offline: the built-in smoke script, no AWS calls
    monkeypatch.setenv("OUTPUT_URI", str(tmp_path))
    monkeypatch.delenv("SCRIPT_FILE", raising=False)
    monkeypatch.setattr(main, "_active_runs", 0)
    return TestClient(main.app)


def sse(resp) -> list[dict]:
    assert resp.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def cli_text(resp) -> str:
    """What `agentcore invoke` prints: it only shows JSON strings and event.contentBlockDelta.delta.text (port of the CLI's parser)."""
    out = []
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        obj = json.loads(line[6:])
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict) and "error" in obj:
            out.append("Error: " + str(obj["error"]))
        else:
            t = (((obj or {}).get("event") or {}).get("contentBlockDelta") or {}).get("delta", {}).get("text")
            if isinstance(t, str):
                out.append(t)
    return "".join(out)


def wait_idle(client, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if client.get("/ping").json()["status"] == "Healthy":
            return
        time.sleep(0.05)
    raise AssertionError("runtime still busy")


def test_ping_is_healthy_when_idle(client):
    body = client.get("/ping").json()
    assert body["status"] == "Healthy" and isinstance(body["time_of_last_update"], int)


def test_invocation_streams_progress_and_ends_with_the_result(client):
    r = client.post("/invocations", json={"prompt": "weekly"})
    assert r.status_code == 200
    events = sse(r)
    types = [e["type"] for e in events]
    assert types[0] == "run_started" and types[-1] == "result" and types.count("result") == 1
    assert "tool_call" in types and "tool_result" in types
    final = events[-1]
    assert final["status"] == "accepted" and final["proposal"]["schema"] == "llmops-research-proposal/v1"
    assert final["saved"].endswith(".json")


def test_agentcore_cli_shows_something_readable(client):
    text = cli_text(client.post("/invocations", json={"prompt": "weekly"}))
    assert "▶ run" in text and "→ lookup_portal" in text and "通過驗證" in text
    assert "結果已儲存" in text


def test_a_json_object_passed_as_the_cli_prompt_is_the_payload(client):
    r = client.post("/invocations", json={"prompt": json.dumps({"kind": "prompt", "instruction": "ping"})})
    assert sse(r)[0]["task"] == {"kind": "prompt", "instruction": "ping", "dryRun": False}


@pytest.mark.parametrize("body, needle", [
    ({"messages": []}, "messages"),
    ({"prompt": 5}, "string"),
    ({"kind": "verify", "prompt": "x"}, "claims"),
])
def test_bad_payloads_come_back_as_a_result_event(client, body, needle):
    r = client.post("/invocations", json=body)
    assert r.status_code == 200
    ev = sse(r)
    assert len(ev) == 1 and ev[0]["status"] == "error" and needle in ev[0]["error"]
    assert "Error" not in cli_text(r) or "invalid request" in cli_text(r)


def test_malformed_json_is_a_400_from_the_sdk(client):
    r = client.post("/invocations", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_background_run_returns_immediately_and_leaves_the_result_in_the_output_location(client, tmp_path):
    r = client.post("/invocations", json={"prompt": "weekly", "background": True})
    ev = sse(r)
    assert len(ev) == 1 and ev[0]["type"] == "accepted"
    run_id = ev[0]["runId"]
    assert ev[0]["resultUri"] == str(tmp_path / "runs" / f"{run_id}.json")
    wait_idle(client)
    saved = json.loads((tmp_path / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "accepted" and saved["runId"] == run_id and saved["task"]["background"] is True


def test_ping_says_busy_while_a_background_run_is_working(client, monkeypatch):
    async def slow_run(payload, *, settings=None, run_id=None, **kw):
        await asyncio.sleep(0.8)
        yield {"type": "result", "status": "no_proposal", "runId": run_id}

    monkeypatch.setattr(main, "run_stream", slow_run)
    sse(client.post("/invocations", json={"prompt": "weekly", "background": True}))
    assert client.get("/ping").json()["status"] == "HealthyBusy"  # keeps the microVM alive past the HTTP response
    wait_idle(client)
    assert main._active_runs == 0


def test_a_crashing_background_run_still_releases_the_slot_and_the_ping_status(client, monkeypatch):
    async def crash(payload, **kw):
        raise RuntimeError("boom")
        yield {}  # pragma: no cover

    monkeypatch.setattr(main, "run_stream", crash)
    sse(client.post("/invocations", json={"prompt": "weekly", "background": True}))
    wait_idle(client)
    assert main._active_runs == 0


def test_background_needs_a_place_to_put_the_result(client, monkeypatch):
    monkeypatch.delenv("OUTPUT_URI")
    ev = sse(client.post("/invocations", json={"prompt": "weekly", "background": True}))
    assert ev[0]["status"] == "error" and "OUTPUT_URI" in ev[0]["error"]


def test_one_session_does_not_accept_unlimited_concurrent_runs(client, monkeypatch):
    monkeypatch.setattr(main, "_active_runs", main.MAX_CONCURRENT_RUNS)
    ev = sse(client.post("/invocations", json={"prompt": "weekly"}))
    assert ev[0]["status"] == "error" and "busy" in ev[0]["error"] and "runtimeSessionId" in ev[0]["error"]


def test_a_client_that_hangs_up_frees_its_slot(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("OUTPUT_URI", str(tmp_path))
    monkeypatch.setattr(main, "_active_runs", 0)

    async def scenario():
        gen = main.invoke({"prompt": "weekly"}, SimpleNamespace(session_id="s-1"))
        await gen.__anext__()
        assert main._active_runs == 1
        await gen.aclose()
        return main._active_runs

    assert asyncio.run(scenario()) == 0
