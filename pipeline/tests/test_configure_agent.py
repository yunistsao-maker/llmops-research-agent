"""scripts/configure_agent.py: the runtime's settings come from the data plane stack, not from hand-edited JSON."""

from __future__ import annotations

import json
import shutil

import pytest

import configure_agent as C
from conftest import REPO

BUCKET = "llmops-research-support-databucket-abc123"
ARN = "arn:aws:iam::123456789012:policy/llmops-research-support-RuntimeDataPolicy-1ABCDEFGHIJKL"
VALUES = ["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN]


class Stacks:
    """cloudformation.describe_stacks for one stack."""

    def __init__(self, outputs: dict[str, str] | None = None, error: Exception | None = None):
        self.outputs = {"PortalDataUri": f"s3://{BUCKET}/portal/", "OutputUri": f"s3://{BUCKET}/research/", "RuntimeDataPolicyArn": ARN,
                        "DataBaseUrl": "https://d111.cloudfront.net/token-token-token-1/"} if outputs is None else outputs
        self.error = error
        self.asked: list[str] = []

    def describe_stacks(self, StackName):  # noqa: N803 - boto3's spelling
        self.asked.append(StackName)
        if self.error:
            raise self.error
        return {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.outputs.items()]}]}


@pytest.fixture()
def config(tmp_path):
    """A copy of the agentcore.json this project ships."""
    path = tmp_path / "agentcore.json"
    shutil.copy(REPO / "agentcore" / "agentcore.json", path)
    return path


def runtime(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["runtimes"][0]


def env(path) -> dict:
    return {e["name"]: e["value"] for e in runtime(path)["envVars"]}


def test_the_shipped_config_is_not_configured_for_any_account(config, capsys):
    """Nothing account-specific is in the file that is delivered - and the check says what deploying it as it is would do."""
    assert C.main(["--file", str(config), "--check"]) == 1
    out = capsys.readouterr().out
    assert "PORTAL_DATA_URI" in out and "SAMPLE" in out and "OUTPUT_URI" in out
    assert "additionalPolicies 沒有 data plane stack 的 RuntimeDataPolicy" in out and "讀不到 portal/" in out


def test_the_three_settings_are_written_from_the_stack_outputs(config, capsys):
    before = runtime(config)
    stacks = Stacks()
    assert C.main(["--file", str(config), "--stack", "llmops-research-support"], client=stacks) == 0
    assert stacks.asked == ["llmops-research-support"]
    after = runtime(config)
    assert env(config)["PORTAL_DATA_URI"] == f"s3://{BUCKET}/portal/" and env(config)["OUTPUT_URI"] == f"s3://{BUCKET}/research/"
    assert after["additionalPolicies"] == [ARN]
    # everything else in the runtime is as it was (model, budgets, gateway ...)
    for key in before:
        if key != "envVars":
            assert after[key] == before[key], key
    assert [e for e in after["envVars"] if e["name"] not in ("PORTAL_DATA_URI", "OUTPUT_URI")] == before["envVars"]
    out = capsys.readouterr().out
    assert "agentcore validate && agentcore deploy" in out and "https://d111" not in out  # the data URL (token) is none of its business
    assert C.main(["--file", str(config), "--stack", "llmops-research-support", "--check"], client=Stacks()) == 0
    assert config.read_text(encoding="utf-8").endswith("}\n")


def test_running_it_again_changes_nothing(config, capsys):
    assert C.main(["--file", str(config), *VALUES]) == 0
    first = config.read_bytes()
    capsys.readouterr()
    assert C.main(["--file", str(config), *VALUES]) == 0
    assert config.read_bytes() == first and "沒有變更" in capsys.readouterr().out


def test_existing_settings_are_updated_not_duplicated(config, capsys):
    doc = json.loads(config.read_text(encoding="utf-8"))
    r = doc["runtimes"][0]
    r["envVars"] += [{"name": "PORTAL_DATA_URI", "value": "s3://old-bucket/portal/"}, {"name": "PORTAL_DATA_URI", "value": "bundled"},
                     {"name": "OUTPUT_URI", "value": f"s3://{BUCKET}/research/"}]
    old = "arn:aws:iam::123456789012:policy/old-stack-RuntimeDataPolicy-ZZZZ"
    browser = "arn:aws:iam::123456789012:policy/agentcore-browser-access"
    r["additionalPolicies"] = [browser, old]
    config.write_text(json.dumps(doc), encoding="utf-8")
    assert C.main(["--file", str(config), *VALUES]) == 0
    names = [e["name"] for e in runtime(config)["envVars"]]
    assert names.count("PORTAL_DATA_URI") == 1 and names.count("OUTPUT_URI") == 1 and env(config)["PORTAL_DATA_URI"] == f"s3://{BUCKET}/portal/"
    assert runtime(config)["additionalPolicies"] == [browser, ARN]  # other policies stay; the same policy of another stack is replaced
    out = capsys.readouterr().out
    assert "s3://old-bucket/portal/" in out and "移除舊的" in out and "移除重複的 PORTAL_DATA_URI" in out


def test_the_check_compares_with_the_stack(config, capsys):
    assert C.main(["--file", str(config), *VALUES]) == 0
    capsys.readouterr()
    moved = Stacks({"PortalDataUri": "s3://another-bucket/portal/", "OutputUri": "s3://another-bucket/research/", "RuntimeDataPolicyArn": ARN + "X"})
    assert C.main(["--file", str(config), "--check", "--stack", "s"], client=moved) == 1
    out = capsys.readouterr().out
    assert f"PORTAL_DATA_URI 是 s3://{BUCKET}/portal/，stack 的 output 是 s3://another-bucket/portal/" in out
    assert f"OUTPUT_URI 是 s3://{BUCKET}/research/，stack 的 output 是 s3://another-bucket/research/" in out and ARN + "X" in out
    assert f"還有另一個 data plane stack 的 policy：{ARN}" in out
    assert C.main(["--file", str(config), "--check"]) == 0  # without a stack: the three things are there, and each has the right form
    assert "形式也正確" in capsys.readouterr().out


@pytest.mark.parametrize("args, expect", [
    ([], "要寫入什麼"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/"], "沒有 OUTPUT_URI；沒有 policy ARN"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN], "PORTAL_DATA_URI 應該像"),
    (["--portal-data-uri", "https://d111.cloudfront.net/token/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN], "不是 S3 位置"),
    (["--portal-data-uri", "bundled", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN], "PORTAL_DATA_URI 是 'bundled'，不是 S3 位置"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", "/tmp/out", "--policy-arn", ARN], "OUTPUT_URI 是 '/tmp/out'，不是 S3 位置"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/../x/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN], "路徑裡沒有 . 或 .."),
    (["--portal-data-uri", f"s3://{BUCKET}/portal//", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN], "PORTAL_DATA_URI 應該像"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", "RuntimeDataPolicy"], "policy ARN 應該像"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", "arn:aws:iam::aws:policy/AdministratorAccess"],
     "是 AWS 的受管 policy"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", f"s3://{BUCKET}/portal/", "--policy-arn", ARN], "不能是同一個位置"),
    (["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", f"s3://{BUCKET}/portal/releases/", "--policy-arn", ARN], "研究結果會寫進 UI 讀的資料裡"),
    (["--portal-data-uri", f"s3://{BUCKET}/research/portal/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", ARN], "和研究結果混在一起"),
    (["--portal-data-uri", f"s3://{BUCKET}/research/", "--output-uri", f"s3://{BUCKET}/portal/", "--policy-arn", ARN], "看起來填反了"),
    (["--stack", "s", "--output-uri", f"s3://{BUCKET}/research/"], "擇一使用"),
    (["--runtime", "nope", *VALUES], "沒有名為 nope 的 runtime"),
])
def test_wrong_input_is_refused_and_nothing_is_written(config, capsys, args, expect):
    before = config.read_bytes()
    assert C.main(["--file", str(config), *args], client=Stacks()) == 3
    assert expect in capsys.readouterr().err and config.read_bytes() == before


def test_a_stack_that_is_not_the_data_plane_or_cannot_be_read_is_said_to_be_so(config, capsys):
    assert C.main(["--file", str(config), "--stack", "llmops-portal-pipeline"], client=Stacks({"StateMachineArn": "arn:…"})) == 3
    assert "沒有 output PortalDataUri、OutputUri、RuntimeDataPolicyArn" in capsys.readouterr().err
    assert C.main(["--file", str(config), "--stack", "missing"], client=Stacks(error=RuntimeError("Stack with id missing does not exist"))) == 3
    err = capsys.readouterr().err
    assert "讀不到 stack missing" in err and "does not exist" in err  # with the reason the service gave


def test_files_that_are_not_an_agentcore_project_are_refused(tmp_path, capsys):
    for content, expect in (("{not json", "不是有效的 JSON"), ("[]", "不是 AgentCore CLI 的專案設定檔"), ('{"runtimes": []}', "沒有 runtimes"),
                            ('{"runtimes": [{"name": "a"}, {"name": "b"}]}', "用 --runtime 指定")):
        path = tmp_path / "agentcore.json"
        path.write_text(content, encoding="utf-8")
        assert C.main(["--file", str(path), *VALUES]) == 3
        assert expect in capsys.readouterr().err, content
    assert C.main(["--file", str(tmp_path / "missing.json"), *VALUES]) == 3


def edited(config, change) -> None:
    doc = json.loads(config.read_text(encoding="utf-8"))
    change(doc["runtimes"][0])
    config.write_text(json.dumps(doc, indent=2), encoding="utf-8")


@pytest.mark.parametrize("change, expect", [
    # a runtime that would read the SAMPLE data, write nowhere a pipeline looks, and has some unrelated policy
    (lambda r: (r["envVars"].extend([{"name": "PORTAL_DATA_URI", "value": "bundled"}, {"name": "OUTPUT_URI", "value": "/tmp/out"}]),
                r.update(additionalPolicies=["arn:aws:iam::123456789012:policy/agentcore-browser-access"])),
     ["PORTAL_DATA_URI 是 'bundled'，不是 S3 位置", "OUTPUT_URI 是 '/tmp/out'，不是 S3 位置", "沒有 data plane stack 的 RuntimeDataPolicy"]),
    # the same variable twice: the first entry is the one a later write would keep, the second the one a reader of a dict sees
    (lambda r: (r["envVars"].extend([{"name": "PORTAL_DATA_URI", "value": "s3://some-other-bucket/portal/"}, {"name": "PORTAL_DATA_URI", "value": f"s3://{BUCKET}/portal/"},
                                     {"name": "OUTPUT_URI", "value": f"s3://{BUCKET}/research/"}]), r.update(additionalPolicies=[ARN])),
     ["envVars 有 2 個 PORTAL_DATA_URI", "只能留一個"]),
    (lambda r: (r["envVars"].extend([{"name": "PORTAL_DATA_URI", "value": f"s3://{BUCKET}/portal/"}, {"name": "OUTPUT_URI", "value": f"s3://{BUCKET}/portal/runs/"}]),
                r.update(additionalPolicies=[ARN, ARN])),
     ["研究結果會寫進 UI 讀的資料裡", "additionalPolicies 有重複的 ARN"]),
    (lambda r: (r["envVars"].extend([{"name": "PORTAL_DATA_URI", "value": f"s3://{BUCKET}/portal/"}, {"name": "OUTPUT_URI", "value": f"s3://{BUCKET}/research/"}]),
                r.update(additionalPolicies=["arn:aws:iam::aws:policy/AdministratorAccess"])),
     ["沒有 data plane stack 的 RuntimeDataPolicy"]),
])
def test_the_check_reads_what_is_there_not_only_that_something_is(config, capsys, change, expect):
    edited(config, change)
    assert C.main(["--file", str(config), "--check"]) == 1
    out = capsys.readouterr().out
    for line in expect:
        assert line in out, (line, out)


@pytest.mark.parametrize("portal, output, expect", [
    # each of the two is a well-formed S3 location - and together they are not what any one data plane stack puts out
    ("s3://bucket-a/portal/", "s3://bucket-b/research/", "PORTAL_DATA_URI 在 bucket bucket-a，OUTPUT_URI 在 bucket bucket-b"),
    (f"s3://{BUCKET}/data/", f"s3://{BUCKET}/research/", f"PORTAL_DATA_URI 是 s3://{BUCKET}/data/：data plane stack 的 output 是 s3://{BUCKET}/portal/"),
    (f"s3://{BUCKET}/portal/", f"s3://{BUCKET}/results/", f"OUTPUT_URI 是 s3://{BUCKET}/results/：data plane stack 的 output 是"),
    (f"s3://{BUCKET}/prod/portal/", f"s3://{BUCKET}/prod/research/", "pipeline 和 runtime 的權限只給這兩個位置"),
])
def test_two_locations_that_no_stack_puts_out_together_are_not_configured(config, capsys, portal, output, expect):
    """The pipeline fetches results from, and publishes to, one bucket's research/ and portal/: a runtime that reads or
    writes anywhere else runs, and nothing it produces arrives.  The check must not call that "configured"."""
    before = config.read_bytes()
    assert C.main(["--file", str(config), "--portal-data-uri", portal, "--output-uri", output, "--policy-arn", ARN]) == 3  # not written ...
    assert expect in capsys.readouterr().err and config.read_bytes() == before
    edited(config, lambda r: (r["envVars"].extend([{"name": "PORTAL_DATA_URI", "value": portal}, {"name": "OUTPUT_URI", "value": output}]),
                              r.update(additionalPolicies=["arn:aws:iam::999999999999:policy/other-RuntimeDataPolicy-1"])))
    assert C.main(["--file", str(config), "--check"]) == 1                                                                 # ... and found when it is there
    out = capsys.readouterr().out
    assert expect in out and "還沒設定好" in out and "形式也正確" not in out


def test_the_check_against_a_stack_sees_a_second_entry_too(config, capsys):
    edited(config, lambda r: (r["envVars"].extend([{"name": "PORTAL_DATA_URI", "value": "s3://some-other-bucket/portal/"},
                                                    {"name": "PORTAL_DATA_URI", "value": f"s3://{BUCKET}/portal/"},
                                                    {"name": "OUTPUT_URI", "value": f"s3://{BUCKET}/research/"}]), r.update(additionalPolicies=[ARN])))
    assert C.main(["--file", str(config), "--check", "--stack", "s"], client=Stacks()) == 1
    out = capsys.readouterr().out
    assert "envVars 有 2 個 PORTAL_DATA_URI" in out and "PORTAL_DATA_URI 是 s3://some-other-bucket/portal/，stack 的 output 是" in out
    assert C.main(["--file", str(config), "--stack", "s"], client=Stacks()) == 0                 # writing puts it right ...
    assert C.main(["--file", str(config), "--check", "--stack", "s"], client=Stacks()) == 0      # ... and then the check agrees


def test_policies_of_this_kind_are_kept_once_and_only_the_current_one(config, capsys):
    old = "arn:aws:iam::123456789012:policy/old-RuntimeDataPolicy-ZZZ"
    browser = "arn:aws:iam::123456789012:policy/agentcore-browser-access"
    edited(config, lambda r: r.update(additionalPolicies=[ARN, browser, ARN, old]))
    assert C.main(["--file", str(config), *VALUES]) == 0
    assert runtime(config)["additionalPolicies"] == [browser, ARN]
    out = capsys.readouterr().out
    assert "移除舊的" in out and "移除重複的" in out


@pytest.mark.parametrize("change, expect", [
    (lambda r: r.update(envVars=None), "envVars 不是陣列"),
    (lambda r: r.update(envVars={"PORTAL_DATA_URI": "x"}), "envVars 不是陣列"),
    (lambda r: r["envVars"].extend(["x", None]), "不是 {\"name\": …, \"value\": …} 的項目"),
    (lambda r: r["envVars"].append({"value": "no name"}), "不是 {\"name\": …, \"value\": …} 的項目"),
    (lambda r: r.update(additionalPolicies=ARN), "additionalPolicies 不是 ARN 的陣列"),
    (lambda r: r.update(additionalPolicies=[ARN, 5]), "additionalPolicies 不是 ARN 的陣列"),
])
def test_a_file_with_entries_of_the_wrong_kind_is_left_alone(config, capsys, change, expect):
    """Writing the three settings next to an entry the CLI will not accept would only move the error to `agentcore validate`."""
    edited(config, change)
    before = config.read_bytes()
    for args in (VALUES, ["--check"]):
        assert C.main(["--file", str(config), *args]) == 3
        assert expect in capsys.readouterr().err and config.read_bytes() == before


def test_a_name_that_does_not_look_like_the_data_policy_is_pointed_out(config, capsys):
    other = "arn:aws:iam::123456789012:policy/my-own-policy"
    args = ["--portal-data-uri", f"s3://{BUCKET}/portal/", "--output-uri", f"s3://{BUCKET}/research/", "--policy-arn", other]
    assert C.main(["--file", str(config), *args]) == 0
    assert "名稱不像 data plane stack 建立的 RuntimeDataPolicy" in capsys.readouterr().out and runtime(config)["additionalPolicies"] == [other]


def test_a_stack_that_cannot_be_asked_about_is_one_line_not_a_traceback(config, monkeypatch, capsys):
    """No region configured: the client cannot even be made.  Exit code 1 would mean "checked, and not configured"."""
    import subprocess
    import sys

    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_EC2_METADATA_DISABLED="true", HOME=str(config.parent))
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "configure_agent.py"), "--stack", "llmops-research-support", "--file", str(config)],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 3, proc.stderr
    assert "讀不到 stack llmops-research-support" in proc.stderr and "--region" in proc.stderr and "Traceback" not in proc.stderr


def test_a_file_nested_too_deep_to_read_is_one_line_with_the_code_for_bad_input(tmp_path):
    """json.loads gives up with RecursionError, which is not a ValueError: it used to end as a traceback with exit code 1 -
    the code that means "checked: not configured yet"."""
    import subprocess
    import sys

    path = tmp_path / "agentcore.json"
    path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    for args in (["--check"], VALUES):
        proc = subprocess.run([sys.executable, str(REPO / "scripts" / "configure_agent.py"), "--file", str(path), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert proc.returncode == 3, proc.stderr[-400:]
        assert "巢狀層數太深" in proc.stderr and "Traceback" not in proc.stderr and len(proc.stderr) < 600


def test_an_error_nobody_expected_does_not_leave_with_the_code_of_a_finished_check(config, capsys, monkeypatch):
    monkeypatch.setattr(C, "run", lambda *a, **k: (_ for _ in ()).throw(KeyError("runtimes")))
    assert C.main(["--file", str(config), "--check"]) == 3
    err = capsys.readouterr().err
    assert "非預期的錯誤（KeyError" in err and "沒有寫入任何東西" in err


def test_a_file_that_cannot_be_written_is_reported(config, capsys, monkeypatch):
    from pathlib import Path

    real = Path.write_text
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")) if self == config else real(self, *a, **k))
    assert C.main(["--file", str(config), *VALUES]) == 3
    assert "無法寫入" in capsys.readouterr().err
