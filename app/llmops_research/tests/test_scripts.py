"""scripts/: apply_proposal.py (git + gh), sync_portal_data.py (S3 / snapshot) and invoke_agent.py (boto3 data plane).

Everything runs offline: a throw-away git repo with a local bare "origin", a stub ``gh``, a stub S3 client and a fake
``bedrock-agentcore`` client that serves the real SSE bytes produced by main.py (so the whole chain
invoke -> result file -> apply -> branch/commit/PR is exercised on a genuine agent response).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_proposal as AP  # noqa: E402
import invoke_agent as IA  # noqa: E402
import main as agent_main  # noqa: E402
import sync_portal_data as SP  # noqa: E402

from e2e_helpers import FOUNDRY_HTML, FOUNDRY_URL, Site, foundry_script, settings  # noqa: E402
from research import apply as A  # noqa: E402
from research.agent import run_once  # noqa: E402
from research.offline.scripted_model import ScriptedModel  # noqa: E402
from research.portal.data import BUNDLED_DIR, PortalData  # noqa: E402
from research.portal.patch import get_at  # noqa: E402
from research.render import render_pr_body  # noqa: E402
from research.web.search_backend import FakeSearchBackend  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/LlmopsResearch_llmops_research-AbCdEf1234"


# ======================================================================================= shared fixtures
@pytest.fixture(scope="module")
def accepted():
    site = Site().add(FOUNDRY_URL, FOUNDRY_HTML)
    res = asyncio.run(run_once({"prompt": "check foundry", "kind": "prompt"}, settings=settings(), model=ScriptedModel(foundry_script()),
                               fetcher=site.fetcher(), search=FakeSearchBackend({})))
    assert res["status"] == "accepted"
    return res


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "portal"
    shutil.copytree(BUNDLED_DIR, root, ignore=shutil.ignore_patterns("snapshot_manifest.json"))
    return root


@pytest.fixture()
def proposal_file(tmp_path, accepted):
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(accepted["proposal"], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.relative_to(root).parts}


def git(cwd, *args, check=True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


@pytest.fixture()
def repo(tmp_path, checkout, monkeypatch):
    """The portal checkout as a git repo on `main` with a local bare `origin` (nothing from the developer's own git config)."""
    for key, value in {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                       "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(checkout, "init", "-b", "main")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "initial portal data")
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "-u", "origin", "main")
    return checkout


@pytest.fixture()
def fake_gh(tmp_path, monkeypatch):
    """A `gh` that records how it was called and prints a PR URL."""
    log = tmp_path / "gh-calls.jsonl"
    exe = tmp_path / "bin" / "gh"
    exe.parent.mkdir()
    exe.write_text(textwrap.dedent("""\
        #!__PY__
        import json, sys
        args = sys.argv[1:]
        body = open(args[args.index("--body-file") + 1], encoding="utf-8").read() if "--body-file" in args else ""
        with open(__LOG__, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "body": body}) + "\\n")
        print("https://github.com/example/portal/pull/7")
        """).replace("__PY__", sys.executable).replace("__LOG__", repr(str(log))))
    exe.chmod(0o755)
    monkeypatch.setenv("GH_BIN", str(exe))
    return log


def gh_calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


# ======================================================================================= apply_proposal.py
def test_preview_shows_the_diff_and_changes_nothing(checkout, proposal_file, capsys):
    before = tree_hash(checkout)
    assert AP.main([str(proposal_file), "--portal", str(checkout)]) == 0
    out = capsys.readouterr().out
    assert "+++ b/data/lifecycle.json" in out and "2026-11-30" in out and "這是預覽" in out
    assert "SAMPLE" in out  # the proposal was made against the bundled sample, and says so
    assert tree_hash(checkout) == before


def test_sample_based_proposals_need_an_explicit_flag_to_be_written(checkout, proposal_file, capsys):
    before = tree_hash(checkout)
    assert AP.main([str(proposal_file), "--portal", str(checkout), "--write"]) == 3
    assert "--allow-sample" in capsys.readouterr().err
    assert tree_hash(checkout) == before

    assert AP.main([str(proposal_file), "--portal", str(checkout), "--write", "--allow-sample"]) == 0
    after = PortalData.from_dir(checkout)
    assert get_at(after.docs, "data/lifecycle.json", "/events/azure-claude-sonnet-4-5-eol/date") == "2026-11-30"
    changed = {k for k, v in tree_hash(checkout).items() if before.get(k) != v}
    assert changed == {"data/lifecycle.json", "data/sources.json"}  # the schema and every other data file are untouched


def test_applying_the_same_proposal_twice_is_reported_as_already_applied(checkout, proposal_file, capsys):
    assert AP.main([str(proposal_file), "--portal", str(checkout), "--write", "--allow-sample"]) == 0
    capsys.readouterr()
    assert AP.main([str(proposal_file), "--portal", str(checkout)]) == 0
    assert "已經都在" in capsys.readouterr().out
    assert A.already_applied(PortalData.from_dir(checkout), json.loads(proposal_file.read_text(encoding="utf-8")))


def test_a_proposal_is_not_reported_as_applied_before_it_is(checkout, accepted):
    assert not A.already_applied(PortalData.from_dir(checkout), accepted["proposal"])
    assert not A.already_applied(PortalData.from_dir(checkout), {"schema": "llmops-research-proposal/v1"})
    assert not A.already_applied(PortalData.from_dir(checkout), {"schema": "other"})


def test_a_portal_that_moved_on_blocks_the_proposal_without_writing(checkout, proposal_file, capsys):
    path = checkout / "data" / "lifecycle.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    next(e for e in doc["events"] if e["id"] == "azure-claude-sonnet-4-5-eol")["date"] = "2026-12-01"  # someone changed it meanwhile
    path.write_text(A.dump_json(doc), encoding="utf-8")
    before = tree_hash(checkout)
    for extra in ([], ["--write", "--allow-sample"]):
        assert AP.main([str(proposal_file), "--portal", str(checkout), *extra]) == 1
        out = capsys.readouterr().out
        assert "不能套用" in out and "re-run the research" in out and "2026-12-01" in out
    assert tree_hash(checkout) == before


def test_a_run_record_and_an_s3_location_are_both_accepted(checkout, accepted, monkeypatch, capsys):
    body = json.dumps(accepted, ensure_ascii=False).encode("utf-8")  # the whole runs/<runId>.json record

    class S3:
        def get_object(self, Bucket, Key):
            assert (Bucket, Key) == ("my-bucket", "research/runs/r1.json")
            return {"Body": io.BytesIO(body)}

    monkeypatch.setattr(AP, "_s3_client", lambda: S3())
    assert AP.main(["s3://my-bucket/research/runs/r1.json", "--portal", str(checkout)]) == 0
    assert "+++ b/data/lifecycle.json" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(body.decode("utf-8")))
    assert AP.main(["-", "--portal", str(checkout), "--no-diff"]) == 0


@pytest.mark.parametrize("doc, needle", [
    ({"type": "result", "status": "no_proposal", "error": "validator rejected the claim"}, "validator rejected the claim"),
    ({"type": "accepted", "runId": "r1", "resultUri": "s3://b/research/runs/r1.json"}, "s3://b/research/runs/r1.json"),
    ({"hello": "world"}, "proposal"),
    ([1, 2], "JSON"),
])
def test_inputs_that_are_not_a_proposal_are_explained(tmp_path, checkout, capsys, doc, needle):
    path = tmp_path / "x.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert AP.main([str(path), "--portal", str(checkout)]) == 3
    assert needle in capsys.readouterr().err


def test_environment_problems_are_exit_3_and_never_a_traceback(tmp_path, proposal_file, checkout, accepted, capsys):
    assert AP.main([str(proposal_file), "--portal", str(tmp_path / "nothing-here")]) == 3
    assert "不像 portal repo" in capsys.readouterr().err
    assert AP.main([str(tmp_path / "missing.json"), "--portal", str(checkout)]) == 3
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert AP.main([str(bad), "--portal", str(checkout)]) == 3
    evil = json.loads(json.dumps(accepted["proposal"]))
    evil["id"] = "prop-1; rm -rf ~"
    bad.write_text(json.dumps(evil), encoding="utf-8")
    assert AP.main([str(bad), "--portal", str(checkout)]) == 3
    assert "id 不合法" in capsys.readouterr().err
    assert AP.main([str(proposal_file), "--portal", str(checkout), "--validate"]) == 3  # --validate needs --write


def test_the_pr_body_can_be_written_without_touching_the_portal(checkout, proposal_file, tmp_path):
    before = tree_hash(checkout)
    body = tmp_path / "pr.md"
    assert AP.main([str(proposal_file), "--portal", str(checkout), "--pr-body", str(body), "--no-diff"]) == 0
    text = body.read_text(encoding="utf-8")
    assert text.startswith("## Research agent proposal `prop-") and "npm run validate" in text
    assert tree_hash(checkout) == before


def test_branch_commit_push_and_draft_pr(repo, proposal_file, fake_gh, capsys):
    pid = json.loads(proposal_file.read_text(encoding="utf-8"))["id"]
    # an unrelated change that is already staged must not be swept into the research commit
    (repo / "NOTES.md").write_text("unrelated\n", encoding="utf-8")
    git(repo, "add", "NOTES.md")
    assert AP.main([str(proposal_file), "--portal", str(repo), "--open-pr", "--allow-sample", "--base", "main"]) == 0
    out = capsys.readouterr().out
    assert "https://github.com/example/portal/pull/7" in out and "draft PR" in out

    assert git(repo, "branch", "--show-current") == f"research/{pid}"
    assert set(git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) == {"data/lifecycle.json", "data/sources.json"}
    assert git(repo, "status", "--porcelain") == "A  NOTES.md"  # still staged, still not committed
    message = git(repo, "log", "-1", "--format=%B")
    assert message.startswith(f"data: research proposal {pid}") and "data/lifecycle.json" in message
    assert "Co-Authored-By" not in message and "Claude" not in message  # the human's commit, not ours
    assert f"refs/heads/research/{pid}" in git(repo, "ls-remote", "--heads", "origin")

    (call,) = gh_calls(fake_gh)
    args = call["args"]
    assert args[:2] == ["pr", "create"] and "--draft" in args
    assert args[args.index("--head") + 1] == f"research/{pid}" and args[args.index("--base") + 1] == "main"
    assert args[args.index("--title") + 1] == f"data: research proposal {pid}"
    assert call["body"].startswith("## Research agent proposal") and "claude-sonnet-4-5 | 1 | GA | 2026-11-30" in call["body"]


def test_ready_flag_opens_a_regular_pr(repo, proposal_file, fake_gh):
    assert AP.main([str(proposal_file), "--portal", str(repo), "--open-pr", "--allow-sample", "--ready"]) == 0
    assert "--draft" not in gh_calls(fake_gh)[0]["args"]


def test_a_custom_branch_name_and_commit_without_a_pr(repo, proposal_file, fake_gh):
    assert AP.main([str(proposal_file), "--portal", str(repo), "--branch", "data/foundry-fix", "--commit", "--allow-sample"]) == 0
    assert git(repo, "branch", "--show-current") == "data/foundry-fix"
    assert git(repo, "status", "--porcelain") == ""
    assert gh_calls(fake_gh) == []
    assert "data/foundry-fix" not in git(repo, "ls-remote", "--heads", "origin")  # not pushed without --open-pr


def test_a_failing_validation_leaves_nothing_behind(repo, proposal_file, capsys):
    before = tree_hash(repo)
    command = f"{sys.executable} -c \"import sys; print('schema check failed'); sys.exit(1)\""
    rc = AP.main([str(proposal_file), "--portal", str(repo), "--commit", "--validate", "--validate-cmd", command, "--allow-sample"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "驗證失敗" in out and "schema check failed" in out
    assert tree_hash(repo) == before and git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--show-current") == "main" and git(repo, "branch", "--list", "research/*") == ""


def test_a_passing_validation_goes_on_to_commit(repo, proposal_file, capsys):
    command = f"{sys.executable} -c \"print('all good')\""
    assert AP.main([str(proposal_file), "--portal", str(repo), "--commit", "--validate", "--validate-cmd", command, "--allow-sample"]) == 0
    assert "驗證通過" in capsys.readouterr().out and git(repo, "log", "-1", "--format=%s").startswith("data: research proposal")


def test_problems_found_before_anything_is_written_leave_the_repo_alone(repo, proposal_file, monkeypatch, capsys):
    pid = json.loads(proposal_file.read_text(encoding="utf-8"))["id"]
    before = tree_hash(repo)
    git(repo, "branch", f"research/{pid}")  # the branch name is taken
    assert AP.main([str(proposal_file), "--portal", str(repo), "--branch", "--allow-sample"]) == 3
    assert "已存在" in capsys.readouterr().err

    git(repo, "branch", "-D", f"research/{pid}")
    monkeypatch.setenv("GH_BIN", str(repo / "no-such-gh"))  # --open-pr needs gh
    assert AP.main([str(proposal_file), "--portal", str(repo), "--open-pr", "--allow-sample"]) == 3
    assert "gh" in capsys.readouterr().err
    assert AP.main([str(proposal_file), "--portal", str(repo), "--branch", "bad name~", "--allow-sample"]) == 3
    assert tree_hash(repo) == before and git(repo, "branch", "--show-current") == "main"
    assert git(repo, "branch", "--list", "research/*") == ""


def test_uncommitted_edits_to_a_target_file_are_refused(repo, proposal_file, capsys):
    path = repo / "data" / "lifecycle.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # dirty, though the JSON is the same
    assert AP.main([str(proposal_file), "--portal", str(repo), "--branch", "--allow-sample"]) == 3
    assert "尚未 commit" in capsys.readouterr().err and git(repo, "branch", "--show-current") == "main"
    assert AP.main([str(proposal_file), "--portal", str(repo), "--write", "--allow-sample"]) == 3
    assert A.dump_json(json.loads(path.read_text(encoding="utf-8"))) != path.read_text(encoding="utf-8")  # untouched


def test_branching_needs_a_git_repo(checkout, proposal_file, capsys):
    assert AP.main([str(proposal_file), "--portal", str(checkout), "--branch", "--allow-sample"]) == 3
    assert "不是 git" in capsys.readouterr().err


def test_the_proposal_says_which_portal_commit_it_was_made_against(repo, accepted, tmp_path, capsys):
    proposal = json.loads(json.dumps(accepted["proposal"]))
    head = git(repo, "rev-parse", "HEAD")
    proposal["portal"]["manifest"] = {"kind": "s3", "source": "s3://b/portal/", "commit": head, "syncedAt": "2026-10-01T00:00:00Z"}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    assert AP.main([str(path), "--portal", str(repo), "--no-diff"]) == 0
    assert "ℹ" not in capsys.readouterr().out  # same commit: nothing to say

    (repo / "docs.md").write_text("x\n", encoding="utf-8")
    git(repo, "add", "docs.md")
    git(repo, "commit", "-m", "later")
    assert AP.main([str(path), "--portal", str(repo), "--no-diff"]) == 0
    assert "之後又前進" in capsys.readouterr().out
    proposal["portal"]["manifest"]["commit"] = "0" * 40
    path.write_text(json.dumps(proposal), encoding="utf-8")
    assert AP.main([str(path), "--portal", str(repo), "--no-diff"]) == 0
    assert "不在目前 checkout" in capsys.readouterr().out
    body = render_pr_body(proposal)
    assert "基準資料" in body and head[:10] not in body and "0" * 10 in body


def test_a_checkout_with_unusual_formatting_is_flagged_before_writing(checkout, proposal_file, capsys):
    path = checkout / "data" / "lifecycle.json"
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=4, ensure_ascii=False), encoding="utf-8")
    assert AP.main([str(proposal_file), "--portal", str(checkout), "--no-diff"]) == 0
    out = capsys.readouterr().out
    assert "排版" in out and "data/lifecycle.json" in out.split("排版")[-1]


# ======================================================================================= sync_portal_data.py
class S3Stub:
    """put/get/list/delete over a dict, with ETags like S3 (MD5 of the body for single-part objects)."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.puts: list[dict] = []
        self.deleted: list[str] = []

    def get_paginator(self, _name):
        outer = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "ETag": '"%s"' % hashlib.md5(outer.objects[k]).hexdigest()}
                                    for k in sorted(outer.objects) if k.startswith(Prefix)]}

        return Pages()

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture()
def portal_repo(repo):
    """A portal checkout that also holds things the agent must never be given."""
    (repo / "data" / "internal").mkdir()
    (repo / "data" / "internal" / "inventory.json").write_text('{"secret": "platform inventory"}', encoding="utf-8")
    (repo / "data" / "notes.txt").write_text("not json", encoding="utf-8")
    (repo / "runs").mkdir()
    (repo / "runs" / "2026-10-01.json").write_text('{"secret": "test results"}', encoding="utf-8")
    (repo / "README.md").write_text("# portal\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "internal files")
    return repo


def test_s3_sync_uploads_only_what_the_agent_reads_plus_a_manifest(portal_repo, capsys):
    s3 = S3Stub()
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://my-bucket/portal"], client=s3) == 0
    keys = set(s3.objects)
    assert "portal/schema/llmops.schema.json" in keys and "portal/data/lifecycle.json" in keys and "portal/sync_manifest.json" in keys
    assert not any("internal" in k or "/runs/" in k or k.endswith((".md", ".txt")) for k in keys)
    assert all(p["ContentType"] == "application/json" and p["Bucket"] == "my-bucket" for p in s3.puts)
    manifest = json.loads(s3.objects["portal/sync_manifest.json"])
    assert manifest["commit"] == git(portal_repo, "rev-parse", "HEAD") and manifest["dirty"] is False and manifest["syncedAt"].endswith("Z")
    assert "PORTAL_DATA_URI" in capsys.readouterr().out
    # the agent can read exactly this back, and learns which commit it came from
    portal = PortalData.from_s3("s3://my-bucket/portal/", client=s3)
    assert portal.manifest["commit"] == manifest["commit"] and portal.manifest["kind"] == "s3" and not portal.is_sample
    assert not any("internal" in k for k in portal.docs)
    assert portal.docs["data/lifecycle.json"] == PortalData.from_dir(portal_repo).docs["data/lifecycle.json"]


def test_a_second_sync_only_uploads_what_changed(portal_repo):
    s3 = S3Stub()
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3) == 0
    s3.puts.clear()
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3) == 0
    assert [p["Key"] for p in s3.puts] == ["portal/sync_manifest.json"]
    path = portal_repo / "data" / "litellm.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # different bytes, same JSON
    s3.puts.clear()
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3) == 0
    assert sorted(p["Key"] for p in s3.puts) == ["portal/data/litellm.json", "portal/sync_manifest.json"]
    assert json.loads(s3.objects["portal/sync_manifest.json"])["dirty"] is True  # the working tree differs from HEAD


def test_delete_removes_only_portal_data_keys_that_are_gone_from_the_repo(portal_repo):
    s3 = S3Stub({"portal/data/digests/old.json": b"{}", "portal/runs/keep.json": b"{}", "portal/notes.txt": b"x",
                 "other/data/digests/old.json": b"{}", "portal/data/internal/inventory.json": b"{}"})
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3) == 0
    assert s3.deleted == []  # opt-in
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/", "--delete"], client=s3) == 0
    assert s3.deleted == ["portal/data/digests/old.json"]
    assert {"portal/runs/keep.json", "portal/notes.txt", "other/data/digests/old.json", "portal/data/internal/inventory.json"} <= set(s3.objects)


def test_dry_run_writes_nothing(portal_repo, capsys):
    s3 = S3Stub({"portal/data/digests/old.json": b"{}"})
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/", "--dry-run", "--delete"], client=s3) == 0
    out = capsys.readouterr().out
    assert s3.puts == [] and s3.deleted == [] and "預覽" in out and "- data/digests/old.json" in out


def test_an_invalid_json_file_stops_the_sync_before_any_upload(portal_repo, capsys):
    (portal_repo / "data" / "catalog.json").write_text("{broken", encoding="utf-8")
    s3 = S3Stub()
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3) == 3
    assert "data/catalog.json" in capsys.readouterr().err and s3.puts == []


def test_s3_errors_are_reported_not_raised(portal_repo, capsys):
    class Denied(S3Stub):
        def put_object(self, **kw):
            raise PermissionError("AccessDenied")

    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=Denied()) == 3
    assert "AccessDenied" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--to", "ftp://x"], ["--to", "s3://b/p/", "--dir", "/tmp/x"]])
def test_bad_destinations_are_refused(portal_repo, argv, capsys):
    assert SP.main(["--portal", str(portal_repo), *argv], client=S3Stub()) == 3


def test_snapshot_target_replaces_the_bundled_sample_in_a_copy(portal_repo, tmp_path, capsys):
    dest = tmp_path / "snapshot"
    shutil.copytree(BUNDLED_DIR, dest)
    stale = dest / "data" / "digests" / "2099-01-01.json"
    stale.write_text("{}", encoding="utf-8")  # a file the real portal does not have
    assert PortalData.from_dir(dest).is_sample
    assert SP.main(["--portal", str(portal_repo), "--to", "snapshot", "--dir", str(dest)]) == 0
    portal = PortalData.from_dir(dest)
    assert portal.manifest["kind"] == "snapshot" and not portal.is_sample and portal.manifest["commit"] == git(portal_repo, "rev-parse", "HEAD")
    assert not stale.exists() and not (dest / "data" / "internal").exists()
    assert portal.docs["data/lifecycle.json"] == PortalData.from_dir(portal_repo).docs["data/lifecycle.json"]
    assert "agentcore deploy" in capsys.readouterr().out


def test_the_real_bundled_snapshot_is_never_touched_by_these_tests():
    assert PortalData.from_dir(BUNDLED_DIR).is_sample


def test_snapshot_will_not_mirror_into_an_unrelated_folder_or_the_portal_itself(portal_repo, tmp_path, capsys):
    unrelated = tmp_path / "Documents"
    unrelated.mkdir()
    (unrelated / "thesis.docx").write_text("precious", encoding="utf-8")
    assert SP.main(["--portal", str(portal_repo), "--to", "snapshot", "--dir", str(unrelated)]) == 3
    assert (unrelated / "thesis.docx").exists() and "不像 snapshot" in capsys.readouterr().err
    assert SP.main(["--portal", str(portal_repo), "--to", "snapshot", "--dir", str(portal_repo)]) == 3
    assert SP.main(["--portal", str(portal_repo), "--to", "snapshot", "--dir", str(tmp_path / "fresh")]) == 0  # a new folder is fine


def test_a_dirty_portal_is_recorded_in_the_manifest_and_warned_about(portal_repo, capsys):
    path = portal_repo / "data" / "requirements.json"
    if path.exists():
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    else:
        (portal_repo / "data" / "digests" / "2099-01-01.json").write_text("{}", encoding="utf-8")
    s3 = S3Stub()
    assert SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3) == 0
    assert "尚未 commit" in capsys.readouterr().out and json.loads(s3.objects["portal/sync_manifest.json"])["dirty"] is True


def test_a_malformed_sync_manifest_does_not_stop_a_run(portal_repo):
    s3 = S3Stub()
    SP.main(["--portal", str(portal_repo), "--to", "s3://b/portal/"], client=s3)
    s3.objects["portal/sync_manifest.json"] = b"{not json"
    portal = PortalData.from_s3("s3://b/portal/", client=s3)
    assert portal.manifest == {"kind": "s3", "source": "s3://b/portal/"}


# ======================================================================================= invoke_agent.py
class FakeBody:
    def __init__(self, data: bytes):
        self.data = data
        self.chunk_sizes: list[int] = []

    def iter_lines(self, chunk_size=1024, keepends=False):
        self.chunk_sizes.append(chunk_size)
        yield from self.data.split(b"\n")

    def read(self):
        return self.data


class FakeAgentCore:
    """The data-plane client, answering with bytes the real main.py produced."""

    def __init__(self, body: bytes, content_type: str = "text/event-stream", error: Exception | None = None):
        self.body, self.content_type, self.error = body, content_type, error
        self.calls: list[dict] = []
        self.stopped: list[dict] = []
        self.response_body: FakeBody | None = None

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        self.response_body = FakeBody(self.body)
        return {"response": self.response_body, "contentType": self.content_type, "runtimeSessionId": kw["runtimeSessionId"], "statusCode": 200}

    def stop_runtime_session(self, **kw):
        self.stopped.append(kw)


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    """main.py's app (offline scripted model) behind Starlette's TestClient, plus a helper that returns its raw SSE bytes."""
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("OUTPUT_URI", str(tmp_path / "output"))
    monkeypatch.delenv("SCRIPT_FILE", raising=False)
    monkeypatch.setattr(agent_main, "_active_runs", 0)
    client = TestClient(agent_main.app)

    def call(payload: dict) -> bytes:
        return client.post("/invocations", json=payload).content

    call.client = client  # type: ignore[attr-defined]
    return call


def wait_idle(client, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if client.get("/ping").json()["status"] == "Healthy":
            return
        time.sleep(0.05)
    raise AssertionError("runtime still busy")


def test_a_run_is_shown_saved_and_its_session_stopped(agent, tmp_path, capsys):
    fake = FakeAgentCore(agent({"prompt": "weekly"}))
    out = tmp_path / "result.json"
    assert IA.main(["--arn", ARN, "--prompt", "weekly", "--out", str(out)], client=fake, environ={}) == 0
    shown = capsys.readouterr()
    assert "▶ run" in shown.out and "→ lookup_portal" in shown.out and "通過驗證" in shown.out  # what a person watching sees

    (call,) = fake.calls
    assert call["agentRuntimeArn"] == ARN and call["accept"] == "text/event-stream" and call["contentType"] == "application/json"
    assert json.loads(call["payload"]) == {"prompt": "weekly"} and isinstance(call["payload"], bytes)
    assert call["runtimeSessionId"].startswith("llmops-research-") and len(call["runtimeSessionId"]) >= 33
    assert fake.response_body.chunk_sizes == [1]  # line-by-line, so progress shows as it happens
    assert fake.stopped == [{"agentRuntimeArn": ARN, "runtimeSessionId": call["runtimeSessionId"]}]

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["type"] == "result" and saved["status"] == "accepted" and "event" not in saved  # no display hint in the file
    assert saved["proposal"]["schema"] == "llmops-research-proposal/v1"


def test_the_result_file_goes_straight_into_apply_proposal(agent, tmp_path, checkout, capsys):
    fake = FakeAgentCore(agent({"prompt": "weekly"}))
    out = tmp_path / "result.json"
    assert IA.main(["--arn", ARN, "--prompt", "weekly", "--out", str(out)], client=fake, environ={}) == 0
    capsys.readouterr()
    assert AP.main([str(out), "--portal", str(checkout), "--no-diff"]) == 0
    printed = capsys.readouterr().out
    assert "proposal prop-" in printed and "沒有需要變更" in printed  # the offline smoke run proposes nothing; the record itself is understood


def test_a_background_run_is_accepted_and_its_session_is_left_running(agent, tmp_path, capsys):
    fake = FakeAgentCore(agent({"prompt": "weekly", "background": True}))
    out = tmp_path / "accepted.json"
    assert IA.main(["--arn", ARN, "--prompt", "weekly", "--background", "--out", str(out)], client=fake, environ={}) == 0
    wait_idle(agent.client)
    assert json.loads(fake.calls[0]["payload"]) == {"prompt": "weekly", "background": True}
    assert fake.stopped == []  # stopping the session would kill the background run
    accepted = json.loads(out.read_text(encoding="utf-8"))
    assert accepted["type"] == "accepted" and accepted["resultUri"].endswith(f"runs/{accepted['runId']}.json")
    assert accepted["resultUri"] in capsys.readouterr().err
    assert Path(accepted["resultUri"]).exists()  # the run finished and left its record


def test_raw_mode_prints_one_json_event_per_line(agent, capsys):
    fake = FakeAgentCore(agent({"prompt": "weekly"}))
    assert IA.main(["--arn", ARN, "--prompt", "weekly", "--raw", "--keep-session"], client=fake, environ={}) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "result" and fake.stopped == []


def test_an_error_result_is_exit_5(agent, capsys):
    payload = {"messages": []}  # the agent refuses a caller-supplied message history
    fake = FakeAgentCore(agent(payload))
    assert IA.main(["--arn", ARN, "--payload-json", json.dumps(payload)], client=fake, environ={}) == 5
    assert "執行失敗" in capsys.readouterr().out


def test_a_run_without_a_proposal_is_exit_4():
    body = b'data: {"type": "result", "status": "no_proposal", "error": "nothing verifiable", "runId": "r"}\n\n'
    fake = FakeAgentCore(body)
    assert IA.main(["--arn", ARN, "--prompt", "x"], client=fake, environ={}) == 4


def test_payload_sources_and_validation(tmp_path):
    f = tmp_path / "p.json"
    f.write_text('{"kind": "focus", "focus": ["bedrock-lifecycle"]}', encoding="utf-8")
    body = b'data: {"type": "result", "status": "accepted", "runId": "r"}\n\n'
    fake = FakeAgentCore(body)
    assert IA.main(["--arn", ARN, "--payload-file", str(f), "--background"], client=fake, environ={}) == 0
    assert json.loads(fake.calls[0]["payload"]) == {"kind": "focus", "focus": ["bedrock-lifecycle"], "background": True}
    assert IA.main(["--arn", ARN, "--payload-json", "[1]"], client=fake, environ={}) == 3
    assert IA.main(["--arn", ARN, "--payload-json", "{oops"], client=fake, environ={}) == 3
    assert IA.main(["--arn", ARN, "--payload-file", str(tmp_path / "none.json")], client=fake, environ={}) == 3
    with pytest.raises(SystemExit) as e:  # nothing to send / two things to send: argparse usage errors
        IA.main(["--arn", ARN], client=fake, environ={})
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        IA.main(["--arn", ARN, "--prompt", "a", "--payload-json", "{}"], client=fake, environ={})
    assert IA.main(["--arn", ARN, "--prompt", "a", "--session-id", "short"], client=fake, environ={}) == 2


def test_the_runtime_arn_comes_from_a_flag_the_environment_or_the_deployed_state(tmp_path):
    other = ARN.replace("AbCdEf1234", "Zz9")
    assert IA.resolve_arn(ARN, {"AGENT_RUNTIME_ARN": other}) == ARN
    assert IA.resolve_arn(None, {"AGENT_RUNTIME_ARN": other}) == other
    state = tmp_path / "deployed-state.json"
    state.write_text(json.dumps({"targets": {"default": {"resources": {"runtimes": {"llmops_research": {"runtimeArn": ARN, "endpointArn": ARN + "/runtime-endpoint/DEFAULT"}}}}}}))
    assert IA.resolve_arn(None, {}, state) == ARN  # the endpoint ARN contains the runtime ARN: still one runtime
    state.write_text(json.dumps({"a": ARN, "b": other}))
    with pytest.raises(IA.ScriptError, match="多個"):
        IA.resolve_arn(None, {}, state)
    state.write_text("{}")
    with pytest.raises(IA.ScriptError, match="找不到 runtime ARN"):
        IA.resolve_arn(None, {}, state)
    with pytest.raises(IA.ScriptError, match="不像"):
        IA.resolve_arn("arn:aws:s3:::bucket", {})


def test_the_region_comes_from_the_arn(monkeypatch):
    seen = {}

    def fake_client(region, timeout):
        seen.update(region=region, timeout=timeout)
        return FakeAgentCore(b'data: {"type": "result", "status": "accepted", "runId": "r"}\n\n')

    monkeypatch.setattr(IA, "make_client", fake_client)
    assert IA.main(["--arn", ARN, "--prompt", "x", "--timeout", "45"], environ={}) == 0
    assert seen == {"region": "ap-northeast-1", "timeout": 45}


def test_the_real_client_is_built_without_retries_and_with_a_read_timeout():
    client = IA.make_client("ap-northeast-1", 77)
    assert client.meta.service_model.service_name == "bedrock-agentcore"
    retries = client.meta.config.retries
    assert client.meta.config.read_timeout == 77 and (retries.get("max_attempts") == 0 or retries.get("total_max_attempts") == 1), retries  # a replayed call = a second research run
    assert "invoke_agent_runtime" in dir(client) and "stop_runtime_session" in dir(client)


@pytest.mark.parametrize("error, needle", [
    (type("ClientError", (Exception,), {"response": {"Error": {"Code": "AccessDeniedException"}}})("denied"), "InvokeAgentRuntime"),
    (type("ClientError", (Exception,), {"response": {"Error": {"Code": "ResourceNotFoundException"}}})("gone"), "READY"),
    (type("ReadTimeoutError", (Exception,), {})("Read timeout on endpoint URL"), "--background"),
])
def test_invocation_failures_get_a_hint_and_exit_3(error, needle, capsys):
    assert IA.main(["--arn", ARN, "--prompt", "x"], client=FakeAgentCore(b"", error=error), environ={}) == 3
    assert needle in capsys.readouterr().err


def test_a_stream_that_ends_early_is_not_mistaken_for_success(capsys):
    fake = FakeAgentCore(b'data: {"type": "run_started", "runId": "r"}\n\n')
    assert IA.main(["--arn", ARN, "--prompt", "x"], client=fake, environ={}) == 3
    assert "沒有收到最後的 result" in capsys.readouterr().err and fake.stopped == []


def test_a_plain_json_reply_is_understood_too(capsys):
    body = json.dumps({"type": "result", "status": "error", "error": "invalid request: x"}).encode()
    assert IA.main(["--arn", ARN, "--prompt", "x"], client=FakeAgentCore(body, content_type="application/json"), environ={}) == 5


def test_stopping_the_session_is_best_effort(capsys):
    class NoStop(FakeAgentCore):
        def stop_runtime_session(self, **kw):
            raise PermissionError("AccessDenied")

    body = b'data: {"type": "result", "status": "accepted", "runId": "r"}\n\n'
    assert IA.main(["--arn", ARN, "--prompt", "x"], client=NoStop(body), environ={}) == 0
    assert "閒置逾時" in capsys.readouterr().err


# ======================================================================================= the scripts as programs
@pytest.mark.parametrize("script", ["apply_proposal.py", "sync_portal_data.py", "invoke_agent.py"])
def test_scripts_run_from_any_directory(script, tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"], cwd=tmp_path, capture_output=True, encoding="utf-8")
    assert proc.returncode == 0 and "usage:" in proc.stdout and "Exit code" in proc.stdout


def test_apply_proposal_as_a_program_end_to_end(checkout, proposal_file, tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPTS / "apply_proposal.py"), str(proposal_file), "--portal", str(checkout), "--no-diff"],
                          cwd=tmp_path, capture_output=True, encoding="utf-8")
    assert proc.returncode == 0 and "可以套用：2 個檔案會變更" in proc.stdout
