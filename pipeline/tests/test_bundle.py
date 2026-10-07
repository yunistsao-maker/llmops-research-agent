"""The Lambda bundle: complete (the handlers import with nothing but the bundle), minimal, and reproducible."""

from __future__ import annotations

import subprocess
import sys
import zipfile

import pytest

import build_lambdas as B


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("build")
    return B.build(out, deps=False), out


def test_the_bundle_holds_the_pipeline_and_only_the_shared_part_of_the_agent(bundle):
    zip_path, _ = bundle
    names = zipfile.ZipFile(zip_path).namelist()
    assert "portal_pipeline/handlers.py" in names and "portal_pipeline/publish.py" in names
    assert sorted(n for n in names if n.startswith("research/")) == sorted(B.SHARED)
    # none of the agent itself (research/ holds exactly the shared modules): no model, no web access, no tools, no sample data
    assert not any(n in names for n in ("research/agent.py", "research/tools.py", "research/prompts.py", "research/proposal.py", "main.py"))
    assert not any(part in n for n in names for part in ("/web/", "snapshot", "tests/", "__pycache__", ".pyc", "strands"))


def test_the_handlers_import_from_the_bundle_alone(bundle, tmp_path):
    """Run in a directory far from the checkout, so a module missing from the bundle cannot be picked up from the repo."""
    _, out = bundle
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import portal_pipeline.handlers as H, research.apply as A, research.portal.protected as G; "
            "assert all(m.__file__.startswith(sys.argv[1]) for m in (H, A, G)), (H.__file__, A.__file__); "
            "print(','.join(sorted(n for n in ('start_research', 'check_research', 'publish', 'report_failure') if callable(getattr(H, n)))))")
    proc = subprocess.run([sys.executable, "-c", code, str(out / "pipeline")], capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "check_research,publish,report_failure,start_research"


def test_the_same_sources_give_the_same_zip(bundle, tmp_path):
    zip_path, _ = bundle
    again = B.build(tmp_path, deps=False)
    assert again.name == zip_path.name and again.read_bytes() == zip_path.read_bytes()
    assert zip_path.name.startswith("pipeline-") and len(zip_path.stem.split("-")[1]) == 16


def test_dependencies_are_installed_for_the_lambda_platform_not_this_machine(tmp_path):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def runner(cmd, **kw):
        calls.append(cmd)
        return Done()

    B.build(tmp_path, deps=True, arch="arm64", python_version="3.13", runner=runner)
    cmd = " ".join(calls[0])
    assert ("aarch64-manylinux2014" in cmd or "manylinux2014_aarch64" in cmd) and "3.13" in cmd and "requirements.txt" in cmd
    assert "--only-binary" in cmd  # never compile on the build machine: the result would not run on Lambda
    assert (tmp_path / "pipeline" / "portal_pipeline" / "handlers.py").is_file()  # our sources go in after the dependencies


def test_a_failed_download_is_reported_not_packaged(tmp_path):
    class Failed:
        returncode, stdout, stderr = 1, "", "error: No solution found: jsonschema"

    with pytest.raises(B.ScriptError, match="PyPI"):
        B.build(tmp_path, deps=True, runner=lambda cmd, **kw: Failed())


def test_upload_puts_the_zip_under_the_prefix_and_prints_the_stack_parameters(bundle, capsys, tmp_path):
    zip_path, _ = bundle

    class S3:
        def __init__(self):
            self.puts = []

        def put_object(self, **kw):
            self.puts.append(kw)

    s3 = S3()
    assert B.main(["--no-deps", "--out", str(tmp_path), "--upload", "s3://my-data-bucket/artifacts"], client=s3) == 0
    out = capsys.readouterr().out
    assert s3.puts[0]["Bucket"] == "my-data-bucket" and s3.puts[0]["Key"] == f"artifacts/{zip_path.name}"
    assert f"CodeBucket=my-data-bucket CodeKey=artifacts/{zip_path.name}" in out and "不能部署" in out
    assert B.main(["--no-deps", "--out", str(tmp_path), "--upload", "not-an-s3-uri"], client=s3) == 3
