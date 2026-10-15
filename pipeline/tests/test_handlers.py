"""The Lambda steps of the update state machine, one at a time (the whole machine is exercised in test_state_machine.py)."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from conftest import ARN, BUCKET, RUN, SFN, T1, accepted_run, squash

from portal_pipeline import handlers as H
from portal_pipeline import notify
from portal_pipeline import publish as P
from portal_pipeline import releases as R

def started(extra=None):
    return {"input": extra or {}, "execution": "arn:aws:states:us-east-1:123456789012:execution:llmops-portal-update:abc",
            "run": {"runId": RUN, "sessionId": "s" * 40, "resultUri": f"s3://{BUCKET}/research/runs/{RUN}.json",
                    "startedAt": "2026-10-02T03:30:00Z", "deadline": "2026-10-02T04:10:00Z"},
            "research": {"status": "running", "checks": 0}}


# ---------------------------------------------------------------------------------------------- start
def test_an_empty_input_starts_the_weekly_research_in_the_background(wired):
    state = H.start_research({"input": {}, "execution": "arn:exec"})
    call = wired.agent.calls[0]
    assert call["agentRuntimeArn"] == ARN and call["contentType"] == "application/json" and call["accept"] == "text/event-stream"
    assert json.loads(call["payload"]) == {"kind": "weekly", "background": True}
    assert len(call["runtimeSessionId"]) >= 33 and "qualifier" not in call
    assert state["run"] == {"runId": RUN, "sessionId": call["runtimeSessionId"], "resultUri": f"s3://{BUCKET}/research/runs/{RUN}.json",
                            "request": {"kind": "weekly"}, "startedAt": "2026-10-02T03:30:00Z", "deadline": "2026-10-02T04:10:00Z"}
    assert state["research"] == {"status": "running", "checks": 0} and state["execution"] == "arn:exec" and state["input"] == {}


@pytest.mark.parametrize("inp, request_sent", [
    ({"kind": "focus", "focus": ["lifecycle"]}, {"kind": "focus", "focus": ["lifecycle"]}),
    ({"request": {"kind": "verify", "claims": ["x"]}, "trigger": "schedule"}, {"kind": "verify", "claims": ["x"]}),
    ({"kind": "weekly", "mode": "review", "by": "eunice", "trigger": "manual"}, {"kind": "weekly"}),
    ({"request": {}, "trigger": "schedule"}, {"kind": "weekly"}),
    ({"prompt": "weekly", "background": False}, {"prompt": "weekly", "background": True}),
])
def test_the_research_request_is_taken_from_the_execution_input(wired, inp, request_sent):
    H.start_research({"input": inp})
    assert json.loads(wired.agent.calls[0]["payload"]) == {**request_sent, "background": True}


def test_each_run_gets_its_own_session_and_the_configured_endpoint(wired, monkeypatch):
    monkeypatch.setenv("AGENT_QUALIFIER", "PROD")
    H.start_research({"input": {}})
    H.start_research({"input": {}})
    a, b = wired.agent.calls
    assert a["runtimeSessionId"] != b["runtimeSessionId"] and a["qualifier"] == "PROD"


@pytest.mark.parametrize("inp, env, error, expect", [
    ({"kind": "weekly", "mode": "manual"}, {}, "BadRequest", "mode 只能是 auto 或 review，收到 'manual'"),
    ({"request": {"kind": "weekly"}, "mode": ["review"]}, {}, "BadRequest", "mode 只能是 auto 或 review"),
    ({"kind": "weekly"}, {"PUBLISH_MODE": "sometimes"}, "ConfigError", "PUBLISH_MODE 只能是 auto 或 review"),
    ({"kind": "weekly"}, {"AUTO_ALLOW_REMOVE": "maybe"}, "ConfigError", "AUTO_ALLOW_REMOVE 只能是 true 或 false"),
    ({"kind": "weekly"}, {"AUTO_MAX_OPS": "many"}, "ConfigError", "AUTO_MAX_OPS"),
])
def test_what_the_publish_step_would_refuse_is_found_before_the_research_is_started(wired, monkeypatch, inp, env, error, expect):
    """A "mode" nobody understands used to surface in the Publish step: after forty minutes of research, and its bill."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(getattr(H, error), match=expect):
        H.start_research({"input": inp, "execution": "arn:exec"})
    assert wired.agent.calls == []


def test_a_dry_run_cannot_be_published_so_it_is_refused(wired):
    with pytest.raises(H.ResearchFailed, match="dryRun"):
        H.start_research({"input": {"kind": "weekly", "dryRun": True}})
    assert wired.agent.calls == []


def test_a_request_that_is_not_an_object_is_refused(wired):
    with pytest.raises(H.BadRequest):
        H.start_research({"input": {"request": "weekly"}})
    with pytest.raises(H.BadRequest):
        H.start_research("weekly")


def test_the_run_must_write_where_the_pipeline_will_look(wired):
    wired.agent.result_uri = f"s3://another-bucket/research/runs/{RUN}.json"
    with pytest.raises(H.ConfigError, match="OUTPUT_URI.*s3://portal-data-bucket/research/"):
        H.start_research({"input": {}})


@pytest.mark.parametrize("events, expect", [
    ([{"type": "result", "status": "error", "error": "busy: this session is already running 2 research run(s)"}], "busy"),
    ([{"type": "result", "status": "error", "error": "background runs write their result to OUTPUT_URI, which is not configured"}], "OUTPUT_URI"),
    ([], "accepted"),
    ([{"type": "accepted", "runId": "../../etc/passwd", "resultUri": "x"}], "accepted"),
])
def test_a_run_the_agent_does_not_accept_fails_the_step(wired, events, expect):
    wired.agent.events = events
    with pytest.raises(H.ResearchFailed, match=expect):
        H.start_research({"input": {}})


def test_an_unreachable_runtime_fails_the_step_with_the_reason(wired):
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "no"}}

    wired.agent.error = Denied("User is not authorized to perform bedrock-agentcore:InvokeAgentRuntime")
    with pytest.raises(H.ResearchFailed, match="AccessDeniedException.*InvokeAgentRuntime"):
        H.start_research({"input": {}})


def test_missing_configuration_is_reported_by_name(wired, monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_ARN")
    with pytest.raises(H.ConfigError, match="AGENT_RUNTIME_ARN"):
        H.start_research({"input": {}})


# ---------------------------------------------------------------------------------------------- wait
def test_while_there_is_no_result_the_run_is_still_running(wired):
    state = H.check_research(started())
    assert state["research"] == {"status": "running", "checks": 1}
    assert H.check_research(state)["research"]["checks"] == 2 and "notification" not in state


def test_a_run_that_never_writes_a_result_times_out(wired):
    wired.now["t"] = dt.datetime(2026, 10, 2, 4, 10, 1, tzinfo=dt.timezone.utc)
    with pytest.raises(H.ResearchTimeout, match=f"{RUN}.*agentcore logs"):
        H.check_research(started())


def test_an_accepted_result_is_handed_to_the_publish_step_by_key(wired):
    key = wired.save_run(accepted_run())
    state = H.check_research(started())
    assert state["research"]["status"] == "accepted" and state["research"]["runKey"] == key == f"runs/{RUN}.json"
    assert state["research"]["proposalId"] == "prop-20261002T030000Z-7dd4e9c6" and state["research"]["checks"] == 1
    # the proposal itself stays in S3: the state only carries where to find it
    assert set(state["research"]) == {"status", "checks", "runKey", "recordSha256", "finishedAt", "stopReason", "totalTokens", "elapsedSeconds", "proposalId"}
    assert state["research"]["recordSha256"] == R.sha256_hex(wired.research.get(key).data)
    assert len(json.dumps(state)) < 2000


def test_a_run_without_a_proposal_ends_with_a_message_not_an_error(wired):
    record = {**accepted_run(), "status": "no_proposal", "proposal": None,
              "report": {"accepted": False, "errors": ["ops[0]: date 2026-12-01 does not appear in the quote"]}}
    wired.save_run(record)
    state = H.check_research(started())
    assert state["research"]["status"] == "no_proposal"
    note = state["notification"]
    assert note["subject"] == "[LLMOps portal] research finished without a proposal (no_proposal)"
    assert RUN in note["message"] and "does not appear in the quote" in note["message"] and "維持原狀" in note["message"]


def test_a_failed_run_fails_the_step_with_the_agents_error(wired):
    wired.save_run({**accepted_run(), "status": "error", "proposal": None, "error": "ThrottlingException: Too many tokens"})
    with pytest.raises(H.ResearchFailed, match="Too many tokens"):
        H.check_research(started())


def test_waiting_needs_a_run(wired):
    for state in ({"input": {}}, {"input": {}, "run": {"runId": "../x"}}):
        with pytest.raises(H.BadRequest):
            H.check_research(state)


# ---------------------------------------------------------------------------------------------- publish
def ready(wired, extra=None):
    wired.bootstrap()
    key = wired.save_run(accepted_run())
    return {**started(extra), "research": {"status": "accepted", "runKey": key, "proposalId": "prop-20261002T030000Z-7dd4e9c6"}}


def test_the_publish_step_makes_the_proposal_live_and_writes_the_message(wired):
    state = H.publish(ready(wired, {"trigger": "schedule"}))
    pub = state["publish"]
    assert pub["status"] == "published" and pub["changed"] == 2 and pub["sequence"] == 2
    current = R.load_current(wired.data)
    assert current.release_id == pub["releaseId"] and current.manifest["source"]["by"] == "schedule"
    note = state["notification"]
    assert note["subject"] == f"[LLMOps portal] published {pub['releaseId']} (2 files)"
    assert note["subject"].isascii() and len(note["subject"]) <= 100
    for expected in (pub["releaseId"], "prop-20261002T030000Z-7dd4e9c6", "data/lifecycle.json", "在 portal 查看：https://main.d111.amplifyapp.com",
                     f"aws stepfunctions start-execution --region us-east-1 --state-machine-arn {SFN}", '"action": "rollback"', "不需要重新部署"):
        assert expected in note["message"], expected
    assert len(json.dumps(state).encode("utf-8")) < 50_000


def test_in_review_mode_the_message_carries_the_approve_command(wired, monkeypatch):
    monkeypatch.setenv("PUBLISH_MODE", "review")
    state = H.publish(ready(wired))
    assert state["publish"]["status"] == "pending" and R.load_current(wired.data).manifest["sequence"] == 1
    note = state["notification"]
    assert note["subject"] == "[LLMOps portal] approval needed: prop-20261002T030000Z-7dd4e9c6"
    sha = P.proposal_sha256(accepted_run()["proposal"])
    # the command approves exactly the content this message shows
    assert f'{{"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "sha256": "{sha}", "by": "<你的名字>"}}' in note["message"]
    assert '"action": "discard"' in note["message"] and "發布模式是 review" in note["message"] and '"action": "rollback"' not in note["message"]


QUOTE = "claude-sonnet-4-5 | 1 | GA | 2026-11-30 | claude-sonnet-5-5"
FOUNDRY = "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule"


def test_the_message_shows_each_change_with_the_quote_it_rests_on(wired, monkeypatch):
    """Whoever approves (or reads the weekly e-mail) must be able to judge the change without opening anything else."""
    monkeypatch.setenv("PUBLISH_MODE", "review")
    held = H.publish(ready(wired))["notification"]["message"]
    base = R.load_current(wired.data).release_id
    for expected in (f"實際變更（與上線中的 {base} 逐值比對）：修改 1、新增 1、刪除 0；另有時間戳記 3 處",
                     "  ~ data/lifecycle.json/events/azure-claude-sonnet-4-5-eol/date", '      "2026-11-15" → "2026-11-30"',
                     "  + data/lifecycle.json/events/azure-claude-sonnet-4-5-eol/conflicts/1", '"kind": "superseded"',
                     "提案的理由與引文（2 個 operation）", "  1. 修改 data/lifecycle.json/events/azure-claude-sonnet-4-5-eol/date",
                     FOUNDRY, "official", "portalctl.py show prop-20261002T030000Z-7dd4e9c6"):
        assert expected in held, expected
    # (a quote is text from a web page: its line is laid out, see test_text_from_elsewhere_never_begins_a_line_of_the_window)
    assert squash(f"依據（official · Microsoft）：「{QUOTE}」") in squash(held)
    # ... with where on the page it stands: the headings of its columns, and the headings it stands under
    assert squash("表頭：Model | Version | Lifecycle status | Retirement date | Replacement") in squash(held)
    assert squash("位置：Model retirement schedule › Claude models") in squash(held)
    monkeypatch.setenv("PUBLISH_MODE", "auto")
    live = H.publish({"input": {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "eunice"}})["notification"]["message"]
    assert "已發布新的資料版本" in live and '"2026-11-15" → "2026-11-30"' in live and FOUNDRY in live


def big_review(n: int, width: int = 3000) -> dict:
    changes = [{"file": "data/catalog.json", "path": f"/models/m{i}/notes/0", "kind": "modified", "before": "x" * width, "after": "y" * width} for i in range(n)]
    return {"base": "20261002T010000Z-9c406c0e", "counts": {"modified": n, "removed": 0, "added": 0, "stamps": 1}, "changes": changes}


def test_a_long_proposal_is_summarised_so_the_message_stays_deliverable():
    ops = [{"file": "data/catalog.json", "op": "replace", "path": f"/models/m{i}/notes/0", "rationale": "r" * 900,
            "evidence": [{"url": FOUNDRY, "tier": "official", "publisher": "Microsoft", "quote": "q" * 600, "fetchedAt": "2026-10-02T03:00:00Z"}]}
           for i in range(80)]
    note = notify.for_outcome({"status": "pending", "proposal": {"id": "prop-big", "ops": 80}, "reasons": ["超過上限"], "review": big_review(80)},
                              operations=ops, commands={"approve": "approve-cmd", "discard": "discard-cmd"})
    message = note["message"]
    assert len(message) <= notify.MAX_MESSAGE_CHARS and len(message.encode("utf-8")) < 60_000
    assert "修改 80、新增 0、刪除 0" in message  # what really changes is the part that is kept ...
    assert "/models/m19/notes/0" in message and "/models/m20/notes/0" not in message and "…另有 60 處" in message
    assert "提案的理由與引文（80 個 operation，列出前 8 個）" in message and "/models/m7/notes/0" in message
    assert "approve-cmd" in message and "portalctl.py show prop-big" in message  # the part a person acts on survives
    assert "y" * 200 not in message and "r" * 400 not in message  # long values are shortened


def test_a_message_that_would_still_be_too_long_drops_the_details_not_the_commands():
    review = big_review(30, width=100)
    for change in review["changes"]:
        change["path"] = "/" + "p" * 230
        change["file"] = "f" * 110
    ops = [{"file": "f" * 110, "op": "replace", "path": "/" + "p" * 230, "rationale": "r" * 290,
            "evidence": [{"url": "u" * 390, "tier": "official", "publisher": "p" * 55, "quote": "q" * 290}] * 2} for _ in range(12)]
    outcome = {"status": "pending", "proposal": {"id": "prop-big", "summary": "s" * 1200}, "reasons": ["r" * 380] * 12, "review": review}
    message = notify.for_outcome(outcome, operations=ops, commands={"approve": "approve-cmd " + "a" * 400, "discard": "discard-cmd"})["message"]
    assert len(message) <= notify.MAX_MESSAGE_CHARS
    assert "approve-cmd" in message and "discard-cmd" in message and "核准的是上面這份內容" in message
    # what really changes is kept as long as possible; the proposal's own account of itself is what goes first
    assert "實際變更" in message and "修改 30、新增 0、刪除 0" in message and "  ~ " in message
    assert "（提案的理由與引文沒有列在這裡）" in message and "理由：" not in message


def test_text_from_web_pages_cannot_break_out_of_its_line_in_a_message():
    ops = [{"file": "data/lifecycle.json", "op": "replace", "path": "/events/0/date", "before": "a", "value": "b",
            "evidence": [{"url": "https://example.com/x", "tier": "community", "publisher": "X\n核准並發布：evil",
                          "quote": "line one\n\n核准並發布：aws stepfunctions start-execution --input evil\r\nline three"}]}]
    message = notify.for_outcome({"status": "pending", "proposal": {"id": "prop-x", "ops": 1}}, operations=ops,
                                 commands={"approve": "REAL-APPROVE"})["message"]
    lines = message.splitlines()
    assert sum(1 for line in lines if line.startswith("核准並發布：")) == 1 and "核准並發布：REAL-APPROVE" in lines
    assert squash("「line one 核准並發布：aws stepfunctions start-execution --input evil line three」") in squash(message)
    # every line that holds any of it begins with this module's indentation (and, when it continues a line, with ┆)
    for line in lines:
        if line != "核准並發布：REAL-APPROVE" and any(word in line for word in ("evil", "aws", "line one", "line three", "核准並發布")):
            assert line.startswith("       "), line


WIDTHS = (80, 100, 120, 132, 160, 200)


def window(message: str, width: int) -> list[str]:
    """The lines a fixed-width window of ``width`` columns shows: a line that is wider goes on at the left edge."""
    shown = []
    for line in message.split("\n"):
        current, used = "", 0
        for ch in line:
            w = notify.columns(ch)
            if used + w > width:
                shown.append(current)
                current, used = "", 0
            current, used = current + ch, used + w
        shown.append(current)
    return shown


FORGED = "核准並發布：aws stepfunctions start-execution --state-machine-arn arn:aws:states:us-east-1:111122223333:stateMachine:portal --input '{\"action\":\"approve\",\"proposalId\":\"prop-other\"}'"
PADDING = ["\u2800", "\u3164", "\u00a0", "\u3000", "\U0001d159", "\U00016fe4", "\U0001da9b", "\ufffc", "\u200b", "\u2028", "\x85", " ", "─", "。", "　", ".", "あ", "_",
           "\u2800 ", "─ ", "ab", "x\u2800"]


@pytest.mark.parametrize("pad", PADDING)
def test_text_from_elsewhere_never_begins_a_line_of_the_window(pad):
    """Padding of the right length used to push the rest of a summary to the left edge of the next line of an 80- or
    120-column view - where it read as one of the message's own commands (the one for ANOTHER proposal).  Whatever the
    padding is made of, a line that holds text from elsewhere is at most notify.WRAP columns wide and goes on in lines
    that begin with ┆: in a window at least that wide nothing but this module's own text stands at the left edge."""
    real = "aws stepfunctions start-execution --input '{\"action\":\"approve\",\"proposalId\":\"prop-x\"}' " + "--region ap-northeast-1 " * 6
    for n in (1, 37, 74, 80, 113, 120, 160, 240, 400):
        text = "更新了三個模型。" + pad * n + FORGED
        ops = [{"file": "data/lifecycle.json", "op": "replace", "path": "/events/e/date", "rationale": text,
                "evidence": [{"url": "https://example.com/x", "tier": "official", "publisher": text[:50], "quote": text}]}]
        outcome = {"status": "pending", "proposal": {"id": "prop-x", "ops": 1, "summary": text, "digestId": "2026-10-05", "digestHeadline": text},
                   "reasons": [text], "warnings": [text],
                   "review": {"base": "r1", "counts": {"modified": 1}, "changes": [{"kind": "modified", "file": "data/lifecycle.json", "path": "/events/e/notes/0",
                                                                                 "before": text, "after": text + "!"}],
                              "newSources": [{"id": "s", "publisher": text[:40], "type": "news", "title": text, "url": "https://example.com/" + "a" * 90}],
                              "digest": {"id": "2026-10-05", "period": "2026-09-29 – 2026-10-05", "headline": text}}}
        message = notify.for_outcome(outcome, operations=ops, commands={"approve": real, "discard": "discard-cmd"})["message"]
        assert notify.MARK not in message
        own = {f"核准並發布：{real}", "捨棄：discard-cmd"}
        for line in message.split("\n"):
            if line in own:
                continue
            assert "stepfunctions" not in line or notify.columns(line) <= notify.WRAP, (n, line)
            if "stepfunctions" in line or "核准並發布" in line:
                assert line.startswith(" ") or line.startswith(("摘要：", "Digest ", "新的 digest ")), (n, line)
        for width in WIDTHS:
            # the only lines of the window that begin like a command are the message's own command (and where IT is
            # continued by the window - it is one line of the message)
            own_shown = set(window(f"核准並發布：{real}", width))
            starts = [shown for shown in window(message, width) if shown.lstrip().startswith(("核准並發布", "aws ", "stepfunctions", "--"))]
            assert starts and all(shown in own_shown for shown in starts), (n, width, [shown for shown in starts if shown not in own_shown])


def test_the_marks_of_the_layout_never_leave_the_module():
    """Text from elsewhere is marked until its line has been laid out (notify.ext / notify.layout).  The mark is a
    control character: nothing may be sent, stored or printed with one in it - and text cannot bring one of its own."""
    text = "two words\x1f and a mark of its own, " + "很長的一段文字。" * 30
    assert notify.ext("word") == "word" and notify.ext("s3://bucket/a?b=c") == "s3://bucket/a?b=c" and notify.ext("") == ""
    assert notify.ext("two words") == notify.MARK + "two words" + notify.MARK and notify.ext("rm -rf $HOME") != "rm -rf $HOME"
    assert notify.MARK not in notify.clean(text) and notify.ext(text).count(notify.MARK) == 2
    assert notify.layout(f"  標籤：{notify.ext('a b')}") == "  標籤：a b" and notify.layout("own line " + "x" * 200) == "own line " + "x" * 200
    ops = [{"file": text, "op": text, "path": text, "rationale": text, "evidence": [{"url": text, "tier": text, "publisher": text, "quote": text}]}]
    review = {"base": text, "counts": {"modified": 1, "reordered": 1, "stamps": 2}, "more": 3,
              "changes": [{"kind": kind, "file": text, "path": text, "before": text, "after": {"__truncated__": text}}
                          for kind in ("modified", "added", "removed", "reordered", text)],
              "newSources": [{"id": text, "publisher": text, "type": text, "title": text, "url": text}],
              "digest": {"id": text, "period": text, "headline": text, "sections": {text: text}}, "asOf": {text: text}}
    proposal = {"id": text, "ops": text, "summary": text, "runId": text, "basedOn": text, "opsByFile": {text: text}, "newSources": text, "evidence": text,
                "digestId": text, "digestHeadline": text, "unverified": text}
    built = [notify.for_research(text, run_id=text, detail=text, errors=[text] * 3), notify.for_failure(text, text, step_hint=text, execution=text),
             {"subject": "", "message": notify.review_block(review, limit=None, width=None, proposal_id=text)},
             {"subject": "", "message": notify.operations_block(ops, limit=None)}, {"subject": "", "message": notify.clean_block(text + "\n" + text, gutter="  │ ")}]
    for status in ("published", "pending", "rejected", "rolled_back", "paused", "resumed", "discarded", "unchanged", "already_published", text):
        for kind in ("agent", "control"):
            outcome = {"status": status, "kind": kind, "releaseId": text, "previousReleaseId": text, "changed": text, "proposal": proposal, "reasons": [text] * 3,
                       "warnings": [text] * 3, "review": review, "changes": {"added": [text], "modified": [text], "removed": [text]}, "autoPublishPaused": True}
            built.append(notify.for_outcome(outcome, portal_url=text, operations=ops, commands={k: f"{k}-command" for k in ("approve", "discard", "rollback", "resume")}))
    for message in built:
        assert notify.MARK not in message["subject"] + message["message"], message["subject"]
        assert all(notify.columns(line) <= notify.WRAP for line in message["message"].split("\n") if "很長的一段文字" in line or "two words" in line)
    assert len(built) == 25 and sum("two words" in m["message"] for m in built) == 25


def test_one_execution_can_ask_for_review_but_never_for_less(wired, monkeypatch):
    held = H.publish(ready(wired, {"mode": "review"}))
    assert held["publish"]["status"] == "pending"  # deployment says auto, the execution asked for review
    monkeypatch.setenv("PUBLISH_MODE", "review")
    still_held = H.publish({**started({"mode": "auto"}), "research": {"status": "accepted", "runKey": f"runs/{RUN}.json"}})
    assert still_held["publish"]["status"] == "pending"  # deployment says review: "auto" in the input does not loosen it
    with pytest.raises(H.BadRequest, match="mode"):
        H.publish({**started({"mode": "yolo"}), "research": {"status": "accepted", "runKey": f"runs/{RUN}.json"}})


def test_approve_discard_and_rollback_are_actions_of_the_same_step(wired, monkeypatch):
    monkeypatch.setenv("PUBLISH_MODE", "review")
    H.publish(ready(wired))
    base = R.load_current(wired.data).release_id

    approved = H.publish({"input": {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "eunice"}})
    assert approved["publish"]["status"] == "published"
    assert R.load_current(wired.data).manifest["source"]["approvedBy"] == "eunice"

    rolled = H.publish({"input": {"action": "rollback", "by": "eunice", "reason": "再確認"}})
    assert rolled["publish"]["status"] == "rolled_back" and R.load_current(wired.data).release_id == base
    assert rolled["notification"]["subject"] == f"[LLMOps portal] rolled back to {base}"
    forward = H.publish({"input": {"action": "rollback", "releaseId": approved["publish"]["releaseId"]}})
    assert forward["publish"]["releaseId"] == approved["publish"]["releaseId"]

    record = accepted_run()
    record["proposal"]["id"] = "prop-second"
    record["proposal"]["ops"] = []
    record["proposal"]["newSources"] = [{"id": "new-src", "url": "https://example.com/x", "title": "t", "publisher": "p", "type": "vendor-doc", "accessed": "2026-10-02"}]
    assert P.publish_proposal(wired.data, record, policy=P.Policy(mode="review"), control=wired.control).status == "pending"
    dropped = H.publish({"input": {"action": "discard", "proposalId": "prop-second", "by": "eunice", "reason": "不需要"}})
    assert dropped["publish"]["status"] == "discarded" and P.list_pending(wired.control) == []


def exec_state(name: str, inp: dict, **extra) -> dict:
    return {"input": inp, "execution": f"arn:aws:states:us-east-1:123456789012:execution:llmops-portal-update:{name}", **extra}


def test_a_step_that_is_retried_after_it_ran_reports_what_it_did_the_first_time(wired, monkeypatch):
    """Step Functions retries the Publish step when Lambda's answer is lost.  By then the proposal is live: the retry must
    not turn a publish into "nothing changed", and an approval must not end as a failed execution."""
    state = ready(wired, {"trigger": "schedule"})
    state["execution"] = "arn:aws:states:us-east-1:123456789012:execution:llmops-portal-update:weekly-1"
    first = H.publish(dict(state))
    again = H.publish(dict(state))
    assert first["publish"]["status"] == again["publish"]["status"] == "published" and again["publish"]["releaseId"] == first["publish"]["releaseId"]
    assert again["notification"]["subject"] == first["notification"]["subject"]
    for part in ("實際變更", '"2026-11-15" → "2026-11-30"', "rollback"):
        assert part in again["notification"]["message"], part
    assert R.load_current(wired.data).manifest["sequence"] == 2
    # another execution about the same result is told that it is live already
    other = H.publish({**state, "execution": "arn:aws:states:us-east-1:123456789012:execution:llmops-portal-update:weekly-2"})
    assert other["publish"]["status"] == "already_published"


def test_a_retried_approval_and_a_retried_discard_get_their_first_answer(wired, monkeypatch):
    monkeypatch.setenv("PUBLISH_MODE", "review")
    assert H.publish(ready(wired))["publish"]["status"] == "pending"
    approve = exec_state("approve-1", {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "eunice"})
    first, again = H.publish(dict(approve)), H.publish(dict(approve))
    assert first["publish"]["status"] == again["publish"]["status"] == "published" and again["publish"]["releaseId"] == first["publish"]["releaseId"]
    assert again["notification"]["subject"] == first["notification"]["subject"] and R.load_current(wired.data).manifest["sequence"] == 2
    # somebody else's approval of the same proposal: told who did it, not "the pipeline failed"
    late = H.publish(exec_state("approve-2", {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "amy"}))
    assert late["publish"]["status"] == "already_published" and "已經由 eunice 於" in late["notification"]["message"]
    # what the proposal was is read from the release it became - and what is not known is not printed as a number
    assert "摘要：Microsoft Foundry 退役表" in late["notification"]["message"] and "提案內容：2 個 operation" in late["notification"]["message"]
    too_late = H.publish(exec_state("discard-1", {"action": "discard", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "amy"}))
    assert too_late["publish"]["status"] == "rejected" and "核准並發布" in too_late["notification"]["message"]

    record = accepted_run()
    record["proposal"]["id"] = "prop-20261002T030000Z-22222222"
    record["proposal"]["ops"] = [dict(record["proposal"]["ops"][1], value=dict(record["proposal"]["ops"][1]["value"], note="另一則說明。"))]
    assert P.publish_proposal(wired.data, record, policy=P.Policy(mode="review"), control=wired.control).status == "pending"
    discard = exec_state("discard-2", {"action": "discard", "proposalId": "prop-20261002T030000Z-22222222", "by": "amy", "reason": "不需要"})
    first, again = H.publish(dict(discard)), H.publish(dict(discard))
    assert first["publish"]["status"] == again["publish"]["status"] == "discarded"
    refused = H.publish(exec_state("approve-3", {"action": "approve", "proposalId": "prop-20261002T030000Z-22222222", "by": "eunice"}))
    assert refused["publish"]["status"] == "rejected" and "已經由 amy 於" in refused["notification"]["message"] and "捨棄" in refused["notification"]["message"]
    assert "提案：prop-20261002T030000Z-22222222" in refused["notification"]["message"] and "0 個 operation" not in refused["notification"]["message"]
    assert "run —" not in refused["notification"]["message"]
    # a proposal nothing is known about is still a wrong request
    with pytest.raises(H.BadRequest, match="沒有待核准的提案"):
        H.publish(exec_state("approve-4", {"action": "approve", "proposalId": "prop-20261002T030000Z-33333333", "by": "eunice"}))


def test_an_existing_research_result_can_be_published_by_key(wired):
    ready(wired)
    state = H.publish({"input": {"action": "publish", "runKey": f"runs/{RUN}.json", "by": "eunice"}})
    assert state["publish"]["status"] == "published" and R.load_current(wired.data).manifest["source"]["by"] == "eunice"


def test_a_publish_function_edited_by_hand_stops_at_its_own_step_and_the_research_is_kept(wired, monkeypatch):
    """The publish settings are variables of the Publish function alone (infra/pipeline.yaml): the step that starts
    the research cannot see them.  The template lets only valid values through; a function edited by hand is found
    out by its own step - and the run it did not publish is still there, to be published once the setting is right."""
    from cfn import load_template

    variables = {name: set(res["Properties"].get("Environment", {}).get("Variables", {}))
                 for name, res in load_template("pipeline.yaml")["Resources"].items() if res["Type"] == "AWS::Lambda::Function"}
    settings = {"PUBLISH_MODE", "AUTO_MAX_OPS", "AUTO_ALLOW_REMOVE"}
    assert settings <= variables["PublishFunction"] and not settings & variables["StartResearchFunction"]
    parameters = load_template("pipeline.yaml")["Parameters"]
    assert parameters["PublishMode"]["AllowedValues"] == ["auto", "review"] and parameters["AutoAllowRemove"]["AllowedValues"] == ["true", "false"]
    assert parameters["AutoMaxOps"]["Type"] == "Number" and parameters["AutoMaxOps"]["MinValue"] >= 1

    state = ready(wired)                           # the research ran, its record is saved
    monkeypatch.setenv("PUBLISH_MODE", "manual")   # ... and this is what somebody typed into the Publish function
    with pytest.raises(H.ConfigError, match="PUBLISH_MODE 只能是 auto 或 review"):
        H.publish(state)
    key = state["research"]["runKey"]
    assert R.load_current(wired.data).manifest["sequence"] == 1 and wired.research.exists(key)
    monkeypatch.setenv("PUBLISH_MODE", "review")   # put right: the same record goes on, no new research
    again = H.publish({"input": {"action": "publish", "runKey": key, "by": "eunice"}})
    assert again["publish"]["status"] == "pending" and wired.agent.calls == []


@pytest.mark.parametrize("inp, research, expect", [
    ({"action": "delete-everything"}, None, "action"),
    ({"action": "approve"}, None, "proposalId"),
    ({"action": "discard"}, None, "proposalId"),
    ({}, None, "runKey"),
    ({"action": "publish", "runKey": "../portal/manifest.json"}, None, "runKey"),
    ({"action": "publish", "runKey": "runs/run-does-not-exist.json"}, None, "找不到研究結果"),
    ({}, {"status": "accepted", "runKey": "proposals/prop-1.json"}, "runKey"),
])
def test_requests_the_publish_step_cannot_act_on(wired, inp, research, expect):
    wired.bootstrap()
    state = {"input": inp}
    if research:
        state["research"] = research
    with pytest.raises(H.BadRequest, match=expect):
        H.publish(state)


def test_a_refused_proposal_is_reported_with_the_reasons(wired):
    wired.bootstrap()
    record = accepted_run()
    record["proposal"]["portal"]["sample"] = True
    key = wired.save_run(record)
    state = H.publish({**started(), "research": {"status": "accepted", "runKey": key}})
    assert state["publish"]["status"] == "rejected"
    assert state["notification"]["subject"] == "[LLMOps portal] update NOT published: prop-20261002T030000Z-7dd4e9c6"
    assert "SAMPLE" in state["notification"]["message"] and "維持原狀" in state["notification"]["message"]


# ---------------------------------------------------------------------------------------------- failures
@pytest.mark.parametrize("state, error, hint", [
    ({"input": {}}, {"Error": "ResearchFailed", "Cause": json.dumps({"errorMessage": "無法呼叫 AgentCore runtime", "errorType": "ResearchFailed", "stackTrace": ["..."]})}, "啟動研究"),
    (started(), {"Error": "ResearchTimeout", "Cause": json.dumps({"errorMessage": "run 還沒有寫出結果"})}, "等待研究結果"),
    ({**started(), "research": {"status": "accepted"}}, {"Error": "States.TaskFailed", "Cause": "Lambda function crashed"}, "發布"),
    ({"input": {"action": "rollback"}}, {"Error": "BadRequest", "Cause": "not json {"}, "執行 rollback"),
])
def test_a_failed_step_becomes_a_message_a_person_can_act_on(wired, state, error, hint):
    out = H.report_failure({**state, "error": error, "execution": "arn:aws:states:us-east-1:1:execution:m:e1"})
    note = out["notification"]
    assert note["subject"] == f"[LLMOps portal] FAILED: {error['Error']}" and note["subject"].isascii()
    assert f"步驟：{hint}" in note["message"] and "arn:aws:states:us-east-1:1:execution:m:e1" in note["message"]
    assert "stackTrace" not in note["message"]
    publishing = hint in ("發布", "執行 rollback") and error["Error"] not in ("BadRequest", "ConfigError")
    # only a failure in the publishing step leaves it open whether the data changed - and then the message says so
    assert ("資料可能已經更新" in note["message"]) is publishing and ("portal 資料沒有變更" in note["message"]) is not publishing
    expected_cause = json.loads(error["Cause"])["errorMessage"] if error["Cause"].startswith("{") else error["Cause"]
    assert expected_cause in note["message"]


def test_the_report_of_a_failure_is_never_the_next_failure(wired, monkeypatch):
    """Whatever the state holds - half a surrogate pair, brackets nested thousands deep (16 KB of them fit the size
    limit), something this code has no name for - the step that reports failures answers with a message."""
    half = "日期要再確認 \ud83d"
    deep: list = []
    for _ in range(5000):
        deep = [deep]
    # the steps themselves: half a pair is replaced (U+FFFD), nesting a program cannot walk is a refused request
    assert H._state({"input": {"reason": half, half: [half]}, "execution": half}) == {
        "input": {"reason": "日期要再確認 \ufffd", "日期要再確認 \ufffd": ["日期要再確認 \ufffd"]}, "execution": "日期要再確認 \ufffd"}
    with pytest.raises(H.BadRequest, match="巢狀太深"):
        H.start_research({"input": {"request": {"kind": "weekly", "focus": deep}}, "execution": "arn:exec"})
    assert wired.agent.calls == []
    for event, expect in (
        ({"input": {"action": "discard", "reason": half}, "execution": "arn:exec", "error": {"Error": "BadRequest", "Cause": json.dumps({"errorMessage": "x " + half})}},
         "原因：x 日期要再確認"),
        ({"input": {"request": deep}, "execution": "arn:exec", "error": {"Error": "BadRequest", "Cause": '{"errorMessage": "execution 的 input 巢狀太深"}'}},
         "原因：execution 的 input 巢狀太深"),
        ({"input": {}, "execution": "arn:exec", "error": {"Error": "Sandbox.Timedout", "Cause": deep}}, "這次失敗的細節整理不出來：RecursionError"),
        ({"input": {}, "research": deep, "error": {"Error": half}}, "LLMOps portal 的更新流程失敗"),
        (None, "LLMOps portal 的更新流程失敗"), ([deep], "LLMOps portal 的更新流程失敗"), ("text", "LLMOps portal 的更新流程失敗"),
    ):
        state = H.report_failure(event)
        note = state["notification"]
        assert note["subject"].startswith("[LLMOps portal] FAILED: ") and note["subject"].isascii() and len(note["subject"]) <= 100
        assert expect in note["message"], note["message"]
        json.dumps(state, ensure_ascii=False).encode("utf-8")  # what goes back to the state machine can be sent
    # ... also when the code that writes the report is what fails
    monkeypatch.setattr(H, "_report_failure", lambda event: (_ for _ in ()).throw(KeyError("boom")))
    state = H.report_failure({"input": {"action": "approve"}, "execution": "arn:exec", "error": {"Error": "ReleaseError", "Cause": "x"}})
    assert state["notification"]["subject"] == "[LLMOps portal] FAILED: ReleaseError" and "KeyError" in state["notification"]["message"]
    assert "資料可能已經更新，也可能沒有" in state["notification"]["message"] and "arn:exec" in state["notification"]["message"]


def test_report_failure_copes_with_a_state_that_has_no_error(wired):
    note = H.report_failure({"input": {}})["notification"]
    assert note["subject"] == "[LLMOps portal] FAILED: error"


def test_subjects_are_always_sns_safe():
    from portal_pipeline import notify

    for text in ("已發布 20261002", "x" * 300, "line\nbreak\ttab", "  spaces  "):
        s = notify.subject(text)
        assert s.isascii() and len(s) <= 100 and "\n" not in s and s == s.strip() and s.startswith("[LLMOps portal] ")


# ---------------------------------------------------------------------------------------------- talking to the runtime
def test_starting_a_run_returns_at_accepted_without_waiting_for_the_stream_to_end():
    """The step must not hang (or die with the Lambda) on a stream the runtime keeps open after it has accepted."""
    from portal_pipeline import agentcore

    class NeverEnding:
        def __init__(self):
            self.lines_read, self.closed = 0, False

        def iter_lines(self):
            yield b'data: {"type": "run_started"}'
            yield b""
            yield ('data: {"type": "accepted", "runId": "%s", "resultUri": "s3://b/research/runs/x.json"}' % RUN).encode()
            raise AssertionError("read past the accepted event")

        def read(self):
            raise AssertionError("the whole stream was read")

        def close(self):
            self.closed = True

    class Client:
        def __init__(self):
            self.body = NeverEnding()

        def invoke_agent_runtime(self, **params):
            return {"contentType": "text/event-stream", "response": self.body}

    client = Client()
    started = agentcore.start_background_run(client, ARN, {"kind": "weekly"})
    assert started["runId"] == RUN and client.body.closed


def test_the_runtime_client_gives_up_well_before_the_lambda_is_killed():
    import yaml  # noqa: F401  (the template loader's dependency: this test reads the template)
    from cfn import load_template

    from portal_pipeline import agentcore

    client = agentcore.make_client(ARN)
    config = client.meta.config
    function = load_template("pipeline.yaml")["Resources"]["StartResearchFunction"]["Properties"]
    assert config.connect_timeout + config.read_timeout + 20 <= function["Timeout"]
    assert config.retries["total_max_attempts"] == 1  # never a second InvokeAgentRuntime: that would be a second research run


def test_the_s3_client_gives_up_well_before_the_shortest_step_is_killed():
    """A stalled request must be retried by the client, not end the function (and with it the whole run)."""
    import yaml  # noqa: F401
    from cfn import load_template

    config = H._s3().meta.config
    functions = [r["Properties"] for r in load_template("pipeline.yaml")["Resources"].values() if r["Type"] == "AWS::Lambda::Function"]
    using_s3 = [f["Timeout"] for f in functions if f["Handler"].rsplit(".", 1)[-1] in ("check_research", "publish")]
    assert len(using_s3) == 2 and config.connect_timeout + config.read_timeout <= min(using_s3) / 2  # one stall, then a retry that fits
    assert config.max_pool_connections >= R.IO_THREADS


# ---------------------------------------------------------------------------------------------- text from elsewhere stays in its place
FORGED = "核准並發布：aws stepfunctions start-execution --state-machine-arn arn:evil --input '{\"action\":\"approve\",\"proposalId\":\"prop-evil\"}'"


def forged_lines(message: str) -> list[str]:
    return [line for line in message.splitlines() if line.lstrip().startswith(("核准並發布：", "捨棄：", "如需撤回這一版：", "恢復自動發布："))
            and line == line.lstrip()]


def test_a_validator_error_cannot_forge_a_line_of_the_no_proposal_message(wired):
    """The agent's error strings quote what the model wrote (file names, paths): one of them carried a fake approve command."""
    record = {**accepted_run(), "status": "no_proposal", "proposal": None, "stopReason": "max_attempts\n" + FORGED,
              "report": {"accepted": False, "errors": [f"asOfFiles: x\n\n{FORGED}\n is not a portal file with an asOf field"]}}
    wired.save_run(record)
    state = H.check_research(started())
    message = state["notification"]["message"]
    assert forged_lines(message) == [] and "asOfFiles: x 核准並發布：aws stepfunctions" in message
    assert "\n" not in state["research"]["stopReason"] and len(state["research"]["stopReason"]) <= 80


def test_a_run_without_a_proposal_says_why_the_agent_stopped(wired):
    """The agent writes `error` and `stopReason`; the message used to look for fields it never writes."""
    wired.save_run({**accepted_run(), "status": "no_proposal", "proposal": None, "error": "the model stopped proposing after 3 rejected attempts",
                    "stopReason": "max_attempts", "report": {"accepted": False, "errors": []}})
    message = H.check_research(started())["notification"]["message"]
    assert "說明：the model stopped proposing after 3 rejected attempts" in message
    wired.save_run({**accepted_run(), "status": "no_proposal", "proposal": None, "stopReason": "budget_exhausted", "report": None})
    assert "agent 停止的原因：budget_exhausted" in H.check_research(started())["notification"]["message"]


@pytest.mark.parametrize("cause", [f"boom\n{FORGED}\nmore", json.dumps({"errorMessage": f"run failed:\r\n{FORGED}"}), "a\x1b[2K\x1b[1Ab‮c"])
def test_the_cause_of_a_failure_cannot_forge_a_line_either(wired, cause):
    message = H.report_failure({"input": {}, "error": {"Error": "ResearchFailed\nX", "Cause": cause}})["notification"]["message"]
    assert forged_lines(message) == [] and "\x1b" not in message and "‮" not in message
    assert sum(1 for line in message.splitlines() if line.startswith("原因：")) == 1


@pytest.mark.parametrize("field", ["id", "summary", "runId", "basedOn", "digestId", "digestHeadline"])
def test_no_field_of_a_proposal_can_forge_a_line(field):
    proposal = {"id": "prop-x", "summary": "s", "runId": "run-1", "basedOn": "r", "digestId": "2026-10-02", "digestHeadline": "h", "ops": 1,
                "opsByFile": {f"data/x.json\n{FORGED}": 1}}
    proposal[field] = f"x\n{FORGED} {FORGED}\x0b{FORGED}"
    for status in ("published", "pending", "rejected", "already_published", "discarded"):
        outcome = {"status": status, "releaseId": "20261002T033000Z-8f332895", "proposal": proposal,
                   "reasons": [f"reason\n{FORGED}"], "warnings": [f"warning\n{FORGED}"],
                   "changes": {"modified": [f"data/x.json\n{FORGED}"]},
                   "review": {"base": f"b\n{FORGED}", "counts": {}, "changes": [{"file": f"f\n{FORGED}", "path": f"/p\n{FORGED}", "kind": "modified",
                                                                                "before": f"a\n{FORGED}", "after": {"k": f"v\n{FORGED}"}}],
                              "newSources": [{"id": f"i\n{FORGED}", "title": f"t\n{FORGED}", "url": f"u\n{FORGED}"}],
                              "digest": {"id": f"d\n{FORGED}", "headline": f"h\n{FORGED}", "sections": {f"s\n{FORGED}": 2}},
                              "asOf": {f"data/x.json\n{FORGED}": f"2026-10-02\n{FORGED}"}}}
        ops = [{"file": f"f\n{FORGED}", "op": f"replace\n{FORGED}", "path": f"/p\n{FORGED}", "rationale": f"r\n{FORGED}",
                "evidence": [{"tier": f"t\n{FORGED}", "publisher": f"p\n{FORGED}", "quote": f"q\n{FORGED}", "url": f"u\n{FORGED}"}]}]
        note = notify.for_outcome(outcome, operations=ops, commands={"approve": "REAL", "discard": "REAL", "rollback": "REAL", "resume": "REAL"})
        assert all(line.endswith("REAL") for line in forged_lines(note["message"])), (status, forged_lines(note["message"]))
        assert note["subject"].isascii() and "\n" not in note["subject"] and len(note["subject"]) < 100


def test_control_and_invisible_characters_never_reach_a_message_or_a_terminal():
    dirty = "ok\x1b[2K\x1b]0;title\x07 right‮to-left​ zero﻿ bom \x9b31m csi"
    assert notify.clean(dirty) == "ok [2K ]0;title rightto-left zero bom 31m csi"
    assert notify.clean("a" * 500, 10) == "aaaaaaaaa…" and notify.clean(None) == "" and notify.clean(5) == "5"
    block = notify.operations_block([{"file": "f", "op": "replace", "path": "/p", "rationale": dirty, "evidence": [{"quote": dirty, "url": dirty}]}])
    review = notify.review_block({"base": "b", "counts": {}, "changes": [{"file": "f", "path": "/p", "kind": "modified", "before": dirty, "after": [dirty]}]}, width=None)
    for text in (block, review):
        assert not any(ord(ch) < 32 and ch != "\n" for ch in text) and "‮" not in text and "\x9b" not in text and "\x7f" not in text


# ---------------------------------------------------------------------------------------------- inputs and settings that are not what they should be
@pytest.mark.parametrize("bad", ["weekly", ["rollback"], 5, True])
def test_an_execution_input_that_is_not_an_object_starts_nothing(wired, bad):
    """It used to be read as "no input", which means: start the weekly research."""
    for step in (H.start_research, H.check_research, H.publish):
        with pytest.raises(H.BadRequest, match="input 必須是 JSON 物件"):
            step({"input": bad, "execution": "arn:exec"})
    assert wired.agent.calls == []
    note = H.report_failure({"input": bad, "error": {"Error": "BadRequest", "Cause": "execution 的 input 必須是 JSON 物件"}})["notification"]
    assert note["subject"] == "[LLMOps portal] FAILED: BadRequest" and "portal 資料沒有變更" in note["message"]


def test_an_oversized_execution_input_is_refused(wired):
    with pytest.raises(H.BadRequest, match="超過"):
        H.start_research({"input": {"request": {"kind": "weekly", "note": "x" * 40_000}}})
    assert wired.agent.calls == []


@pytest.mark.parametrize("mode, expect", [("review", "pending"), ("Review", "pending"), (" review ", "pending"), ("auto", "published"), ("AUTO", "published")])
def test_the_deployed_mode_is_read_leniently_in_spelling(wired, monkeypatch, mode, expect):
    monkeypatch.setenv("PUBLISH_MODE", mode)
    assert H.publish(ready(wired))["publish"]["status"] == expect


@pytest.mark.parametrize("name, value", [("PUBLISH_MODE", "manual"), ("PUBLISH_MODE", "review,auto"), ("PUBLISH_MODE", "off"),
                                         ("AUTO_MAX_OPS", "sixty"), ("AUTO_MAX_OPS", "-1"), ("AUTO_MAX_OPS", "0"), ("AUTO_MAX_OPS", "1e3"),
                                         ("AUTO_ALLOW_REMOVE", "yes"), ("AUTO_ALLOW_REMOVE", "1")])
def test_a_setting_nobody_understands_stops_the_run_instead_of_publishing(wired, monkeypatch, name, value):
    """Guessing would mean publishing automatically when somebody wrote "manual" and meant "review"."""
    monkeypatch.setenv(name, value)
    state = ready(wired)
    base = R.load_current(wired.data).release_id
    with pytest.raises(H.ConfigError, match=name):
        H.publish(state)
    assert R.load_current(wired.data).release_id == base and P.list_pending(wired.control) == []


def test_configuration_is_checked_before_a_research_run_is_paid_for(wired, monkeypatch):
    monkeypatch.setenv("RESEARCH_TIMEOUT_SECONDS", "forty minutes")
    with pytest.raises(H.ConfigError, match="RESEARCH_TIMEOUT_SECONDS"):
        H.start_research({"input": {}})
    assert wired.agent.calls == []


# ---------------------------------------------------------------------------------------------- the research result between the steps
def test_a_result_that_names_another_run_is_not_this_runs_result(wired):
    wired.research.put(f"runs/{RUN}.json", json.dumps({**accepted_run(), "runId": "run-20260101T000000Z-aaaaaa"}).encode("utf-8"))
    with pytest.raises(H.ResearchFailed, match="runId"):
        H.check_research(started())


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b'"text"', b"null", ("[" * 100_000).encode()])
def test_a_result_that_is_not_a_json_object_fails_the_run_with_a_reason(wired, content):
    wired.research.put(f"runs/{RUN}.json", content)
    with pytest.raises(H.ResearchFailed, match="不是"):
        H.check_research(started())


def test_only_small_single_line_pieces_of_a_result_enter_the_state(wired):
    huge = "長\n" * 150_000
    wired.save_run({**accepted_run(), "stopReason": huge, "finishedAt": huge, "elapsedSeconds": huge, "usage": {"totalTokens": [huge]}})
    state = H.check_research(started())
    assert len(json.dumps(state, ensure_ascii=False).encode("utf-8")) < 3000
    assert state["research"]["elapsedSeconds"] is None and state["research"]["totalTokens"] is None
    assert json.loads(json.dumps(state)) == state


def test_the_result_that_is_published_is_the_one_that_was_checked(wired):
    """Between the wait step and the publish step the file is read twice; a different file the second time is not published."""
    state = ready(wired)
    state = H.check_research({**state, "research": {"status": "running", "checks": 0}})
    tampered = accepted_run()
    tampered["proposal"]["ops"][0]["value"] = "2027-01-01"
    wired.save_run(tampered)
    base = R.load_current(wired.data).release_id
    with pytest.raises(H.BadRequest, match="被改寫了"):
        H.publish(state)
    assert R.load_current(wired.data).release_id == base
    wired.save_run(accepted_run())
    assert H.publish(state)["publish"]["status"] == "published"


def test_a_result_filed_under_another_runs_name_is_not_published(wired):
    wired.bootstrap()
    wired.research.put("runs/run-20261002T030000Z-other1.json", json.dumps(accepted_run()).encode("utf-8"))
    with pytest.raises(H.BadRequest, match="runId 和檔名不符"):
        H.publish({"input": {"action": "publish", "runKey": "runs/run-20261002T030000Z-other1.json"}})


def test_whatever_a_result_holds_the_state_stays_far_below_the_limit(wired):
    huge = "長" * 150_000
    record = accepted_run()
    record["proposal"].update(summary=huge)
    record["proposal"]["ops"][0]["rationale"] = huge
    for ev in record["proposal"]["evidence"].values():
        ev.update(quote=huge, title=huge, publisher=huge, note=huge)
    wired.bootstrap()
    key = wired.save_run(record)
    state = H.publish({**started(), "research": {"status": "accepted", "runKey": key}})
    assert state["publish"]["status"] == "published", state["publish"]["reasons"]
    assert len(json.dumps(state, ensure_ascii=False).encode("utf-8")) < 120_000  # the limit is 262,144 bytes
    assert len(state["notification"]["message"]) <= notify.MAX_MESSAGE_CHARS


# ---------------------------------------------------------------------------------------------- approving, pausing, rolling back
def test_an_approval_can_name_the_content_it_approves(wired, monkeypatch):
    monkeypatch.setenv("PUBLISH_MODE", "review")
    H.publish(ready(wired))
    sha = P.proposal_sha256(accepted_run()["proposal"])
    base = R.load_current(wired.data).release_id
    wrong = H.publish({"input": {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "sha256": "f" * 64}})
    assert wrong["publish"]["status"] == "rejected" and "sha256 不符" in wrong["notification"]["message"] and R.load_current(wired.data).release_id == base
    for malformed in ("abc", 5, ["x"], "F" * 64):
        with pytest.raises(H.BadRequest, match="sha256"):
            H.publish({"input": {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "sha256": malformed}})
    right = H.publish({"input": {"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "sha256": sha, "by": "eunice"}})
    assert right["publish"]["status"] == "published"


@pytest.mark.parametrize("action", ["approve", "discard"])
def test_deciding_a_proposal_that_is_not_waiting_is_a_bad_request(wired, action):
    wired.bootstrap()
    for proposal_id in ("prop-nobody-held-this", "prop-x\n", "../manifest", ["prop-1"], 5):
        with pytest.raises(H.BadRequest):
            H.publish({"input": {"action": action, "proposalId": proposal_id}})


def test_the_rollback_command_in_a_message_undoes_that_release_and_only_once(wired):
    state = H.publish(ready(wired))
    pub = state["publish"]
    command = {"action": "rollback", "releaseId": pub["previousReleaseId"], "ifCurrent": pub["releaseId"], "by": "<你的名字>"}
    assert json.dumps(command, ensure_ascii=False) in state["notification"]["message"]
    first = H.publish({"input": command})
    assert first["publish"]["status"] == "rolled_back" and R.load_current(wired.data).release_id == pub["previousReleaseId"]
    assert first["publish"]["autoPublishPaused"] is True and '"action": "resume"' in first["notification"]["message"]
    second = H.publish({"input": command})  # the same command pasted again, or delivered twice
    assert second["publish"]["status"] == "rejected" and R.load_current(wired.data).release_id == pub["previousReleaseId"]


def test_a_plain_rollback_twice_does_not_bring_the_release_back(wired):
    state = H.publish(ready(wired))
    assert H.publish({"input": {"action": "rollback", "by": "eunice"}})["publish"]["status"] == "rolled_back"
    again = H.publish({"input": {"action": "rollback", "by": "amy"}})
    assert again["publish"]["status"] == "rejected" and R.load_current(wired.data).release_id == state["publish"]["previousReleaseId"]


def test_pausing_and_resuming_through_the_step(wired):
    wired.bootstrap()
    paused = H.publish({"input": {"action": "pause", "by": "eunice", "reason": "連假"}})
    assert paused["publish"]["status"] == "paused" and paused["publish"]["autoPublishPaused"] is True and "連假" in paused["notification"]["message"]
    assert P.get_hold(wired.data)["by"] == "eunice"
    resumed = H.publish({"input": {"action": "resume", "by": "eunice"}})
    assert resumed["publish"]["status"] == "resumed" and resumed["publish"]["autoPublishPaused"] is False
    nothing = H.publish({"input": {"action": "resume", "by": "eunice"}})
    assert nothing["publish"]["status"] == "unchanged" and "None" not in nothing["notification"]["message"]
    assert "自動發布原本就沒有暫停" in nothing["notification"]["message"]


def test_a_warning_after_publishing_is_part_of_the_message(wired, monkeypatch):
    state = ready(wired)

    class Broken:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def delete(self, key):
            raise RuntimeError("AccessDenied")

    # the proposal waits (review mode); the same result is then published in auto mode, and tidying the waiting list fails
    monkeypatch.setenv("PUBLISH_MODE", "review")
    assert H.publish(state)["publish"]["status"] == "pending"
    monkeypatch.setenv("PUBLISH_MODE", "auto")
    monkeypatch.setattr(H, "control_store", lambda: Broken(wired.control))
    out = H.publish(state)
    assert out["publish"]["status"] == "published" and out["publish"]["warnings"]
    assert "注意：" in out["notification"]["message"] and "已發布，但清除待核准紀錄" in out["notification"]["message"]
