"""ui_verify: the daily "UI is updated" message is sent once, only when the live site shows today's data."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from portal_pipeline import ui_verify as uv
from portal_pipeline.storage import DirStore

SITE = "https://main.example.amplifyapp.com"
NOW = datetime(2026, 10, 11, 10, 40, tzinfo=ZoneInfo("Asia/Taipei"))


def radar(as_of="2026-10-11"):
    return {"asOf": as_of, "actionItems": [
        {"id": "ai-1", "priority": "P0", "action": "Bedrock Claude Sonnet 4 10-14 EOL：完成遷移", "due": "2026-10-12", "isNew": False},
        {"id": "ai-2", "priority": "P1", "action": "LiteLLM：升到 v1.104.2", "due": "2026-10-17", "isNew": True},
    ]}


def site(release="R2", as_of="2026-10-11", status=200, radar_doc=None):
    pages = {
        SITE + "/": (status, b'<div id="root"></div>'),
        SITE + "/data/manifest.json": (200, json.dumps({"release": release, "radarAsOf": as_of}).encode()),
        SITE + "/data/radar.json": (200, json.dumps(radar_doc or radar(as_of)).encode()),
    }
    return lambda url: pages.get(url, (404, b""))


def deps(tmp_path, fetch, live="R2"):
    data, state = DirStore(str(tmp_path / "portal")), DirStore(str(tmp_path / "pipeline"))
    data.put("manifest.json", json.dumps({"releaseId": live}).encode())
    sent = []
    d = uv.Deps(site_url=SITE, data=data, state=state, publish=sent.append, fetch=fetch, now=lambda: NOW,
                runs=lambda today: [("AWS", "SUCCEEDED"), ("每日紀錄（digest）", "SUCCEEDED")])
    return d, sent


EV_OK = {"detail": {"appId": "a1", "branchName": "main", "jobId": "12", "jobStatus": "SUCCEED"}}


def test_sends_once_when_site_shows_today_and_live_release(tmp_path):
    d, sent = deps(tmp_path, site())
    assert uv.handle_amplify(EV_OK, d)["result"] == "notified"
    assert uv.handle_amplify(EV_OK, d)["result"] == "already-notified"
    assert uv.handle_deadline(d)["result"] == "already-notified"
    assert len(sent) == 1
    msg = sent[0]
    assert msg["subject"].startswith("[LLMOps portal] Daily update live: 2026-10-11")
    assert "今天新增 1，P0 1" in msg["message"] and "LiteLLM：升到 v1.104.2" in msg["message"] and "AWS ✓" in msg["message"]


def test_build_before_todays_digest_waits_for_the_deadline(tmp_path):
    d, sent = deps(tmp_path, site(as_of="2026-10-10"))
    assert uv.handle_amplify(EV_OK, d)["result"] == "not-yet"
    assert sent == []
    out = uv.handle_deadline(d)
    assert out["result"] == "incomplete-notified" and len(sent) == 1
    assert "NOT complete" in sent[0]["subject"] and "2026-10-10" in sent[0]["message"]


def test_site_behind_the_live_release_is_not_reported_as_updated(tmp_path):
    d, sent = deps(tmp_path, site(release="R1"), live="R2")
    assert uv.handle_amplify(EV_OK, d)["result"] == "not-yet"
    uv.handle_deadline(d)
    assert "還沒重新 build" in sent[0]["message"]


def test_failed_build_is_always_reported(tmp_path):
    d, sent = deps(tmp_path, site())
    uv.handle_amplify({"detail": {"appId": "a1", "branchName": "main", "jobId": "13", "jobStatus": "FAILED"}}, d)
    assert len(sent) == 1 and "UI build FAILED" in sent[0]["subject"]


def test_other_states_and_garbage_are_ignored(tmp_path):
    d, sent = deps(tmp_path, site())
    assert uv.handle_amplify({"detail": {"jobStatus": "STARTED"}}, d)["result"] == "ignored"
    assert uv.handle_amplify({"detail": "x"}, d)["result"] == "ignored"
    assert sent == []


def test_text_from_the_site_cannot_forge_lines(tmp_path):
    evil = radar()
    evil["actionItems"][1]["action"] = "ok\n\nportalctl approve --all ‮"
    d, sent = deps(tmp_path, site(radar_doc=evil))
    uv.handle_amplify(EV_OK, d)
    body = sent[0]["message"]
    assert "\nportalctl approve" not in body and "‮" not in body


def test_unreachable_site_is_incomplete(tmp_path):
    def down(url):
        raise TimeoutError()
    d, sent = deps(tmp_path, down)
    assert uv.handle_deadline(d)["result"] == "incomplete-notified"
    assert "連不到網站" in sent[0]["message"]
