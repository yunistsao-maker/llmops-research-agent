"""Checks against the REAL public pages.  Skipped unless you pass ``--live`` (they need outbound HTTPS and nothing else - no AWS):

    python -m pytest tests/test_live.py --live -v -rP

    # compare with your real portal data instead of the bundled sample
    PORTAL_REPO=../llmops-portal python -m pytest tests/test_live.py --live -v -rP

    # also test your deployed web-search gateway (needs AWS credentials + the same WEB_SEARCH_GATEWAY_* variables as the runtime)
    WEB_SEARCH_GATEWAY_NAME=ResearchSearch AWS_REGION=ap-northeast-1 python -m pytest tests/test_live.py --live -k gateway -v

    # acceptance test with the REAL model (needs Bedrock access for MODEL_ID; costs a few cents of tokens)
    LIVE_MODEL=1 python -m pytest tests/test_live.py --live -k real_model -v -rP

What these prove that the offline suite cannot: today's real HTML (Microsoft Learn, platform.claude.com) is fetched by the agent's own
fetcher, parsed into one line per table row, quoted verbatim, graded as official evidence, and a proposal built from it passes the agent's
validator and applies cleanly to the portal files.  Except in the ``real_model`` test the model is still a script: it plays a careful
researcher that READS the date off the page instead of having it hard-coded, so a failure here means "the page changed" or "the network is
blocked", not "the LLM got it wrong".

Expected *values* are never hard-coded - Microsoft and Anthropic may legitimately move a date tomorrow.  Each test compares the page with the
portal data and asserts that the agent reacts correctly to whichever case it finds.  Run with ``-rP`` (or ``-s``) to see what was found.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import os
import re
from typing import Callable

import pytest

from e2e_helpers import ANTHROPIC_DEPRECATIONS_URL, FOUNDRY_URL, settings
from research.agent import build_fetcher, run_once
from research.apply import apply_saved_proposal
from research.config import Settings
from research.offline.scripted_model import Ctx, ScriptedModel, call, say
from research.portal.data import PortalData
from research.web.search_backend import FakeSearchBackend

pytestmark = pytest.mark.live

MODEL = "claude-sonnet-4-5"
EVENT = "azure-claude-sonnet-4-5-eol"
FOUNDRY_SOURCE = "ms-foundry-retirement-schedule"
ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
LONG = re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b")
PROMPT = {"prompt": f"Check the Foundry retirement date of {MODEL}", "kind": "prompt"}


# ---------------------------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------------------------
def _settings():
    return settings(portal_data=os.environ.get("PORTAL_REPO") or "bundled")


def _portal() -> PortalData:
    return PortalData.load(_settings().portal_data)


def _event(portal: PortalData) -> dict:
    ev = next((e for e in portal.docs["data/lifecycle.json"].get("events", []) if e.get("id") == EVENT), None)
    if ev is None:
        pytest.skip(f"the portal data has no {EVENT!r} event to compare the live page with")
    return ev


class Probe:
    """What the scripted researcher noticed.  A script step must never raise inside the agent loop, so problems are recorded here
    and the test asserts on them afterwards."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.row: str | None = None
        self.row_date: str | None = None
        self.claimed: str | None = None
        self.fetch: dict | None = None  # the full fetch_page result the model saw (the event stream only carries a brief form)
        self.rows: list[str] = []  # the lines find_in_page returned


def live_run(script, *, payload=PROMPT) -> dict:
    st = _settings()
    return asyncio.run(run_once(payload, settings=st, model=ScriptedModel(script), fetcher=build_fetcher(st), search=FakeSearchBackend({})))


def _page_id(ctx: Ctx, probe: Probe) -> str | None:
    res = ctx.last("fetch_page")
    probe.fetch = res if isinstance(res, dict) else None
    if isinstance(res, dict) and res.get("pageId"):
        return res["pageId"]
    probe.problems.append(f"fetch_page did not return a page (network blocked? page moved?): {str(res)[:300]}")
    return None


def _hit_lines(ctx: Ctx) -> list[str]:
    """The matching lines (``L12> text``) of the last find_in_page result."""
    out = []
    for line in str(ctx.last("find_in_page")).splitlines():
        m = re.match(r"^L\d+> (.*)$", line)
        if m:
            out.append(m.group(1))
    return out


def _first_cell(row: str) -> str:
    return row.split("|")[0].strip()


def _different_date(*taken: str) -> str:
    """A calendar date that is none of ``taken`` (and so cannot be in a quote that holds only those)."""
    base = dt.date.fromisoformat(taken[0])
    for days in range(1, 40):
        cand = (base + dt.timedelta(days=days)).isoformat()
        if cand not in taken:
            return cand
    raise AssertionError("unreachable")


def foundry_script(probe: Probe, *, current_date: str, has_history: bool, pick: Callable[[list[str]], str | None] | None = None,
                   query: str = MODEL, claim: Callable[[Probe], str] | None = None, finish_after_find: bool = False) -> list:
    """lookup -> fetch -> find -> record_evidence -> submit_proposal, the date taken from the live row.

    ``pick(rows)`` chooses the row to quote (default: the row whose first cell is the Sonnet 4.5 model id); ``claim(probe)`` can lie about the date.
    """
    pick = pick or (lambda rows: next((r for r in rows if _first_cell(r) == MODEL and ISO.search(r)), None))

    def find(ctx):
        pid = _page_id(ctx, probe)
        return [call("find_in_page", page_id=pid, query=query, context_lines=0, max_hits=20)] if pid else [say("fetch failed")]

    def record(ctx):
        pid, rows = _page_id(ctx, probe), _hit_lines(ctx)
        probe.rows = rows
        if finish_after_find:
            return [say("done")]
        row = pick(rows) if pid else None
        if row is None:
            probe.problems.append(f"no usable row for {query!r} on the live page (layout changed?). Lines found: {rows[:6]}")
            return [say("no usable row")]
        probe.row, probe.row_date = row, ISO.search(row).group(1)
        return [call("record_evidence", page_id=pid, quote=row, note="Foundry retirement table row")]

    def propose(ctx):
        ev = ctx.last("record_evidence")
        if not (isinstance(ev, dict) and ev.get("ok")):
            probe.problems.append(f"record_evidence refused a quote copied from the live page: {str(ev)[:500]}")
            return [say("evidence refused")]
        probe.claimed = claim(probe) if claim else probe.row_date
        return [call("submit_proposal", proposal=_proposal(ev["evidenceId"], probe.claimed, current_date, has_history))]

    return [[say("先查 portal 目前的值。"), call("lookup_portal", file="data/lifecycle.json", query="sonnet-4-5")],
            [call("fetch_page", url=FOUNDRY_URL)], find, record, propose]


def _proposal(eid: str, date: str, current: str, has_history: bool) -> dict:
    if date == current:
        return {"summary": f"Foundry 退役表列 {MODEL} 為 {date}，與 portal 相同，不需要變更。", "ops": [],
                "checked": [{"topic": "Foundry Claude 退役表", "result": f"{MODEL} 列 {date}，與 portal 一致"}]}
    ops = [{"file": "data/lifecycle.json", "op": "replace", "path": f"/events/{EVENT}/date", "value": date, "evidence": [eid],
            "rationale": f"Foundry 退役表目前列 {MODEL} 為 {date}；portal 為 {current}。"}]
    if has_history:  # the portal's convention: the superseded date stays visible for the reviewer
        ops.append({"file": "data/lifecycle.json", "op": "replace", "path": f"/events/{EVENT}/conflicts", "evidence": [eid],
                    "value": [{"date": current, "kind": "superseded", "note": f"Foundry 退役表先前列 {current}，現已改列 {date}。",
                               "sources": [{"id": FOUNDRY_SOURCE}]}],
                    "rationale": "舊日期保留為 superseded，方便審核者對照。"})
    return {"summary": f"Microsoft Foundry 退役表現在把 {MODEL} 列為 {date}（portal 為 {current}）。", "ops": ops,
            "checked": [{"topic": "Foundry Claude 退役表", "result": f"{MODEL} 列 {date}"}]}


def _setup() -> tuple[PortalData, dict, bool]:
    portal = _portal()
    event = _event(portal)
    return portal, event, FOUNDRY_SOURCE in portal.source_by_id() and event.get("conflicts") is not None


# ---------------------------------------------------------------------------------------------
# Microsoft Foundry retirement table
# ---------------------------------------------------------------------------------------------
def test_live_foundry_page_is_fetched_as_official_evidence_with_one_line_per_model_row():
    """Canary for the page layout: if Microsoft restructures the table this fails first, with the lines that were found."""
    probe = Probe()
    live_run(foundry_script(probe, current_date="2000-01-01", has_history=False, query="claude-", finish_after_find=True))
    assert not probe.problems, probe.problems
    fetch = probe.fetch
    assert fetch and fetch.get("pageId"), f"fetch failed: {fetch}"
    assert (fetch["tier"], fetch["publisher"], fetch["status"]) == ("official", "Microsoft", 200), fetch
    assert fetch["truncated"] is False and fetch["chars"] > 3000 and fetch["portalSourceId"] == FOUNDRY_SOURCE, fetch
    assert fetch["suspiciousText"] is False
    claude_rows = [r for r in probe.rows if _first_cell(r).startswith("claude-")]
    assert len(claude_rows) >= 2, f"expected several 'claude-...' table rows, found: {probe.rows}"
    assert all(r.count("|") >= 3 for r in claude_rows), claude_rows  # model | version | status | date | replacement
    assert any(ISO.search(r) for r in claude_rows), claude_rows
    assert any(_first_cell(r) == MODEL for r in claude_rows), f"no row for {MODEL}: {claude_rows}"


def test_live_foundry_date_is_read_from_the_real_page_and_proposed_against_the_portal(record_property, capsys):
    portal, event, has_history = _setup()
    probe = Probe()
    res = live_run(foundry_script(probe, current_date=event["date"], has_history=has_history))
    assert not probe.problems, probe.problems
    assert res["status"] == "accepted", res.get("error") or res.get("report")
    p, live, current = res["proposal"], probe.row_date, event["date"]
    record_property("portal_date", current)
    record_property("foundry_live_date", live)
    with capsys.disabled():
        print(f"\n[live] Foundry lists {MODEL} on {live} (row: {probe.row!r}); the portal has {current}"
              f" -> {'no change needed' if live == current else 'proposal: replace date'}")
    if live == current:
        assert p["ops"] == []
        return
    date_op = next(o for o in p["ops"] if o["path"] == f"/events/{EVENT}/date")
    assert (date_op["before"], date_op["value"]) == (current, live)
    ev = p["evidence"][date_op["evidence"][0]]
    assert (ev["tier"], ev["publisher"]) == ("official", "Microsoft")
    assert ev["url"] == FOUNDRY_URL and live in ev["quote"] and MODEL in ev["quote"]
    assert ev["pageSha256"] and ev["fetchedAt"]
    if FOUNDRY_SOURCE in portal.source_by_id():
        assert p["touchSources"] == [FOUNDRY_SOURCE]
    # what a reviewer would get: the same machinery as scripts/apply_proposal.py, on the same portal data
    staged, problems = apply_saved_proposal(portal, p)
    assert problems == [], problems
    new_event = next(e for e in staged.docs["data/lifecycle.json"]["events"] if e["id"] == EVENT)
    assert new_event["date"] == live


def test_live_a_date_that_is_not_in_the_real_row_is_refused():
    _portal_obj, event, has_history = _setup()
    probe = Probe()
    res = live_run(foundry_script(probe, current_date=event["date"], has_history=has_history,
                                  claim=lambda pr: _different_date(pr.row_date, event["date"])))
    assert not probe.problems, probe.problems
    assert res["status"] == "no_proposal", res.get("proposal")
    assert any(probe.claimed in e for e in res["report"]["errors"]), res["report"]["errors"]
    assert res["evidence"] and res["evidence"][0]["tier"] == "official"  # the research is kept for a human even though nothing was proposed


def test_live_another_models_row_cannot_support_a_change_to_sonnet_4_5():
    _portal_obj, event, has_history = _setup()
    probe = Probe()

    def other_models_row(rows: list[str]) -> str | None:
        sonnet = next((r for r in rows if _first_cell(r) == MODEL and ISO.search(r)), None)
        taken = {event["date"], ISO.search(sonnet).group(1) if sonnet else ""}
        return next((r for r in rows if _first_cell(r).startswith("claude-") and _first_cell(r) != MODEL
                     and ISO.search(r) and ISO.search(r).group(1) not in taken), None)

    script = foundry_script(probe, current_date=event["date"], has_history=has_history, pick=other_models_row, query="claude-")
    res = live_run(script)
    if probe.problems and "no usable row" in probe.problems[0]:
        pytest.skip("the live table has no other Claude row with a date that differs from the portal's: scenario cannot be built today")
    assert not probe.problems, probe.problems
    assert res["status"] == "no_proposal", res.get("proposal")
    assert any(f"mention {MODEL}" in e for e in res["report"]["errors"]), res["report"]["errors"]


# ---------------------------------------------------------------------------------------------
# every kind of official source the portal depends on (a canary for page layouts AND for the HTML -> text converter)
# ---------------------------------------------------------------------------------------------
SOURCES = [
    # id, url, query for find_in_page, regex a table row must match, minimum rows, publisher
    ("anthropic-deprecations", ANTHROPIC_DEPRECATIONS_URL, MODEL, r"^[A-Z][a-z]+ \d{1,2}, \d{4} \| claude-sonnet-4-5", 1, "Anthropic"),
    ("google-agent-platform", "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions", "gemini-2.5-",
     r"^gemini-2\.5-[\w.-]+ \| [A-Z][a-z]+ \d{1,2}, \d{4} \| [A-Z][a-z]+ \d{1,2}, \d{4}", 2, "Google Cloud"),
    ("gemini-api-deprecations", "https://ai.google.dev/gemini-api/docs/deprecations", "gemini-2.5-",
     r"^gemini-2\.5-[\w.-]+ \| [A-Z][a-z]+ \d{1,2}, \d{4} \| [A-Z][a-z]+ \d{1,2}, \d{4}", 1, "Google"),
    ("aws-bedrock-legacy", "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html", "Claude",
     r"^Anthropic \| Claude [\w. ]+ \| anthropic\.[\w.:-]+ \|.*[A-Z][a-z]+ \d{1,2}, \d{4}", 2, "AWS"),
    ("openai-deprecations", "https://developers.openai.com/api/docs/deprecations", "gpt-",
     r"^[A-Z][a-z]{2} \d{1,2}, \d{4} \| [\w.-]+ \|", 1, "OpenAI"),
    ("litellm-release-notes", "https://docs.litellm.ai/release_notes/", "v1.",
     r"^v\d+\.\d+\.\d+ \| [A-Z][a-z]{2} \d{1,2}, \d{4} \| \S", 5, "LiteLLM"),
]


@pytest.mark.parametrize("name, url, query, row_regex, min_rows, publisher", SOURCES, ids=[s[0] for s in SOURCES])
def test_live_official_source_is_readable_as_quotable_rows(name, url, query, row_regex, min_rows, publisher, capsys):
    seen: dict = {}

    def find(ctx):
        res = ctx.last("fetch_page")
        seen["fetch"] = res
        pid = res.get("pageId") if isinstance(res, dict) else None
        return [call("find_in_page", page_id=pid, query=query, context_lines=0, max_hits=20)] if pid else [say("fetch failed")]

    def record(ctx):
        seen["rows"] = [r for r in _hit_lines(ctx) if re.search(row_regex, r)]
        if not seen["rows"]:
            return [say("no row")]
        return [call("record_evidence", page_id=seen["fetch"]["pageId"], quote=seen["rows"][0], note=f"{name}: first matching row")]

    def finish(ctx):
        seen["evidence"] = ctx.last("record_evidence")
        return [say("done")]

    live_run([[call("fetch_page", url=url)], find, record, finish])
    fetch = seen.get("fetch")
    assert isinstance(fetch, dict) and fetch.get("pageId"), f"fetch failed (network? page moved?): {fetch}"
    assert fetch["tier"] == "official" and fetch["publisher"] == publisher and fetch["truncated"] is False, fetch
    assert fetch["suspiciousText"] is False, "the page contains instruction-like text (or the scanner needs a look)"
    rows = seen.get("rows") or []
    assert len(rows) >= min_rows, f"expected >= {min_rows} table row(s) matching {row_regex!r} on {url}; the layout may have changed or the converter regressed"
    ev = seen.get("evidence")
    assert isinstance(ev, dict) and ev.get("ok") is True and ev["tier"] == "official" and ev["datesInQuote"], ev
    with capsys.disabled():
        print(f"\n[live] {name}: {len(rows)} row(s); first: {rows[0][:170]!r}")


# ---------------------------------------------------------------------------------------------
# your own deployment
# ---------------------------------------------------------------------------------------------
def test_live_gateway_web_search_returns_results():
    """Needs AWS credentials and your gateway: it uses the gateway backend directly (no silent fallback to the built-in seed pages)."""
    from research.web.search_backend import GatewaySearchBackend

    st = Settings.from_env()
    if not (st.search_gateway_id or st.search_gateway_arn or st.search_gateway_endpoint or st.search_gateway_name):
        pytest.skip("set WEB_SEARCH_GATEWAY_NAME (or _ID / _ARN / _ENDPOINT) and AWS credentials to test your deployed web-search gateway")
    hits = GatewaySearchBackend.from_settings(st).search(f"{MODEL} retirement date Microsoft Foundry", max_results=5)
    assert hits, "the gateway answered but returned no results"
    assert all(h.url.startswith("https://") and h.title for h in hits), hits


def live_foundry_row(model: str = MODEL) -> tuple[str, str]:
    """Read the row of ``model`` off the live page with a throw-away scripted run: the oracle for the real-model test."""
    seen: dict = {}

    def find(ctx):
        res = ctx.last("fetch_page")
        seen["fetch"] = res
        pid = res.get("pageId") if isinstance(res, dict) else None
        return [call("find_in_page", page_id=pid, query=model, context_lines=0, max_hits=20)] if pid else [say("fetch failed")]

    def done(ctx):
        seen["rows"] = _hit_lines(ctx)
        return [say("done")]

    live_run([[call("fetch_page", url=FOUNDRY_URL)], find, done])
    row = next((r for r in seen.get("rows", []) if _first_cell(r) == model and ISO.search(r)), None)
    assert row, f"the live page has no dated row for {model} (fetch result: {str(seen.get('fetch'))[:300]})"
    return row, ISO.search(row).group(1)


@pytest.mark.skipif(not os.environ.get("LIVE_MODEL"), reason="set LIVE_MODEL=1 (+ AWS credentials with Bedrock access) to run the agent with the real model")
def test_live_real_model_checks_the_foundry_date_and_proposes_only_what_the_page_supports(capsys):
    """The acceptance test: a real model, the real page, the real validator.  A few cents of tokens; budgets are kept small."""
    portal, event, _has_history = _setup()
    row, live = live_foundry_row()
    st = dataclasses.replace(Settings.from_env(), portal_data=os.environ.get("PORTAL_REPO") or "bundled", output_uri="",
                             max_fetches=8, max_searches=6, max_tool_calls=40, max_runtime_seconds=600)
    payload = {"prompt": f"Check when Microsoft Foundry retires {MODEL} and compare it with the portal. Propose an update only if the page shows a different date.",
               "kind": "prompt", "urls": [FOUNDRY_URL], "dryRun": True}
    res = asyncio.run(run_once(payload, settings=st))
    with capsys.disabled():
        print(f"\n[live/real model] {st.model_provider}:{st.model_id} status={res['status']} stop={res.get('stopReason')} stats={res.get('stats')}"
              f"\n  page row: {row!r}\n  portal date: {event['date']}")
    assert res["status"] in ("accepted", "no_proposal"), res.get("error") or res
    date_ops = [o for o in (res.get("proposal") or {}).get("ops", []) if o["path"] == f"/events/{EVENT}/date"]
    if live != event["date"]:
        assert res["status"] == "accepted", f"the page differs from the portal ({live} vs {event['date']}) but nothing was proposed: {res.get('report')}"
        assert [o["value"] for o in date_ops] == [live], res["proposal"]["ops"]
        _staged, problems = apply_saved_proposal(portal, res["proposal"])
        assert problems == [], problems
    else:  # nothing to change: whatever the model proposed must not touch this date
        assert date_ops == [], date_ops
