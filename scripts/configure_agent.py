#!/usr/bin/env python3
"""把 data plane stack 的 output 寫進 agentcore/agentcore.json：讓 runtime 讀上線中的資料、把研究結果寫回 bucket。

    python scripts/configure_agent.py --stack llmops-research-support     # 從 stack 的 output 讀（需要 cloudformation:DescribeStacks）
    python scripts/configure_agent.py --portal-data-uri s3://<bucket>/portal/ --output-uri s3://<bucket>/research/ \\
                                      --policy-arn arn:aws:iam::<account>:policy/<RuntimeDataPolicy…>
    python scripts/configure_agent.py --check                             # 只檢查目前的設定，不寫入

它設定 runtime 的三件事，可以重複執行（已經正確的不會動）：

  envVars PORTAL_DATA_URI = <PortalDataUri>       agent 讀 manifest.json 指到的 release。沒有設定時讀的是內建的 SAMPLE，
                                                  依它產生的提案會被 pipeline 拒絕
  envVars OUTPUT_URI      = <OutputUri>           研究結果寫到 research/，pipeline 從這裡取
  additionalPolicies 含 <RuntimeDataPolicyArn>    runtime role 讀 portal/、寫 research/ 的權限。agentcore.json 裡沒有這一項時，
                                                  `agentcore deploy` 會把它從 role 上拿掉（deploy 是照這個檔案設定 role 的）

其他設定（模型、預算、Gateway …）不動。寫完之後：agentcore validate && agentcore deploy

Exit code：0 完成（或 --check 發現設定正確）· 1 --check 發現還沒設定好 · 2 參數錯誤 · 3 環境或輸入問題
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = PROJECT_ROOT / "agentcore" / "agentcore.json"
OK, PENDING, ENV = 0, 1, 3
#: stack output -> what it becomes in agentcore.json
OUTPUTS = {"PortalDataUri": "portal_data_uri", "OutputUri": "output_uri", "RuntimeDataPolicyArn": "policy_arn"}
ENV_NAMES = {"portal_data_uri": "PORTAL_DATA_URI", "output_uri": "OUTPUT_URI"}
_S3_PREFIX = re.compile(r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/(?:[A-Za-z0-9._~-]+/)*")
_POLICY_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:policy/[A-Za-z0-9+=,.@_/-]+")
_AWS_MANAGED = re.compile(r"arn:aws[a-z-]*:iam::aws:policy/.+")
#: the managed policy the data plane stack creates is named "<stack>-RuntimeDataPolicy-<suffix>" by CloudFormation
_OURS = re.compile(r":policy/(?:.*-)?RuntimeDataPolicy-[A-Za-z0-9]+$")


class ScriptError(Exception):
    """A problem with the environment or the input (exit code 3)."""


def say(text: str = "") -> None:
    print(text, flush=True)


def stack_outputs(stack: str, region: str | None = None, client: Any = None) -> dict[str, str]:
    """The three values, read from the data plane stack."""
    try:
        if client is None:
            try:
                import boto3
            except ImportError as e:
                raise ScriptError("--stack 需要 boto3：uv run --with-requirements scripts/requirements.txt python scripts/configure_agent.py …") from e
            client = boto3.client("cloudformation", **({"region_name": region} if region else {}))
        stacks = client.describe_stacks(StackName=stack)["Stacks"]
    except ScriptError:
        raise
    except Exception as e:  # botocore's errors carry the reason (no such stack, no credentials, no region ...)
        hint = "（用 --region 指定 stack 所在的 region，或設定 AWS_REGION）" if "region" in str(e).lower() else ""
        raise ScriptError(f"讀不到 stack {stack}：{type(e).__name__}: {e}{hint}") from e
    found = {o.get("OutputKey"): o.get("OutputValue") for o in (stacks[0].get("Outputs") or [])} if stacks else {}
    missing = [key for key in OUTPUTS if not found.get(key)]
    if missing:
        raise ScriptError(f"stack {stack} 沒有 output {'、'.join(missing)}：它是用 infra/data-plane.yaml 部署的嗎？")
    return {name: str(found[key]) for key, name in OUTPUTS.items()}


def location_problem(label: str, value: Any, example: str) -> str | None:
    """Why ``value`` cannot be where the runtime reads the portal data from / writes its results to (None: it can)."""
    if not isinstance(value, str) or not value:
        return f"沒有 {label}"
    if value == "bundled" or not value.startswith("s3://"):
        return f"{label} 是 {value!r}，不是 S3 位置（應該像 {example}）"
    if not _S3_PREFIX.fullmatch(value) or any(part in (".", "..") for part in value[len("s3://"):].split("/")):
        return f"{label} 應該像 {example}（結尾有 /，路徑裡沒有 . 或 ..），拿到的是 {value!r}"
    return None


def _bucket_and_prefix(uri: str) -> tuple[str, str]:
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    return bucket, prefix


def pair_problems(portal: Any, output: Any) -> list[str]:
    """What is wrong with the two locations taken together (each of them being fine on its own).

    The data plane stack puts both into one bucket, as ``portal/`` and ``research/``; the pipeline and the runtime's
    policy are written for exactly that.  Anything else is not one of its outputs, however well-formed it looks."""
    if not (isinstance(portal, str) and isinstance(output, str)) or location_problem("x", portal, "") or location_problem("x", output, ""):
        return []
    if portal == output:
        return ["PORTAL_DATA_URI 和 OUTPUT_URI 不能是同一個位置：一個是 UI 讀的資料（portal/），一個是研究結果（research/）"]
    if output.startswith(portal):
        return [f"OUTPUT_URI（{output}）在 PORTAL_DATA_URI（{portal}）底下：研究結果會寫進 UI 讀的資料裡"]
    if portal.startswith(output):
        return [f"PORTAL_DATA_URI（{portal}）在 OUTPUT_URI（{output}）底下：UI 讀的資料會和研究結果混在一起"]
    if portal.rstrip("/").endswith("/research") and output.rstrip("/").endswith("/portal"):
        return [f"PORTAL_DATA_URI（{portal}）和 OUTPUT_URI（{output}）看起來填反了"]
    (portal_bucket, portal_prefix), (output_bucket, output_prefix) = _bucket_and_prefix(portal), _bucket_and_prefix(output)
    if portal_bucket != output_bucket:
        return [f"PORTAL_DATA_URI 在 bucket {portal_bucket}，OUTPUT_URI 在 bucket {output_bucket}：data plane stack 的兩個 output 在同一個 bucket"
                "（PortalDataUri 與 OutputUri），pipeline 也只到那一個 bucket 取研究結果、發布資料。這兩個值不是同一個 stack 的"]
    odd = [f"{label} 是 {value}" for label, value, prefix, expected in (("PORTAL_DATA_URI", portal, portal_prefix, "portal/"),
                                                                     ("OUTPUT_URI", output, output_prefix, "research/")) if prefix != expected]
    if odd:
        return ["、".join(odd) + f"：data plane stack 的 output 是 s3://{portal_bucket}/portal/ 與 s3://{portal_bucket}/research/，"
                "pipeline 和 runtime 的權限只給這兩個位置"]
    return []


def policy_problem(arn: Any) -> str | None:
    if not isinstance(arn, str) or not arn:
        return "沒有 policy ARN"
    if _AWS_MANAGED.fullmatch(arn):
        return (f"{arn} 是 AWS 的受管 policy，不是 data plane stack 建立的 RuntimeDataPolicy：runtime role 需要的只有讀 portal/、"
                "寫 research/（stack 的 output RuntimeDataPolicyArn）")
    if not _POLICY_ARN.fullmatch(arn):
        return f"policy ARN 應該像 arn:aws:iam::<account>:policy/…，拿到的是 {arn!r}"
    return None


def check_values(values: dict[str, str | None]) -> dict[str, str]:
    problems = [p for p in (location_problem("PORTAL_DATA_URI", values.get("portal_data_uri"), "s3://<bucket>/portal/"),
                            location_problem("OUTPUT_URI", values.get("output_uri"), "s3://<bucket>/research/"),
                            policy_problem(values.get("policy_arn"))) if p]
    problems += pair_problems(values.get("portal_data_uri"), values.get("output_uri"))
    if problems:
        raise ScriptError("；".join(problems))
    return {k: str(v) for k, v in values.items()}


def pick_runtime(doc: dict, name: str | None) -> dict:
    runtimes = doc.get("runtimes")
    if not isinstance(runtimes, list) or not all(isinstance(r, dict) for r in runtimes) or not runtimes:
        raise ScriptError("agentcore.json 沒有 runtimes：這是 AgentCore CLI 的專案設定檔嗎？")
    if name is None:
        if len(runtimes) > 1:
            raise ScriptError(f"agentcore.json 有 {len(runtimes)} 個 runtime，用 --runtime 指定：{'、'.join(str(r.get('name')) for r in runtimes)}")
        return runtimes[0]
    for runtime in runtimes:
        if runtime.get("name") == name:
            return runtime
    raise ScriptError(f"agentcore.json 沒有名為 {name} 的 runtime（有：{'、'.join(str(r.get('name')) for r in runtimes)}）")


def _env(runtime: dict) -> list:
    env = runtime.get("envVars", [])
    if not isinstance(env, list):
        raise ScriptError("runtime 的 envVars 不是陣列")
    odd = [e for e in env if not (isinstance(e, dict) and isinstance(e.get("name"), str))]
    if odd:
        raise ScriptError(f"runtime 的 envVars 裡有 {len(odd)} 個不是 {{\"name\": …, \"value\": …}} 的項目（例如 {json.dumps(odd[0], ensure_ascii=False)[:60]}）："
                          "請先修正，agentcore validate 不會接受它")
    return env


def _policies(runtime: dict) -> list:
    policies = runtime.get("additionalPolicies", [])
    if policies is None:
        return []
    if not isinstance(policies, list) or not all(isinstance(p, str) for p in policies):
        raise ScriptError("runtime 的 additionalPolicies 不是 ARN 的陣列")
    return policies


def current(runtime: dict) -> dict[str, Any]:
    """What the file says now.  An environment variable set twice is reported with every value it has: which of them
    the deployed runtime ends up with is the deployment tool's business, and not something to guess."""
    env = _env(runtime)
    values = {key: [e.get("value") for e in env if e["name"] == name] for key, name in ENV_NAMES.items()}
    return {**values, "policies": list(_policies(runtime))}


def apply(runtime: dict, values: dict[str, str]) -> list[str]:
    """Set the three things on ``runtime`` (in place).  Returns what was changed, in words."""
    done: list[str] = []
    env = _env(runtime)
    runtime["envVars"] = env
    for key, name in ENV_NAMES.items():
        value = values[key]
        entries = [e for e in env if e["name"] == name]
        if not entries:
            env.append({"name": name, "value": value})
            done.append(f"envVars：加上 {name}={value}")
            continue
        if entries[0].get("value") != value:
            done.append(f"envVars：{name} 從 {entries[0].get('value')!r} 改成 {value}")
            entries[0]["value"] = value
        for extra in entries[1:]:  # a second entry of the same name would win or lose by position: keep one
            env.remove(extra)
            done.append(f"envVars：移除重複的 {name}")
    policies = _policies(runtime)
    runtime["additionalPolicies"] = policies
    arn = values["policy_arn"]
    for old in [p for p in policies if _OURS.search(p) and p != arn]:  # the same policy from an earlier (or another) data plane stack
        policies.remove(old)
        done.append(f"additionalPolicies：移除舊的 {old}")
    while policies.count(arn) > 1:
        policies.remove(arn)
        done.append(f"additionalPolicies：移除重複的 {arn}")
    if arn not in policies:
        policies.append(arn)
        done.append(f"additionalPolicies：加上 {arn}")
    return done


def problems_of(state: dict[str, Any], values: dict[str, str] | None) -> list[str]:
    """What is wrong or missing in the file (compared with ``values`` when they are known)."""
    out = []
    hints = {"portal_data_uri": "agent 會讀內建的 SAMPLE 資料，提案會被 pipeline 拒絕", "output_uri": "研究結果沒有地方存，pipeline 等不到結果"}
    examples = {"portal_data_uri": "s3://<bucket>/portal/", "output_uri": "s3://<bucket>/research/"}
    for key, label in ENV_NAMES.items():
        found = state[key]
        if not found:
            out.append(f"envVars 沒有 {label}：{hints[key]}")
            continue
        if len(found) > 1:
            out.append(f"envVars 有 {len(found)} 個 {label}（{'、'.join(repr(v) for v in found)}）：只能留一個")
        for value in dict.fromkeys(found):
            problem = location_problem(label, value, examples[key])
            if problem:
                out.append(f"envVars 的 {problem}：{hints[key]}")
            elif values and value != values[key]:
                out.append(f"envVars 的 {label} 是 {value}，stack 的 output 是 {values[key]}")
    if len(state["portal_data_uri"]) == 1 and len(state["output_uri"]) == 1:
        out += pair_problems(state["portal_data_uri"][0], state["output_uri"][0])
    policies = state["policies"]
    if values:
        if values["policy_arn"] not in policies:
            out.append(f"additionalPolicies 沒有 {values['policy_arn']}：deploy 之後 runtime role 讀不到 portal/、寫不了 research/")
        stale = [p for p in policies if _OURS.search(p) and p != values["policy_arn"]]
        if stale:
            out.append(f"additionalPolicies 還有另一個 data plane stack 的 policy：{'、'.join(stale)}")
    elif not any(_OURS.search(p) and _POLICY_ARN.fullmatch(p) for p in policies):
        out.append("additionalPolicies 沒有 data plane stack 的 RuntimeDataPolicy：deploy 之後 runtime role 讀不到 portal/、寫不了 research/"
                   "（它的 ARN 是 stack 的 output RuntimeDataPolicyArn）")
    if len(set(policies)) != len(policies):
        out.append("additionalPolicies 有重複的 ARN")
    return out


def run(args: argparse.Namespace, client: Any = None) -> int:
    path = Path(args.file)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ScriptError(f"讀不到 {path}：{e}") from e
    except RecursionError as e:  # brackets nested thousands deep: not a settings file, and not a traceback either
        raise ScriptError(f"{path} 不是 AgentCore CLI 的專案設定檔（巢狀層數太深，無法讀取）") from e
    except ValueError as e:
        raise ScriptError(f"{path} 不是有效的 JSON：{str(e)[:200]}") from e
    if not isinstance(doc, dict):
        raise ScriptError(f"{path} 不是 AgentCore CLI 的專案設定檔")
    runtime = pick_runtime(doc, args.runtime)

    given = {"portal_data_uri": args.portal_data_uri, "output_uri": args.output_uri, "policy_arn": args.policy_arn}
    values: dict[str, str] | None = None
    if args.stack:
        if any(given.values()):
            raise ScriptError("--stack 和 --portal-data-uri／--output-uri／--policy-arn 擇一使用")
        values = check_values(dict(stack_outputs(args.stack, args.region, client)))
    elif any(given.values()):
        values = check_values(given)
        if not _OURS.search(values["policy_arn"]):
            say(f"  注意：{values['policy_arn']} 的名稱不像 data plane stack 建立的 RuntimeDataPolicy；請確認它就是 stack 的 output RuntimeDataPolicyArn。")

    if args.check:
        found = problems_of(current(runtime), values)
        for line in found:
            say(f"  ✘ {line}")
        if found:
            say(f"\n{path} 的 runtime {runtime.get('name')} 還沒設定好：python scripts/configure_agent.py --stack <data plane stack 名稱>")
            return PENDING
        say(f"✔ {path} 的 runtime {runtime.get('name')}：PORTAL_DATA_URI、OUTPUT_URI、additionalPolicies 都已設定"
            + ("，而且和 stack 的 output 一致。" if values else "，形式也正確（沒有給 --stack，所以沒有和 stack 的 output 比對）。"))
        return OK
    if values is None:
        raise ScriptError("要寫入什麼？用 --stack <data plane stack 名稱>，或 --portal-data-uri／--output-uri／--policy-arn 三個一起給")

    changed = apply(runtime, values)
    if not changed:
        say(f"✔ {path} 已經是這些設定，沒有變更。")
        return OK
    try:
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as e:
        raise ScriptError(f"無法寫入 {path}：{e}") from e
    say(f"已更新 {path}（runtime {runtime.get('name')}）：")
    for line in changed:
        say(f"  · {line}")
    say("\n下一步：agentcore validate && agentcore deploy")
    return OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="configure_agent.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stack", help="data plane stack 的名稱（例如 llmops-research-support）：從它的 output 讀三個值")
    p.add_argument("--region", help="stack 所在的 region（預設用 AWS CLI／環境變數的設定）")
    p.add_argument("--portal-data-uri", help="s3://<bucket>/portal/（output PortalDataUri）")
    p.add_argument("--output-uri", help="s3://<bucket>/research/（output OutputUri）")
    p.add_argument("--policy-arn", help="output RuntimeDataPolicyArn")
    p.add_argument("--runtime", help="runtime 的名稱（agentcore.json 只有一個 runtime 時不用給）")
    p.add_argument("--file", default=str(DEFAULT_FILE), help="預設是這個專案的 agentcore/agentcore.json")
    p.add_argument("--check", action="store_true", help="只檢查，不寫入（還沒設定好時 exit code 1）")
    return p


def main(argv: list[str] | None = None, client: Any = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # captured streams in tests, exotic consoles
            pass
    args = build_parser().parse_args(argv)
    try:
        return run(args, client)
    except ScriptError as e:
        print(f"✘ {e}", file=sys.stderr, flush=True)
        return ENV
    except Exception as e:  # an uncaught error would leave with exit code 1 - which here means "checked: not configured yet"
        print(f"✘ 非預期的錯誤（{type(e).__name__}：{str(e)[:300]}）：沒有完成，也沒有寫入任何東西。", file=sys.stderr, flush=True)
        return ENV


if __name__ == "__main__":
    sys.exit(main())
