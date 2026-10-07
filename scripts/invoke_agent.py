#!/usr/bin/env python3
"""呼叫已部署在 AgentCore Runtime 的 research agent，邊跑邊顯示進度，並把最後的結果存成檔案。

這個 script 只用 boto3 資料平面（bedrock-agentcore → InvokeAgentRuntime），和 `agentcore invoke` 送出的是同一種請求；
差別是可以送完整的 JSON payload、拿到結構化的結果檔（用 scripts/review_proposal.html 檢視，或用 portalctl.py publish 發布）。
平常的更新請用 `portalctl.py run`（研究 → 驗證 → 發布 → 通知一次做完）；這支 script 適合單獨測試 agent。

    # 每週研究（同步：連線要一直開著，約數分鐘到二十幾分鐘）
    uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py --prompt weekly --out proposal.json

    # 只查某一件事
    uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py \\
        --payload-json '{"kind":"verify","claims":["claude-sonnet-4-5 在 Microsoft Foundry 的退役日是 2026-11-30"]}' --out verify.json

    # 背景執行：立刻回傳 runId，結果寫到 OUTPUT_URI（排程用的就是這個模式）
    uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py --prompt weekly --background

runtime ARN 依序取自 --arn、環境變數 AGENT_RUNTIME_ARN、agentcore/.cli/deployed-state.json（`agentcore deploy` 之後才有；
也可以用 `agentcore status` 查）。每次執行都用新的 runtimeSessionId（一個新的 microVM）。

Exit code：0 取得 proposal（或背景 run 已受理）· 3 無法呼叫或連線中斷 · 4 這次沒有產出通過驗證的 proposal · 5 agent 回報錯誤 · 2 參數錯誤
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = PROJECT_ROOT / "agentcore" / ".cli" / "deployed-state.json"

OK, ENV, NO_PROPOSAL, RUN_ERROR = 0, 3, 4, 5
_ARN_FIND = re.compile(r"arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[A-Za-z0-9_-]+")
_ARN_FULL = re.compile(r"^arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/[A-Za-z0-9_-]+\Z")


class ScriptError(Exception):
    """The call could not be made or did not complete (exit code 3)."""


def resolve_arn(explicit: str | None, environ: Mapping[str, str], state_file: Path = STATE_FILE) -> str:
    arn = explicit or environ.get("AGENT_RUNTIME_ARN") or ""
    if not arn and state_file.is_file():
        found = sorted(set(_ARN_FIND.findall(state_file.read_text(encoding="utf-8", errors="replace"))))
        if len(found) > 1:
            raise ScriptError(f"{state_file} 裡有多個 runtime ARN，請用 --arn 指定：\n  " + "\n  ".join(found))
        arn = found[0] if found else ""
    if not arn:
        raise ScriptError("找不到 runtime ARN：用 --arn 或環境變數 AGENT_RUNTIME_ARN 指定（`agentcore status` 可以查到），或先執行 `agentcore deploy`")
    if not _ARN_FULL.match(arn):
        raise ScriptError(f"這不像 AgentCore runtime ARN（arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>）：{arn}")
    return arn


def build_payload(args: argparse.Namespace) -> dict:
    try:
        if args.prompt is not None:
            payload: Any = {"prompt": args.prompt}
        elif args.payload_json is not None:
            payload = json.loads(args.payload_json)
        else:
            payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ScriptError(f"payload 讀不到或不是有效的 JSON：{e}") from e
    if not isinstance(payload, dict):
        raise ScriptError("payload 必須是 JSON 物件，例如 {\"kind\": \"weekly\"}")
    return {**payload, "background": True} if args.background else payload


def make_client(region: str, read_timeout: int) -> Any:
    import boto3
    from botocore.config import Config

    # no automatic retries: replaying InvokeAgentRuntime would start a second research run (and a second bill)
    return boto3.client("bedrock-agentcore", region_name=region,
                        config=Config(read_timeout=read_timeout, connect_timeout=10, retries={"max_attempts": 0}))


def iter_events(response: Mapping[str, Any]) -> Iterator[dict]:
    """The agent's events, as they arrive (SSE: one JSON object per ``data:`` line)."""
    body = response["response"]
    if "text/event-stream" in str(response.get("contentType") or ""):
        for raw in body.iter_lines(chunk_size=1):  # chunk_size=1: hand each line over as soon as it is complete
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                ev = json.loads(data)
            except ValueError:
                ev = {"type": "raw", "text": data}
            yield ev if isinstance(ev, dict) else {"type": "raw", "text": data}
        return
    data = body.read()
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    try:
        ev = json.loads(text)
    except ValueError:
        ev = None
    yield ev if isinstance(ev, dict) else {"type": "raw", "text": text}


def display_text(ev: Mapping[str, Any]) -> str | None:
    """The human-readable line the agent attaches to each event (the same one ``agentcore invoke`` prints)."""
    try:
        text = ev["event"]["contentBlockDelta"]["delta"]["text"]
    except (KeyError, TypeError):
        text = ev.get("text") if ev.get("type") == "raw" else None
    return text if isinstance(text, str) and text else None


def explain(e: Exception) -> str:
    code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
    msg = f"{type(e).__name__}: {e}"
    hints = {
        "AccessDeniedException": "你的 IAM 身分需要 bedrock-agentcore:InvokeAgentRuntime（資源是 runtime ARN 與 <ARN>/runtime-endpoint/*）",
        "ResourceNotFoundException": "找不到這個 runtime；確認 ARN 與 region，且 runtime 狀態是 READY（agentcore status）",
        "ThrottlingException": "被限流了，稍後再試",
        "RuntimeClientError": "runtime 內部的 client 錯誤；用 `agentcore logs` 看 runtime 日誌",
    }
    if code in hints:
        msg += f"\n  → {hints[code]}"
    elif "Read timeout" in msg or "ReadTimeout" in type(e).__name__:
        msg += "\n  → 連線逾時（調高 --timeout）；同步 run 可能還在 runtime 裡執行，長時間任務請加 --background"
    return msg


def run(args: argparse.Namespace, *, client: Any = None, environ: Mapping[str, str] | None = None, out=None) -> int:
    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    arn = resolve_arn(args.arn, environ)
    region = args.region or _ARN_FULL.match(arn).group(1)  # type: ignore[union-attr]
    payload = build_payload(args)
    session_id = args.session_id or f"llmops-research-{uuid.uuid4().hex}"  # the API wants >= 33 characters
    client = client or make_client(region, args.timeout)

    params: dict[str, Any] = {
        "agentRuntimeArn": arn, "runtimeSessionId": session_id, "payload": json.dumps(payload).encode("utf-8"),
        "contentType": "application/json", "accept": "text/event-stream",
    }
    if args.qualifier:
        params["qualifier"] = args.qualifier

    print(f"→ {arn}\n  session {session_id}  payload {json.dumps(payload, ensure_ascii=False)[:200]}", file=sys.stderr, flush=True)
    final: dict | None = None
    try:
        response = client.invoke_agent_runtime(**params)
        for ev in iter_events(response):
            if args.raw:
                print(json.dumps(ev, ensure_ascii=False), file=out, flush=True)
            elif (text := display_text(ev)) is not None:
                out.write(text)
                out.flush()
            if ev.get("type") in ("result", "accepted"):
                final = ev
    except Exception as e:  # botocore ClientError / EndpointConnectionError / ReadTimeoutError / a dropped stream
        raise ScriptError(explain(e)) from e
    if final is None:
        raise ScriptError("連線結束了，但沒有收到最後的 result 事件（連線中斷？）；用 `agentcore logs` 查 runtime 日誌，長時間任務請加 --background")

    saved = {k: v for k, v in final.items() if k != "event"}  # "event" is only the display hint
    if args.out:
        Path(args.out).write_text(json.dumps(saved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\n結果已存到 {args.out}", file=sys.stderr, flush=True)

    if final.get("type") == "accepted":  # a background run keeps the microVM busy: never stop its session
        print(f"\n背景 run {final.get('runId')} 已受理；完成後結果在 {final.get('resultUri')}\n"
              f"完成後發布到資料位置（會先對上線中的資料重新驗證）：python scripts/portalctl.py publish {final.get('resultUri')}\n"
              "（要「研究 → 驗證 → 發布 → 通知」一次做完，用 python scripts/portalctl.py run --wait）", file=sys.stderr, flush=True)
        return OK
    if not args.keep_session:
        try:
            client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id)
        except Exception as e:  # needs bedrock-agentcore:StopRuntimeSession; the idle timeout reclaims the session anyway
            print(f"（沒有主動結束 session：{type(e).__name__}；閒置逾時後會自動回收）", file=sys.stderr, flush=True)
    status = final.get("status")
    if status == "accepted":
        if args.out:
            print(f"查看內容：用瀏覽器打開 scripts/review_proposal.html，選 {args.out}\n"
                  f"發布到資料位置（會先對上線中的資料重新驗證；加 --mode review 則先等待核准）：python scripts/portalctl.py publish {args.out}",
                  file=sys.stderr, flush=True)
        return OK
    return NO_PROPOSAL if status == "no_proposal" else RUN_ERROR


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invoke_agent.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--prompt", help="文字指令，等同 `agentcore invoke \"…\"`；'weekly' = 每週研究，其他文字 = 自由指示")
    what.add_argument("--payload-json", metavar="JSON", help="完整的 JSON payload（kind / focus / claims / urls / budget …）")
    what.add_argument("--payload-file", metavar="FILE", help="從檔案讀 JSON payload")
    p.add_argument("--background", action="store_true", help="背景執行：立刻回傳 runId，結果寫到 runtime 的 OUTPUT_URI")
    p.add_argument("--arn", help="runtime ARN（預設取自 AGENT_RUNTIME_ARN 或 agentcore/.cli/deployed-state.json）")
    p.add_argument("--region", help="region（預設取自 ARN）")
    p.add_argument("--qualifier", help="runtime endpoint 名稱（預設 DEFAULT）")
    p.add_argument("--session-id", help="runtimeSessionId（至少 33 個字元；預設每次產生新的）")
    p.add_argument("--timeout", type=int, default=300, metavar="SECONDS", help="等待 agent 下一個事件的秒數上限（agent 每 15 秒送一次 heartbeat；預設 300）")
    p.add_argument("--out", metavar="FILE", help="把最後的 result（含 proposal）存成 JSON 檔")
    p.add_argument("--raw", action="store_true", help="輸出每個事件的 JSON（一行一個），而不是人看的進度文字")
    p.add_argument("--keep-session", action="store_true", help="結束後不呼叫 StopRuntimeSession")
    return p


def main(argv: list[str] | None = None, *, client: Any = None, environ: Mapping[str, str] | None = None, out=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    if args.session_id is not None and len(args.session_id) < 33:
        print("✘ --session-id 至少要 33 個字元", file=sys.stderr)
        return 2
    try:
        return run(args, client=client, environ=environ, out=out)
    except ScriptError as e:
        print(f"\n✘ {e}", file=sys.stderr, flush=True)
        return ENV


if __name__ == "__main__":
    sys.exit(main())
