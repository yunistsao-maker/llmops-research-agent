"""The operator's CLI, driven the way a person would use it (against a folder instead of S3, and a fake Step Functions)."""

from __future__ import annotations

import datetime as dt
import io
import json

import pytest
from conftest import COMPAT, FIXTURES, PERF, snapshot_tree, squash, tool_file, write_tree

import portalctl as CTL
from portal_pipeline import notify as N
from portal_pipeline import publish as P
from portal_pipeline import releases as R
from portal_pipeline.storage import DirStore

SFN_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:llmops-portal-pipeline-update"
PROP = "prop-20261002T030000Z-1169b48e"


class FakeSfn:
    def __init__(self, status="SUCCEEDED", output=None, error=None, cause=None):
        self.started: list[dict] = []
        self.status, self.output, self.error, self.cause = status, output, error, cause

    def start_execution(self, **kw):
        assert "\x1f" not in json.dumps(kw, ensure_ascii=False) and "\\u001f" not in json.dumps(kw), kw   # (see notify.MARK)
        self.started.append(kw)
        return {"executionArn": f"{kw['stateMachineArn'].replace(':stateMachine:', ':execution:')}:e{len(self.started)}"}

    def describe_execution(self, executionArn):
        out = {"status": self.status, "executionArn": executionArn}
        if self.output is not None:
            out["output"] = json.dumps(self.output, ensure_ascii=False)
        if self.error:
            out.update(error=self.error, cause=self.cause or "")
        return out

    def list_executions(self, stateMachineArn, maxResults):
        return {"executions": [{"status": "SUCCEEDED", "name": "weekly-1", "startDate": dt.datetime(2026, 10, 5, 1, 0, tzinfo=dt.timezone.utc)},
                               {"status": "FAILED", "name": "manual-2", "startDate": dt.datetime(2026, 10, 6, 2, 30, tzinfo=dt.timezone.utc)}]}


@pytest.fixture()
def env(tmp_path):
    """A portal checkout and an (empty) data location, plus a helper that runs one command."""
    checkout = write_tree(tmp_path / "llmops-portal", snapshot_tree())
    data = tmp_path / "bucket" / "portal"

    class Env:
        portal, store, control = checkout, DirStore(data), DirStore(tmp_path / "bucket" / "pipeline")
        sfn: FakeSfn | None = None

        def run(self, *argv, tty_answer=None, environ=None):
            out = io.StringIO()
            stdin = None
            if tty_answer is not None:
                stdin = io.StringIO(tty_answer + "\n")
                stdin.isatty = lambda: True  # type: ignore[method-assign]
            else:
                stdin = io.StringIO("")
            clients = {"stepfunctions": self.sfn} if self.sfn else {}
            environ = {"PORTAL_DATA_URI": str(data), "PORTALCTL_BY": "eunice", **(environ or {})}
            code = CTL.main(list(argv), environ=environ, clients=clients, out=out, stdin=stdin)
            self.out = out.getvalue()
            # the mark that stands around text from elsewhere until its line is laid out never reaches a reader
            assert N.MARK not in self.out and "\\x1f" not in self.out and "\\u001f" not in self.out, self.out
            return code

        def live(self):
            return R.load_current(self.store)

        def bootstrap(self):
            assert self.run("bootstrap", "--portal", str(self.portal), "--yes") == 0, self.out
            return self.live().release_id

    return Env()


BUCKET_URI = "s3://portal-data-bucket/portal/"


@pytest.fixture()
def cloud(tmp_path):
    """The same, with the data location on (moto's) S3 - where a state machine could be in charge of it."""
    moto = pytest.importorskip("moto")
    import boto3

    from portal_pipeline.storage import S3Store

    checkout = write_tree(tmp_path / "llmops-portal", snapshot_tree())
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="portal-data-bucket")

        class Cloud:
            portal, store, control = checkout, S3Store("portal-data-bucket", "portal/", s3), S3Store("portal-data-bucket", "pipeline/", s3)
            sfn = FakeSfn(output={"publish": {"status": "published"}, "notification": {"message": "ok"}})

            def run(self, *argv, environ=None):
                out = io.StringIO()
                environ = {"PORTAL_DATA_URI": BUCKET_URI, "PORTALCTL_BY": "eunice", **(environ or {})}
                code = CTL.main(list(argv), environ=environ, clients={"s3": s3, "stepfunctions": self.sfn}, out=out, stdin=io.StringIO(""))
                self.out = out.getvalue()
                return code

            def live(self):
                return R.load_current(self.store)

            def inputs(self):
                return [json.loads(s["input"]) for s in self.sfn.started]

        yield Cloud()


def lifecycle_date(files):
    return next(e for e in json.loads(files["data/lifecycle.json"])["events"] if e["id"] == "azure-claude-sonnet-4-5-eol")["date"]


# ---------------------------------------------------------------------------------------------- first release, status
def test_status_before_the_first_release_says_what_to_do(env):
    assert env.run("status") == 0 and "bootstrap" in env.out


def test_bootstrap_publishes_the_checkout_and_remembers_where_it_came_from(env, capsys):
    assert env.run("bootstrap", "--portal", str(env.portal), "--dry-run") == 0 and "預覽" in env.out and "沒有寫入任何東西" in env.out
    assert not env.store.exists("manifest.json")
    assert env.run("bootstrap", "--portal", str(env.portal), "--note", "初始資料", "--yes") == 0
    assert "沒有寫入任何東西" not in env.out and "預覽" not in env.out  # the listing before a real run does not claim to be a preview
    current = env.live()
    assert current.files == snapshot_tree() and current.manifest["source"] == {"kind": "bootstrap", "by": "eunice", "note": "初始資料"}
    assert json.loads((env.portal / ".portal-release.json").read_text())["releaseId"] == current.release_id
    assert "configure_agent.py --stack" in env.out and "config.json" in env.out  # what to do next, for the agent and for the UI
    assert env.run("bootstrap", "--portal", str(env.portal), "--yes") == 4 and "push" in env.out  # already bootstrapped

    assert env.run("status") == 0
    for expected in (current.release_id, "第 1 次發布", "第一個 release", "by eunice", "自動發布　正常", "待核准　　0 份", "1 份 digest"):
        assert expected in env.out, expected
    assert env.run("history") == 0 and current.release_id in env.out


def test_bootstrap_warns_about_a_leftover_flat_copy(env):
    env.store.put("data/lifecycle.json", b"{}")  # what scripts/sync_portal_data.py used to upload
    env.store.put("schema/llmops.schema.json", b"{}")
    assert env.run("bootstrap", "--portal", str(env.portal), "--yes") == 0
    assert "平面複本" in env.out and "aws s3 rm" in env.out


def test_a_folder_that_is_not_a_portal_checkout_is_refused(env, tmp_path, capsys):
    assert env.run("bootstrap", "--portal", str(tmp_path)) == 3
    assert "不像 portal repo" in capsys.readouterr().err


def test_without_a_data_location_nothing_is_guessed(env, capsys):
    assert CTL.main(["status"], environ={}, clients={}, out=io.StringIO(), stdin=io.StringIO("")) == 3
    assert "PORTAL_DATA_URI" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------- research results
def test_a_research_result_file_can_be_published_reviewed_approved_or_discarded(env):
    env.bootstrap()
    record = str(FIXTURES / "accepted_run.json")
    assert env.run("publish", record, "--mode", "review") == 4
    assert "需要核准" in env.out and f"approve {PROP}" in env.out and lifecycle_date(env.live().files) == "2026-11-15"
    assert env.run("pending") == 0 and PROP in env.out and "發布模式是 review" in env.out
    assert env.run("status") == 0 and f"待核准　　1 份：{PROP}" in env.out

    assert env.run("approve", PROP) == 0 and "已發布" in env.out and "~ data/lifecycle.json" in env.out
    assert lifecycle_date(env.live().files) == "2026-11-30" and env.live().manifest["source"]["approvedBy"] == "eunice"
    assert env.run("pending") == 0 and "沒有待核准的提案" in env.out
    assert env.run("publish", record) == 0 and "already_published" in env.out


def test_publishing_a_file_by_hand_puts_it_up_for_approval_unless_told_otherwise(env):
    """One command must not take a result file straight to live data, whatever mode the deployment is in."""
    base = env.bootstrap()
    record = str(FIXTURES / "accepted_run.json")
    assert env.run("publish", record) == 4 and env.live().release_id == base
    assert "需要核准" in env.out and "--mode auto" in env.out and "實際變更" in env.out and '"2026-11-15" → "2026-11-30"' in env.out
    assert env.run("discard", PROP) == 0
    assert env.run("publish", record, "--mode", "auto") == 0 and lifecycle_date(env.live().files) == "2026-11-30"


def test_show_prints_what_a_pending_proposal_would_change_and_can_save_it(env, tmp_path, capsys):
    base = env.bootstrap()
    env.run("publish", str(FIXTURES / "accepted_run.json"))
    sha = P.proposal_sha256(json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))["proposal"])
    assert env.run("show", PROP) == 0
    for expected in ("待核准", "發布模式是 review", f"sha256 {sha}", "Microsoft Foundry 退役表",
                     f"實際變更（與上線中的 {base} 逐值比對）：修改 1、新增 1、刪除 0；另有時間戳記 3 處",
                     "  ~ data/lifecycle.json/events/azure-claude-sonnet-4-5-eol/date", '"2026-11-15" → "2026-11-30"',
                     "  + data/lifecycle.json/events/azure-claude-sonnet-4-5-eol/conflicts/1", '"kind": "superseded"',
                     "learn.microsoft.com", f"核准這份內容：portalctl.py approve {PROP} --sha256 {sha}\n"):
        assert expected in env.out, expected
    # a quote is text from a web page: it is shown whole, on lines that are continued where they are too long for one
    assert squash("依據（official · Microsoft）：「claude-sonnet-4-5 | 1 | GA | 2026-11-30 | claude-sonnet-5-5」") in squash(env.out)
    assert squash("表頭：Model | Version | Lifecycle status | Retirement date | Replacement") in squash(env.out) and "位置：Model retirement schedule" in env.out
    assert all(N.columns(line) <= N.WRAP for line in env.out.splitlines() if "┆" in line or "依據" in line)
    saved = tmp_path / "pending.json"
    assert env.run("show", PROP, "--out", str(saved)) == 0
    assert json.loads(saved.read_text(encoding="utf-8"))["proposal"]["id"] == PROP  # opens in scripts/review_proposal.html
    assert env.run("show", "prop-unknown") == 3 and "沒有待核准的提案" in capsys.readouterr().err


def test_show_leaves_nothing_of_a_proposal_out(env):
    """What gets approved is all of it: new sources, the whole digest, the asOf stamps and values of any length."""
    env.bootstrap()
    record = json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))
    long_note = "很長的說明。" * 900 + "【更正】最後一句也要看得到。"  # longer than what the stored difference keeps of one value
    record["proposal"]["ops"][1]["value"]["note"] = long_note
    record["proposal"]["newSources"] = [{"id": "ms-new-page", "title": "A new page", "publisher": "Microsoft", "type": "vendor-doc",
                                        "url": "https://learn.microsoft.com/new-page", "accessed": "2026-10-02"}]
    record["proposal"]["asOfFiles"] = ["data/lifecycle.json"]
    record["proposal"]["unverified"] = [{"claim": "Foundry 的日期是否含時區", "reason": "頁面沒有寫"}]
    record_file = env.portal.parent / "record.json"
    record_file.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert env.run("publish", str(record_file)) == 4
    assert env.run("show", PROP) == 0
    for expected in ("新增的來源（1）", "ms-new-page", "https://learn.microsoft.com/new-page",
                     "「資料查核至」（asOf）改為 2026-10-02：data/lifecycle.json", "未能確認的項目（1）", "Foundry 的日期是否含時區（頁面沒有寫）"):
        assert expected in env.out, expected[:60]
    assert squash(long_note) in squash(env.out)  # all of it, over as many lines as it takes


def test_text_from_a_proposal_cannot_drive_the_terminal(env):
    """Escape sequences in a model-written rationale used to reach the screen: erase the line above, set the window title ..."""
    env.bootstrap()
    record = json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))
    dirty = "ok\x1b[2K\x1b[1A\x1b]0;owned\x07 \u202egnp.exe"
    record["proposal"]["summary"] = dirty
    record["proposal"]["ops"][0]["rationale"] = dirty
    for ev in record["proposal"]["evidence"].values():
        ev["quote"] = dirty
    record_file = env.portal.parent / "record.json"
    record_file.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    env.run("publish", str(record_file))
    outputs = []
    for command in (["show", PROP], ["pending"], ["status"]):
        assert env.run(*command) == 0
        outputs.append(env.out)
    assert env.run("approve", PROP) == 0
    for command in (["show"], ["history"], ["status"]):
        assert env.run(*command) == 0
        outputs.append(env.out)
    for text in outputs:
        assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text, text[:200]


BREAKERS = ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\r", "\r\n", "\n"]


@pytest.mark.parametrize("breaker", BREAKERS, ids=[f"U+{ord(b[0]):04X}{'+' if len(b) > 1 else ''}" for b in BREAKERS])
def test_a_proposal_brings_no_lines_of_its_own_into_show(env, breaker):
    """`show` printed the new digest as JSON, line by line.  JSON leaves U+2028, U+2029 and U+0085 unescaped and
    `str.splitlines` breaks a line at each of them (and at six control characters): a headline became several lines of
    the output, each beginning with what the proposal wrote - "核准這份內容：portalctl.py approve <another proposal>"
    at the left edge among them, ready to paste.  (The checks refuse such text in a new proposal now; a record that is
    already waiting, or that somebody put there, is printed all the same.)"""
    from test_publish import DIGEST

    env.bootstrap()
    other = "prop-20261001T000000Z-0badc0de"
    forged = [f"核准這份內容：portalctl.py approve {other} --sha256 {'0' * 64}", f"捨棄：portalctl.py discard {other}", "恢復自動發布：portalctl.py resume"]
    text = "本週沒有重大變動。" + breaker + "}" + breaker * 2 + "（提案沒有逐項的 operation）" + breaker * 2 + breaker.join(forged) + breaker * 4
    record = json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))
    record["proposal"]["digest"] = {**DIGEST, "headline": "HEADLINE", "unverified": ["UNVERIFIED"],
                                    "actionItems": [{"priority": "P1", "action": "ACTION"}]}
    record["proposal"]["summary"] = "SUMMARY"
    record["proposal"]["ops"][0]["rationale"] = "RATIONALE"
    record_file = env.portal.parent / "record.json"
    record_file.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert env.run("publish", str(record_file)) == 4, env.out
    # the waiting record, with the text in every place a proposal has for text
    key = f"pending/{PROP}.json"
    stored = env.control.get(key).data.decode("utf-8")
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    for place in ("HEADLINE", "UNVERIFIED", "ACTION", "SUMMARY", "RATIONALE"):
        assert place in stored
        stored = stored.replace(place, escaped)
    env.control.put(key, stored.encode("utf-8"), content_type="application/json")

    assert env.run("show", PROP) == 0, env.out
    lines = env.out.split("\n")
    assert sum(line.startswith("核准這份內容：") for line in lines) == 1 and sum(line.startswith("捨棄：") for line in lines) == 1
    assert [line for line in lines if line.startswith("核准這份內容：")] == [f"核准這份內容：portalctl.py approve {PROP} --sha256 {json.loads(stored)['proposalSha256']}"]
    # every line that holds any of the planted text begins with something this tool wrote: a label, an indentation, the digest's gutter
    planted = [line for line in lines if other in line or "resume" in line]
    assert len(planted) >= 5 and all(line.startswith(("    │ ", "  ", "摘要：")) for line in planted), planted
    assert not any(ch in env.out for ch in "\u2028\u2029\x85\x0b\x0c\x1c\x1d\x1e\r\x1f")
    start, end = lines.index("新的 digest 全文："), lines.index("", lines.index("新的 digest 全文："))
    assert end - start > 10 and all(line.startswith("    │ ") for line in lines[start + 1:end]), lines[start:end]
    assert env.run("pending") == 0 and not any(line.startswith(("核准這份內容：", "捨棄：", "恢復自動發布：")) for line in env.out.split("\n"))


def test_show_prints_the_record_of_a_release(env):
    first = env.bootstrap()
    env.run("publish", str(FIXTURES / "accepted_run.json"), "--mode", "auto")
    assert env.run("show", env.live().release_id) == 0
    for expected in ("agent 研究", PROP, f"前一版 {first}", "~ data/lifecycle.json", '"2026-11-15" → "2026-11-30"', "learn.microsoft.com",
                     "實際變更", "提案的理由與引文（2 個 operation）"):
        assert expected in env.out, expected
    assert env.run("show") == 0 and env.live().release_id in env.out  # no argument: the live release
    assert env.run("show", first) == 0 and "第一個 release" in env.out and "+ data/catalog.json" in env.out


def test_discard_and_unknown_proposals(env, capsys):
    env.bootstrap()
    env.run("publish", str(FIXTURES / "accepted_run.json"))
    assert env.run("discard", PROP, "--reason", "日期要再確認") == 0 and "已捨棄" in env.out
    assert P.list_pending(env.control) == []
    # what was decided is on record: the next person is told who discarded it, not "perhaps approved, perhaps discarded"
    assert env.run("approve", PROP) == 4 and "這份提案已經由 eunice 於" in env.out and "捨棄；要重新提出" in env.out
    assert env.live().manifest["sequence"] == 1
    assert env.run("discard", PROP) == 0 and "這份提案已經由 eunice 於" in env.out          # a second discard changes nothing, and says so
    capsys.readouterr()
    for verb in ("approve", "discard", "show"):
        assert env.run(verb, "prop-20261002T030000Z-00000000") == 3 and "沒有待核准的提案" in capsys.readouterr().err


def test_an_unreadable_result_file_is_an_input_problem(env, tmp_path, capsys):
    env.bootstrap()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert env.run("publish", str(bad)) == 3 and "不是有效的 JSON" in capsys.readouterr().err
    assert env.run("publish", str(tmp_path / "missing.json")) == 3


# ---------------------------------------------------------------------------------------------- checkouts
AUTO = ("publish", str(FIXTURES / "accepted_run.json"), "--mode", "auto")


def marker(env) -> str:
    return json.loads((env.portal / ".portal-release.json").read_text())["releaseId"]


def edit_json(path, **fields):
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(fields)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_bootstrap_says_what_will_be_readable_and_asks_first(env, capsys):
    (env.portal / "data" / "internal" / "platform-inventory.json").write_bytes(tool_file("data/internal/platform-inventory.example.json"))
    assert env.run("bootstrap", "--portal", str(env.portal)) == 3  # not a terminal, no --yes: nothing is published
    assert "--yes" in capsys.readouterr().err and not env.store.exists("manifest.json")
    for expected in ("內部清單與測試結果（平台團隊與工具）", "data/internal/platform-inventory.json", "runs/compat/example-mock.json",
                     "能開 UI 的人都讀得到", "研究資料（agent 維護）"):
        assert expected in env.out, expected
    assert env.run("bootstrap", "--portal", str(env.portal), tty_answer="n") == 0 and "已取消" in env.out and not env.store.exists("manifest.json")
    assert env.run("bootstrap", "--portal", str(env.portal), tty_answer="y") == 0 and env.live().manifest["hasInventory"] is True


def test_only_the_files_the_ui_reads_leave_the_checkout(env):
    """A checkout holds more than data: the real gateway config next to the example one, notes, exports."""
    (env.portal / "config" / "litellm.yaml").write_text("general_settings:\n  master_key: sk-REAL-KEY\n")
    (env.portal / "data" / "internal" / "notes.txt").write_text("internal notes\n")
    (env.portal / "data" / "internal" / "customers.json").write_text("{}\n")
    (env.portal / "data" / "scratch.json").write_text('{"draft": true}\n')          # JSON right next to the data files is still not one of them
    (env.portal / "data" / "catalog.backup.json").write_text("{}\n")
    (env.portal / "runs" / "raw-export.csv").write_text("a,b\n")
    (env.portal / "config" / "notes.md").write_text("# 說明\n")
    assert env.run("bootstrap", "--portal", str(env.portal), "--yes") == 0
    live = env.live().files
    assert sorted(live) == sorted(snapshot_tree())
    for skipped in ("config/litellm.yaml", "data/internal/notes.txt", "data/internal/customers.json", "data/scratch.json", "data/catalog.backup.json",
                    "runs/raw-export.csv", "config/notes.md"):
        assert f"略過 {skipped}：不是 UI 會讀的檔案" in env.out and skipped not in live
    assert "sk-REAL-KEY" not in env.out and "--include" in env.out
    # a person can still decide to publish one of them, by naming it
    assert env.run("push", "--portal", str(env.portal), "--only", "config", "--include", "config/notes.md", "--yes") == 0
    assert "config/notes.md" in env.live().files and "config/litellm.yaml" not in env.live().files


def test_a_symlinked_folder_is_not_followed(env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "perf").mkdir(parents=True)
    (elsewhere / "perf" / "secret.json").write_bytes(tool_file(PERF, runId="perf-20261003T010000Z-secret"))
    import shutil

    shutil.rmtree(env.portal / "runs")
    (env.portal / "runs").symlink_to(elsewhere, target_is_directory=True)
    assert env.run("bootstrap", "--portal", str(env.portal), "--yes") == 0
    assert "略過 runs/：symlink 資料夾，不會跟隨" in env.out and not any(k.startswith("runs/") for k in env.live().files)


def test_test_results_are_pushed_without_touching_research_data(env):
    env.bootstrap()
    env.run(*AUTO)  # the agent's update is live; the checkout is now behind
    (env.portal / "runs" / "compat" / "2026-10-03-sonnet55.json").write_bytes(tool_file(COMPAT, runId="compat-20261003T010000Z-cccccc"))
    base = marker(env)
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--dry-run") == 0
    assert "+ runs/compat/2026-10-03-sonnet55.json" in env.out and "預覽" in env.out and env.live().manifest["sequence"] == 2
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes", "--note", "compat") == 0
    assert "會發布以下變更：" in env.out and "沒有寫入任何東西" not in env.out
    current = env.live()
    assert current.manifest["compatRuns"] == ["2026-10-03-sonnet55.json", "example-mock.json"] and lifecycle_date(current.files) == "2026-11-30"
    assert marker(env) == base  # the checkout is still behind on research data: its base does not move


def test_a_checkout_that_is_behind_can_still_publish_what_it_changed(env):
    """Three-way merge: the agent's update (which this checkout never saw) stays, the hand edit goes live."""
    env.bootstrap()
    env.run(*AUTO)
    edit_json(env.portal / "data" / "litellm.json", asOf="2026-10-04")
    assert env.run("push", "--portal", str(env.portal), "--yes", "--note", "更新 asOf") == 0, env.out
    assert "~ data/litellm.json" in env.out and "data/lifecycle.json" not in env.out
    current = env.live()
    assert json.loads(current.files["data/litellm.json"])["asOf"] == "2026-10-04" and lifecycle_date(current.files) == "2026-11-30"


def test_a_file_changed_here_and_upstream_stops_the_push_until_someone_decides(env):
    base = env.bootstrap()
    env.run(*AUTO)
    lifecycle = env.portal / "data" / "lifecycle.json"
    doc = json.loads(lifecycle.read_text(encoding="utf-8"))
    next(e for e in doc["events"] if e["id"] == "azure-claude-sonnet-4-5-eol")["date"] = "2026-12-24"
    lifecycle.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    mine = lifecycle.read_bytes()
    assert env.run("push", "--portal", str(env.portal), "--yes") == 4
    assert "都改過" in env.out and "data/lifecycle.json" in env.out and base in env.out and lifecycle_date(env.live().files) == "2026-11-30"

    # pull does not settle it by overwriting: the edit stays, the conflict is named, and the base does not move
    sources = (env.portal / "data" / "sources.json").read_bytes()
    assert env.run("pull", "--portal", str(env.portal)) == 4
    assert "本機和上線都改過，沒有覆蓋" in env.out and "data/lifecycle.json" in env.out and "--overwrite" in env.out and "push --force" in env.out
    assert lifecycle.read_bytes() == mine and marker(env) == base
    # ... and it does not do half of the job either: the file that only upstream changed is named, and left as it was
    assert "沒有寫入任何檔案" in env.out and "~ data/sources.json" in env.out
    assert (env.portal / "data" / "sources.json").read_bytes() == sources != env.live().files["data/sources.json"]

    assert env.run("push", "--portal", str(env.portal), "--yes", "--force") == 0 and lifecycle_date(env.live().files) == "2026-12-24"
    # what only upstream changed was merged into what went live, and the next pull - with nothing left to decide - brings it here
    assert env.live().files["data/sources.json"] != sources and marker(env) == base
    assert env.run("pull", "--portal", str(env.portal)) == 0, env.out
    assert marker(env) == env.live().release_id and (env.portal / "data" / "sources.json").read_bytes() == env.live().files["data/sources.json"]
    assert lifecycle.read_bytes() == mine  # now the checkout is exactly what is live


def test_pull_takes_the_upstream_version_only_when_asked(env):
    env.bootstrap()
    env.run(*AUTO)
    lifecycle = env.portal / "data" / "lifecycle.json"
    edit_json(lifecycle, asOf="2026-12-31")
    # a preview ends the way the pull would: with the conflict, and its exit code (a script that asks first gets the answer)
    assert env.run("pull", "--portal", str(env.portal), "--dry-run") == 4 and "預覽" in env.out and json.loads(lifecycle.read_text())["asOf"] == "2026-12-31"
    assert "會因為上面 1 個要先決定的檔案而停下" in env.out
    assert env.run("pull", "--portal", str(env.portal), "--overwrite", "--dry-run") == 0 and "停下" not in env.out
    assert json.loads(lifecycle.read_text())["asOf"] == "2026-12-31"
    assert env.run("pull", "--portal", str(env.portal), "--overwrite") == 0
    assert lifecycle.read_bytes() == env.live().files["data/lifecycle.json"] and marker(env) == env.live().release_id


def test_a_pull_that_cannot_finish_leaves_nothing_for_the_next_push_to_mistake_for_an_edit(env):
    """A pull that met one conflict used to write every other file of the release under the marker of the OLD release.
    The next push then read pull's own writes as the operator's changes: when that release was rolled back in the
    meantime, its files went live again with an unrelated push."""
    base = env.bootstrap()
    before = env.live().files["data/sources.json"]
    env.run(*AUTO)                                                 # upstream: lifecycle.json and sources.json change
    edit_json(env.portal / "data" / "lifecycle.json", asOf="2026-12-31")  # here: an edit to the same file
    assert env.run("pull", "--portal", str(env.portal)) == 4 and marker(env) == base
    assert (env.portal / "data" / "sources.json").read_bytes() == before
    assert env.run("rollback", "--reason", "這一版有誤") == 0 and env.live().files["data/sources.json"] == before
    assert env.run("push", "--portal", str(env.portal), "--yes", "--note", "只改了 asOf") == 0, env.out
    assert "~ data/lifecycle.json" in env.out and "data/sources.json" not in env.out
    assert env.live().files["data/sources.json"] == before        # what was rolled back stays rolled back


def test_pull_keeps_what_only_the_checkout_changed(env):
    env.bootstrap()
    env.run(*AUTO)  # changes lifecycle.json and sources.json upstream
    litellm = env.portal / "data" / "litellm.json"
    edit_json(litellm, asOf="2026-10-04")
    mine = litellm.read_bytes()
    assert env.run("pull", "--portal", str(env.portal)) == 0, env.out
    assert "保留妳改過的 1 個檔案" in env.out and "data/litellm.json" in env.out and "寫入 2 個檔案" in env.out
    assert litellm.read_bytes() == mine and marker(env) == env.live().release_id
    assert lifecycle_date({"data/lifecycle.json": (env.portal / "data" / "lifecycle.json").read_bytes()}) == "2026-11-30"
    assert env.run("push", "--portal", str(env.portal), "--yes") == 0 and "~ data/litellm.json" in env.out


def test_a_checkout_without_a_record_of_its_origin_is_not_overwritten_blindly(env, capsys):
    """A fresh clone, or a folder somebody copied: nothing says which of its differences are edits."""
    env.bootstrap()
    env.run(*AUTO)
    (env.portal / ".portal-release.json").unlink()
    before = (env.portal / "data" / "lifecycle.json").read_bytes()
    assert env.run("pull", "--portal", str(env.portal)) == 4
    assert "沒有來源紀錄" in env.out and "data/lifecycle.json" in env.out and (env.portal / "data" / "lifecycle.json").read_bytes() == before
    assert not (env.portal / ".portal-release.json").exists()
    assert env.run("push", "--portal", str(env.portal), "--yes") == 4 and "沒有記錄是從哪個 release 取得的" in env.out
    assert env.run("pull", "--portal", str(env.portal), "--overwrite") == 0 and marker(env) == env.live().release_id
    assert env.run("push", "--portal", str(env.portal), "--yes") == 0 and "unchanged" in env.out


def test_pull_does_not_stop_at_a_broken_local_file_and_does_not_write_through_links(env, tmp_path):
    env.bootstrap()
    env.run(*AUTO)
    (env.portal / "data" / "digests" / "draft.json").write_text("{broken", encoding="utf-8")  # a local draft that is not valid JSON
    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    sources = env.portal / "data" / "sources.json"
    original = sources.read_bytes()
    sources.unlink()
    sources.symlink_to(outside)
    assert env.run("pull", "--portal", str(env.portal)) == 4
    assert "無法寫入 1 個檔案" in env.out and "symlink" in env.out and outside.read_text(encoding="utf-8") == "untouched"
    # nothing else was written either: a checkout is moved to a release as a whole or not at all
    assert "沒有寫入任何檔案" in env.out and lifecycle_date({"data/lifecycle.json": (env.portal / "data" / "lifecycle.json").read_bytes()}) == "2026-11-15"
    sources.unlink()
    sources.write_bytes(original)
    assert env.run("pull", "--portal", str(env.portal)) == 0, env.out  # (the broken draft of a digest is no reason to stop)
    assert lifecycle_date({"data/lifecycle.json": (env.portal / "data" / "lifecycle.json").read_bytes()}) == "2026-11-30"
    assert sources.read_bytes() == env.live().files["data/sources.json"] and outside.read_text(encoding="utf-8") == "untouched"


def test_pull_removes_what_the_release_no_longer_has_and_nothing_else(env, tmp_path):
    """After a pull the checkout says it is based on the new release.  A file that release dropped must then be gone
    from the checkout too, or the next push publishes it again as something this checkout added."""
    first = env.bootstrap()
    mine = env.portal / "runs" / "perf" / "local-only.json"
    mine.write_bytes(tool_file(PERF, runId="perf-20261003T010000Z-aaaaaa"))
    notes = env.portal / "config" / "local.yaml"
    notes.write_text("a: 1\n")
    # upstream removes the example run
    assert P.publish_tree(env.store, {k: v for k, v in snapshot_tree().items() if k != PERF}, base_release=first, actor="ci").status == "published"
    second = env.live().release_id
    assert env.run("pull", "--portal", str(env.portal), "--dry-run") == 0 and f"- {PERF}" in env.out and (env.portal / PERF).exists()
    assert env.run("pull", "--portal", str(env.portal)) == 0, env.out
    assert f"- {PERF}" in env.out and "刪除 1 個" in env.out
    assert not (env.portal / PERF).exists() and mine.exists() and notes.exists()   # files the release never had are not its to delete
    assert json.loads((env.portal / ".portal-release.json").read_text())["releaseId"] == second
    # ... so pushing the checkout's own new file does not bring the removed one back
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes") == 0, env.out
    assert PERF not in env.live().files and "runs/perf/local-only.json" in env.live().files


def test_pull_keeps_a_file_you_changed_even_when_the_release_dropped_it(env):
    first = env.bootstrap()
    (env.portal / PERF).write_bytes(tool_file(PERF, runId="perf-20261003T010000Z-bbbbbb"))        # changed here ...
    assert P.publish_tree(env.store, {k: v for k, v in snapshot_tree().items() if k != PERF}, base_release=first, actor="ci").status == "published"
    assert env.run("pull", "--portal", str(env.portal)) == 4                                        # ... removed upstream: a conflict
    assert "上線已移除、但妳改過的 1 個檔案保留在本機" in env.out and (env.portal / PERF).exists()
    assert json.loads((env.portal / ".portal-release.json").read_text())["releaseId"] == first      # the base does not move past a conflict
    assert env.run("pull", "--portal", str(env.portal), "--overwrite") == 0 and not (env.portal / PERF).exists()


def test_a_checkout_without_a_record_of_its_release_loses_nothing_on_pull(env):
    first = env.bootstrap()
    (env.portal / ".portal-release.json").unlink()
    assert P.publish_tree(env.store, {k: v for k, v in snapshot_tree().items() if k != PERF}, base_release=first, actor="ci").status == "published"
    assert env.run("pull", "--portal", str(env.portal)) == 0 and (env.portal / PERF).exists()
    assert "沒有來源紀錄" in env.out and "不會刪除任何檔案" in env.out


def test_pull_can_create_a_local_copy(env, tmp_path):
    env.bootstrap()
    copy = tmp_path / "local-data"
    assert env.run("pull", "--portal", str(copy)) == 3  # not a checkout: say so rather than scatter files
    assert env.run("pull", "--portal", str(copy), "--init") == 0
    assert R.collect_tree(copy)[0] == snapshot_tree()
    other = tmp_path / "not-a-portal"
    (other / "config").mkdir(parents=True)
    (other / "config" / "app.yaml").write_text("mine: true\n")
    (other / "data").mkdir()
    (other / "data" / "export.csv").write_text("a,b\n")
    assert env.run("pull", "--portal", str(other), "--init") == 0
    assert (other / "config" / "app.yaml").read_text() == "mine: true\n" and (other / "data" / "export.csv").exists()
    assert env.run("pull", "--portal", str(copy), "--release", "20200101T000000Z-deadbeef") == 3


def test_push_adds_and_replaces_but_removes_only_on_request(env):
    env.bootstrap()
    (env.portal / ".portal-release.json").unlink()  # a CI checkout: no record of where its files came from
    (env.portal / PERF).unlink()
    (env.portal / "runs" / "perf" / "new.json").write_bytes(tool_file(PERF, runId="perf-20261003T010000Z-eeeeee"))
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes") == 0
    assert "只會新增與取代檔案" in env.out and env.live().manifest["perfRuns"] == ["example-mock.json", "new.json"]
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--delete", "--yes") == 0 and "- runs/perf/example-mock.json" in env.out
    assert env.live().manifest["perfRuns"] == ["new.json"] and env.live().manifest["hasInventory"] is False


@pytest.mark.parametrize("only", ["", ",", " ", "runs,,config", "/"])
def test_an_empty_only_is_not_everything(env, only, capsys):
    """`--only ""` used to mean a full push - which removed what the checkout did not have."""
    env.bootstrap()
    (env.portal / "data" / "internal" / "platform-inventory.example.json").unlink()
    assert env.run("push", "--portal", str(env.portal), "--only", only, "--yes") == 3
    assert "--only" in capsys.readouterr().err and env.live().manifest["sequence"] == 1


def test_push_asks_before_publishing(env, capsys):
    env.bootstrap()
    (env.portal / "runs" / "perf" / "new.json").write_bytes(tool_file(PERF, runId="perf-20261003T010000Z-eeeeee"))
    assert env.run("push", "--portal", str(env.portal), "--only", "runs") == 3  # not a terminal and no --yes
    assert "--yes" in capsys.readouterr().err and env.live().manifest["sequence"] == 1
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", tty_answer="n") == 0 and "已取消" in env.out
    assert env.live().manifest["sequence"] == 1
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", tty_answer="y") == 0 and env.live().manifest["sequence"] == 2


def test_push_refuses_a_test_result_the_ui_cannot_read_and_says_what_to_do(env):
    env.bootstrap()
    (env.portal / "runs" / "compat" / "broken.json").write_text('{"schema": "compat-run/v1", "results": []}\n')
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes") == 4
    assert "資料驗證：runs/compat/broken.json" in env.out and "--allow-invalid" in env.out and env.live().manifest["sequence"] == 1
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes", "--force") == 4  # --force is about conflicts, not about validity
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes", "--allow-invalid") == 0
    assert "broken.json" in env.live().manifest["compatRuns"]


def test_push_refuses_invalid_data_and_unknown_folders(env, capsys):
    env.bootstrap()
    (env.portal / "data" / "lifecycle.json").write_text('{"schema": "lifecycle/v1", "events": "nope"}')
    assert env.run("push", "--portal", str(env.portal), "--yes") == 4 and "資料驗證" in env.out
    (env.portal / "data" / "lifecycle.json").write_text("{broken")
    assert env.run("push", "--portal", str(env.portal), "--yes") == 3 and "not valid JSON" in capsys.readouterr().err
    (env.portal / "data" / "lifecycle.json").write_text('{"schema": "lifecycle/v1", "big": 1e999}')
    assert env.run("push", "--portal", str(env.portal), "--yes") == 3 and "not valid JSON" in capsys.readouterr().err  # Infinity for a browser
    assert env.run("push", "--portal", str(env.portal), "--only", "web,tools", "--yes") == 3
    assert env.live().manifest["sequence"] == 1


# ---------------------------------------------------------------------------------------------- what a push leaves alone
def test_a_file_published_by_name_is_not_removed_by_the_next_ordinary_push(env, tmp_path):
    """`--include config/notes.md` published it.  A colleague's push skips that file ("not a file the UI reads") - and
    "skipped" used to be read as "this checkout deleted it"."""
    env.bootstrap()
    (env.portal / "config" / "notes.md").write_text("# 說明\n", encoding="utf-8")
    assert env.run("push", "--portal", str(env.portal), "--only", "config", "--include", "config/notes.md", "--yes") == 0
    colleague = tmp_path / "colleague"
    assert env.run("pull", "--portal", str(colleague), "--init") == 0 and (colleague / "config" / "notes.md").exists()
    edit_json(colleague / "data" / "litellm.json", asOf="2026-10-03")
    assert env.run("push", "--portal", str(colleague), "--yes") == 0, env.out
    assert "略過 config/notes.md：不是 UI 會讀的檔案" in env.out and "略過的 1 個路徑不會因為這次發布而改變或消失" in env.out
    assert "- config/notes.md" not in env.out and "~ data/litellm.json" in env.out
    live = env.live().files
    assert live["config/notes.md"] == "# 說明\n".encode() and json.loads(live["data/litellm.json"])["asOf"] == "2026-10-03"
    # the colleague's checkout is not "changed" by what it left out: nothing to push, nothing to pull
    assert env.run("push", "--portal", str(colleague), "--yes") == 0 and "沒有變更" in env.out
    # taking it out of the release is still possible - by saying so
    (colleague / "config" / "notes.md").unlink()
    assert env.run("push", "--portal", str(colleague), "--yes") == 0 and "- config/notes.md" in env.out and "config/notes.md" not in env.live().files


def test_a_folder_that_is_left_out_does_not_take_its_neighbour_with_it(env):
    """A duplicated folder ("perf (copy)") is left out, with the reason behind its path in brackets.  The path used to
    be cut back out of that text at the first " (": "runs/perf" - so nothing under runs/perf/ counted as read from the
    checkout, and the file that had been deleted there on purpose stayed live ("沒有變更")."""
    env.bootstrap()
    perf = next(k for k in env.live().files if k.startswith("runs/perf/"))
    (env.portal / perf).unlink()
    stray = env.portal / "runs" / "perf (copy)" / "old (1).json"
    stray.parent.mkdir()
    stray.write_text("{}\n", encoding="utf-8")
    files, skipped = R.collect_tree(env.portal)
    assert [(s.path, s.why, str(s)) for s in skipped] == [("runs/perf (copy)/old (1).json", "not a publishable file name or type",
                                                         "runs/perf (copy)/old (1).json (not a publishable file name or type)")]
    assert env.run("push", "--portal", str(env.portal), "--yes") == 0, env.out
    assert "略過 runs/perf (copy)/old (1).json：檔名或類型不能發布" in env.out and f"- {perf}" in env.out
    assert perf not in env.live().files and env.live().manifest["sequence"] == 2


def test_what_a_checkout_holds_as_a_link_is_left_as_it_is_live(env, tmp_path):
    """A linked folder or file is not read (it may point anywhere) - and what was not read is not "deleted here"."""
    import shutil

    env.bootstrap()
    before = env.live().files
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(env.portal / "runs"), str(elsewhere))
    (env.portal / "runs").symlink_to(elsewhere, target_is_directory=True)
    edit_json(env.portal / "data" / "litellm.json", asOf="2026-10-03")
    assert env.run("push", "--portal", str(env.portal), "--yes") == 0, env.out
    assert "略過 runs/：symlink 資料夾，不會跟隨" in env.out and "- runs/" not in env.out
    assert {k: v for k, v in env.live().files.items() if k.startswith("runs/")} == {k: v for k, v in before.items() if k.startswith("runs/")}

    real = tmp_path / "catalog.json"
    shutil.move(str(env.portal / "data" / "catalog.json"), str(real))
    (env.portal / "data" / "catalog.json").symlink_to(real)
    assert env.run("push", "--portal", str(env.portal), "--yes") == 0 and "略過 data/catalog.json：symlink，不會跟隨" in env.out and "沒有變更" in env.out
    assert env.live().files["data/catalog.json"] == before["data/catalog.json"]
    assert env.run("verify") == 0 and "資料通過 schema 與交叉檢查" in env.out


@pytest.mark.parametrize("missing", ["data/catalog.json", "data/sources.json", "data/lifecycle.json", "data/litellm.json"])
@pytest.mark.parametrize("switches", [(), ("--force",), ("--allow-invalid",), ("--force", "--allow-invalid", "--delete")])
def test_no_push_publishes_a_portal_without_the_files_every_page_starts_from(env, missing, switches):
    """Without one of them the UI shows an error page instead of the portal.  That is not "invalid data" to wave through.
    (A checkout without the schema is not taken for a portal checkout at all; test_publish_gates covers the library.)"""
    base = env.bootstrap()
    (env.portal / missing).unlink()
    assert env.run("push", "--portal", str(env.portal), "--yes", *switches) == 4, env.out
    assert f"這次發布之後會缺少 UI 必要的檔案：{missing}" in env.out and env.live().release_id == base


def test_pull_does_not_bring_back_what_the_checkout_deleted(env):
    """Deleting a file and pulling used to restore it silently: the deletion could never be pushed."""
    env.bootstrap()
    (env.portal / PERF).unlink()
    assert env.run("pull", "--portal", str(env.portal)) == 0, env.out
    assert f"保留妳刪除的 1 個檔案（沒有寫回；下一次 push 會把它們從上線的資料移除，要取回請加 --overwrite）：{PERF}" in env.out
    assert not (env.portal / PERF).exists()
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--dry-run") == 0 and f"- {PERF}" in env.out
    assert env.run("pull", "--portal", str(env.portal), "--overwrite") == 0 and (env.portal / PERF).exists()   # changed my mind
    assert env.run("push", "--portal", str(env.portal), "--yes") == 0 and "沒有變更" in env.out
    (env.portal / PERF).unlink()
    assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes") == 0 and PERF not in env.live().files


# ---------------------------------------------------------------------------------------------- rollback, pause
def test_a_release_that_never_was_live_needs_the_operator_to_say_so(env, cloud, capsys, monkeypatch):
    base = env.bootstrap()
    env.run(*AUTO)
    agent = env.live().release_id
    files = dict(env.live().files)
    orphan = "20261002T050000Z-0badc0de"   # a complete release nothing ever pointed at (a publish that lost the race leaves one)
    R.write_release(env.store, orphan, files, R.release_meta(orphan, files, created_at="2026-10-02T05:00:00Z", parent=base, source={"kind": "agent", "by": "pipeline"},
                                                             changes={"added": [], "modified": [], "removed": []}))
    assert env.run("rollback", "--to", orphan) == 4 and f"release {orphan} 不在發布紀錄裡" in env.out and "--unlisted" in env.out
    assert env.live().release_id == agent
    assert env.run("rollback", "--unlisted") == 3 and "--unlisted 要和 --to" in capsys.readouterr().err
    assert env.run("rollback", "--to", orphan, "--unlisted", "--reason", "確認過內容") == 0 and env.live().release_id == orphan
    # through the state machine there is no such switch: it is a decision for somebody with the data location in front of them
    cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes")
    assert cloud.run("rollback", "--to", orphan, "--unlisted", environ=MACHINE) == 3 and cloud.inputs() == []
    assert "只能直接操作資料位置" in capsys.readouterr().err


def test_a_pause_reason_cannot_drive_the_terminal_of_whoever_looks_at_the_status(env):
    env.bootstrap()
    dirty = "維護中\x1b[2K\x1b[1A\x1b]0;owned\x07 \u202egnp.exe"
    assert env.run("pause", "--reason", dirty) == 0
    outputs = [env.out]
    for command in (["status"], ["verify"], ["history"], ["pending"]):
        env.run(*command)
        outputs.append(env.out)
    for text in outputs:
        assert not any(ch in text for ch in ("\x1b", "\x07", "\u202e")), text[:300]
    assert "維護中" in outputs[1] and "維護中" in outputs[2]
    raw = env.store.get("manifest.json").data.decode("utf-8")
    assert "\\u001b" not in raw and "\x1b" not in raw


def test_rollback_restores_the_previous_release_and_pauses_publishing(env, capsys):
    base = env.bootstrap()
    env.run(*AUTO)
    agent = env.live().release_id
    assert env.run("rollback", "--reason", "日期有疑問") == 0
    assert f"已回復到 {base}" in env.out and "自動發布已暫停" in env.out and env.live().release_id == base
    assert env.run("status") == 0 and "⏸ 已暫停（eunice" in env.out and "日期有疑問" in env.out
    assert env.run(*AUTO) == 4 and "自動發布已暫停" in env.out
    assert env.run("rollback") == 4 and "本身就是一次 rollback 的結果" in env.out and env.live().release_id == base  # not a toggle
    assert env.run("rollback", "--to", agent) == 0 and env.live().release_id == agent
    assert env.run("resume") == 0 and "已恢復自動發布" in env.out
    assert env.run("resume") == 0 and "自動發布原本就沒有暫停" in env.out and "None" not in env.out
    assert env.run("pause", "--reason", "連假") == 0 and P.get_hold(env.store)["reason"] == "連假"
    assert env.run("rollback", "--to", "not-a-release") == 3 and "not a release id" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------- the state machine
def test_run_needs_the_state_machine(env, capsys):
    assert env.run("run") == 3 and "PIPELINE_STATE_MACHINE_ARN" in capsys.readouterr().err


ONLINE = {"PIPELINE_STATE_MACHINE_ARN": SFN_ARN, "PORTAL_DATA_URI": "s3://portal-data-bucket/portal/"}  # both from the environment


def test_run_starts_an_execution_with_the_request(env):
    env.sfn = FakeSfn()
    machine = ONLINE
    assert env.run("run", environ=machine) == 0 and "已啟動" in env.out and "--wait" in env.out
    assert env.run("run", "--payload-json", '{"kind": "focus", "focus": ["lifecycle"]}', "--mode", "review", environ=machine) == 0
    first, second = [json.loads(s["input"]) for s in env.sfn.started]
    assert first == {"request": {"kind": "weekly"}, "trigger": "manual", "by": "eunice"}
    assert second == {"request": {"kind": "focus", "focus": ["lifecycle"]}, "trigger": "manual", "by": "eunice", "mode": "review"}
    assert env.sfn.started[0]["stateMachineArn"] == SFN_ARN
    assert env.run("run", "--payload-json", "[1]", environ=machine) == 3 and env.run("run", "--payload-json", "{oops", environ=machine) == 3


@pytest.mark.parametrize("status, output, error, code, expect", [
    ("SUCCEEDED", {"publish": {"status": "published"}, "notification": {"message": "已發布新的資料版本 X"}}, None, 0, "已發布新的資料版本 X"),
    ("SUCCEEDED", {"publish": {"status": "pending"}, "notification": {"message": "需要人工核准"}}, None, 4, "需要人工核准"),
    ("SUCCEEDED", {"research": {"status": "no_proposal"}, "notification": {"message": "沒有可發布的提案"}}, None, 0, "沒有可發布的提案"),
    ("SUCCEEDED", ["not", "an", "object"], None, 0, "SUCCEEDED"),
    ("FAILED", None, "PortalUpdateRejected", 4, "PortalUpdateRejected"),
    ("FAILED", None, "PortalUpdateFailed", 5, "PortalUpdateFailed"),
    ("TIMED_OUT", None, None, 5, "TIMED_OUT"),
])
def test_run_wait_reports_how_the_execution_ended(env, status, output, error, code, expect):
    env.sfn = FakeSfn(status=status, output=output, error=error)
    assert env.run("run", "--wait", "--poll", "0", environ=ONLINE) == code
    assert expect in env.out and "→ 透過 state machine 執行（llmops-portal-pipeline-update）" in env.out


def test_an_operator_who_may_start_but_not_describe_is_told_what_happened(env, capsys):
    """StartExecution alone is not enough to wait for the result: the action runs, and the tool says so instead of crashing."""

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform: states:DescribeExecution"}}

    class StartOnly(FakeSfn):
        def describe_execution(self, executionArn):
            raise Denied("User is not authorized to perform: states:DescribeExecution")

    env.sfn = StartOnly()
    assert env.run("run", "--wait", environ=ONLINE) == 3
    err = capsys.readouterr().err
    assert "已經啟動" in err and "states:DescribeExecution" in err and "Traceback" not in err and len(env.sfn.started) == 1


def test_an_unexpected_aws_error_is_one_line_and_not_exit_code_one(env, capsys):
    """Exit code 1 means "a verify check failed"; a traceback helps nobody at 2 a.m."""

    class Throttled(Exception):
        response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}

    class Broken(FakeSfn):
        def list_executions(self, **kw):
            raise Throttled("Rate exceeded")

    env.sfn = Broken()
    assert env.run("executions", environ=ONLINE) == 3
    err = capsys.readouterr().err
    assert "states:ListExecutions" in err and "Rate exceeded" in err and "Traceback" not in err


# ---------------------------------------------------------------------------------------------- which way an action goes
MACHINE = {"PIPELINE_STATE_MACHINE_ARN": SFN_ARN}


def test_a_local_folder_is_never_handled_by_the_deployed_state_machine(env):
    """The quick-start commands ran against production as soon as the state machine's ARN was in the environment."""
    base = env.bootstrap()
    env.run(*AUTO)
    env.sfn = FakeSfn(output={"publish": {"status": "rolled_back"}, "notification": {"message": "ok"}})
    assert env.run("rollback", "--reason", "試試看", environ=MACHINE) == 0
    assert env.sfn.started == [] and env.live().release_id == base and "直接操作" in env.out and "沒有經過 state machine" in env.out
    assert env.run("--data", str(env.store.root), "resume", environ=MACHINE) == 0 and env.sfn.started == []
    assert env.run("pause", "--reason", "x", "--data", str(env.store.root), environ=MACHINE) == 0 and env.sfn.started == []
    assert env.run("publish", str(FIXTURES / "accepted_run.json"), environ=MACHINE) == 4 and env.sfn.started == []
    assert env.run("approve", PROP, environ=MACHINE) == 0 and env.sfn.started == [] and lifecycle_date(env.live().files) == "2026-11-30"


def test_the_waiting_list_in_the_environment_is_not_used_for_a_data_location_on_the_command_line(env, tmp_path, capsys):
    """PIPELINE_STATE_URI in the environment is production's.  `--data <a test folder> approve <id>` used to look the id up
    there, publish it into the test folder and record "approved" in production - where the proposal then was gone."""
    env.bootstrap()                                              # "production": data and waiting list from the environment
    assert env.run("publish", str(FIXTURES / "accepted_run.json")) == 4
    production = {"PIPELINE_STATE_URI": str(env.control.root)}
    test_data = tmp_path / "test" / "portal"
    assert env.run("--data", str(test_data), "bootstrap", "--portal", str(env.portal), "--yes", environ=production) == 0
    for verb in ("approve", "discard"):
        assert env.run("--data", str(test_data), verb, PROP, environ=production) == 3
        assert "沒有待核准的提案" in capsys.readouterr().err
        assert f"待核准清單：{tmp_path / 'test' / 'pipeline'}" in env.out and "環境變數 PIPELINE_STATE_URI 屬於環境變數裡的資料位置" in env.out and "--state" in env.out
    assert [p["proposalId"] for p in P.list_pending(env.control)] == [PROP] and not env.control.exists(f"decided/{PROP}.json")
    assert env.live().manifest["sequence"] == 1

    other = json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))
    other["proposal"]["id"] = "prop-20261002T030000Z-0000test"
    scratch = tmp_path / "scratch.json"
    scratch.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
    assert env.run("--data", str(test_data), "publish", str(scratch), environ=production) == 4
    assert [p["proposalId"] for p in P.list_pending(env.control)] == [PROP]                                     # production's list is as it was
    assert [p["proposalId"] for p in P.list_pending(DirStore(tmp_path / "test" / "pipeline"))] == ["prop-20261002T030000Z-0000test"]
    # naming the waiting list is how to use another one; and with everything from the environment it is used as before
    assert env.run("--data", str(test_data), "--state", str(env.control.root), "pending", environ=production) == 0 and PROP in env.out
    assert env.run("pending", environ=production) == 0 and PROP in env.out and "PIPELINE_STATE_URI 屬於" not in env.out


def test_only_this_deployments_own_run_records_go_through_the_state_machine(cloud, capsys):
    """The execution is told "runs/run-X.json" and reads its own copy.  A corrected copy uploaded elsewhere used to be
    accepted - and the uncorrected original was what got published."""
    cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes")
    run = "run-20261002T030000Z-d3eca3"
    for spec, said in (
        (f"s3://my-scratch-bucket/edited/runs/{run}.json", "只能發布這個部署自己的研究紀錄（s3://portal-data-bucket/research/runs/run-….json）"),
        (f"s3://portal-data-bucket/research/other/{run}.json", "只能發布這個部署自己的研究紀錄"),
        (f"s3://portal-data-bucket/research/runs/sub/{run}.json", "只能發布這個部署自己的研究紀錄"),
        (f"s3://portal-data-bucket/research/runs/notes.json", "只能發布這個部署自己的研究紀錄"),
    ):
        assert cloud.run("publish", spec, environ=MACHINE) == 3, spec
        err = capsys.readouterr().err
        assert said in err and "--direct" in err and spec in err, err
    assert cloud.inputs() == []
    assert cloud.run("publish", f"s3://portal-data-bucket/research/runs/{run}.json", environ=MACHINE) == 0
    assert cloud.inputs() == [{"action": "publish", "runKey": f"runs/{run}.json", "by": "eunice"}]
    # a deployment that keeps its results somewhere else says where
    elsewhere = {**MACHINE, "PIPELINE_RESEARCH_URI": "s3://results-bucket/research"}
    assert cloud.run("publish", f"s3://results-bucket/research/runs/{run}.json", environ=elsewhere) == 0 and len(cloud.inputs()) == 2
    assert cloud.run("publish", f"s3://portal-data-bucket/research/runs/{run}.json", environ=elsewhere) == 3 and len(cloud.inputs()) == 2
    assert "s3://results-bucket/research/runs/run-….json" in capsys.readouterr().err
    # ... and for a data location that is not the stack's layout nothing is guessed
    odd = {**MACHINE, "PORTAL_DATA_URI": "s3://portal-data-bucket/data/"}
    assert cloud.run("publish", f"s3://portal-data-bucket/data/research/runs/{run}.json", environ=odd) == 3 and len(cloud.inputs()) == 2
    assert "PIPELINE_RESEARCH_URI" in capsys.readouterr().err


def test_where_the_environments_deployment_keeps_its_results_says_nothing_about_another_one(cloud, capsys):
    """PIPELINE_RESEARCH_URI describes the deployment the environment names.  With another deployment on the command
    line (--data and --state-machine) it used to be applied all the same: a record of production's passed as "this
    deployment's own" - the other state machine was started with its file name - and the deployment's real record
    was refused."""
    run = "run-20261002T030000Z-d3eca3"
    production = {**MACHINE, "PIPELINE_RESEARCH_URI": "s3://prod-results/research/"}
    staging = ["--data", "s3://staging-data/portal/", "--state-machine", "arn:aws:states:us-east-1:222222222222:stateMachine:staging-update"]
    assert cloud.run(*staging, "publish", f"s3://prod-results/research/runs/{run}.json", environ=production) == 3
    err = capsys.readouterr().err
    assert "只能發布這個部署自己的研究紀錄（s3://staging-data/research/runs/run-….json）" in err
    assert "環境變數 PIPELINE_RESEARCH_URI 屬於環境變數裡的資料位置，沒有用在指令列指定的這個位置上；要指定請用 --research" in err
    assert cloud.inputs() == []
    # the deployment's own record, next to the data location that was named
    assert cloud.run(*staging, "publish", f"s3://staging-data/research/runs/{run}.json", environ=production) == 0
    assert cloud.inputs() == [{"action": "publish", "runKey": f"runs/{run}.json", "by": "eunice"}]
    assert cloud.sfn.started[-1]["stateMachineArn"].endswith(":stateMachine:staging-update")
    # ... or wherever --research says it is (before or after the command)
    for argv in ([*staging, "--research", "s3://staging-results/r/", "publish", f"s3://staging-results/r/runs/{run}.json"],
                 [*staging, "publish", f"s3://staging-results/r/runs/{run}.json", "--research", "s3://staging-results/r"]):
        assert cloud.run(*argv, environ=production) == 0, argv
    assert len(cloud.inputs()) == 3
    assert cloud.run(*staging, "--research", "s3://staging-results/r/", "publish", f"s3://staging-data/research/runs/{run}.json", environ=production) == 3
    err = capsys.readouterr().err
    assert "s3://staging-results/r/runs/run-….json" in err and "屬於環境變數裡的資料位置" not in err
    # with everything from the environment the variable is used as before (see the test above)
    assert cloud.run("publish", f"s3://prod-results/research/runs/{run}.json", environ=production) == 0 and len(cloud.inputs()) == 4


@pytest.mark.parametrize("argv, environ, named", [
    (["discard", PROP, "--reason", "日期要再確認 \ud83d"], {}, "--reason"),                 # half an emoji (the rest was cut off)
    (["rollback", "--reason", "\udce6\udc97\udca5 wrong"], {}, "--reason"),      # bytes this terminal's encoding could not decode
    (["push", "--portal", ".", "--note", "ok \udc80", "--yes"], {}, "--note"),
    (["--by", "eunice\udcff", "pause"], {}, "--by"),
    (["pause"], {"PORTALCTL_BY": "eu\ud83dnice"}, "PORTALCTL_BY"),
])
def test_an_argument_that_is_not_text_is_refused_before_anything_is_done(env, cloud, capsys, argv, environ, named):
    """Such a string cannot be encoded: sent on, it made every step of the execution fail - the one that reports failures included."""
    env.bootstrap()
    for world in (env, cloud):
        assert world.run(*argv, environ={**(MACHINE if world is cloud else {}), **environ}) == 2
        err = capsys.readouterr().err
        assert named in err and "沒有執行任何動作" in err and "UTF-8" in err and "\ud83d" not in err
    assert cloud.inputs() == [] and env.live().manifest["sequence"] == 1 and P.get_hold(env.store) is None


def test_what_only_the_state_machine_can_do_is_refused_where_other_actions_would_go_direct(env, cloud, capsys):
    """`run` and `executions` have no direct form.  With a data location named on the command line (or a local folder)
    they used to go to whatever state machine the environment named - some deployment's, not necessarily this one's."""
    env.sfn = FakeSfn()
    folder = str(env.store.root)
    for argv, environ, said in (
        (("run",), MACHINE, f"不是這裡指定的 {folder}"),                                              # the data location is a local folder
        (("executions",), MACHINE, f"不是這裡指定的 {folder}"),
        (("--data", BUCKET_URI, "run", "--wait"), MACHINE, f"不是這裡指定的 {BUCKET_URI}"),             # named on the command line
        (("executions", "--data", BUCKET_URI), MACHINE, f"不是這裡指定的 {BUCKET_URI}"),
        (("run", "--direct"), ONLINE, "不能和 --direct 一起用"),
    ):
        assert env.run(*argv, environ=environ) == 3, argv
        err = capsys.readouterr().err
        assert env.sfn.started == [] and said in err and "Traceback" not in err, (argv, err)
    # naming the state machine is saying which deployment is meant
    assert env.run("--data", BUCKET_URI, "--state-machine", SFN_ARN, "run") == 0 and len(env.sfn.started) == 1
    assert env.run("executions", "--state-machine", SFN_ARN, environ={"PORTAL_DATA_URI": BUCKET_URI}) == 0 and "weekly-1" in env.out


def test_a_local_file_is_not_published_past_the_pipeline_without_saying_so(cloud, capsys):
    """It used to print "through the state machine" and then write to the bucket itself - with `--mode auto`, past the
    review mode the stack was deployed with."""
    cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes")
    base = cloud.live().release_id
    record = str(FIXTURES / "accepted_run.json")
    for extra in ((), ("--mode", "auto")):
        assert cloud.run("publish", record, *extra, environ=MACHINE) == 3
        err = capsys.readouterr().err
        assert "--direct" in err and "PublishMode" in err and "透過 state machine 執行" not in cloud.out
    assert cloud.inputs() == [] and cloud.live().release_id == base and P.list_pending(cloud.control) == []
    assert cloud.run("publish", record, "--direct", environ=MACHINE) == 4 and "→ 直接操作 s3://portal-data-bucket/portal/" in cloud.out
    assert cloud.inputs() == [] and len(P.list_pending(cloud.control)) == 1
    # an object that is not a run record of this deployment cannot be handed to the state machine either
    assert cloud.run("publish", "s3://portal-data-bucket/tmp/proposal.json", environ=MACHINE) == 3 and "--direct" in capsys.readouterr().err


def test_a_state_machine_and_a_local_folder_named_together_is_a_contradiction(env, capsys):
    env.bootstrap()
    env.sfn = FakeSfn()
    assert env.run("--data", str(env.store.root), "--state-machine", SFN_ARN, "pause") == 3
    assert "不能和本機資料夾一起用" in capsys.readouterr().err and env.sfn.started == [] and P.get_hold(env.store) is None


def test_with_everything_from_the_environment_actions_go_through_the_state_machine(cloud):
    assert cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes") == 0  # (bootstrap is always direct)
    base = cloud.live().release_id
    assert cloud.run("publish", str(FIXTURES / "accepted_run.json"), "--direct") == 4  # a pending proposal to decide about
    sha = P.load_pending(cloud.control, PROP)["proposalSha256"]
    assert cloud.run("approve", PROP, environ=MACHINE) == 0 and "透過 state machine 執行" in cloud.out
    assert cloud.run("discard", PROP, "--reason", "不需要", environ=MACHINE) == 0
    assert cloud.run("pause", "--reason", "連假", environ=MACHINE) == 0
    assert cloud.run("resume", environ=MACHINE) == 0
    assert cloud.run("publish", "s3://portal-data-bucket/research/runs/run-20261002T030000Z-49c8b9.json", "--mode", "review", environ=MACHINE) == 0
    assert cloud.inputs() == [
        {"action": "approve", "by": "eunice", "proposalId": PROP, "sha256": sha},  # what is stored now is what gets approved
        {"action": "discard", "by": "eunice", "proposalId": PROP, "reason": "不需要"},
        {"action": "pause", "by": "eunice", "reason": "連假"},
        {"action": "resume", "by": "eunice"},
        {"action": "publish", "runKey": "runs/run-20261002T030000Z-49c8b9.json", "by": "eunice", "mode": "review"},
    ]
    assert P.get_hold(cloud.store) is None and cloud.live().release_id == base and len(P.list_pending(cloud.control)) == 1  # nothing was done directly


def test_a_data_location_named_on_the_command_line_is_acted_on_directly(cloud):
    """The ARN in the environment belongs to some deployment; nothing says it is the one for the location just named."""
    cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes")
    assert cloud.run("--data", BUCKET_URI, "pause", "--reason", "直接", environ=MACHINE) == 0
    assert cloud.inputs() == [] and P.get_hold(cloud.store)["reason"] == "直接" and "要經由 pipeline，請同時加上 --state-machine" in cloud.out
    assert cloud.run("resume", "--direct", environ=MACHINE) == 0 and cloud.inputs() == [] and P.get_hold(cloud.store) is None
    # naming both is an explicit request to go through that state machine
    assert cloud.run("--data", BUCKET_URI, "--state-machine", SFN_ARN, "pause", "--reason", "經由 pipeline") == 0
    assert cloud.inputs() == [{"action": "pause", "by": "eunice", "reason": "經由 pipeline"}] and P.get_hold(cloud.store) is None
    assert cloud.run("pause", "--state-machine", SFN_ARN, "--by", "amy") == 0 and cloud.inputs()[-1] == {"action": "pause", "by": "amy"}


def test_a_rollback_through_the_state_machine_names_both_releases(cloud):
    """ "Back to the previous one" must not mean something else by the time the execution runs."""
    cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes")
    base = cloud.live().release_id
    assert cloud.run("publish", str(FIXTURES / "accepted_run.json"), "--mode", "auto", "--direct") == 0
    agent = cloud.live().release_id
    assert cloud.run("rollback", "--reason", "日期有疑問", environ=MACHINE) == 0
    assert cloud.inputs() == [{"action": "rollback", "by": "eunice", "reason": "日期有疑問", "releaseId": base, "ifCurrent": agent}]
    assert cloud.run("rollback", "--to", base, environ=MACHINE) == 0 and cloud.inputs()[-1] == {"action": "rollback", "by": "eunice", "releaseId": base}
    assert cloud.run("rollback", "--direct") == 0 and cloud.live().release_id == base
    assert cloud.run("rollback", environ=MACHINE) == 3 and len(cloud.inputs()) == 2  # live is the result of a rollback: say so, start nothing


def test_approving_through_the_state_machine_checks_that_there_is_something_to_approve(cloud, capsys):
    cloud.run("bootstrap", "--portal", str(cloud.portal), "--yes")
    assert cloud.run("approve", "prop-nobody-held-this", environ=MACHINE) == 3 and cloud.inputs() == []
    assert "沒有待核准的提案" in capsys.readouterr().err
    assert cloud.run("approve", PROP, "--sha256", "not-a-hash", environ=MACHINE) == 3 and cloud.inputs() == []


def test_without_read_access_the_request_is_sent_as_it_is(env):
    """An operator who may only start executions: the state machine resolves and checks everything itself."""
    env.sfn = FakeSfn(output={"publish": {"status": "published"}, "notification": {"message": "ok"}})
    environ = {"PORTAL_DATA_URI": "s3://some-bucket/portal/", "PORTALCTL_BY": "eunice", **MACHINE}
    out = io.StringIO()
    clients = {"s3": object(), "stepfunctions": env.sfn}  # an S3 client every call of which fails
    assert CTL.main(["approve", PROP], environ=environ, clients=clients, out=out, stdin=io.StringIO("")) == 0
    assert CTL.main(["rollback", "--reason", "回復"], environ=environ, clients=clients, out=out, stdin=io.StringIO("")) == 0
    assert [json.loads(s["input"]) for s in env.sfn.started] == [{"action": "approve", "by": "eunice", "proposalId": PROP},
                                                                 {"action": "rollback", "by": "eunice", "reason": "回復"}]


def test_executions_lists_recent_runs_in_taipei_time(env):
    env.sfn = FakeSfn()
    assert env.run("executions", environ=ONLINE) == 0
    assert "SUCCEEDED  2026-10-05 09:00（台北）  weekly-1" in env.out and "FAILED" in env.out


def test_history_shows_everything_the_pointer_keeps(env):
    env.bootstrap()
    for i in range(25):
        (env.portal / "runs" / "perf" / f"r{i}.json").write_bytes(tool_file(PERF, runId=f"perf-20261003T0100{i:02d}Z-aaaaaa"))
        assert env.run("push", "--portal", str(env.portal), "--only", "runs", "--yes") == 0
    def releases(out):  # a release has a line of its own; what its summary says stands under it, indented
        return [line for line in out.splitlines() if line[2:3].strip()]

    assert env.run("history") == 0 and len(releases(env.out)) == 26  # not just the first 20
    assert env.run("history", "--limit", "5") == 0 and len(releases(env.out)) == 5 and env.out.startswith("→ ")
    assert all(line.startswith("      ") for line in env.out.splitlines() if not line[2:3].strip())


# ---------------------------------------------------------------------------------------------- where the state lives
def ctx_for(argv, environ):
    return CTL.Ctx(CTL.build_parser().parse_args(argv), environ, {"s3": object()}, io.StringIO(), io.StringIO(""))


@pytest.mark.parametrize("data, expected", [
    ("s3://bucket/portal/", "s3://bucket/pipeline/"), ("s3://bucket/portal", "s3://bucket/pipeline/"),
    ("s3://bucket/env/prod/portal/", "s3://bucket/env/prod/pipeline/"),
])
def test_the_pipeline_state_lives_next_to_the_data_by_default(data, expected):
    assert ctx_for(["status"], {"PORTAL_DATA_URI": data}).control().uri() == expected
    assert ctx_for(["--state", "s3://other/state/", "status"], {"PORTAL_DATA_URI": data}).control().uri() == "s3://other/state/"
    assert ctx_for(["status", "--state", "s3://other/state/"], {"PORTAL_DATA_URI": data}).control().uri() == "s3://other/state/"


@pytest.mark.parametrize("data", ["s3://my-bucket/", "s3://my-bucket", "s3://bucket/data", "s3://bucket/portal-data/", "s3://portal"])
def test_the_state_location_is_not_guessed_for_other_layouts(data):
    """`s3://my-bucket/` used to become `s3://my-bucket-pipeline/`: another bucket altogether."""
    with pytest.raises(CTL.ScriptError, match="--state"):
        ctx_for(["status"], {"PORTAL_DATA_URI": data}).control()
    assert ctx_for(["status"], {"PORTAL_DATA_URI": data, "PIPELINE_STATE_URI": "s3://b/state/"}).control().uri() == "s3://b/state/"


def test_switches_are_understood_before_and_after_the_command(env):
    env.bootstrap()
    for argv in (["--direct", "--by", "amy", "pause", "--reason", "a"], ["pause", "--reason", "a", "--direct", "--by", "amy"],
                 ["pause", "--by", "amy", "--reason", "a"], ["--by", "eunice", "pause", "--reason", "a", "--by", "amy"]):
        assert env.run(*argv) == 0, argv
        assert P.get_hold(env.store)["by"] == "amy"
        env.run("resume")
    assert env.run("status", "--data", str(env.store.root), environ={"PORTAL_DATA_URI": "/nowhere"}) == 0 and env.live().release_id in env.out


# ---------------------------------------------------------------------------------------------- verify
import urllib.request  # noqa: E402

import serve_data  # noqa: E402

TOKEN = "verify-token-0123456789"
UI_ORIGIN = "https://main.d111.amplifyapp.com"


def direct_http(url, headers):
    """portalctl's own HTTP function, minus any proxy the test machine is configured with."""
    return CTL.http_get(url, headers, opener=urllib.request.build_opener(urllib.request.ProxyHandler({})))


@pytest.fixture()
def delivered(env):
    """The bootstrapped data location, served the way the distribution serves it."""
    env.bootstrap()
    servers = []

    def serve(**options):
        server = serve_data.make_server(env.store.root, port=0, quiet=True, **{"token": TOKEN, "origins": (UI_ORIGIN,), **options})
        serve_data.serve_in_thread(server)
        servers.append(server)
        return serve_data.base_url(server, options.get("token", TOKEN))

    def verify(*argv, environ=None, http=None):
        out = io.StringIO()
        environ = {"PORTAL_DATA_URI": str(env.store.root), "PORTALCTL_BY": "eunice"} if environ is None else environ
        code = CTL.main(["verify", *argv], environ=environ, clients={"http": http or direct_http}, out=out, stdin=io.StringIO(""))
        env.out = out.getvalue()
        assert N.MARK not in env.out and "\\x1f" not in env.out, env.out
        return code

    env.serve, env.verify = serve, verify
    yield env
    for server in servers:
        server.shutdown()
        server.server_close()


def test_verify_checks_the_store_and_what_a_browser_gets(delivered):
    env = delivered
    url = env.serve()
    assert env.verify("--url", url, "--origin", UI_ORIGIN) == 0, env.out
    assert "✘" not in env.out and "全部通過" in env.out
    for expected in (env.live().release_id, "每個檔案都和發布時的內容一致", "資料通過 schema 與交叉檢查", "manifest.json 不會被快取",
                     "release 的檔案可以長期快取", "沒有 token 的路徑被拒絕", f"允許 {UI_ORIGIN} 讀取", "和資料位置上線中的版本一致"):
        assert expected in env.out, expected
    assert TOKEN not in env.out  # the report is pasted into tickets and chats: it must not carry the access token


def test_verify_without_a_url_checks_the_store_only(delivered):
    assert delivered.verify() == 0 and "全部通過" in delivered.out and "--url" in delivered.out


def test_verify_with_only_a_url_needs_no_aws_access(delivered):
    url = delivered.serve()
    assert delivered.verify("--url", url, environ={}) == 0, delivered.out
    assert "每個檔案都和 release.json 記錄的內容一致" in delivered.out and "沒有指定資料位置" in delivered.out


def test_verify_reports_a_file_that_is_not_what_was_published(delivered):
    env = delivered
    url = env.serve()
    release = env.live().release_id
    (env.store.root / "releases" / release / "data" / "catalog.json").write_text("{}", encoding="utf-8")
    assert env.verify("--url", url) == 1
    assert "✘" in env.out and "data/catalog.json" in env.out and "有 " in env.out


def test_verify_reports_a_page_that_would_be_blocked_by_cors(delivered):
    url = delivered.serve()
    assert delivered.verify("--url", url, "--origin", "https://another-site.example") == 1
    assert "✘" in delivered.out and "https://another-site.example" in delivered.out and "AllowedOrigins" in delivered.out


def test_verify_reports_a_data_location_that_is_open_to_anyone(delivered):
    url = delivered.serve(token="", origins=("*",))
    assert delivered.verify("--url", url, "--origin", UI_ORIGIN) == 0
    assert "沒有 token" in delivered.out and "任何網站" in delivered.out  # said plainly, but a choice rather than a failure


def test_verify_shows_the_headers_it_objects_to_as_they_were_sent(delivered):
    """A header value is text from elsewhere like any other: shown in quotes, cleaned, and without the marks this tool
    puts around such text while it lays a line out (they were printed as `\\x1f` when the value held a blank)."""
    env = delivered
    url = env.serve()

    def cdn(target, headers):
        status, got, body = direct_http(target, headers)
        got = dict(got)
        got["cache-control"] = "public, max-age=60\x1b[2K, stale-while-revalidate=30"
        if target.endswith("data/catalog.json"):
            got["content-type"] = "text/html; charset=utf-8"
        return status, got, body

    assert env.verify("--url", url, "--origin", UI_ORIGIN, http=cdn) == 1
    said = squash(env.out)
    assert squash("manifest.json 的 Cache-Control 是「public, max-age=60 [2K, stale-while-revalidate=30」：新的 release 會延遲出現") in said
    assert squash("release 檔案的 Cache-Control 是「public, max-age=60 [2K, stale-while-revalidate=30」") in said
    assert squash("data/catalog.json: Content-Type 是「text/html; charset=utf-8」") in said and "\x1b" not in env.out


def test_verify_reports_a_wrong_or_unreachable_url(delivered):
    url = delivered.serve()
    assert delivered.verify("--url", url.replace(TOKEN, "not-the-token-000000000")) == 1 and "HTTP 403" in delivered.out
    assert delivered.verify("--url", "http://127.0.0.1:9/nothing/") == 1 and "連不上" in delivered.out


def test_verify_notices_a_url_that_serves_another_data_location(delivered, tmp_path):
    env = delivered
    other = tmp_path / "other" / "portal"
    P.publish_tree(DirStore(other), {**snapshot_tree(), "config/extra.md": b"x\n"}, bootstrap=True, actor="test")
    server = serve_data.make_server(other, port=0, quiet=True)
    serve_data.serve_in_thread(server)
    try:
        assert env.verify("--url", serve_data.base_url(server)) == 1
        assert "不是資料位置上線中的版本" in env.out
    finally:
        server.shutdown()
        server.server_close()


def test_verify_reports_data_problems_and_the_pause_flag(delivered):
    env = delivered
    env.run("pause", "--reason", "檢查中")
    assert env.verify() == 0 and "已暫停" in env.out  # paused is a state, not a failure
    bad = {**env.live().files, "runs/perf/bad.json": b'{"results": []}\n'}
    assert P.publish_tree(env.store, bad, only=["runs"], allow_invalid=True, actor="test").status == "published"
    assert env.verify() == 1 and "runs/perf/bad.json" in env.out and "已暫停" in env.out  # a push by hand does not lift the pause


def test_what_will_not_work_as_written_is_told_and_does_not_stop_the_first_release(env):
    """The portal's own data holds an `avoid` entry the Gateway page shows and never matches.  `bootstrap` publishes
    it - saying so - and `verify` passes, listing it; a file the UI cannot read is still refused."""
    from test_publish import REAL_AVOID

    doc = json.loads((env.portal / "data" / "litellm.json").read_text(encoding="utf-8"))
    edit_json(env.portal / "data" / "litellm.json", avoid=[*doc["avoid"], REAL_AVOID])
    assert env.run("bootstrap", "--portal", str(env.portal), "--yes") == 0, env.out
    assert "✔ 已發布" in env.out and squash("注意：資料提醒（不影響發布）：data/litellm.json: avoid:") in squash(env.out)
    assert squash("（有啟用 semantic cache 的多租戶部署）") in squash(env.out) and squash("is not a range the Gateway page can compute with") in squash(env.out)
    assert env.run("verify") == 0, env.out
    assert "資料通過 schema 與交叉檢查" in env.out and "資料有 1 處提醒（資料可以用，但這些值不會照字面運作）：" in env.out
    assert squash("data/litellm.json: avoid:") in squash(env.out) and "✘" not in env.out
    # the same checkout with an entry no page can read at all
    edit_json(env.portal / "data" / "litellm.json", avoid=[*doc["avoid"], REAL_AVOID, dict(REAL_AVOID, versions="1.")])
    assert env.run("push", "--portal", str(env.portal), "--yes") == 4 and squash('資料驗證：data/litellm.json: avoid:') in squash(env.out)
    assert "--allow-invalid" in env.out and env.run("verify") == 0


def test_verify_before_the_first_release(env):
    assert env.run("verify") == 1 and "bootstrap" in env.out


def test_verify_with_nothing_to_check_is_not_a_pass(capsys):
    """It used to print "all checks passed" and exit 0 - having checked nothing."""
    out = io.StringIO()
    assert CTL.main(["verify"], environ={}, clients={}, out=out, stdin=io.StringIO("")) == 3
    assert "沒有可以檢查的對象" in capsys.readouterr().err and "通過" not in out.getvalue()


def test_verify_notices_a_file_and_its_recorded_hash_edited_together(delivered):
    """The list of hashes lives in release.json; the pointer records the hash of that file, so the list cannot vouch for itself."""
    env = delivered
    url = env.serve()
    release = env.live().release_id
    folder = env.store.root / "releases" / release
    (folder / "data" / "catalog.json").write_text("{}", encoding="utf-8")
    meta = json.loads((folder / "release.json").read_text(encoding="utf-8"))
    meta["files"]["data/catalog.json"] = {"sha256": R.sha256_hex(b"{}"), "bytes": 2}
    (folder / "release.json").write_text(json.dumps(meta), encoding="utf-8")
    assert env.verify() == 1 and "release.json is not the one that was published" in env.out
    assert env.verify("--url", url, environ={}) == 1 and "不是 manifest.json 記錄的那一份" in env.out
    with pytest.raises(R.IntegrityError):
        env.live()  # ... and the publisher refuses to build on it


def test_verify_does_not_pass_a_release_that_lists_no_files(delivered):
    env = delivered
    url = env.serve()
    release = env.live().release_id
    path = env.store.root / "releases" / release / "release.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["files"] = {}
    path.write_text(json.dumps(meta), encoding="utf-8")
    manifest, etag = R.read_pointer(env.store)
    manifest.pop("releaseSha256")  # (as an old pointer would be: without the pin, the empty list is all there is to go by)
    R.switch_pointer(env.store, manifest, etag)
    assert env.verify() == 1 and "沒有列出任何檔案" in env.out
    assert env.verify("--url", url, environ={}) == 1 and "沒有列出任何檔案" in env.out


@pytest.mark.parametrize("mangle", [lambda u: u + " ", lambda u: u.replace("/" + TOKEN, "/ " + TOKEN), lambda u: u + "\n", lambda u: u.replace("http://", "http://[")])
def test_a_mistyped_url_fails_a_check_without_printing_the_token(delivered, mangle, capsys):
    url = mangle(delivered.serve())
    assert delivered.verify("--url", url, environ={}) == 1
    assert TOKEN not in delivered.out and TOKEN not in capsys.readouterr().err and "✘" in delivered.out
