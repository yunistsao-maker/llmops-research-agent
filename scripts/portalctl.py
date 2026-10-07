#!/usr/bin/env python3
"""LLMOps portal 資料平面的操作工具：看狀態、建立第一個 release、手動發布、核准、rollback、觸發更新。

資料位置用 --data（或環境變數 PORTAL_DATA_URI）指定：s3://<bucket>/portal/，或本機資料夾（離線測試用）。

    export PORTAL_DATA_URI=s3://<DataBucketName>/portal/
    export PIPELINE_STATE_MACHINE_ARN=arn:aws:states:...:stateMachine:llmops-portal-pipeline-update   # 有部署 pipeline 時

    portalctl.py status                                  # 上線中的版本、是否暫停自動發布、待核准的提案、最近的紀錄
    portalctl.py verify --url <DataBaseUrl> --origin <UI 的網址>   # 驗收：資料完整性，以及瀏覽器實際拿到的內容（token、CORS、快取）
    portalctl.py bootstrap --portal ../llmops-portal     # 第一次：把 portal checkout 裡 UI 會讀的檔案發布成第一個 release
    portalctl.py run --wait                              # 立刻跑一次「研究 → 發布」（等同每週排程做的事）
    portalctl.py run --payload-json '{"kind":"focus","focus":["lifecycle"]}' --wait
    portalctl.py pending / show <proposalId> / approve <proposalId> / discard <proposalId>
    portalctl.py show [<releaseId>]                      # 某個 release（預設是上線中的）實際改了什麼、依據哪些引文
    portalctl.py rollback [--to <releaseId>] --reason "…"   # 回復到前一版，並暫停自動發布
    portalctl.py pause --reason "…" / resume
    portalctl.py push --portal ../llmops-portal --only runs,data/internal   # 發布測試結果與平台清單（只新增／取代，不動研究資料）
    portalctl.py pull --portal ../llmops-portal          # 把上線中的資料合併回 checkout（不會覆蓋妳改過的檔案）
    portalctl.py push --portal ../llmops-portal          # 發布手動修改（和上線中的資料逐檔合併；兩邊都改過的檔案會擋下）
    portalctl.py publish research-result.json            # 把某一次研究的結果送去待核准（--mode auto：通過關卡就直接發布）
    portalctl.py history / executions

動作經由哪裡執行：
  · PORTAL_DATA_URI 與 PIPELINE_STATE_MACHINE_ARN 都來自環境變數時，approve / discard / rollback / pause / resume /
    publish <s3 上的 run> 會透過 state machine 執行（有執行紀錄、會發通知）。
  · 在指令列用 --data 指定資料位置（或資料位置是本機資料夾）時，一律直接操作那個位置，不會碰 state machine；
    要對某個資料位置經由 pipeline 操作，就同時用 --state-machine 指定。--direct 則一律直接操作。
  · run 與 executions 只存在於 state machine：同樣的規則下它們不是「改成直接操作」，而是拒絕執行並說明原因
    （環境變數裡的 state machine 不一定屬於指令列指定的資料位置）。
  · publish <本機檔案> 沒辦法交給 state machine（它只讀 research/runs/ 底下的 run 紀錄）：在會經由 pipeline 的環境裡
    要加 --direct 才會執行，而且那是直接寫入資料位置，不受 stack 的 PublishMode 約束。
  · status / history / pending / show / verify / pull 只讀取；bootstrap / push、以及上面「直接操作」的動作需要資料位置
    的寫入權限。
  每個動作開始前都會印出它走的是哪一條路。--data / --state / --state-machine / --by / --direct 寫在子指令前後都可以。

--by、--reason、--note 會寫進 manifest.json 與 release.json，UI 的使用者讀得到：不要放不該公開的內容。

Exit code：0 成功 · 1 verify 有檢查沒過 · 2 參數錯誤 · 3 環境或輸入問題 · 4 沒有發布（被拒絕、待核准、或 pull 有衝突）· 5 流程執行失敗
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "pipeline") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from portal_pipeline import notify as N  # noqa: E402
from portal_pipeline import publish as P  # noqa: E402
from portal_pipeline import releases as R  # noqa: E402
from portal_pipeline.storage import StorageError, Store, open_store  # noqa: E402

OK, CHECK_FAILED, USAGE, ENV, NOT_PUBLISHED, FAILED = 0, 1, 2, 3, 4, 5
_UNDECODED = re.compile("[\ud800-\udfff]")
MARKER = ".portal-release.json"  # written into a checkout by pull / bootstrap: which release its files came from
GLOBAL_FLAGS = ("data", "state", "state_machine", "by", "direct")
#: Everything printed that came out of a stored document goes through this: one cleaned line, marked as text from
#: elsewhere, so that ``Ctx.say`` lays the line it is on out (see portal_pipeline.notify: ``ext`` / ``layout``).
c = N.ext


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


class Ctx:
    """Where things are, who is asking, and how to talk to AWS (replaced by fakes in tests)."""

    def __init__(self, args: argparse.Namespace, environ: Mapping[str, str], clients: dict[str, Any], out, stdin):
        self.args, self.environ, self.clients, self.out, self.stdin = args, environ, clients, out, stdin
        self.data_uri = args.data or environ.get("PORTAL_DATA_URI") or ""
        # PIPELINE_STATE_URI in the environment describes the deployment PORTAL_DATA_URI points at.  With another data
        # location named on the command line it says nothing: an approval for a test folder must never be looked up -
        # or recorded - in production's waiting list.
        self.state_uri = args.state or ("" if args.data else environ.get("PIPELINE_STATE_URI")) or ""
        self.state_ignored = bool(args.data and not args.state and environ.get("PIPELINE_STATE_URI"))
        # ... and the same for PIPELINE_RESEARCH_URI, where that deployment keeps the results of its research
        self.research_uri = args.research or ("" if args.data else environ.get("PIPELINE_RESEARCH_URI")) or ""
        self.research_ignored = bool(args.data and not args.research and environ.get("PIPELINE_RESEARCH_URI"))
        self.machine = args.state_machine or environ.get("PIPELINE_STATE_MACHINE_ARN") or ""
        self.by = args.by or environ.get("PORTALCTL_BY") or _whoami()
        self.left_out: list[str] = []  # see _collect

    def say(self, msg: str = "") -> None:
        """Print ``msg``.  A line that holds text out of a stored document (``c``) is laid out first: what does not fit
        on it goes on in lines that begin with ``┆``, so that such text - however it is padded - never begins a line
        of the window as if it were one of this tool's own.  The tool's own lines are printed as they are."""
        print(N.layout(msg), file=self.out, flush=True)

    def s3(self) -> Any:
        if "s3" not in self.clients:
            import boto3
            from botocore.config import Config

            self.clients["s3"] = boto3.client("s3", config=Config(max_pool_connections=16, retries={"total_max_attempts": 4, "mode": "standard"}))
        return self.clients["s3"]

    def http(self) -> Callable[..., tuple[int, dict[str, str], bytes]]:
        return self.clients.get("http") or http_get

    def sfn(self) -> Any:
        if "stepfunctions" not in self.clients:
            import boto3

            parts = self.machine.split(":")
            self.clients["stepfunctions"] = boto3.client("stepfunctions", region_name=parts[3] if len(parts) > 3 else None)
        return self.clients["stepfunctions"]

    def _open(self, uri: str) -> Store:
        try:
            return open_store(uri, self.s3() if uri.startswith("s3://") else None)
        except StorageError as e:
            raise ScriptError(c(e, 1200)) from e

    def data(self) -> Store:
        if not self.data_uri:
            raise ScriptError("沒有指定資料位置：用 --data s3://<bucket>/portal/（或資料夾），或設定環境變數 PORTAL_DATA_URI")
        return self._open(self.data_uri)

    def control(self) -> Store:
        """Where proposals wait for approval: ``pipeline/`` next to ``portal/``, unless --state says otherwise."""
        uri = self.state_uri
        if not uri:
            base = self.data_uri.rstrip("/")
            if not base:
                raise ScriptError("沒有指定資料位置（--data），無法推得 pipeline 狀態的位置")
            head, _, last = base.rpartition("/")
            if last != "portal" or not head or head.endswith(":/"):
                raise ScriptError(f"無法從資料位置 {self.data_uri} 推得 pipeline 狀態的位置（只有 …/portal/ 旁邊的 …/pipeline/ 才會自動採用）："
                                  "請用 --state <位置> 或環境變數 PIPELINE_STATE_URI 指定")
            uri = head + "/pipeline/"
        return self._open(uri)

    def via_pipeline(self) -> bool:
        """Does an action go through the state machine (True) or straight to the data location (False)?

        A data location named on the command line, and any local folder, is acted on directly: a state machine from
        the environment belongs to some deployment, and nothing says it is the one for that location.
        """
        a = self.args
        if a.direct or not self.machine:
            return False
        if self.data_uri and not self.data_uri.startswith("s3://"):
            if a.state_machine and a.data:
                raise ScriptError("--state-machine 不能和本機資料夾一起用：state machine 只會操作它部署時設定的 S3 資料位置")
            return False
        return not (a.data and not a.state_machine)

    def pipeline_only(self, what: str) -> None:
        """For commands that exist only as an execution of the state machine: the same rule as ``via_pipeline``, but
        where an action would fall back to the data location, these have nothing to fall back to."""
        if self.args.direct:
            raise ScriptError(f"{what} 只能透過 state machine 執行，不能和 --direct 一起用")
        if not self.machine:
            raise ScriptError("沒有指定 state machine：用 --state-machine <ARN> 或環境變數 PIPELINE_STATE_MACHINE_ARN（pipeline stack 的 StateMachineArn）")
        if not self.via_pipeline():
            raise ScriptError(
                f"{what} 是由 state machine 執行的，它操作的是它部署時設定的資料位置，不是這裡指定的 {self.data_uri}。"
                f"環境變數裡的 state machine（{self.machine.rsplit(':', 1)[-1]}）不一定屬於這個資料位置："
                "確定就是它，請明確加上 --state-machine <ARN>；否則拿掉 --data，讓資料位置與 state machine 都來自環境變數。")
        self.say(f"→ 透過 state machine 執行（{self.machine.rsplit(':', 1)[-1]}）")

    def state_location(self) -> str:
        """Where the waiting list is, for the line that says what a command is about to touch ("" when unknown)."""
        try:
            return self.control().uri()
        except ScriptError:
            return ""

    def route(self) -> bool:
        """``via_pipeline``, said out loud - so nobody has to guess what a command is about to touch."""
        through = self.via_pipeline()
        if through:
            self.say(f"→ 透過 state machine 執行（{self.machine.rsplit(':', 1)[-1]}）")
            return through
        state = self.state_location()
        waiting = f"；待核准清單：{state}" if state else ""
        if self.machine and not self.args.direct:
            self.say(f"→ 直接操作 {self.data_uri}{waiting}（資料位置是在指令列或以本機資料夾指定的，所以沒有經過 state machine；"
                     "要經由 pipeline，請同時加上 --state-machine <ARN>）")
        else:
            self.say(f"→ 直接操作 {self.data_uri or '（未指定資料位置）'}{waiting}")
        if self.state_ignored:
            self.say("  （環境變數 PIPELINE_STATE_URI 屬於環境變數裡的資料位置，沒有用在指令列指定的這個位置上；要指定請用 --state）")
        return through


def http_get(url: str, headers: Mapping[str, str] | None = None, *, opener: Any = None, timeout: float = 20) -> tuple[int, dict[str, str], bytes]:
    """One GET, the way a page would send it: (status, lower-cased headers, body). An unreachable host raises OSError."""
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as e:  # an answer, just not a 2xx one
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def _whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no login name in some containers
        return "operator"


def _taipei(iso_utc: Any) -> str:
    try:
        t = dt.datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        return c(iso_utc, 40)
    return t.strftime("%Y-%m-%d %H:%M") + "（台北）"


def _kind(kind: Any) -> str:
    return {"agent": "agent 研究", "manual": "手動發布", "bootstrap": "第一個 release", "rollback": "rollback"}.get(str(kind), c(kind, 20))


def _print_changes(ctx: Ctx, changes: Any) -> None:
    changes = changes if isinstance(changes, dict) else {}
    for key, sign in (("added", "+"), ("modified", "~"), ("removed", "-")):
        for rel in changes.get(key) or []:
            ctx.say(f"  {sign} {c(rel, 300)}")


def _hold_line(hold: dict | None) -> str:
    if not hold:
        return "正常"
    return f"⏸ 已暫停（{hold['by']}，{_taipei(hold['at'])}：{hold['reason'] or '未註明原因'}）"


def _report(ctx: Ctx, outcome: P.Outcome) -> int:
    status, code = outcome.status, OK
    pid = c(outcome.proposal.get("id"), 100)
    if status == "published":
        ctx.say(f"✔ 已發布 {outcome.release_id}（前一版 {outcome.previous_release_id or '無'}）；UI 下次載入就會讀到")
        _print_changes(ctx, outcome.changes)
    elif status == "rolled_back":
        ctx.say(f"✔ 已回復到 {outcome.release_id}（原為 {outcome.previous_release_id}）")
        _print_changes(ctx, outcome.changes)
        ctx.say("⏸ 自動發布已暫停（下一次研究讀到的是回復後的資料，很可能再提出同樣的變更）；確認後用 `portalctl.py resume` 恢復。")
    elif status in ("unchanged", "already_published"):
        ctx.say(f"沒有變更（{status}）" + (f"；上線中的版本是 {outcome.release_id}" if outcome.release_id else ""))
        for r in outcome.reasons:
            ctx.say(f"  - {c(r, 600)}")
    elif status == "pending":
        ctx.say(f"⏸ 提案 {pid} 需要核准才會發布：")
        for r in outcome.reasons:
            ctx.say(f"  - {c(r, 600)}")
        if outcome.review is not None:
            ctx.say(N.review_block(outcome.review.to_doc(limit=P.STATE_CHANGES, width=P.STATE_VALUE), proposal_id=pid))
        ctx.say(f"  檢視：portalctl.py show {pid}　核准：portalctl.py approve {pid}　捨棄：portalctl.py discard {pid}")
        code = NOT_PUBLISHED
    elif status == "preview":
        # shown before the confirmation of a real run as well: only a dry run can say that nothing was written
        ctx.say("（預覽）會發布以下變更；沒有寫入任何東西：" if getattr(ctx.args, "dry_run", False) else "會發布以下變更：")
        _print_changes(ctx, outcome.changes)
    elif status in ("paused", "resumed", "discarded"):
        ctx.say({"paused": "⏸ 自動發布已暫停：之後的提案都會等待核准", "resumed": "▶ 已恢復自動發布", "discarded": "已捨棄提案"}[status])
        for r in outcome.reasons:
            ctx.say(f"  - {c(r, 600)}")
    else:
        ctx.say("✘ 沒有發布，資料維持原狀：")
        for r in outcome.reasons:
            ctx.say(f"  - {c(r, 800)}")
        code = NOT_PUBLISHED
    for w in outcome.warnings:
        ctx.say(f"  注意：{c(w, 600)}")
    return code


def _report_checkout(ctx: Ctx, outcome: P.Outcome) -> int:
    """_report, plus what to do when a checkout was refused because of its content."""
    code = _report(ctx, outcome)
    if outcome.status == "rejected" and any(r.startswith("資料驗證：") for r in outcome.reasons):
        ctx.say("  以上是這次發布會帶進來的資料問題（已經在線上的問題不算）。修正檔案後再試一次；")
        ctx.say("  確定仍要發布（讀到這些檔案的頁面可能無法顯示）才加 --allow-invalid。")
    return code


# ---------------------------------------------------------------------------------------------- reading
def _pending(ctx: Ctx) -> list[dict] | None:
    """The proposals that wait for approval - None when this invocation cannot tell where they are kept."""
    try:
        return P.list_pending(ctx.control())
    except ScriptError:
        return None


def cmd_status(ctx: Ctx) -> int:
    store = ctx.data()
    try:
        manifest, _ = R.read_pointer(store)
    except R.NotBootstrapped:
        ctx.say(f"{store.uri()} 還沒有任何 release。先執行：portalctl.py bootstrap --portal <portal repo>")
        return OK
    src = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    ctx.say(f"資料位置　{store.uri()}")
    ctx.say(f"上線版本　{manifest['releaseId']}（第 {c(manifest.get('sequence'), 9)} 次發布，{_taipei(manifest.get('publishedAt', ''))}）")
    ctx.say(f"來源　　　{_kind(src.get('kind', ''))}，by {c(src.get('by') or '—', 80)}" + (f"，提案 {c(src['proposalId'], 100)}" if src.get("proposalId") else ""))
    digests = manifest.get("digests") if isinstance(manifest.get("digests"), list) else []
    ctx.say(f"內容　　　{len(manifest.get('models') or [])} 個 deep dive、{len(digests)} 份 digest（最新 {c((digests or ['—'])[-1], 40)}）、"
            f"{len(manifest.get('compatRuns') or []) + len(manifest.get('perfRuns') or [])} 份測試結果、平台清單 {'有' if manifest.get('hasInventory') else '無（顯示 example）'}")
    ctx.say("自動發布　" + _hold_line(R.hold_of(manifest)))
    pending = _pending(ctx)
    if pending is None:
        ctx.say("待核准　　（沒有指定 pipeline 狀態的位置：--state）")
    else:
        ctx.say(f"待核准　　{len(pending)} 份" + ("" if not pending else "：" + "、".join(c(p.get("proposalId"), 100) for p in pending)))
    ctx.say("最近紀錄")
    for h in [h for h in manifest.get("history") or [] if isinstance(h, dict)][:5]:
        _history_entry(ctx, h, " ")
    return OK


def _history_entry(ctx: Ctx, h: dict, mark: str) -> None:
    """One release of the history: a line of this tool's own (which, when, how, by whom) and, under it, the summary -
    which is somebody's text and gets a line to itself."""
    ctx.say(f"{mark} {c(h.get('releaseId'), 40)}  {_taipei(h.get('publishedAt', ''))}  {_kind(h.get('kind', ''))}  by {c(h.get('by') or '—', 20)}  "
            f"{c(h.get('changed', 0), 6)} 個檔案" + (f"  {c(h['proposalId'], 100)}" if h.get("proposalId") else ""))
    if h.get("summary"):
        ctx.say(f"      {c(h['summary'], 200)}")


def cmd_history(ctx: Ctx) -> int:
    manifest, _ = R.read_pointer(ctx.data())
    history = [h for h in manifest.get("history") or [] if isinstance(h, dict)]
    for i, h in enumerate(history[: ctx.args.limit]):
        _history_entry(ctx, h, "→" if i == 0 and h.get("releaseId") == manifest["releaseId"] else " ")
    return OK


def cmd_pending(ctx: Ctx) -> int:
    pending = P.list_pending(ctx.control())
    if not pending:
        ctx.say("沒有待核准的提案。")
        return OK
    for p in pending:
        info = p.get("proposal") if isinstance(p.get("proposal"), dict) else {}
        pid = c(p.get("proposalId"), 100)
        ctx.say(f"{pid}　暫存於 {_taipei(p.get('heldAt', ''))}（當時上線的是 {c(p.get('baseReleaseId'), 40)}）")
        ctx.say(f"  摘要：{c(info.get('summary', ''), 300)}")
        ctx.say(f"  內容：{c(info.get('ops', 0), 6)} 個 operation；檔案 {', '.join(c(f, 120) for f in p.get('changedFiles') or [])}")
        for r in p.get("reasons") or []:
            ctx.say(f"  需要核准的原因：{c(r, 600)}")
        refusal = p.get("lastRefusal")
        if isinstance(refusal, dict):
            ctx.say(f"  上次核准被拒絕（{c(refusal.get('by'), 80)}，{_taipei(refusal.get('at', ''))}）：" + "；".join(c(r, 300) for r in (refusal.get("reasons") or [])[:3]))
        ctx.say(f"  檢視：portalctl.py show {pid}")
    return OK


def _save(ctx: Ctx, doc: Any, *, proposal: bool) -> None:
    if ctx.args.out:
        Path(ctx.args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ctx.say(f"已存成 {ctx.args.out}" + ("（用瀏覽器打開 scripts/review_proposal.html，再選這個檔案，可以逐項對照引文）" if proposal else ""))


def _difference_now(ctx: Ctx, proposal: dict, doc: dict) -> tuple[dict | None, list[str]]:
    """(what the proposal would change in the data that is live NOW - every value in full, what to say about it).

    The difference kept with a waiting proposal was computed when it was held and is stored cut to a fixed size; what a
    person approves is checked against the live data at that moment.  So this is what they are shown: the proposal
    replayed on the live release, nothing cut.  None when that cannot be done from here (the stored one is shown then).
    """
    stored = "以下是提案暫存時記錄的差異（超過 4,000 字元的值已截斷；完整的值在 --out 存下的檔案的 proposal.ops[].value）"
    try:
        current = R.load_current(ctx.data())
        portal = P.portal_view(current.files, current.release_id)
        if P.already_applied(portal, proposal):
            return None, [f"這份提案的內容已經在上線的資料裡（release {current.release_id}）；核准不會再改變什麼。", stored]
        staged, problems = P.apply_saved_proposal(portal, proposal, [])
    except Exception as e:  # no read access to the data location, a damaged release ...
        return None, [f"（無法在上線中的資料上重算差異：{type(e).__name__}：{c(e, 200)}）", stored]
    notes = []
    base = doc.get("baseReleaseId")
    if current.release_id != base:
        notes.append(f"注意：目前上線的是 {current.release_id}，不是提案暫存時的 {c(base, 40)}。以下的差異是對目前上線的資料重算的。")
    if problems:
        notes.append("✘ 這份提案已經無法套用到目前上線的資料，核准會被拒絕（請捨棄後重跑研究）：")
        notes += [f"    - {c(problem, 500)}" for problem in problems[:8]]
        notes.append(stored)
        return None, notes
    return P.build_review(portal, staged, proposal, current.release_id).to_doc(), notes


def cmd_show(ctx: Ctx) -> int:
    """What a pending proposal would really change, or what a release changed - with the quotes each change rests on."""
    target = ctx.args.id
    if target and target.startswith("prop-"):
        try:
            doc = P.load_pending(ctx.control(), target)
        except R.ReleaseError as e:
            raise ScriptError(c(e, 1200)) from e
        proposal, _run = P.split_record(doc["record"])
        proposal = proposal or {}
        info = doc.get("proposal") if isinstance(doc.get("proposal"), dict) else {}
        sha = doc.get("proposalSha256") or P.proposal_sha256(proposal)
        ctx.say(f"提案 {target}　待核准（暫存於 {_taipei(doc.get('heldAt', ''))}；當時上線的是 {c(doc.get('baseReleaseId'), 40)}）")
        ctx.say(f"sha256 {c(sha, 80)}")
        for r in doc.get("reasons") or []:
            ctx.say(f"  需要核准的原因：{c(r, 600)}")
        ctx.say(f"摘要：{c(info.get('summary', ''), 1200)}")
        ctx.say()
        review, notes = _difference_now(ctx, proposal, doc)
        for line in notes:
            ctx.say(line)
        ctx.say(N.review_block(review if review is not None else doc.get("review"), limit=None, width=None) or "（沒有記錄逐值的差異）")
        digest = proposal.get("digest")
        if isinstance(digest, dict):
            ctx.say()
            ctx.say("新的 digest 全文：")
            ctx.say(N.clean_block(json.dumps(digest, ensure_ascii=False, indent=2), gutter="    │ "))
        ctx.say()
        ctx.say(N.operations_block(P.operations(proposal), limit=None, width=2000) or "（提案沒有逐項的 operation）")
        unverified = [u for u in proposal.get("unverified") or [] if isinstance(u, (dict, str))] if isinstance(proposal.get("unverified"), list) else []
        if unverified:
            ctx.say(f"未能確認的項目（{len(unverified)}）：")
            for u in unverified:
                ctx.say("  - " + (c(u, 600) if isinstance(u, str) else f"{c(u.get('claim'), 500)}" + (f"（{c(u.get('reason'), 300)}）" if u.get("reason") else "")))
        ctx.say()
        ctx.say(f"核准這份內容：portalctl.py approve {c(target, 120)} --sha256 {c(sha, 80)}")
        ctx.say(f"捨棄：portalctl.py discard {c(target, 120)}")
        _save(ctx, doc["record"], proposal=True)
        return OK
    store = ctx.data()
    release_id = target or R.read_pointer(store)[0]["releaseId"]
    meta, _files = R.read_release(store, release_id, verify=False)
    src = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    ctx.say(f"release {release_id}　{_kind(src.get('kind', ''))}，by {c(src.get('by') or '—', 80)}"
            + (f"，{c(src['approvedBy'], 80)} 核准" if src.get("approvedBy") else "") + f"，{_taipei(meta.get('createdAt', ''))}")
    ctx.say(f"前一版 {c(meta.get('parent') or '無', 40)}")
    info = meta.get("proposal") if isinstance(meta.get("proposal"), dict) else {}
    if info:
        ctx.say(f"提案 {c(info.get('id'), 100)}（run {c(info.get('runId') or '—', 100)}）：{c(info.get('summary', ''), 1200)}")
    if src.get("note"):
        ctx.say(f"說明：{c(src['note'], 300)}")
    _print_changes(ctx, meta.get("changes"))
    for block in (N.review_block(meta.get("review"), limit=None, width=None), N.operations_block(meta.get("operations"), limit=None, width=2000)):
        if block:
            ctx.say()
            ctx.say(block)
    if isinstance(meta.get("unverified"), list) and meta["unverified"]:
        ctx.say(f"未能確認的項目：{len(meta['unverified'])} 項")
    _save(ctx, meta, proposal=False)
    return OK


# ---------------------------------------------------------------------------------------------- verify
class Checks:
    """A list of findings: ✔ passed, ✘ failed (counts towards the exit code), · worth knowing."""

    def __init__(self, ctx: Ctx):
        self.ctx, self.failed, self.passed = ctx, 0, 0

    def ok(self, msg: str) -> None:
        self.passed += 1
        self.ctx.say(f"  ✔ {msg}")

    def bad(self, msg: str, *details: str) -> None:
        self.failed += 1
        self.ctx.say(f"  ✘ {msg}")
        for d in details:
            self.ctx.say(f"      {d}")

    def info(self, msg: str) -> None:
        self.ctx.say(f"  · {msg}")


_RELEASE_PATH = re.compile(r"releases/[A-Za-z0-9][A-Za-z0-9._-]{0,99}/")


def _verify_store(ctx: Ctx, checks: Checks) -> str | None:
    """Integrity and validity of what is live. Returns the live release id (None when there is none)."""
    store = ctx.data()
    ctx.say(f"資料位置 {store.uri()}")
    try:
        manifest, _ = R.read_pointer(store)
    except R.NotBootstrapped:
        checks.bad("還沒有任何 release：先執行 portalctl.py bootstrap --portal <portal repo>")
        return None
    except R.ReleaseError as e:
        checks.bad(f"manifest.json 讀不懂：{c(e, 400)}")
        return None
    release_id = manifest["releaseId"]
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    checks.ok(f"上線版本 {release_id}（第 {c(manifest.get('sequence'), 9)} 次發布，{_taipei(manifest.get('publishedAt', ''))}，{_kind(source.get('kind', ''))}）")
    pinned = manifest.get("releaseSha256") if isinstance(manifest.get("releaseSha256"), str) else None
    try:
        _meta, files = R.read_release(store, release_id, expect_sha=pinned)
    except R.ReleaseError as e:
        checks.bad(f"上線中的 release 不完整或被改過：{c(e, 400)}", "用 portalctl.py rollback --to <完好的 release> 回復，再找出是誰改了 bucket 裡的檔案")
        return release_id
    if not files:
        checks.bad("上線中的 release 沒有列出任何檔案（release.json 的 files 是空的）")
        return release_id
    checks.ok(f"每個檔案都和發布時的內容一致（{len(files)} 個檔案" + ("；release.json 本身也和 manifest.json 記錄的一致）" if pinned else "）"))
    if not pinned:
        checks.info("這個 release 是舊版 pipeline 發布的：manifest.json 沒有記錄 release.json 的雜湊，下一次發布後就會有")
    index = R.ui_index(files)
    drift = [k for k, v in index.items() if manifest.get(k) != v]
    if drift:
        checks.bad(f"manifest.json 的索引和 release 的內容不一致：{', '.join(drift)}")
    else:
        checks.ok("manifest.json 的索引和 release 的內容一致")
    try:
        problems = P.data_problems(files, release_id)
    except R.ReleaseError as e:
        problems = None
        checks.bad(f"資料無法驗證：{c(e, 400)}")
    errors, advice = (P.blocking(problems), P.advisory(problems)) if problems else ([], [])
    if errors:
        checks.bad(f"資料有 {len(errors)} 個 schema / 交叉檢查問題（讀到這些檔案的頁面可能無法顯示）：", *[c(x, 400) for x in errors[:8]])
    elif problems is not None:
        checks.ok("資料通過 schema 與交叉檢查")
    if advice:
        checks.info(f"資料有 {len(advice)} 處提醒（資料可以用，但這些值不會照字面運作）：")
        for x in advice[:8]:
            ctx.say(f"      {c(x, 500)}")
    hold = R.hold_of(manifest)
    checks.info("自動發布：" + (_hold_line(hold) + "；提案會等待核准，resume 後恢復" if hold else "正常"))
    pending = _pending(ctx)
    if pending:
        checks.info(f"待核准的提案：{len(pending)} 份（portalctl.py pending）")
    return release_id


def _verify_url(ctx: Ctx, checks: Checks, url: str, origin: str | None, live_release: str | None) -> None:
    """What a browser gets from the data location. The URL can contain the access token, so it is never printed in full."""
    if re.search(r"[\s\x00-\x1f\x7f]", url):
        checks.bad("--url 裡有空白或控制字元（貼上時多帶了東西？）；請只貼 data plane stack 的 DataBaseUrl")
        return
    base = url if url.endswith("/") else url + "/"
    try:
        parts = urlsplit(base)
        _port = parts.port  # raises for a port that is not a number
        valid = parts.scheme in ("http", "https") and bool(parts.netloc)
    except ValueError:
        valid = False
    if not valid:
        checks.bad("--url 必須是 http(s)://…/ 形式的資料位置網址（data plane stack 的 DataBaseUrl）")
        return
    has_prefix = any(seg for seg in parts.path.split("/"))
    shown = f"{parts.scheme}://{parts.netloc}/" + ("…/" if has_prefix else "")
    fetch = ctx.http()
    headers = {"Origin": origin} if origin else {}

    def get(target: str) -> tuple[int, dict[str, str], bytes]:
        try:
            return fetch(target, headers)
        except OSError:
            raise
        except Exception as e:  # e.g. http.client.InvalidURL - whose message would repeat the URL, token and all
            raise OSError(f"無法送出請求（{type(e).__name__}）") from None

    ctx.say(f"瀏覽器讀到的內容 {shown}")
    try:
        status, got, body = get(base + "manifest.json")
    except OSError as e:
        checks.bad(f"連不上 {shown}manifest.json：{c(getattr(e, 'reason', e), 200)}")
        return
    if status != 200:
        hint = "token 不對、來源 IP 被 WAF 擋下，或 distribution 還沒部署完成" if status in (401, 403) else "網址的路徑不對，或資料位置還沒有 release"
        checks.bad(f"{shown}manifest.json 回應 HTTP {status}（{hint}）")
        return
    try:
        manifest = json.loads(body)
        base_path = manifest["basePath"]
        release_id = manifest["releaseId"]
        if manifest.get("schema") != R.MANIFEST_SCHEMA or not isinstance(base_path, str) or not _RELEASE_PATH.fullmatch(base_path):
            raise ValueError("not a release pointer")
        R.check_release_id(release_id)
    except (ValueError, KeyError, TypeError, R.ReleaseError):
        checks.bad(f"{shown}manifest.json 不是資料版本指標（拿到的可能是網站的 fallback 頁面：網址對嗎？）")
        return
    checks.ok(f"manifest.json 讀得到，指向 release {release_id}")
    if live_release:
        if release_id == live_release:
            checks.ok("和資料位置上線中的版本一致")
        else:
            checks.bad(f"URL 回應的版本 {release_id} 不是資料位置上線中的版本 {live_release}",
                       "網址指到別的 bucket / prefix，或是 CDN 還在回應舊的 manifest.json")
    cache = got.get("cache-control", "")
    if "no-cache" in cache or "no-store" in cache or "max-age=0" in cache:
        checks.ok(f"manifest.json 不會被快取（Cache-Control: {c(cache, 80)}）")
    else:
        checks.bad(f"manifest.json 的 Cache-Control 是「{c(cache, 80)}」：新的 release 會延遲出現，應該是 no-cache")

    if origin:
        allowed = got.get("access-control-allow-origin")
        if allowed == "*":
            checks.ok(f"允許 {origin} 讀取（Access-Control-Allow-Origin: *，任何網站的頁面都能讀；靠 token / WAF 限制存取）")
        elif allowed == origin:
            checks.ok(f"允許 {origin} 讀取（CORS）")
        else:
            checks.bad(f"從 {origin} 開啟的頁面會被瀏覽器擋下：回應沒有允許這個來源的 Access-Control-Allow-Origin",
                       "把這個網址加進 data plane stack 的 AllowedOrigins 參數（不含結尾的 /），重新部署 stack")
    else:
        checks.info("加上 --origin <UI 的網址> 可以檢查 CORS（UI 與資料位置不同網域時才需要）")

    status = 0
    try:
        status, got, body = get(base + base_path + R.RELEASE_FILE)
        listed = json.loads(body)["files"] if status == 200 else None
        if not isinstance(listed, dict):
            raise ValueError("no file list")
    except (OSError, ValueError, KeyError, TypeError):
        checks.bad(f"讀不到 {base_path}{R.RELEASE_FILE}（HTTP {status or '—'}）：release 不完整，或 CDN 沒有把請求送到 portal/ 底下")
        return
    pinned = manifest.get("releaseSha256")
    if isinstance(pinned, str) and R.sha256_hex(body) != pinned:
        checks.bad(f"{base_path}{R.RELEASE_FILE} 不是 manifest.json 記錄的那一份（雜湊不符）：release 被改過")
        return
    if not listed:
        checks.bad(f"{base_path}{R.RELEASE_FILE} 沒有列出任何檔案")
        return
    cache = got.get("cache-control", "")
    if "immutable" in cache or "max-age=31536000" in cache:
        checks.ok("release 的檔案可以長期快取（immutable）")
    else:
        checks.bad(f"release 檔案的 Cache-Control 是「{c(cache, 80)}」：每次開頁面都會重新下載，應該是 {R.IMMUTABLE}")
    wrong: list[str] = []
    for rel, info in sorted(listed.items()):
        shown_rel = c(rel, 200)
        if not R.allowed(rel):
            wrong.append(f"{shown_rel}: 不是 release 該有的檔案名稱")
            continue
        try:
            status, got, body = get(base + base_path + rel)
        except OSError as e:
            wrong.append(f"{shown_rel}: {c(getattr(e, 'reason', e), 200)}")
            continue
        if status != 200:
            wrong.append(f"{shown_rel}: HTTP {status}")
        elif R.sha256_hex(body) != (info.get("sha256") if isinstance(info, dict) else None):
            wrong.append(f"{shown_rel}: 內容和發布時不同")
        elif not got.get("content-type", "").startswith(R.content_type(rel).split(";")[0]):
            wrong.append(f"{shown_rel}: Content-Type 是「{c(got.get('content-type'), 80)}」")
    if wrong:
        checks.bad(f"{len(wrong)} 個檔案有問題：", *wrong[:8])
    else:
        checks.ok(f"每個檔案都和 release.json 記錄的內容一致（{len(listed)} 個檔案，經由這個網址下載）")

    if not has_prefix:
        checks.info("這個資料位置沒有 token：知道網址的人都能讀（DataAccessToken=none）")
        return
    try:
        status, _got, body = get(f"{parts.scheme}://{parts.netloc}/manifest.json")
    except OSError:
        status, body = 0, b""
    open_without = False
    if status == 200:
        try:
            open_without = json.loads(body).get("schema") == R.MANIFEST_SCHEMA
        except (ValueError, AttributeError):
            open_without = False
    if open_without:
        checks.bad("不帶 token 的網址也讀得到資料：token 沒有生效（CloudFront Function 有關聯到 distribution 嗎？）")
    elif status in (401, 403):
        checks.ok(f"沒有 token 的路徑被拒絕（HTTP {status}）")
    else:
        checks.info(f"網址去掉路徑後回應 HTTP {status or '—'}，不是資料（例如經由 UI 網站轉送的路徑）")


def cmd_verify(ctx: Ctx) -> int:
    if not ctx.data_uri and not ctx.args.url:
        raise ScriptError("沒有可以檢查的對象：用 --data（或 PORTAL_DATA_URI）指定資料位置，和／或用 --url 指定 UI 讀資料的網址")
    checks = Checks(ctx)
    live_release = None
    if ctx.data_uri:
        live_release = _verify_store(ctx, checks)
    else:
        ctx.say("  · 沒有指定資料位置（--data / PORTAL_DATA_URI）：只檢查網址，不需要 AWS 權限")
    if ctx.args.url:
        if ctx.data_uri and live_release is None:
            ctx.say("  · 資料位置沒有可用的 release，略過網址檢查")
        else:
            _verify_url(ctx, checks, ctx.args.url, ctx.args.origin, live_release)
    else:
        checks.info("加上 --url <DataBaseUrl> 可以再檢查瀏覽器實際拿到的內容（token、CORS、快取、檔案內容）")
    ctx.say()
    if checks.failed:
        ctx.say(f"✘ 有 {checks.failed} 項檢查沒有通過。")
        return CHECK_FAILED
    if not checks.passed:
        ctx.say("✘ 沒有任何一項檢查實際執行。")
        return CHECK_FAILED
    ctx.say(f"✔ {checks.passed} 項檢查全部通過。")
    return OK


# ---------------------------------------------------------------------------------------------- checkouts
def _marker(portal: Path) -> dict:
    try:
        doc = json.loads((portal / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_marker(portal: Path, release_id: str, uri: str) -> None:
    (portal / MARKER).write_text(json.dumps({"releaseId": release_id, "source": uri, "at": R.iso(R.utc_now())}, indent=2) + "\n", encoding="utf-8")


def _checkout(ctx: Ctx, must_exist: bool = True) -> Path:
    portal = Path(ctx.args.portal).resolve()
    if must_exist and not ((portal / "schema" / "llmops.schema.json").is_file() and (portal / "data").is_dir()):
        raise ScriptError(f"{portal} 不像 portal repo：找不到 schema/llmops.schema.json 或 data/（用 --portal 指到 repo 根目錄）")
    return portal


def _collect(ctx: Ctx, portal: Path, roots=R.ROOTS, *, quiet: bool = False) -> dict[str, bytes]:
    """The files of the checkout that a release takes.  What was left out is remembered in ``ctx.left_out`` (paths; a
    folder ends with "/"): those were not read, which is not the same as "the checkout deleted them"."""
    include = [p.strip() for p in (getattr(ctx.args, "include", None) or "").split(",") if p.strip()]
    try:
        files, skipped = R.collect_tree(portal, roots, include=include)
    except R.ReleaseError as e:
        raise ScriptError(c(e, 1200)) from e
    ctx.left_out = [entry.path for entry in skipped]  # (the path as it is: a folder may be called "perf (copy)")
    if not quiet:
        reasons = {"symlink": "symlink，不會跟隨", "symlinked folder": "symlink 資料夾，不會跟隨",
                   "not a publishable file name or type": "檔名或類型不能發布", "not a file the UI reads": "不是 UI 會讀的檔案"}
        for s in skipped:
            ctx.say(f"  （略過 {c(s.path, 300)}：{reasons.get(s.why, c(s.why, 80))}）")  # a file name is text from elsewhere too
        if any(s.why == "not a file the UI reads" for s in skipped):
            ctx.say("  （只會發布 UI 會讀的檔案；確定要多發布某個檔案，用 --include <路徑或樣式>，例如 --include config/notes.md）")
    return files


def _confirm(ctx: Ctx, question: str) -> bool:
    if ctx.args.yes:
        return True
    if ctx.stdin is None or not getattr(ctx.stdin, "isatty", lambda: False)():
        raise ScriptError("這個動作需要確認：加上 --yes（或先用 --dry-run 預覽）")
    ctx.say(question + " [y/N] ")
    return ctx.stdin.readline().strip().lower() in ("y", "yes")


def _only(ctx: Ctx) -> list[str] | None:
    raw = ctx.args.only
    if raw is None:
        return None
    only = [p.strip().strip("/") for p in raw.split(",")]
    if not only or any(not p for p in only):
        raise ScriptError("--only 需要至少一個資料夾（逗號分隔，例如 runs,data/internal）；空白的 --only 不代表「全部」")
    unknown = sorted({p.split("/")[0] for p in only} - set(R.ROOTS))
    if unknown:
        raise ScriptError(f"--only 只能是 {', '.join(R.ROOTS)} 底下的路徑，收到 {unknown}")
    return only


def _listing(ctx: Ctx, files: dict[str, bytes]) -> None:
    """What is about to become readable by everyone who can open the UI, grouped by who owns it."""
    groups = {"研究資料（agent 維護）": [], "內部清單與測試結果（平台團隊與工具）": [], "其他（schema、設定範例）": []}
    for rel in sorted(files):
        if rel.startswith(("runs/", "data/internal/")):
            groups["內部清單與測試結果（平台團隊與工具）"].append(rel)
        elif rel.startswith("data/"):
            groups["研究資料（agent 維護）"].append(rel)
        else:
            groups["其他（schema、設定範例）"].append(rel)
    for label, names in groups.items():
        if not names:
            continue
        ctx.say(f"  {label}：{len(names)} 個檔案")
        if label.startswith("研究資料") and len(names) > 12:
            ctx.say(f"    {names[0]} … {names[-1]}")
        else:
            for rel in names:
                ctx.say(f"    {rel}")


def cmd_bootstrap(ctx: Ctx) -> int:
    portal, store = _checkout(ctx), ctx.data()
    files = _collect(ctx, portal)
    ctx.say(f"{portal} → {store.uri()}：{len(files)} 個檔案（{sum(map(len, files.values())) / 1024:.0f} KB）")
    _listing(ctx, files)
    ctx.say("這些檔案發布後，能開 UI 的人都讀得到（包含內部清單與測試結果）。")
    common = dict(bootstrap=True, force=ctx.args.force, allow_invalid=ctx.args.allow_invalid, actor=ctx.by, note=ctx.args.note or "")
    preview = P.publish_tree(store, files, dry_run=True, **common)
    if preview.status != "preview" or ctx.args.dry_run:
        return _report_checkout(ctx, preview)
    if not _confirm(ctx, f"發布成 {store.uri()} 的第一個 release？"):
        ctx.say("已取消。")
        return OK
    outcome = P.publish_tree(store, files, **common)
    code = _report_checkout(ctx, outcome)
    if outcome.status == "published":
        _write_marker(portal, outcome.release_id, store.uri())
        flat = [k for k in store.list("data/") + store.list("schema/")]
        if flat:
            ctx.say(f"提醒：{store.uri()} 底下還有舊的平面複本（{len(flat)} 個檔案，來自 sync_portal_data.py）。它們不再被讀取，可以刪除：")
            ctx.say(f"  aws s3 rm {store.uri('data/')} --recursive && aws s3 rm {store.uri('schema/')} --recursive")
        ctx.say("下一步：讓 runtime 讀這個位置（python scripts/configure_agent.py --stack <data plane stack>，再 agentcore deploy），"
                "UI 則設定 DataBaseUrl（Amplify 的 VITE_DATA_BASE_URL 或 config.json）。")
    return code


def cmd_push(ctx: Ctx) -> int:
    portal, store = _checkout(ctx), ctx.data()
    only = _only(ctx)
    roots = sorted({p.split("/")[0] for p in only}) if only else R.ROOTS
    files = _collect(ctx, portal, roots)
    base = _marker(portal).get("releaseId")
    if not isinstance(base, str):
        base = None
    common = dict(only=only, base_release=base, force=ctx.args.force, allow_invalid=ctx.args.allow_invalid, delete=ctx.args.delete,
                  actor=ctx.by, note=ctx.args.note or "", untouched=ctx.left_out)
    preview = P.publish_tree(store, files, dry_run=True, **common)
    if preview.status != "preview":
        return _report_checkout(ctx, preview)
    _report(ctx, preview)
    if ctx.left_out:
        ctx.say(f"  （略過的 {len(ctx.left_out)} 個路徑不會因為這次發布而改變或消失：上線中的版本照舊）")
    if base is None and not ctx.args.delete:
        ctx.say("  （checkout 沒有來源紀錄：只會新增與取代檔案，不會刪除上線中多出來的檔案；要刪除請加 --delete）")
    if ctx.args.dry_run:
        return OK
    if not _confirm(ctx, f"發布到 {store.uri()}？"):
        ctx.say("已取消。")
        return OK
    outcome = P.publish_tree(store, files, **common)
    code = _report_checkout(ctx, outcome)
    if outcome.status == "published":
        # The checkout "is" the new release only if every file of it is: then (and only then) its base moves along.
        try:
            now_live = R.read_release(store, outcome.release_id, verify=False)[1]
            mine, _ = R.collect_tree(portal, validate=False, include=[p.strip() for p in (ctx.args.include or "").split(",") if p.strip()])
            left = [k.rstrip("/") for k in ctx.left_out]  # not read from the checkout, so not part of the comparison either
            if mine == {rel: data for rel, data in now_live.items() if not any(rel == k or rel.startswith(k + "/") for k in left)}:
                _write_marker(portal, outcome.release_id, store.uri())
        except (R.ReleaseError, OSError):
            pass
    return code


def _local(portal: Path, rel: str) -> tuple[bytes | None, str | None]:
    """(the checkout's content of ``rel`` or None, why it must not be written to - a symlink on the way - or None)."""
    path = portal / rel
    for part in [path, *path.parents]:
        if part == portal:
            break
        if part.is_symlink():
            return None, "路徑上有 symlink"
    if not path.exists():
        return None, None
    if not path.is_file():
        return None, "不是一般檔案"
    return path.read_bytes(), None


def cmd_pull(ctx: Ctx) -> int:
    """Merge a release into the checkout, file by file, three ways: what was only changed upstream is written, what was
    only changed here is kept, what both changed is reported (and kept, unless --overwrite)."""
    store = ctx.data()
    portal = _checkout(ctx, must_exist=not ctx.args.init)
    release_id = ctx.args.release or R.read_pointer(store)[0]["releaseId"]
    _meta, files = R.read_release(store, release_id)
    base_id = _marker(portal).get("releaseId")
    base: dict[str, bytes] | None = None
    if isinstance(base_id, str) and base_id:
        try:
            base = files if base_id == release_id else R.read_release(store, R.check_release_id(base_id), verify=False)[1]
        except R.ReleaseError:
            ctx.say(f"  （checkout 記錄的來源 release {c(base_id, 40)} 已經讀不到：無法分辨哪些檔案是妳改的）")

    write: list[str] = []      # new here, or untouched here and changed upstream
    kept: list[str] = []       # changed here only
    conflict: list[str] = []   # changed on both sides
    unknown: list[str] = []    # differs, and there is no base to tell who changed it
    blocked: list[str] = []    # cannot be written safely
    remove: list[str] = []     # removed upstream, untouched here
    deleted: list[str] = []    # removed here on purpose (it was in the release this checkout came from), unchanged upstream
    for rel, data in sorted(files.items()):
        mine, problem = _local(portal, rel)
        if problem:
            blocked.append(f"{rel}（{problem}）")
        elif mine == data:
            continue
        elif mine is None and base is not None and rel in base:
            # the checkout had this file and no longer has it: a deletion is a change like any other, and is kept -
            # unless the file was changed upstream in the meantime, which somebody has to decide
            (deleted if data == base[rel] else conflict).append(rel)
        elif mine is None:
            write.append(rel)
        elif base is None:
            unknown.append(rel)
        elif mine == base.get(rel):
            write.append(rel)
        elif data == base.get(rel):
            kept.append(rel)
        else:
            conflict.append(rel)
    for rel in sorted(base or {}):
        if rel in files:
            continue
        mine, problem = _local(portal, rel)
        if mine is None or problem:
            continue
        (remove if mine == (base or {})[rel] else conflict).append(rel)

    overwrite = ctx.args.overwrite
    to_write = write + (conflict + unknown + deleted if overwrite else [])
    to_write = [rel for rel in to_write if rel in files]
    # A file the release no longer has, and that nobody touched here, goes: the checkout is then the release it says it
    # is based on.  (Left behind, the next push would publish it again as "added by this checkout".)
    to_remove = remove + ([rel for rel in conflict if rel not in files] if overwrite else [])
    undecided = [] if overwrite else conflict + unknown
    stuck = len(undecided) + len(blocked)
    ctx.say(f"{store.uri()} 的 release {release_id} → {portal}"
            + ("（有檔案要先決定：這一次不會寫入或刪除任何東西，以下是處理完之後會做的事）" if stuck and not ctx.args.dry_run else ""))
    for rel in to_write:
        ctx.say(f"  {'~' if (portal / rel).exists() else '+'} {rel}")
    for rel in to_remove:
        ctx.say(f"  - {rel}")
    if kept:
        ctx.say(f"  保留妳改過的 {len(kept)} 個檔案（上線的資料沒有動到它們）：" + "、".join(kept[:8]) + ("…" if len(kept) > 8 else ""))
    if deleted:
        ctx.say(f"  保留妳刪除的 {len(deleted)} 個檔案（沒有寫回；下一次 push 會把它們從上線的資料移除，要取回請加 --overwrite）："
                + "、".join(deleted[:8]) + ("…" if len(deleted) > 8 else ""))
    gone_here = [rel for rel in conflict if rel not in files]
    if gone_here and not overwrite:
        ctx.say(f"  上線已移除、但妳改過的 {len(gone_here)} 個檔案保留在本機（算作衝突）：" + "、".join(gone_here[:8]) + ("…" if len(gone_here) > 8 else ""))
    if base is None:
        ctx.say("  （checkout 沒有來源紀錄：只會寫入新檔案，不會刪除任何檔案——無法分辨哪些是 release 曾經有的）")
    if blocked:
        ctx.say(f"  無法寫入 {len(blocked)} 個檔案：" + "、".join(blocked[:8]))
    if conflict and not overwrite:
        ctx.say(f"  ✘ {len(conflict)} 個檔案本機和上線都改過，沒有覆蓋：" + "、".join(conflict[:8]) + ("…" if len(conflict) > 8 else ""))
    if unknown and not overwrite:
        ctx.say(f"  ✘ {len(unknown)} 個檔案和上線的版本不同，而 checkout 沒有來源紀錄，無法分辨是誰改的，沒有覆蓋："
                + "、".join(unknown[:8]) + ("…" if len(unknown) > 8 else ""))
    if undecided:
        ctx.say("    要以上線的版本為準：加 --overwrite（本機的修改會被取代）；要以本機為準：push --force")
    if ctx.args.dry_run:
        # the preview ends the way the pull would: a script that asks "would it go through?" gets the answer
        ctx.say("（預覽）沒有寫入任何東西。" + (f"實際執行時會因為上面 {stuck} 個要先決定的檔案而停下，什麼都不寫。" if stuck else ""))
        return NOT_PUBLISHED if stuck else OK
    if stuck:
        # All or nothing.  A checkout that holds some files of the new release under the marker of the old one tells the
        # next push that pull's own writes (and removals) were the operator's changes - and a file of a release that
        # was rolled back in the meantime goes live again with an unrelated push.
        ctx.say(f"✘ 沒有寫入任何檔案：有 {stuck} 個檔案要先決定（見上）。checkout 和它的來源紀錄都維持原狀——pull 不會只做一半。")
        return NOT_PUBLISHED
    for rel in to_write:
        path = portal / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if portal not in path.resolve().parents:
            raise ScriptError(f"拒絕寫到 checkout 之外：{rel}")
        path.write_bytes(files[rel])
    for rel in to_remove:
        (portal / rel).unlink()
    _write_marker(portal, release_id, store.uri())
    ctx.say(f"✔ 寫入 {len(to_write)} 個檔案" + (f"、刪除 {len(to_remove)} 個" if to_remove else "") + f"；checkout 現在以 release {release_id} 為基準")
    return OK


# ---------------------------------------------------------------------------------------------- through the state machine
def _start(ctx: Ctx, execution_input: dict, wait: bool) -> int:
    if not ctx.machine:
        raise ScriptError("沒有指定 state machine：用 --state-machine <ARN> 或環境變數 PIPELINE_STATE_MACHINE_ARN（pipeline stack 的 StateMachineArn）")
    sfn = ctx.sfn()
    try:
        started = sfn.start_execution(stateMachineArn=ctx.machine, input=json.dumps(execution_input, ensure_ascii=False))
    except Exception as e:
        raise ScriptError(f"無法啟動 execution（需要 states:StartExecution）：{type(e).__name__}: {c(e, 400)}") from e
    arn = started["executionArn"]
    ctx.say(f"已啟動 {arn}")
    if not wait:
        ctx.say("加上 --wait 可以等它結束；完成後會發通知，也可以用 `portalctl.py executions` 查看。")
        return OK
    deadline = time.monotonic() + ctx.args.timeout
    announced = False
    while True:
        try:
            d = sfn.describe_execution(executionArn=arn)
        except Exception as e:
            raise ScriptError(f"execution 已經啟動，但無法查詢它的結果（需要 states:DescribeExecution）：{type(e).__name__}: {c(e, 300)}。"
                              "動作仍在執行，結果會以通知寄出；也可以在 Step Functions console 查看。") from e
        if d["status"] != "RUNNING":
            break
        if time.monotonic() > deadline:
            raise ScriptError(f"等了 {ctx.args.timeout} 秒還沒結束（execution 仍在執行，稍後用 executions 查看）")
        if not announced:
            ctx.say("  執行中…（一次研究的上限是 runtime 的 MAX_RUNTIME_SECONDS，預設 25 分鐘）")
            announced = True
        time.sleep(ctx.args.poll)
    output = {}
    try:
        output = json.loads(d.get("output") or "{}")
    except ValueError:
        pass
    output = output if isinstance(output, dict) else {}
    pub = output.get("publish") if isinstance(output.get("publish"), dict) else {}
    note = output.get("notification") if isinstance(output.get("notification"), dict) else {}
    ctx.say(f"execution {d['status']}" + (f"：{c(d.get('error'), 80)} {c(d.get('cause') or '', 400)}" if d["status"] != "SUCCEEDED" else ""))
    if d.get("error") == "PortalNotificationFailed":
        ctx.say("  動作本身已經執行完畢，只是通知寄不出去：用 `portalctl.py status` 與 `portalctl.py pending` 確認結果，並檢查 SNS topic 的權限與訂閱。")
    if note.get("message"):
        ctx.say(N.clean_block(note["message"], laid_out=True))  # (laid out by the pipeline that wrote it: its command lines are whole)
    if d["status"] != "SUCCEEDED":
        return NOT_PUBLISHED if d.get("error") == "PortalUpdateRejected" else FAILED
    return NOT_PUBLISHED if pub.get("status") == "pending" else OK


def cmd_run(ctx: Ctx) -> int:
    try:
        request = json.loads(ctx.args.payload_json) if ctx.args.payload_json else {"kind": ctx.args.kind}
    except ValueError as e:
        raise ScriptError(f"--payload-json 不是有效的 JSON：{e}") from e
    if not isinstance(request, dict):
        raise ScriptError("--payload-json 必須是 JSON 物件，例如 {\"kind\": \"weekly\"}")
    ctx.pipeline_only("run（研究 → 發布）")
    execution_input: dict[str, Any] = {"request": request, "trigger": "manual", "by": ctx.by}
    if ctx.args.mode:
        execution_input["mode"] = ctx.args.mode
    return _start(ctx, execution_input, ctx.args.wait)


def cmd_executions(ctx: Ctx) -> int:
    ctx.pipeline_only("executions（執行紀錄）")
    try:
        listed = ctx.sfn().list_executions(stateMachineArn=ctx.machine, maxResults=ctx.args.limit)
    except Exception as e:
        raise ScriptError(f"無法列出 execution（需要 states:ListExecutions）：{type(e).__name__}: {c(e, 300)}") from e
    for e in listed.get("executions", []):
        start = e.get("startDate")
        when = _taipei(start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")) if hasattr(start, "astimezone") else c(start, 40)
        ctx.say(f"{c(e.get('status', ''), 12):<10} {when}  {c(e.get('name', ''), 100)}")
    return OK


def _pending_sha(ctx: Ctx, proposal_id: str) -> str | None:
    """The hash of the pending proposal as it is stored right now (None when this invocation cannot read it)."""
    try:
        doc = P.load_pending(ctx.control(), proposal_id)
    except R.ReleaseError:
        raise
    except Exception:  # no read access to the bucket: the state machine will check the request as it is
        return None
    return doc.get("proposalSha256") or P.proposal_sha256(P.split_record(doc.get("record"))[0])


def cmd_decide(ctx: Ctx) -> int:
    """approve / discard."""
    action, proposal_id = ctx.args.command, ctx.args.proposal_id
    sha = getattr(ctx.args, "sha256", None)
    if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ScriptError("--sha256 必須是 64 個十六進位字元（`portalctl.py show <提案>` 與通知信都會列出）")
    reason = getattr(ctx.args, "reason", "") or ""
    if ctx.route():
        fields: dict[str, Any] = {"action": action, "by": ctx.by, "proposalId": proposal_id}
        if action == "approve":
            try:
                sha = sha or _pending_sha(ctx, proposal_id)
            except R.ReleaseError as e:
                raise ScriptError(c(e, 1200)) from e
            if sha:
                fields["sha256"] = sha  # approve what is stored now, not whatever is there when the execution runs
        elif reason:
            fields["reason"] = reason
        return _start(ctx, fields, True)
    data, control = ctx.data(), ctx.control()
    try:
        if action == "approve":
            outcome = P.approve(data, control, proposal_id, actor=ctx.by, expected_sha=sha)
        else:
            outcome = P.discard(control, proposal_id, actor=ctx.by, reason=reason, store=data)
    except R.ReleaseError as e:
        told = P.answered(data, control, proposal_id, action)  # not waiting (any more): say what was decided about it
        if told is None:
            raise ScriptError(c(e, 1200)) from e
        outcome = told
    return _report(ctx, outcome)


def cmd_rollback(ctx: Ctx) -> int:
    target, reason = ctx.args.to, ctx.args.reason or ""
    if target:
        try:
            R.check_release_id(target)
        except R.ReleaseError as e:
            raise ScriptError(c(e, 1200)) from e
    unlisted = bool(getattr(ctx.args, "unlisted", False))
    if unlisted and not target:
        raise ScriptError("--unlisted 要和 --to <release id> 一起用")
    if not ctx.route():
        return _report(ctx, P.rollback(ctx.data(), target, actor=ctx.by, reason=reason, allow_unlisted=unlisted))
    if unlisted:
        raise ScriptError("--unlisted（回到發布紀錄以外的 release）只能直接操作資料位置：加上 --direct")
    fields: dict[str, Any] = {"action": "rollback", "by": ctx.by}
    if reason:
        fields["reason"] = reason
    if target:
        fields["releaseId"] = target
    else:
        # Name both releases in the request: "back to the previous one" must not mean something else by the time it runs.
        try:
            manifest, _ = R.read_pointer(ctx.data())
        except Exception:  # no read access from here: the state machine resolves (and guards) the default itself
            manifest = None
        if manifest is not None:
            source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
            if source.get("kind") == "rollback":
                raise ScriptError(f"上線中的 {manifest['releaseId']} 本身就是一次 rollback 的結果（從 {c(source.get('from'), 40)} 回復）；"
                                  "再回復一次只會回到被撤下的那一版。要換到別的版本請用 --to <release id>（portalctl.py history）")
            if not manifest.get("previousReleaseId"):
                raise ScriptError("沒有更早的 release 可以回復")
            fields.update(releaseId=manifest["previousReleaseId"], ifCurrent=manifest["releaseId"])
    return _start(ctx, fields, True)


def cmd_pause(ctx: Ctx) -> int:
    action = ctx.args.command
    reason = getattr(ctx.args, "reason", "") or ""
    if ctx.route():
        return _start(ctx, {"action": action, "by": ctx.by, **({"reason": reason} if reason else {})}, True)
    data = ctx.data()
    return _report(ctx, P.pause(data, actor=ctx.by, reason=reason) if action == "pause" else P.resume(data, actor=ctx.by))


def cmd_publish(ctx: Ctx) -> int:
    spec = ctx.args.record
    if ctx.via_pipeline():
        if not spec.startswith("s3://"):
            raise ScriptError(
                f"這個環境的動作是透過 state machine 執行的，而它讀不到本機檔案 {spec}（它只發布 research/runs/ 底下的 run 紀錄）。"
                "要直接把這個檔案發布到資料位置，加上 --direct：那需要資料位置的寫入權限，而且不受 stack 的 PublishMode 約束"
                "（預設仍然先送審；--mode auto 才會通過關卡後直接上線）。")
        ctx.route()
        # The state machine is only told the name of the record (runs/run-….json) and reads it from its own research
        # location.  A record anywhere else would not be the one that gets published.
        base = ctx.data_uri.rstrip("/")
        own = ctx.research_uri or (base[: -len("portal")] + "research/" if base.endswith("/portal") else "")
        # (the variable describes the deployment the environment names - see Ctx: it is not applied to another one)
        unused = ("（環境變數 PIPELINE_RESEARCH_URI 屬於環境變數裡的資料位置，沒有用在指令列指定的這個位置上；要指定請用 --research）"
                  if ctx.research_ignored else "")
        if not own.startswith("s3://"):  # (a layout the data plane stack does not make: nothing here says where its results are)
            raise ScriptError(f"無法從資料位置 {ctx.data_uri} 得知這個部署的研究結果放在哪裡（data plane stack 的資料位置是 s3://<bucket>/portal/，"
                              "研究結果在旁邊的 research/）。用 --research s3://<bucket>/research/ 或環境變數 PIPELINE_RESEARCH_URI 指出來，"
                              "或加上 --direct 直接發布這個檔案。" + unused)
        own = own.rstrip("/") + "/"
        if not spec.startswith(own + "runs/run-") or "/" in spec[len(own + "runs/"):]:
            raise ScriptError(
                f"透過 state machine 只能發布這個部署自己的研究紀錄（{own}runs/run-….json）：它只收到檔名，讀的是它自己的那一份。"
                f"{spec} 不在那裡；要發布別處的檔案（例如修改過的複本），加上 --direct，那會直接讀這個檔案、寫入資料位置。" + unused)
        key = spec[len(own):]
        fields = {"action": "publish", "runKey": key, "by": ctx.by}
        if ctx.args.mode == "review":
            fields["mode"] = "review"
        return _start(ctx, fields, True)
    try:
        if spec.startswith("s3://"):
            bucket, _, key = spec[len("s3://"):].partition("/")
            raw = ctx.s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        else:
            raw = Path(spec).read_bytes()
        record = json.loads(raw)
    except (OSError, ValueError, RecursionError) as e:
        raise ScriptError(f"讀不到 {spec} 或它不是有效的 JSON：{c(e, 300)}") from e
    except Exception as e:
        raise ScriptError(f"讀不到 {spec}：{type(e).__name__}: {c(e, 300)}") from e
    ctx.route()
    # A person publishing a file by hand is still one command away from live data: the default is to put the proposal
    # up for approval and show what it would change.  --mode auto applies the automatic gates and publishes at once.
    mode = ctx.args.mode or "review"
    outcome = P.publish_proposal(ctx.data(), record, policy=P.Policy(mode=mode), control=ctx.control(), actor=ctx.by)
    code = _report(ctx, outcome)
    if outcome.status == "pending" and not ctx.args.mode:
        ctx.say("  （手動發布預設先送審；確定要讓它通過自動關卡後直接上線，用 --mode auto）")
    return code


# ---------------------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="portalctl.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    def global_flags(parser: argparse.ArgumentParser, *, default: Any) -> None:
        """The same switches before and after the subcommand (after it they only override when given)."""
        parser.add_argument("--data", metavar="URI", default=default, help="資料位置：s3://bucket/portal/ 或資料夾（預設取自 PORTAL_DATA_URI）")
        parser.add_argument("--state", metavar="URI", default=default, help="pipeline 狀態位置（預設：…/portal/ 旁的 …/pipeline/；或 PIPELINE_STATE_URI）")
        parser.add_argument("--state-machine", metavar="ARN", default=default, help="pipeline 的 state machine（預設取自 PIPELINE_STATE_MACHINE_ARN）")
        parser.add_argument("--research", metavar="URI", default=default, help="這個部署的研究結果位置（預設：…/portal/ 旁的 …/research/；或 PIPELINE_RESEARCH_URI）")
        parser.add_argument("--by", metavar="NAME", default=default, help="記錄在發布紀錄裡的操作者（預設是登入名稱；UI 的使用者讀得到）")
        parser.add_argument("--direct", action="store_true", default=False if default is None else default, help="不經由 state machine，直接操作資料位置")

    global_flags(p, default=None)
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("status", help="上線中的版本、是否暫停、待核准的提案、最近紀錄")
    h = sub.add_parser("history", help="發布紀錄（manifest.json 保留最近 60 次）")
    h.add_argument("--limit", type=int, default=R.HISTORY_LIMIT)
    sub.add_parser("pending", help="列出待核准的提案")
    sh = sub.add_parser("show", help="待核准的提案實際會改什麼，或某個 release 改了什麼（含引文）")
    sh.add_argument("id", nargs="?", help="提案 id（prop-…）或 release id；不給就是上線中的 release")
    sh.add_argument("--out", metavar="FILE", help="把完整內容存成 JSON 檔")
    v = sub.add_parser("verify", help="驗收：資料完整性，以及（--url）瀏覽器實際拿到的內容")
    v.add_argument("--url", metavar="URL", help="UI 讀資料的網址（data plane stack 的 DataBaseUrl）")
    v.add_argument("--origin", metavar="ORIGIN", help="UI 的網址（例如 https://main.d111.amplifyapp.com），用來檢查 CORS")

    def checkout(name: str, help_: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, help=help_)
        s.add_argument("--portal", default=".", help="portal repo 根目錄（含 schema/ 與 data/；預設為目前目錄）")
        s.add_argument("--dry-run", action="store_true", help="只預覽，不寫入")
        s.add_argument("--yes", action="store_true", help="不詢問，直接執行")
        return s

    def publishing(s: argparse.ArgumentParser) -> None:
        s.add_argument("--note", help="寫進發布紀錄的說明（UI 的使用者讀得到）")
        s.add_argument("--include", metavar="PATTERNS", help="除了 UI 會讀的檔案之外，額外發布這些路徑（逗號分隔，* 不跨資料夾）")
        s.add_argument("--allow-invalid", action="store_true", help="資料驗證沒過仍然發布（讀到那些檔案的頁面可能無法顯示）")

    b = checkout("bootstrap", "把 portal checkout 發布成第一個 release")
    publishing(b)
    b.add_argument("--force", action="store_true", help="資料位置已經有 release 時仍然用 checkout 整個取代（請確定妳知道會覆蓋什麼）")
    u = checkout("push", "從 portal checkout 發布（測試結果、平台清單、手動修改）：和上線中的資料逐檔合併")
    publishing(u)
    u.add_argument("--only", metavar="PATHS", help="只處理這些路徑（schema、data、runs、config 底下的資料夾或單一檔案，逗號分隔），例如 runs,data/internal；"
                                                   "範圍以外的檔案維持上線中的版本")
    u.add_argument("--delete", action="store_true", help="checkout 沒有來源紀錄時，也刪除上線中有、checkout 沒有的檔案（有 --only 時只在那個範圍內）")
    u.add_argument("--force", action="store_true", help="兩邊都改過的檔案以 checkout 為準（不會略過資料驗證）")
    d = checkout("pull", "把上線中（或指定）的 release 合併回 portal checkout（不會覆蓋妳改過的檔案）")
    d.add_argument("--release", metavar="ID", help="指定 release（預設是上線中的）")
    d.add_argument("--overwrite", action="store_true", help="本機和上線不同、無法自動合併的檔案，以上線的版本取代（上線已移除的就刪除）")
    d.add_argument("--init", action="store_true", help="目的資料夾還不是 portal repo 也可以（建立一份本機資料副本）")

    pub = sub.add_parser("publish", help="把某一次研究的結果（run 紀錄或 proposal）送去待核准，或直接發布")
    pub.add_argument("record", help="檔案路徑或 s3://bucket/key")
    pub.add_argument("--mode", choices=["auto", "review"], help="review（直接操作時的預設）= 先暫存、等核准；auto = 通過自動關卡就發布")

    ap = sub.add_parser("approve", help="核准並發布待核准的提案")
    ap.add_argument("proposal_id")
    ap.add_argument("--sha256", metavar="HASH", help="只核准內容是這個雜湊的提案（`show` 與通知信會列出）")
    dc = sub.add_parser("discard", help="捨棄待核准的提案")
    dc.add_argument("proposal_id")
    dc.add_argument("--reason", default="")
    rb = sub.add_parser("rollback", help="回復到前一個（或指定的）release，並暫停自動發布")
    rb.add_argument("--to", metavar="RELEASE_ID")
    rb.add_argument("--reason", default="")
    rb.add_argument("--unlisted", action="store_true", help="--to 指定的 release 不在發布紀錄裡也回復（它可能從未上線過：請先用 show 確認內容）")
    ps = sub.add_parser("pause", help="暫停自動發布（提案改為等待核准）")
    ps.add_argument("--reason", default="")
    sub.add_parser("resume", help="恢復自動發布")

    r = sub.add_parser("run", help="立刻啟動一次「研究 → 發布」")
    r.add_argument("--kind", default="weekly", help="沒有 --payload-json 時的研究種類（預設 weekly）")
    r.add_argument("--payload-json", metavar="JSON", help="完整的研究請求（agent 接受的 payload）")
    r.add_argument("--mode", choices=["review"], help="這一次的結果先暫存、等核准")
    r.add_argument("--wait", action="store_true", help="等 execution 結束並顯示結果")
    e = sub.add_parser("executions", help="最近的 execution")
    e.add_argument("--limit", type=int, default=10)

    for s in sub.choices.values():
        global_flags(s, default=argparse.SUPPRESS)
        s.add_argument("--timeout", type=int, default=3600, help=argparse.SUPPRESS)
        s.add_argument("--poll", type=float, default=15, help=argparse.SUPPRESS)
        if not any(a.dest == "yes" for a in s._actions):
            s.set_defaults(yes=False)
    return p


COMMANDS: dict[str, Callable[[Ctx], int]] = {
    "status": cmd_status, "history": cmd_history, "pending": cmd_pending, "show": cmd_show, "verify": cmd_verify,
    "bootstrap": cmd_bootstrap, "push": cmd_push, "pull": cmd_pull, "run": cmd_run, "executions": cmd_executions,
    "publish": cmd_publish, "approve": cmd_decide, "discard": cmd_decide, "rollback": cmd_rollback, "pause": cmd_pause, "resume": cmd_pause,
}


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None, clients: dict[str, Any] | None = None,
         out=None, stdin=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    # An argument that holds half a surrogate pair is one this terminal's encoding could not decode (Python keeps such
    # bytes that way), or half an emoji that was cut in two.  It cannot be stored, hashed or sent as text - and what a
    # --reason or a --note says ends up in the release record: say so here instead of sending something unreadable on.
    undecoded = sorted("--" + name.replace("_", "-") for name, value in vars(args).items() if isinstance(value, str) and _UNDECODED.search(value))
    undecoded += [name for name in ("PORTALCTL_BY",) if not args.by and _UNDECODED.search(environ.get(name) or "")]
    if undecoded:
        print(f"✘ {'、'.join(undecoded)} 的內容有無法當成文字的字元（終端機的編碼不是 UTF-8，或貼上的文字裡只有半個 emoji）："
              "沒有執行任何動作。請確認終端機的 locale 是 UTF-8（例如 export LC_ALL=C.UTF-8），或把那個字元拿掉再執行一次。", file=sys.stderr, flush=True)
        return USAGE
    ctx = Ctx(args, environ, {} if clients is None else clients, out or sys.stdout, sys.stdin if stdin is None else stdin)
    try:
        return COMMANDS[args.command](ctx)
    except (ScriptError, R.ReleaseError, StorageError) as e:
        # (a message of this tool's own making marks what it quotes; one that comes up from the pipeline's modules does
        #  not, and is laid out as a whole)
        text = f"✘ {e}"
        print(N.layout(text) if N.MARK in text or isinstance(e, ScriptError) else N.clean_block(text), file=sys.stderr, flush=True)
        return ENV
    except Exception as e:  # AWS errors, mostly: one line that says what was refused, not a traceback and not exit code 1
        if environ.get("PORTALCTL_DEBUG"):
            raise
        code = ((getattr(e, "response", None) or {}).get("Error") or {}).get("Code")
        print(N.layout(f"✘ {c(code or type(e).__name__, 80)}：{c(e, 600)}（設定 PORTALCTL_DEBUG=1 可以看到完整的錯誤）"), file=sys.stderr, flush=True)
        return ENV


if __name__ == "__main__":
    sys.exit(main())
