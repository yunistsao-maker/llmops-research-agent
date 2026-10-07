"""Start a background research run on the AgentCore runtime.

A background run answers at once with ``{"type": "accepted", "runId": ..., "resultUri": ...}`` and writes its result to
the runtime's OUTPUT_URI when it finishes, so the caller never has to hold a connection open for the whole research.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterator, Mapping

RUN_ID = re.compile(r"run-[A-Za-z0-9-]{1,80}")  # used with fullmatch
_ARN = re.compile(r"arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/[A-Za-z0-9_-]+")
#: Network timeouts of one InvokeAgentRuntime call. Together they must stay well below the Lambda's own timeout
#: (infra/pipeline.yaml), so that a slow runtime is reported as an error instead of killing the function mid-call.
CONNECT_TIMEOUT, READ_TIMEOUT = 10, 75


class AgentError(Exception):
    """The runtime could not be called, or it did not accept the run."""


def region_of(arn: str) -> str:
    m = _ARN.fullmatch(arn or "")
    if not m:
        raise AgentError(f"not an AgentCore runtime ARN (arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>): {arn!r}")
    return m.group(1)


def make_client(arn: str) -> Any:
    import boto3
    from botocore.config import Config

    # no automatic retries: replaying InvokeAgentRuntime would start a second research run (and a second bill)
    return boto3.client("bedrock-agentcore", region_name=region_of(arn),
                        config=Config(read_timeout=READ_TIMEOUT, connect_timeout=CONNECT_TIMEOUT, retries={"max_attempts": 0}))


def iter_events(response: Mapping[str, Any]) -> Iterator[dict]:
    """The agent's events from an InvokeAgentRuntime response, one at a time as they arrive.

    Server-sent events (``data: {...}`` lines) are yielded while the stream is still open, so the caller can stop at the
    event it was waiting for; a plain JSON answer is yielded as one event.
    """
    body = response["response"]
    if "text/event-stream" in str(response.get("contentType") or "") and hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if text.startswith("data:"):
                event = _json_object(text[5:])
                if event is not None:
                    yield event
        return
    raw = body.read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if text.lstrip().startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                event = _json_object(line[5:])
                if event is not None:
                    yield event
        return
    event = _json_object(text)
    if event is not None:
        yield event


def _json_object(text: str) -> dict | None:
    try:
        value = json.loads(text.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def new_session_id() -> str:
    return "llmops-portal-" + uuid.uuid4().hex  # a fresh microVM per run; the API wants at least 33 characters


def start_background_run(client: Any, arn: str, request: Mapping[str, Any], *, qualifier: str | None = None,
                         session_id: str | None = None) -> dict:
    """Ask the runtime to research ``request`` in the background.  Returns {"runId", "resultUri", "sessionId"}."""
    if not isinstance(request, Mapping):
        raise AgentError("the research request must be a JSON object, e.g. {\"kind\": \"weekly\"}")
    if request.get("dryRun"):
        raise AgentError("dryRun 的 run 不會存檔，pipeline 無從發布；要試跑請直接用 scripts/invoke_agent.py")
    payload = {k: v for k, v in request.items() if k != "dryRun"}
    payload["background"] = True
    session_id = session_id or new_session_id()
    params: dict[str, Any] = {
        "agentRuntimeArn": arn, "runtimeSessionId": session_id, "payload": json.dumps(payload).encode("utf-8"),
        "contentType": "application/json", "accept": "text/event-stream",
    }
    if qualifier:
        params["qualifier"] = qualifier
    seen: list[dict] = []
    response: Mapping[str, Any] | None = None
    try:
        response = client.invoke_agent_runtime(**params)
        # Stop at the answer: a background run says "accepted" at once, and nothing after that is needed. Waiting for the
        # runtime to close the stream would tie this call to how long that takes.
        for ev in iter_events(response):
            seen.append(ev)
            if ev.get("type") == "accepted" and RUN_ID.fullmatch(str(ev.get("runId", ""))):
                return {"runId": ev["runId"], "resultUri": ev.get("resultUri"), "sessionId": session_id}
            if ev.get("type") == "result":
                raise AgentError(f"agent 沒有受理這次 run：{str(ev.get('error') or ev.get('status'))[:500]}")
    except AgentError:
        raise
    except Exception as e:  # botocore ClientError, timeouts, connection errors
        code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
        raise AgentError(f"無法呼叫 AgentCore runtime（{code or type(e).__name__}，session {session_id}）：{str(e)[:500]}") from e
    finally:
        close = getattr((response or {}).get("response"), "close", None)
        if callable(close):
            close()
    raise AgentError(f"agent 沒有回覆 accepted 事件（session {session_id}）：" + json.dumps(seen[:1], ensure_ascii=False)[:300])
