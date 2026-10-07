"""AgentCore Runtime entrypoint of the LLMOps research agent.

    POST /invocations   JSON payload -> text/event-stream.  The last event has type "result".
    GET  /ping          health; "HealthyBusy" while a background run is active

Payload (all optional; ``agentcore invoke "weekly"`` sends {"prompt": "weekly"}):

    {"kind": "weekly" | "focus" | "verify" | "prompt",
     "instruction": "free text task",          "focus": ["lifecycle", "litellm", ...],
     "claims": ["..."],                        "urls": ["https://..."],
     "periodStart": "YYYY-MM-DD",              "periodEnd": "YYYY-MM-DD",
     "dryRun": false,                          "background": false,
     "budget": {"searches": 10, "fetches": 15, "toolCalls": 80}}

``background: true`` returns at once and keeps researching; the result is written to OUTPUT_URI.
Every invocation is a fresh, single-shot run: nothing is remembered between requests.
"""

from __future__ import annotations

import asyncio
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from research.agent import new_run_id, parse_payload, run_stream
from research.config import Settings
from research.events import with_display
from research.store import run_location

app = BedrockAgentCoreApp()
log = app.logger

MAX_CONCURRENT_RUNS = max(1, int(os.environ.get("MAX_CONCURRENT_RUNS", "2") or 2))
_active_runs = 0  # touched only from the SDK's single worker event loop
_background_tasks: set[asyncio.Task] = set()  # strong references: asyncio keeps only weak ones


def _error(message: str, run_id: str | None = None) -> dict:
    ev = {"type": "result", "status": "error", "error": message}
    if run_id:
        ev["runId"] = run_id
    return with_display(ev)


async def _run_in_background(payload: dict, settings: Settings, run_id: str, ping_task_id: int) -> None:
    global _active_runs
    try:
        async for ev in run_stream(payload, settings=settings, run_id=run_id):
            if ev.get("type") == "result":
                log.info("background run %s finished: status=%s saved=%s saveError=%s error=%s", run_id, ev.get("status"),
                         ev.get("saved"), ev.get("saveError"), ev.get("error"))
    except asyncio.CancelledError:
        log.warning("background run %s cancelled", run_id)
        raise
    except Exception:
        log.exception("background run %s crashed", run_id)
    finally:
        _active_runs -= 1
        app.complete_async_task(ping_task_id)  # /ping goes back to Healthy


@app.entrypoint
async def invoke(payload, context):
    global _active_runs
    session_id = getattr(context, "session_id", None)
    try:
        task = parse_payload(payload)
    except ValueError as e:
        yield _error(f"invalid request: {e}")
        return
    if _active_runs >= MAX_CONCURRENT_RUNS:
        yield _error(f"busy: this session is already running {_active_runs} research run(s) (MAX_CONCURRENT_RUNS={MAX_CONCURRENT_RUNS}); "
                     "invoke with a new runtimeSessionId to get a separate microVM")
        return

    settings = Settings.from_env()
    log.info("research request kind=%s background=%s dryRun=%s session=%s", task["kind"], task.get("background", False),
             task.get("dryRun", False), session_id)

    if task.get("background"):
        if not settings.output_uri:
            yield _error("background runs write their result to OUTPUT_URI, which is not configured for this deployment")
            return
        run_id = new_run_id()
        where = run_location(settings.output_uri, run_id)
        _active_runs += 1
        ping_task_id = app.add_async_task("research-run", {"runId": run_id})  # keeps the microVM alive: /ping -> HealthyBusy
        job = asyncio.get_running_loop().create_task(_run_in_background(payload, settings, run_id, ping_task_id))
        _background_tasks.add(job)
        job.add_done_callback(_background_tasks.discard)
        yield with_display({"type": "accepted", "runId": run_id, "resultUri": where,
                            "message": f"background run {run_id} started; the full result will be written to {where}"})
        return

    _active_runs += 1
    try:
        async for ev in run_stream(payload, settings=settings):
            yield with_display(ev)
    finally:
        _active_runs -= 1


if __name__ == "__main__":
    app.run()
