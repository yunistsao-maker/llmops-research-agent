#!/usr/bin/env python3
"""把 pipeline 的 Lambda 程式打包成一個 zip（四個 function 共用），需要時上傳到 S3。

    # 打包並上傳；最後一行印出 pipeline stack 要用的 CodeBucket / CodeKey
    uv run --with boto3 python scripts/build_lambdas.py --upload s3://<DataBucketName>/artifacts/

    # 只打包（不上傳）：build/pipeline/ 與 build/pipeline-<hash>.zip
    python scripts/build_lambdas.py

內容：pipeline/portal_pipeline/、與 agent 共用的 portal 資料模型（app/llmops_research/research 的一小部分）、
以及 pipeline/requirements.txt 的相依套件（為 Lambda 的 Python 3.13 / arm64 下載對應的 wheel）。
zip 的內容相同時檔名（hash）也相同，所以重複執行不會造成 CloudFormation 的無謂更新。

Exit code：0 成功 · 2 參數錯誤 · 3 環境或輸入問題
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = PROJECT_ROOT / "pipeline"
APP = PROJECT_ROOT / "app" / "llmops_research"
#: the part of the agent's code the publisher shares with it (the portal data model and the proposal replay) - nothing else
SHARED = ["research/__init__.py", "research/apply.py", "research/portal/__init__.py", "research/portal/checks.py",
          "research/portal/data.py", "research/portal/diff.py", "research/portal/labels.py", "research/portal/patch.py",
          "research/portal/protected.py", "research/validate/__init__.py", "research/validate/injection.py",
          "research/validate/urls.py"]
PLATFORMS = {"arm64": ("aarch64-manylinux2014", "manylinux2014_aarch64"), "x86_64": ("x86_64-manylinux2014", "manylinux2014_x86_64")}
OK, ENV = 0, 3


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def copy_sources(dest: Path) -> list[str]:
    copied = []
    for path in sorted((PIPELINE / "portal_pipeline").rglob("*.py")):
        rel = path.relative_to(PIPELINE).as_posix()
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest / rel)
        copied.append(rel)
    for rel in SHARED:
        src = APP / rel
        if not src.is_file():
            raise ScriptError(f"找不到共用模組 {src}")
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / rel)
        copied.append(rel)
    return copied


def vendor(dest: Path, arch: str, python_version: str, runner=subprocess.run) -> None:
    """Install the runtime dependencies for the Lambda platform (not this machine's) into ``dest``."""
    uv_platform, pip_platform = PLATFORMS[arch]
    requirements = str(PIPELINE / "requirements.txt")
    attempts = []
    if shutil.which("uv"):
        attempts.append(["uv", "pip", "install", "--quiet", "--target", str(dest), "--python-platform", uv_platform,
                         "--python-version", python_version, "--only-binary", ":all:", "--no-compile", "-r", requirements])
    attempts.append([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(dest), "--platform", pip_platform,
                     "--implementation", "cp", "--python-version", python_version, "--only-binary=:all:", "--no-compile",
                     "--upgrade", "-r", requirements])
    errors = []
    for cmd in attempts:
        try:
            proc = runner(cmd, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{cmd[0]}: {e}")
            continue
        if proc.returncode == 0:
            return
        errors.append(f"{' '.join(cmd[:3])}: {(proc.stderr or proc.stdout).strip()[-600:]}")
    raise ScriptError("無法下載相依套件（需要能連到 PyPI，並安裝 uv 或 pip）：\n  " + "\n  ".join(errors))


def prune(dest: Path) -> None:
    for path in sorted(dest.rglob("*"), reverse=True):
        if path.is_dir() and path.name in ("__pycache__", "bin"):
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.suffix in (".pyc", ".pyo"):
            path.unlink()


def make_zip(src: Path, out_dir: Path) -> Path:
    """A reproducible zip: sorted entries, fixed timestamps and modes, so equal content gives an equal file (and name)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "pipeline.zip.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(src).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix == ".so" else 0o644) << 16
            z.writestr(info, path.read_bytes())
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:16]
    final = out_dir / f"pipeline-{digest}.zip"
    tmp.replace(final)
    return final


def upload(zip_path: Path, target: str, client: Any = None) -> tuple[str, str]:
    if not target.startswith("s3://"):
        raise ScriptError("--upload 要寫成 s3://bucket/prefix/")
    bucket, _, prefix = target[len("s3://"):].partition("/")
    if not bucket:
        raise ScriptError("--upload 要寫成 s3://bucket/prefix/")
    prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
    key = prefix + zip_path.name
    if client is None:
        import boto3

        client = boto3.client("s3")
    try:
        client.put_object(Bucket=bucket, Key=key, Body=zip_path.read_bytes(), ContentType="application/zip")
    except Exception as e:
        raise ScriptError(f"上傳 s3://{bucket}/{key} 失敗：{type(e).__name__}: {e}") from e
    return bucket, key


def build(out_dir: Path, *, deps: bool = True, arch: str = "arm64", python_version: str = "3.13", runner=subprocess.run) -> Path:
    bundle = out_dir / "pipeline"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    if deps:
        vendor(bundle, arch, python_version, runner)
    copy_sources(bundle)  # after the dependencies: our own modules always win
    prune(bundle)
    return make_zip(bundle, out_dir)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_lambdas.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=str(PROJECT_ROOT / "build"), help="輸出資料夾（預設 build/）")
    p.add_argument("--upload", metavar="s3://bucket/prefix/", help="打包後上傳到這個位置")
    p.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64", help="Lambda 架構（預設 arm64，要和 infra/pipeline.yaml 的 Architectures 一致）")
    p.add_argument("--python-version", default="3.13", help="Lambda runtime 的 Python 版本（預設 3.13）")
    p.add_argument("--no-deps", action="store_true", help="只放原始碼，不下載相依套件（測試用；這樣的 zip 不能部署）")
    return p


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    try:
        zip_path = build(Path(args.out), deps=not args.no_deps, arch=args.arch, python_version=args.python_version)
        size = zip_path.stat().st_size
        say(f"已打包 {zip_path}（{size / 1_048_576:.1f} MB）")
        if args.no_deps:
            say("⚠ --no-deps：這個 zip 不含相依套件，不能部署。")
        if args.upload:
            bucket, key = upload(zip_path, args.upload, client)
            say(f"已上傳 s3://{bucket}/{key}")
            say(f"CodeBucket={bucket} CodeKey={key}")
        else:
            say("加上 --upload s3://<DataBucketName>/artifacts/ 就會上傳，並印出 pipeline stack 要用的 CodeBucket / CodeKey。")
    except ScriptError as e:
        print(f"✘ {e}", file=sys.stderr, flush=True)
        return ENV
    return OK


if __name__ == "__main__":
    sys.exit(main())
