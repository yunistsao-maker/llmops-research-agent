"""AWS Lambda entry points for the portal update state machine (infra/pipeline.yaml).

Every handler takes the state machine's state and returns the new state, so the state machine itself stays a plain
sequence of steps.  The state is small on purpose (Step Functions limits it to 256 KB): proposals stay in S3 and are
referred to by key, and everything copied into the state from a research result is cut to a fixed size.

    {"input": <what the execution was started with>, "execution": <execution ARN>,
     "run": {...}, "research": {...}, "publish": {...}, "notification": {"subject", "message"}, "error": {...}}

Execution inputs (see docs/runbook.md):

    {}                                              weekly research, then publish
    {"kind": "focus", "focus": ["lifecycle"]}       any agent request, then publish
    {"request": {...}, "trigger": "schedule"}       the same, with the request spelled out (the weekly schedule uses this)
    {"action": "approve", "proposalId": "prop-...", "sha256": "..."}
                                                    publish a proposal that is waiting for approval (sha256: the content
                                                    that was reviewed; optional, checked when given)
    {"action": "discard", "proposalId": "prop-..."} drop it
    {"action": "rollback"}                          make the previous release live again (or "releaseId": "...");
                                                    this also pauses automatic publishing.  "ifCurrent": "<release id>"
                                                    makes it apply only while that release is the live one
    {"action": "pause", "reason": "..."}            every proposal waits for approval until ...
    {"action": "resume"}                            ... automatic publishing is resumed
    {"action": "publish", "runKey": "runs/run-....json"}   publish an existing research result
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any, Callable, Mapping

from . import notify
from .agentcore import RUN_ID, AgentError, make_client, start_background_run
from .publish import Policy, answered, approve, discard, get_hold, load_pending, pause, publish_proposal, resume, rollback
from .releases import ReleaseError, iso, sha256_hex
from .storage import NotFound, S3Store, Store

CONTROL_KEYS = {"request", "trigger", "mode", "by", "action", "proposalId", "releaseId", "ifCurrent", "sha256", "reason", "runKey"}
ACTIONS = ("publish", "approve", "discard", "rollback", "pause", "resume")
_RUN_KEY = re.compile(r"runs/run-[A-Za-z0-9-]{1,80}\.json")  # used with fullmatch
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: An execution input is echoed through every step (and its request once more); with the rest of the state it has to
#: stay far below 256 KB.  A research request is a few hundred bytes.
MAX_INPUT_BYTES = 16_000


class BadRequest(Exception):
    """The execution input cannot be acted on."""


class ConfigError(Exception):
    """The deployment is wired inconsistently (the message says what to change)."""


class ResearchTimeout(Exception):
    """The research run did not write a result in time."""


class ResearchFailed(Exception):
    """The research run ended with an error."""


# ---------------------------------------------------------------------------------------------- wiring (replaced in tests)
def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        if default is not None:
            return default
        raise ConfigError(f"environment variable {name} is not set")
    return value


def _int_env(name: str, default: str, minimum: int) -> int:
    raw = _env(name, default).strip()
    if not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) < minimum:
        raise ConfigError(f"{name} 必須是不小於 {minimum} 的整數，目前是 {raw!r}")
    return int(raw)


def _s3() -> Any:
    import boto3
    from botocore.config import Config

    # Timeouts well inside the shortest function timeout (CheckResearch: 30 s), so that a stalled request is retried by
    # the client instead of killing the function; the pool is large enough for the publisher's concurrent reads.
    return boto3.client("s3", config=Config(connect_timeout=5, read_timeout=10, max_pool_connections=16,
                                            retries={"total_max_attempts": 3, "mode": "standard"}))


clients: dict[str, Callable[..., Any]] = {"s3": _s3, "agentcore": make_client}
clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)  # noqa: E731


def _store(prefix_env: str, default_prefix: str) -> Store:
    return S3Store(_env("DATA_BUCKET"), _env(prefix_env, default_prefix), clients["s3"]())


def data_store() -> Store:
    return _store("PORTAL_PREFIX", "portal/")


def research_store() -> Store:
    return _store("RESEARCH_PREFIX", "research/")


def control_store() -> Store:
    return _store("PIPELINE_PREFIX", "pipeline/")


def policy_for(state: Mapping[str, Any]) -> Policy:
    """The deployment's policy; one execution may ask for stricter handling ("mode": "review"), never for looser.

    A setting this code does not understand stops the run: guessing would mean publishing automatically when somebody
    wrote "manual" and meant "review".
    """
    asked = (state.get("input") or {}).get("mode")
    if asked is not None and asked not in ("auto", "review"):
        raise BadRequest(f"mode 只能是 auto 或 review，收到 {notify.clean(asked, 40)!r}")
    deployed = _env("PUBLISH_MODE", "review").strip().lower()  # a deployment that does not say is not one that publishes unseen
    if deployed not in ("auto", "review"):
        raise ConfigError(f"PUBLISH_MODE 只能是 auto 或 review，目前是 {deployed!r}（pipeline stack 的 PublishMode 參數）")
    allow_remove = _env("AUTO_ALLOW_REMOVE", "false").strip().lower()
    if allow_remove not in ("true", "false"):
        raise ConfigError(f"AUTO_ALLOW_REMOVE 只能是 true 或 false，目前是 {allow_remove!r}")
    return Policy(mode="review" if "review" in (deployed, asked) else "auto", max_ops=_int_env("AUTO_MAX_OPS", "60", 1),
                  allow_remove=allow_remove == "true")


# ---------------------------------------------------------------------------------------------- helpers
def _state(event: Any) -> dict:
    if not isinstance(event, dict):
        raise BadRequest("the state machine passes a JSON object to every step")
    try:
        state = _whole(dict(event))
        given = state.get("input", {})
        if given is None:
            given = {}
        if not isinstance(given, dict):
            raise BadRequest("execution 的 input 必須是 JSON 物件，例如 {} 或 {\"action\": \"rollback\"}；"
                             f"收到的是 {type(given).__name__}（沒有啟動任何動作）")
        size = len(json.dumps(given, ensure_ascii=False).encode("utf-8"))
    except RecursionError:  # brackets nested a thousand deep: nothing this pipeline is ever asked
        raise BadRequest("execution 的 input 巢狀太深，無法處理（沒有啟動任何動作）") from None
    if size > MAX_INPUT_BYTES:
        raise BadRequest(f"execution 的 input 超過 {MAX_INPUT_BYTES} bytes")
    state["input"] = given
    return state


_HALF = re.compile("[\ud800-\udfff]")


def _whole(value: Any) -> Any:
    """``value`` (parsed JSON) in which no text holds half a surrogate pair: each half becomes U+FFFD.

    JSON may say "\\ud83d" - an emoji cut in two by whatever shortened a text.  Python keeps it, and a text that holds
    one cannot be encoded: not measured, not hashed, not stored, not sent.  Every step would fail on it, the step that
    reports failures included.
    """
    if isinstance(value, str):
        return _HALF.sub("\ufffd", value) if _HALF.search(value) else value
    if isinstance(value, dict):
        return {_whole(k): _whole(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole(v) for v in value]
    return value


def request_of(state: Mapping[str, Any]) -> dict:
    """The research request inside an execution input (see the module docstring for the accepted shapes)."""
    inp = state.get("input") or {}
    if "request" in inp:
        req = inp["request"]
        if not isinstance(req, dict):
            raise BadRequest("request 必須是 JSON 物件，例如 {\"kind\": \"weekly\"}")
    else:
        req = {k: v for k, v in inp.items() if k not in CONTROL_KEYS}
    return dict(req) or {"kind": "weekly"}


def actor_of(state: Mapping[str, Any]) -> str:
    inp = state.get("input") or {}
    return notify.clean(inp.get("by") or inp.get("trigger") or "manual", 80)


def _request_id(state: Mapping[str, Any]) -> str | None:
    """A token for "this execution": the same for every retry of a step, different for every new request.

    (A hash, because it is stored in manifest.json, which the UI's users can read: no account id, no ARN.)
    """
    execution = state.get("execution")
    return sha256_hex(execution.encode("utf-8"))[:16] if isinstance(execution, str) and execution else None


def _text(value: Any, limit: int) -> str | None:
    return None if value is None else notify.clean(value, limit)


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf") else None


def _commands(outcome: dict) -> dict[str, str]:
    """Ready-to-paste commands for what a person may want to do next.  Each one refers to exactly what this message is
    about: the approval carries the hash of the content shown, the rollback names both releases."""
    arn = os.environ.get("STATE_MACHINE_ARN", "")
    if not arn:
        return {}
    parts = arn.split(":")
    region = f" --region {parts[3]}" if len(parts) > 3 and parts[3] else ""
    base = f"aws stepfunctions start-execution{region} --state-machine-arn {arn} --input "

    def cmd(doc: dict) -> str:
        return base + "'" + json.dumps(doc, ensure_ascii=False) + "'"

    who = {"by": "<你的名字>"}
    out = {"resume": cmd({"action": "resume", **who})}
    proposal = outcome.get("proposal") if isinstance(outcome.get("proposal"), dict) else {}
    proposal_id, sha = str(proposal.get("id") or ""), str(proposal.get("sha256") or "")
    if outcome.get("status") == "pending" and re.fullmatch(r"prop-[A-Za-z0-9][A-Za-z0-9._-]{0,80}", proposal_id):
        out["approve"] = cmd({"action": "approve", "proposalId": proposal_id, **({"sha256": sha} if _SHA256.fullmatch(sha) else {}), **who})
        out["discard"] = cmd({"action": "discard", "proposalId": proposal_id, **who})
    live, previous = outcome.get("releaseId"), outcome.get("previousReleaseId")
    if outcome.get("status") == "published" and live and previous:
        out["rollback"] = cmd({"action": "rollback", "releaseId": previous, "ifCurrent": live, **who})
    return out


# ---------------------------------------------------------------------------------------------- steps
def start_research(event: Any, context: Any = None) -> dict:
    """Start a background research run and remember where its result will appear."""
    state = _state(event)
    request = request_of(state)
    # What can be wrong with the request, and with the settings of THIS function, is read before a run (and its bill)
    # is started: a "mode" this pipeline does not know must not be found out after forty minutes of research.
    # (PUBLISH_MODE, AUTO_MAX_OPS and AUTO_ALLOW_REMOVE are settings of the Publish function: the template only lets
    # valid values through, and a function edited by hand is found out by the Publish step - the research it ran is
    # kept, and `portalctl publish s3://.../research/runs/<run>.json` publishes it once the setting is right.)
    policy_for(state)
    arn = _env("AGENT_RUNTIME_ARN")
    bucket, prefix = _env("DATA_BUCKET"), _env("RESEARCH_PREFIX", "research/")
    timeout = _int_env("RESEARCH_TIMEOUT_SECONDS", "2400", 60)
    now = clock()
    try:
        started = start_background_run(clients["agentcore"](arn), arn, request, qualifier=os.environ.get("AGENT_QUALIFIER") or None)
    except AgentError as e:
        raise ResearchFailed(str(e)) from e
    run_id = started["runId"]
    print(json.dumps({"event": "research_started", "runId": run_id, "sessionId": started["sessionId"]}), flush=True)
    expected = f"s3://{bucket}/{prefix}runs/{run_id}.json"
    if started.get("resultUri") != expected:
        raise ConfigError(f"runtime 會把結果寫到 {notify.clean(started.get('resultUri'), 300)}，但 pipeline 等的是 {expected}："
                          f"把 runtime 的 OUTPUT_URI 設成 s3://{bucket}/{prefix} 後重新 deploy")
    state["run"] = {"runId": run_id, "sessionId": started["sessionId"], "resultUri": expected, "request": request,
                    "startedAt": iso(now), "deadline": iso(now + dt.timedelta(seconds=timeout))}
    state["research"] = {"status": "running", "checks": 0}
    return state


def _read_record(store: Store, key: str) -> tuple[Any, str]:
    """(the parsed result, the sha256 of its bytes).  Raises NotFound; a file that is not JSON is a failed run."""
    data = store.get(key).data
    try:
        return json.loads(data), sha256_hex(data)
    except (ValueError, RecursionError) as e:
        raise ResearchFailed(f"研究結果 {store.uri(key)} 不是有效的 JSON（{type(e).__name__}）") from e


def check_research(event: Any, context: Any = None) -> dict:
    """Has the run written its result yet?  research.status: running | accepted | no_proposal (anything else raises)."""
    state = _state(event)
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    run_id = str(run.get("runId", ""))
    if not RUN_ID.fullmatch(run_id):
        raise BadRequest(f"no research run to wait for (run.runId = {notify.clean(run_id, 100)!r})")
    key = f"runs/{run_id}.json"
    previous = state.get("research") if isinstance(state.get("research"), dict) else {}
    checks = (previous.get("checks") if isinstance(previous.get("checks"), int) else 0) + 1
    store = research_store()
    try:
        record, digest = _read_record(store, key)
    except NotFound:
        deadline = dt.datetime.fromisoformat(str(run.get("deadline")).replace("Z", "+00:00"))
        if clock() > deadline:
            raise ResearchTimeout(f"run {run_id} 到 {run['deadline']} 還沒有寫出結果（{store.uri(key)}）。"
                                  "用 `agentcore logs` 查 runtime 的日誌；常見原因是 runtime 沒有寫入 research/ 的權限，或 run 中途當掉") from None
        state["research"] = {"status": "running", "checks": checks}
        return state
    if not isinstance(record, dict):
        raise ResearchFailed(f"研究結果 {store.uri(key)} 不是 JSON 物件")
    if record.get("runId") != run_id:
        raise ResearchFailed(f"研究結果 {store.uri(key)} 記載的 runId 是 {notify.clean(record.get('runId'), 100)!r}，不是這次等待的 {run_id}")
    status = record.get("status")
    proposal = record.get("proposal") if isinstance(record.get("proposal"), dict) else {}
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    # the record is the agent's output: only fixed-size, single-line pieces of it enter the state
    research: dict[str, Any] = {"status": _text(status, 40), "checks": checks, "runKey": key, "recordSha256": digest,
                                "finishedAt": _text(record.get("finishedAt"), 40), "stopReason": _text(record.get("stopReason"), 80),
                                "totalTokens": _number(usage.get("totalTokens")), "elapsedSeconds": _number(record.get("elapsedSeconds"))}
    if status == "accepted" and proposal.get("id"):
        research["proposalId"] = _text(proposal["id"], 100)
    elif status == "no_proposal":
        report = record.get("report") if isinstance(record.get("report"), dict) else {}
        errors = report.get("errors") if isinstance(report.get("errors"), list) else []
        detail = record.get("error") or record.get("summary") or record.get("message") or (f"agent 停止的原因：{record.get('stopReason')}" if record.get("stopReason") else "")
        state["notification"] = notify.for_research("no_proposal", run_id=run_id, detail=str(detail), errors=[str(e) for e in errors])
    else:
        raise ResearchFailed(f"run {run_id} 結束時的狀態是 {notify.clean(status, 40)!r}：{notify.clean(record.get('error') or '沒有錯誤訊息', 1500)}")
    state["research"] = research
    return state


def publish(event: Any, context: Any = None) -> dict:
    """Publish the research result (or carry out an approve / discard / rollback action)."""
    state = _state(event)
    inp = state["input"]
    action = inp.get("action") or "publish"
    if action not in ACTIONS:
        raise BadRequest(f"action 只能是 {' | '.join(ACTIONS)}，收到 {notify.clean(action, 40)!r}")
    actor = actor_of(state)
    reason = notify.clean(inp.get("reason") or "", 300)
    data, control = data_store(), control_store()
    if action == "rollback":
        outcome = rollback(data, _text(inp.get("releaseId"), 60) or None, actor=actor, reason=reason, now=clock(),
                           if_current=_text(inp.get("ifCurrent"), 60) or None, request_id=_request_id(state))
    elif action == "pause":
        outcome = pause(data, actor=actor, reason=reason, now=clock())
    elif action == "resume":
        outcome = resume(data, actor=actor, now=clock(), request_id=_request_id(state))
    elif action in ("approve", "discard"):
        proposal_id = inp.get("proposalId")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise BadRequest(f"{action} 需要 proposalId")
        sha = inp.get("sha256")
        if action == "approve" and sha is not None and not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
            raise BadRequest("sha256 必須是 64 個十六進位字元（通知信與 `portalctl show` 都會列出）")
        request = _request_id(state)
        try:
            load_pending(control, proposal_id)
        except ReleaseError as e:
            # Not waiting (any more).  When this very request already ran - the step is retried after its reply was lost -
            # it gets the answer it got then; a request about a proposal somebody else decided is told so; anything else
            # is a wrong request, not a broken pipeline.
            outcome = answered(data, control, proposal_id, action, request)
            if outcome is None:
                raise BadRequest(str(e)) from e
        else:
            if action == "approve":
                outcome = approve(data, control, proposal_id, actor=actor, policy=policy_for(state), now=clock(), expected_sha=sha, request_id=request)
            else:
                outcome = discard(control, proposal_id, actor=actor, reason=reason, now=clock(), store=data, request_id=request)
    else:
        research = state.get("research") if isinstance(state.get("research"), dict) else {}
        key = research.get("runKey") or inp.get("runKey") or ""
        if not isinstance(key, str) or not _RUN_KEY.fullmatch(key):
            raise BadRequest(f"沒有可發布的研究結果（runKey = {notify.clean(key, 120)!r}，格式應為 runs/run-….json）")
        store = research_store()
        try:
            record, digest = _read_record(store, key)
        except NotFound:
            raise BadRequest(f"找不到研究結果 {store.uri(key)}") from None
        except ResearchFailed as e:
            raise BadRequest(str(e)) from e
        seen = research.get("recordSha256")
        if seen and seen != digest:
            raise BadRequest(f"研究結果 {store.uri(key)} 在檢查之後被改寫了（內容和等待步驟讀到的不同）；沒有發布")
        if isinstance(record, dict) and "runId" in record and f"runs/{record.get('runId')}.json" != key:
            raise BadRequest(f"研究結果 {store.uri(key)} 記載的 runId 和檔名不符；沒有發布")
        outcome = publish_proposal(data, record, policy=policy_for(state), control=control, actor=actor, now=clock(), request_id=_request_id(state))
    state["publish"] = outcome.to_state()
    try:
        state["publish"]["autoPublishPaused"] = get_hold(data) is not None
    except Exception:  # the action itself is done; the flag only decides one sentence of the message
        state["publish"]["autoPublishPaused"] = False
    state["notification"] = notify.for_outcome(state["publish"], portal_url=os.environ.get("PORTAL_URL", ""),
                                               commands=_commands(state["publish"]), operations=outcome.operations)
    return state


def report_failure(event: Any, context: Any = None) -> dict:
    """Turn the error a step raised into a message a person can act on.

    Whatever the state holds, this step answers: the report of a failure must not be the next failure (an execution
    that ends red without a message is the one outcome nobody can act on).
    """
    try:
        return _report_failure(event)
    except Exception as e:  # noqa: BLE001 - see above
        err = event.get("error") if isinstance(event, dict) and isinstance(event.get("error"), dict) else {}
        execution = event.get("execution") if isinstance(event, dict) else ""
        return {"input": {}, "execution": notify.clean(execution, 300), "error": {"Error": notify.clean(err.get("Error") or "error", 80)},
                "notification": notify.for_failure(
                    notify.clean(err.get("Error") or "error", 80),
                    f"（這次失敗的細節整理不出來：{type(e).__name__}。execution 的 input 或某一步的輸出裡，有程式處理不了的內容；請看 execution history）",
                    execution=notify.clean(execution, 300), data_untouched=False)}


def _report_failure(event: Any) -> dict:
    try:
        state = _state(event)
    except BadRequest:  # the input itself is what was wrong: report that, with an empty input
        state = {k: _whole(v) for k, v in event.items() if k != "input"} if isinstance(event, dict) else {}
        state["input"] = {}
    err = state.get("error") if isinstance(state.get("error"), dict) else {}
    cause: Any = err.get("Cause") or ""
    try:  # a Lambda error's Cause is a JSON document: {"errorMessage": ..., "errorType": ..., "stackTrace": [...]}
        parsed = json.loads(cause)
        if isinstance(parsed, dict) and parsed.get("errorMessage"):
            cause = parsed["errorMessage"]
    except (TypeError, ValueError):
        pass
    action = state["input"].get("action")
    research = state.get("research") if isinstance(state.get("research"), dict) else {}
    error = str(err.get("Error") or "error")
    if action:
        hint, publishing = f"執行 {notify.clean(action, 40)}", True
    elif research.get("status") == "accepted":
        hint, publishing = "發布", True
    else:
        hint, publishing = ("等待研究結果" if state.get("run") else "啟動研究"), False
    # A request that was refused before anything ran (BadRequest, ConfigError) cannot have changed the data either.
    untouched = not publishing or error in ("BadRequest", "ConfigError")
    state["notification"] = notify.for_failure(error, str(cause), step_hint=hint, execution=str(state.get("execution") or ""),
                                               data_untouched=untouched)
    return state
