#!/usr/bin/env python3
"""把 research agent 的 proposal 套用到 LLMOps portal repo 的 data/；需要時建立分支、commit、開 draft PR。

預設只「預覽」，不會寫任何檔案。每往前一步都要明確的旗標：

    --write              寫入 data/ 下變更的檔案
    --validate           寫入後執行 portal 自己的驗證（預設 `npm run validate`）；失敗會還原檔案
    --branch [NAME]      先建立分支（預設 research/<proposal id>），隱含 --write
    --commit             commit 變更的檔案（只 commit 這些檔案），隱含 --branch
    --open-pr            push 並用 gh 開 draft PR（內文是 proposal 的證據表），隱含 --commit

proposal 可以是檔案、s3://bucket/key 物件，或 `-`（stdin）；agent 存下來的 run 紀錄（runs/<runId>.json）也可以。
這個 script 不呼叫模型、也不上網：套用邏輯和 agent 在回傳 proposal 之前驗證時用的完全相同，
並且逐一比對每個 op 的「原值」，portal 在 proposal 產生後被改過的地方會被擋下來。

範例（先預覽，再開 PR）：

    uv run --with-requirements scripts/requirements.txt python scripts/apply_proposal.py proposal.json --portal ../llmops-portal
    uv run --with-requirements scripts/requirements.txt python scripts/apply_proposal.py proposal.json --portal ../llmops-portal --open-pr --validate

Exit code：0 成功 · 1 proposal 無法套用或驗證失敗 · 2 參數錯誤 · 3 環境或輸入問題
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401  (puts app/llmops_research on sys.path)

from research.apply import already_applied, apply_saved_proposal, changed_files, diff_text, dump_json, write_changes
from research.portal.data import SCHEMA_PATH, PortalData
from research.render import render_pr_body

OK, PROBLEMS, ENV = 0, 1, 3
PROPOSAL_SCHEMA = "llmops-research-proposal/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}\Z")


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------- input
def _s3_client() -> Any:
    import boto3

    return boto3.client("s3")


def load_json_source(spec: str) -> Any:
    try:
        if spec == "-":
            text = sys.stdin.read()
        elif spec.startswith("s3://"):
            bucket, _, key = spec[5:].partition("/")
            if not bucket or not key:
                raise ScriptError(f"S3 位置要寫成 s3://bucket/key：{spec}")
            text = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        else:
            text = Path(spec).read_text(encoding="utf-8")
    except ScriptError:
        raise
    except Exception as e:  # missing file, S3 access denied, bad encoding
        raise ScriptError(f"讀不到 {spec}：{type(e).__name__}: {e}") from e
    try:
        return json.loads(text)
    except ValueError as e:
        raise ScriptError(f"{spec} 不是有效的 JSON：{e}") from e


def split_record(doc: Any) -> tuple[dict, dict | None]:
    """Return (proposal, run record or None) for a proposal file or a ``runs/<runId>.json`` record."""
    if not isinstance(doc, dict):
        raise ScriptError("輸入不是 JSON 物件")
    if doc.get("schema") == PROPOSAL_SCHEMA:
        return doc, None
    if isinstance(doc.get("proposal"), dict):
        return doc["proposal"], doc
    if doc.get("type") == "accepted":
        raise ScriptError(f"這是背景 run 的 accepted 事件，結果在 {doc.get('resultUri', '(未提供)')}；請改用那個位置")
    if "status" in doc:
        reason = str(doc.get("error") or "沒有 proposal")[:300]
        raise ScriptError(f"這次 run 沒有產出 proposal（status={doc.get('status')}）：{reason}")
    raise ScriptError(f"看不出這是 proposal 或 run 紀錄（缺少 schema={PROPOSAL_SCHEMA!r} 或 proposal 欄位）")


def load_portal(root: Path) -> PortalData:
    if not (root / SCHEMA_PATH).is_file() or not (root / "data").is_dir():
        raise ScriptError(f"{root} 不像 portal repo：找不到 {SCHEMA_PATH} 或 data/（用 --portal 指到 repo 根目錄）")
    try:
        return PortalData.from_dir(root)
    except (OSError, ValueError) as e:
        raise ScriptError(f"讀取 portal 資料失敗：{e}") from e


# --------------------------------------------------------------------------------------------- git / gh
def _bin(env_name: str, default: str) -> str:
    return os.environ.get(env_name) or default


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run([_bin("GIT_BIN", "git"), *args], cwd=root, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise ScriptError(f"無法執行 git：{e}") from e
    if check and proc.returncode != 0:
        raise ScriptError(f"git {' '.join(args)} 失敗：{(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def in_git_repo(root: Path) -> bool:
    try:
        return run_git(root, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"
    except ScriptError:
        return False


def base_note(root: Path, proposal: dict) -> str | None:
    """Tell the reviewer how the checkout relates to the portal data the proposal was made against."""
    manifest = (proposal.get("portal") or {}).get("manifest") or {}
    commit = manifest.get("commit")
    notes = []
    if manifest.get("dirty"):
        notes.append("基準資料同步時工作目錄有未 commit 的變更")
    if commit and in_git_repo(root):
        head = run_git(root, "rev-parse", "HEAD", check=False)
        if head.returncode == 0 and not head.stdout.strip().startswith(commit[:40]):
            anc = run_git(root, "merge-base", "--is-ancestor", commit, "HEAD", check=False)
            if anc.returncode == 0:
                notes.append(f"portal 在 proposal 的基準 commit {commit[:10]} 之後又前進了；各 op 的原值會逐一比對，不符的會被擋下")
            else:
                notes.append(f"proposal 的基準 commit {commit[:10]} 不在目前 checkout 的歷史中（別的分支，或尚未 fetch）")
    return "；".join(notes) or None


def git_preflight(root: Path, files: list[str], branch: str | None, *, open_pr: bool) -> dict:
    """Everything that could make the git steps fail half-way, checked before anything is written."""
    if not in_git_repo(root):
        raise ScriptError(f"{root} 不是 git 工作目錄，不能建立分支或 commit（只想寫檔請用 --write）")
    if run_git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0:
        raise ScriptError("repo 還沒有任何 commit")
    dirty = run_git(root, "status", "--porcelain", "--untracked-files=all", "--", *files).stdout.rstrip()
    if dirty:
        raise ScriptError("這些檔案有尚未 commit 的變更，請先處理再套用：\n" + dirty)
    if branch:
        if not _BRANCH.match(branch) or run_git(root, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            raise ScriptError(f"不是合法的分支名稱：{branch}")
        if run_git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
            raise ScriptError(f"分支 {branch} 已存在；換一個名稱，或先刪除舊分支")
    if open_pr:
        if not shutil.which(_bin("GH_BIN", "gh")):
            raise ScriptError("找不到 gh（GitHub CLI）；請安裝並執行 `gh auth login`，或不要加 --open-pr、自行 push 並開 PR")
        if run_git(root, "remote", "get-url", "origin", check=False).returncode != 0:
            raise ScriptError("repo 沒有 origin remote，無法 push")
    current = run_git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    sha = run_git(root, "rev-parse", "HEAD").stdout.strip()
    return {"ref": current or sha, "sha": sha, "branch": current}


def commit_message(proposal: dict, files: list[str]) -> str:
    """Only fixed text, the proposal id (validated) and file paths: nothing that came from a web page."""
    n = len(proposal.get("ops") or [])
    lines = [f"data: research proposal {proposal['id']}", "",
             f"Applied from LLMOps research agent proposal {proposal['id']} ({n} op(s)).", "", "Files:"]
    lines += [f"- {f}" for f in files]
    lines += ["", "Generated by the research agent; needs human review before merge (evidence is in the pull request)."]
    return "\n".join(lines) + "\n"


def run_validation(root: Path, command: str) -> tuple[bool, str]:
    argv = shlex.split(command)
    if not argv:
        raise ScriptError("--validate-cmd 是空的")
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as e:
        raise ScriptError(f"無法執行驗證指令 {command!r}：{e}") from e
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def open_pull_request(root: Path, branch: str, title: str, body_file: Path, base: str | None, ready: bool) -> str:
    run_git(root, "push", "-u", "origin", branch)
    cmd = [_bin("GH_BIN", "gh"), "pr", "create", "--title", title, "--body-file", str(body_file), "--head", branch]
    if base:
        cmd += ["--base", base]
    if not ready:
        cmd.append("--draft")
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise ScriptError(f"無法執行 gh：{e}（分支 {branch} 已 push）") from e
    if proc.returncode != 0:
        raise ScriptError(f"gh pr create 失敗：{(proc.stderr or proc.stdout).strip()[:400]}（分支 {branch} 已 push，可自行開 PR）")
    return proc.stdout.strip()


# --------------------------------------------------------------------------------------------- files
def backup_files(root: Path, files: list[str]) -> dict[str, bytes | None]:
    return {rel: (root / rel).read_bytes() if (root / rel).is_file() else None for rel in files}


def restore_files(root: Path, backups: dict[str, bytes | None]) -> None:
    for rel, old in backups.items():
        path = root / rel
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(old)


def unformatted(root: Path, portal: PortalData, files: list[str]) -> list[str]:
    """Files whose current text differs from what this script writes (so git diff would show more than the preview)."""
    out = []
    for rel in files:
        path = root / rel
        if rel in portal.docs and path.is_file() and path.read_text(encoding="utf-8") != dump_json(portal.docs[rel]):
            out.append(rel)
    return out


# --------------------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apply_proposal.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("proposal", help="proposal 或 run 紀錄：檔案路徑、s3://bucket/key，或 -（stdin）")
    p.add_argument("--portal", default=".", help="portal repo 根目錄（含 schema/ 與 data/；預設為目前目錄）")
    p.add_argument("--write", action="store_true", help="寫入 data/ 下變更的檔案（預設只預覽）")
    p.add_argument("--allow-sample", action="store_true", help="proposal 是依 agent 內建 SAMPLE 資料產生時，仍然允許寫入")
    p.add_argument("--validate", action="store_true", help="寫入後執行 portal 的驗證指令；失敗就還原檔案")
    p.add_argument("--validate-cmd", default="npm run validate", help="驗證指令（預設 'npm run validate'，在 portal 根目錄執行）")
    p.add_argument("--pr-body", metavar="FILE", help="把 PR 內文（zh-TW markdown）寫到這個檔案（預覽時也可用）")
    p.add_argument("--branch", nargs="?", const="", default=None, metavar="NAME", help="先建立分支（預設 research/<proposal id>），隱含 --write")
    p.add_argument("--commit", action="store_true", help="commit 變更的檔案，隱含 --branch")
    p.add_argument("--open-pr", action="store_true", help="push 分支並用 gh 開 PR，隱含 --commit")
    p.add_argument("--base", metavar="BRANCH", help="PR 的目標分支（預設用 repo 的預設分支）")
    p.add_argument("--ready", action="store_true", help="開正式 PR（預設為 draft，等人工複核）")
    p.add_argument("--no-diff", action="store_true", help="不印出 diff")
    return p


def run(args: argparse.Namespace) -> int:
    if args.open_pr:
        args.commit = True
    if args.commit and args.branch is None:
        args.branch = ""
    if args.branch is not None:
        args.write = True
    if args.validate and not args.write:
        raise ScriptError("--validate 要搭配 --write（驗證的是寫入後的檔案）")

    root = Path(args.portal).resolve()
    proposal, record = split_record(load_json_source(args.proposal))
    pid = proposal.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise ScriptError(f"proposal 的 id 不合法：{str(pid)[:80]!r}")
    portal = load_portal(root)

    sample = bool((proposal.get("portal") or {}).get("sample"))
    say(f"proposal {pid}（{proposal.get('createdAt', '?')}）→ {root}")
    if sample:
        say("⚠ 這份 proposal 是根據 agent 內建的 SAMPLE portal 資料產生，不是你的實際 repo。")
    note = base_note(root, proposal)
    if note:
        say(f"ℹ {note}")

    if already_applied(portal, proposal):
        say("✔ 這份 proposal 的內容已經都在這個 checkout 裡了（之前套用或 merge 過），沒有需要變更的檔案。")
        return OK
    staged, problems = apply_saved_proposal(portal, proposal)
    if problems:
        say("")
        say(f"✘ 這份 proposal 不能套用到這個 checkout（{len(problems)} 個問題），沒有寫入任何檔案：")
        for pr in problems:
            say(f"  - {pr}")
        return PROBLEMS

    files = changed_files(portal, staged)
    if not files:
        say("✔ 沒有需要變更的檔案：proposal 的內容已經在這個 checkout 裡。")
        return OK
    say(f"✔ 可以套用：{len(files)} 個檔案會變更")
    for f in files:
        say(f"  - {f}")
    for w in (proposal.get("validation") or {}).get("warnings") or []:
        say(f"  ⚠ agent 驗證器警告：{str(w)[:300]}")
    if not args.no_diff:
        say("")
        say(diff_text(portal, staged).rstrip("\n"))
    odd = unformatted(root, portal, files)
    if odd:
        say("")
        say("⚠ 這些檔案目前的排版和本工具寫入的格式（2 空格縮排、保留中文、結尾換行）不同，"
            "寫入後 git diff 會比上面的預覽多出排版變更：" + "、".join(odd))

    body_path: Path | None = Path(args.pr_body) if args.pr_body else None
    temp_body: Path | None = None
    if body_path or args.open_pr:
        if body_path is None:
            fd, name = tempfile.mkstemp(prefix="research-pr-", suffix=".md")
            os.close(fd)
            body_path = temp_body = Path(name)
        body_path.write_text(render_pr_body(proposal, record), encoding="utf-8")
        say(f"\nPR 內文已寫到 {body_path}")

    try:
        if not args.write:
            say("\n這是預覽，沒有寫入任何檔案。確認無誤後加上 --write（或 --open-pr）。")
            return OK
        if sample and not args.allow_sample:
            raise ScriptError("proposal 是依 SAMPLE 資料產生的；若確定要套用到這個 repo（ops 的原值仍會逐一比對），加上 --allow-sample")
        return write_and_publish(args, root, portal, staged, proposal, files, pid, body_path)
    finally:
        if temp_body is not None:
            temp_body.unlink(missing_ok=True)


def write_and_publish(args, root: Path, portal: PortalData, staged: PortalData, proposal: dict, files: list[str], pid: str,
                      body_path: Path | None) -> int:
    branch = None
    start = None
    if args.branch is not None:
        branch = args.branch or f"research/{pid}"
        start = git_preflight(root, files, branch, open_pr=args.open_pr)
    elif in_git_repo(root):
        dirty = run_git(root, "status", "--porcelain", "--untracked-files=all", "--", *files).stdout.rstrip()
        if dirty:
            raise ScriptError("這些檔案有尚未 commit 的變更，請先處理再套用：\n" + dirty)

    backups = backup_files(root, files)
    created_branch = False
    done = False
    try:
        if branch:
            run_git(root, "checkout", "-b", branch)
            created_branch = True
            say(f"\n已建立分支 {branch}（從 {start['ref'][:12]}）")
        write_changes(root, portal, staged)
        say(f"已寫入 {len(files)} 個檔案。")
        if args.validate:
            ok, output = run_validation(root, args.validate_cmd)
            if not ok:
                say(f"\n✘ 驗證失敗（{args.validate_cmd}），已還原檔案：")
                say(output[-3000:])
                return PROBLEMS
            say(f"✔ 驗證通過（{args.validate_cmd}）")
        if args.commit:
            run_git(root, "add", "--", *files)
            run_git(root, "commit", "-m", commit_message(proposal, files), "--", *files)
            say(f"已 commit：{run_git(root, 'rev-parse', '--short', 'HEAD').stdout.strip()}")
        done = True
    finally:
        if not done:  # leave nothing half-applied behind: unstage, restore the files, drop the branch we made
            if args.commit:
                run_git(root, "reset", "-q", "--", *files, check=False)
            restore_files(root, backups)
            if created_branch:
                run_git(root, "checkout", start["ref"], check=False)
                run_git(root, "branch", "-D", branch, check=False)
    if args.open_pr:
        url = open_pull_request(root, branch, f"data: research proposal {pid}", body_path, args.base, args.ready)
        say(f"✔ 已開 {'PR' if args.ready else 'draft PR'}：{url}")
    elif branch:
        say(f"\n接下來：git push -u origin {branch}，再開 PR（PR 內文可用 --pr-body 產生）。")
    else:
        say("\n接下來：檢查 git diff，執行 `npm run validate`，再自行 commit。")
    return OK


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ScriptError as e:
        warn(f"✘ {e}")
        return ENV


if __name__ == "__main__":
    sys.exit(main())
