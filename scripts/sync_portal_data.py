#!/usr/bin/env python3
"""（舊流程）把 LLMOps portal repo 的 schema 與資料檔「平鋪」同步給 research agent 讀取。

**採用 pipeline 之後不需要這支 script**：上線的資料由 `portalctl bootstrap`／`push` 發布成 release，agent 讀 `manifest.json`
指到的那一個 release。資料位置一旦有 release 指標，這裡上傳的平面複本就不再被讀取（`portalctl bootstrap` 會提醒並列出它們）。
它留著是為了兩種情況：還沒部署 pipeline 時單獨測試 agent，以及更新離線測試用的內建 snapshot。

    --to s3://bucket/portal/    上傳到一個還沒有 release 指標的位置；agent 用 PORTAL_DATA_URI 讀取
    --to snapshot               覆寫 agent 套件內建的 snapshot（隨 `agentcore deploy` 一起打包；要重新部署才會更新）。
                                覆寫之後它不再標示為 SAMPLE：依它產生的提案和其他提案一樣，由 pipeline 對上線中的資料
                                逐項比對原值，資料不同就會被拒絕

只會複製 schema/llmops.schema.json、data/{sources,catalog,lifecycle,litellm,requirements}.json、
data/digests/*.json、data/models/*.json；data/internal/（平台內部清單）和 repo 的其他檔案一律不複製。
上傳前每個檔案都會先確認是有效的 JSON。同步完成後會寫入 sync_manifest.json（portal 的 commit、時間、
工作目錄是否有未 commit 的變更），之後 proposal 與 PR 內文會註明它是根據哪一個 commit 的資料產生的。

範例：

    uv run --with-requirements scripts/requirements.txt python scripts/sync_portal_data.py --portal ../llmops-portal --to s3://my-bucket/portal/ --dry-run
    uv run --with-requirements scripts/requirements.txt python scripts/sync_portal_data.py --portal ../llmops-portal --to s3://my-bucket/portal/ --delete

Exit code：0 成功 · 2 參數錯誤 · 3 環境或輸入問題
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401  (puts app/llmops_research on sys.path)

from research.portal.data import BUNDLED_DIR, SCHEMA_PATH, SYNC_MANIFEST, is_data_path

OK, ENV = 0, 3
SNAPSHOT_MANIFEST = "snapshot_manifest.json"


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def wanted(rel: str) -> bool:
    return rel == SCHEMA_PATH or is_data_path(rel)


# --------------------------------------------------------------------------------------------- collect
def collect(portal: Path) -> dict[str, bytes]:
    """The portal files the agent reads, as raw bytes (every one parsed first, so nothing invalid is ever uploaded)."""
    if not (portal / SCHEMA_PATH).is_file() or not (portal / "data").is_dir():
        raise ScriptError(f"{portal} 不像 portal repo：找不到 {SCHEMA_PATH} 或 data/（用 --portal 指到 repo 根目錄）")
    files: dict[str, bytes] = {}
    for path in [portal / SCHEMA_PATH, *sorted((portal / "data").rglob("*.json"))]:
        rel = path.relative_to(portal).as_posix()
        if not wanted(rel) or path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            json.loads(raw)
        except ValueError as e:
            raise ScriptError(f"{rel} 不是有效的 JSON，沒有同步任何檔案：{e}") from e
        files[rel] = raw
    if not any(rel.startswith("data/") for rel in files):
        raise ScriptError(f"{portal / 'data'} 底下沒有 agent 會讀的資料檔")
    return files


def git_info(portal: Path) -> dict[str, Any]:
    """The portal's commit and whether the synced paths had uncommitted changes (empty when it is not a git checkout)."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=portal, capture_output=True, text=True, timeout=60)

    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return {}
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=all", "--", "schema", "data").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {}
    info: dict[str, Any] = {"dirty": bool(dirty)}
    if head.returncode == 0:
        info["commit"] = head.stdout.strip()
    return info


def make_manifest(portal: Path, files: dict[str, bytes], kind: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {"kind": kind, "syncedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": len(files)}
    manifest.update(git_info(portal))
    return manifest


def dump(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# --------------------------------------------------------------------------------------------- S3
def _s3_client() -> Any:
    import boto3

    return boto3.client("s3")


def parse_s3(uri: str) -> tuple[str, str]:
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    if not bucket:
        raise ScriptError(f"S3 位置要寫成 s3://bucket/prefix/：{uri}")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def sync_s3(files: dict[str, bytes], manifest: dict, uri: str, *, delete: bool, dry_run: bool, client: Any = None) -> dict[str, list[str]]:
    bucket, prefix = parse_s3(uri)
    s3 = client or _s3_client()
    remote: dict[str, str] = {}  # relative key -> ETag
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                remote[obj["Key"][len(prefix):]] = str(obj.get("ETag", "")).strip('"')
    except Exception as e:
        raise ScriptError(f"無法列出 s3://{bucket}/{prefix}：{type(e).__name__}: {e}") from e

    uploads = {**files, SYNC_MANIFEST: dump(manifest)}
    result: dict[str, list[str]] = {"uploaded": [], "unchanged": [], "deleted": []}
    for rel, raw in sorted(uploads.items()):
        # a single-part object's ETag is the MD5 of its body (not so with SSE-KMS, where we simply upload again)
        if rel != SYNC_MANIFEST and remote.get(rel) == hashlib.md5(raw).hexdigest():  # noqa: S324 - change detection only
            result["unchanged"].append(rel)
            continue
        result["uploaded"].append(rel)
        if not dry_run:
            try:
                s3.put_object(Bucket=bucket, Key=prefix + rel, Body=raw, ContentType="application/json")
            except Exception as e:
                raise ScriptError(f"上傳 s3://{bucket}/{prefix}{rel} 失敗：{type(e).__name__}: {e}") from e
    if delete:
        for rel in sorted(remote):
            if wanted(rel) and rel not in files:  # only portal-data keys: never runs/, proposals/ or anything else
                result["deleted"].append(rel)
                if not dry_run:
                    try:
                        s3.delete_object(Bucket=bucket, Key=prefix + rel)
                    except Exception as e:
                        raise ScriptError(f"刪除 s3://{bucket}/{prefix}{rel} 失敗：{type(e).__name__}: {e}") from e
    return result


# --------------------------------------------------------------------------------------------- snapshot
def check_snapshot_dir(dest: Path, portal: Path) -> None:
    """The snapshot is mirrored (stale files are deleted), so only ever do that to a folder that is plainly a snapshot."""
    if dest == portal.resolve():
        raise ScriptError("snapshot 目的地不能是 portal repo 本身")
    if (dest / ".git").exists():
        raise ScriptError(f"{dest} 是 git repo，不像 snapshot 資料夾；用 --dir 指到別的位置")
    if dest.exists() and not dest.is_dir():
        raise ScriptError(f"{dest} 不是資料夾")
    if dest.is_dir() and any(dest.iterdir()) and not ((dest / SNAPSHOT_MANIFEST).is_file() or (dest / SCHEMA_PATH).is_file()):
        raise ScriptError(f"{dest} 不是空的，也不像 snapshot 資料夾（沒有 {SNAPSHOT_MANIFEST}）；為免誤刪，請指到空資料夾或不存在的路徑")


def sync_snapshot(files: dict[str, bytes], manifest: dict, dest: Path, portal: Path, *, dry_run: bool) -> dict[str, list[str]]:
    dest = dest.resolve()
    check_snapshot_dir(dest, portal)
    result: dict[str, list[str]] = {"uploaded": [], "unchanged": [], "deleted": []}
    if dest.is_dir():
        for path in sorted(dest.rglob("*.json")):
            rel = path.relative_to(dest).as_posix()
            if wanted(rel) and rel not in files:  # the snapshot mirrors the portal: drop what the portal no longer has
                result["deleted"].append(rel)
                if not dry_run:
                    path.unlink()
    for rel, raw in sorted(files.items()):
        path = dest / rel
        if path.is_file() and path.read_bytes() == raw:
            result["unchanged"].append(rel)
            continue
        result["uploaded"].append(rel)
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    result["uploaded"].append(SNAPSHOT_MANIFEST)
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / SNAPSHOT_MANIFEST).write_bytes(dump({**manifest, "kind": "snapshot", "description": "Portal data copied by scripts/sync_portal_data.py"}))
    return result


# --------------------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sync_portal_data.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--portal", default=".", help="portal repo 根目錄（含 schema/ 與 data/；預設為目前目錄）")
    p.add_argument("--to", required=True, metavar="s3://bucket/prefix/|snapshot", help="目的地：S3 位置，或 snapshot（agent 套件內建的資料夾）")
    p.add_argument("--dir", metavar="DIR", help="搭配 --to snapshot：改寫到這個資料夾（預設是 agent 套件內建的 snapshot）")
    p.add_argument("--delete", action="store_true", help="S3：刪除 portal 已經沒有的資料檔（只動 schema/ 與資料檔，不碰其他 key）")
    p.add_argument("--dry-run", action="store_true", help="只列出會做的事，不寫入")
    return p


def run(args: argparse.Namespace, *, client: Any = None) -> int:
    portal = Path(args.portal).resolve()
    to_s3 = args.to.startswith("s3://")
    if not to_s3 and args.to != "snapshot":
        raise ScriptError("--to 要是 s3://bucket/prefix/ 或 snapshot")
    if args.dir and to_s3:
        raise ScriptError("--dir 只能搭配 --to snapshot")
    files = collect(portal)
    manifest = make_manifest(portal, files, "sync")
    if manifest.get("dirty"):
        say("⚠ portal 的 schema/ 或 data/ 有尚未 commit 的變更；manifest 會註明，proposal 也會帶著這個提醒。")

    if to_s3:
        result = sync_s3(files, manifest, args.to, delete=args.delete, dry_run=args.dry_run, client=client)
        where = args.to if args.to.endswith("/") else args.to + "/"
    else:
        dest = Path(args.dir) if args.dir else BUNDLED_DIR
        result = sync_snapshot(files, manifest, dest, portal, dry_run=args.dry_run)
        where = str(dest)

    verb = "會" if args.dry_run else "已"
    say(f"{'（預覽）' if args.dry_run else ''}{portal} → {where}")
    say(f"  {verb}寫入 {len(result['uploaded'])} 個、未變動 {len(result['unchanged'])} 個、{verb}刪除 {len(result['deleted'])} 個"
        + (f"；commit {manifest['commit'][:10]}" if manifest.get("commit") else ""))
    for rel in result["uploaded"]:
        say(f"  + {rel}")
    for rel in result["deleted"]:
        say(f"  - {rel}")
    if args.dry_run:
        say("這是預覽，沒有寫入任何東西。")
    elif to_s3:
        say(f"完成。把 agent 的 PORTAL_DATA_URI 設成 {where}（agentcore.json 的 envVars），之後每次 run 都讀這份資料。")
    else:
        say("完成。重新執行 `agentcore deploy` 後，agent 才會使用這份內建 snapshot（PORTAL_DATA_URI 留空 = bundled）。")
    return OK


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    try:
        return run(args, client=client)
    except ScriptError as e:
        print(f"✘ {e}", file=sys.stderr, flush=True)
        return ENV


if __name__ == "__main__":
    sys.exit(main())
