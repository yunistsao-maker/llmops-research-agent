"""The CloudFormation templates: do they wire up what the code expects, and do the permissions keep the three planes apart?

These are static checks plus two things that can really be executed here: cfn-lint and the CloudFront Function's
JavaScript (in Node).  Nothing in this file proves that a deployment works - see docs/delivery-plan.md.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from cfn import INFRA, load_template, state_machine
from conftest import token_cases

from portal_pipeline import handlers as H

REPO = Path(__file__).resolve().parents[2]
PIPELINE = load_template("pipeline.yaml")
DATA = load_template("data-plane.yaml")
FUNCTIONS = {name: res["Properties"] for name, res in PIPELINE["Resources"].items() if res["Type"] == "AWS::Lambda::Function"}


def statements(role_or_policy: dict) -> list[dict]:
    props = role_or_policy["Properties"]
    docs = [p["PolicyDocument"] for p in props.get("Policies", [])] + ([props["PolicyDocument"]] if "PolicyDocument" in props else [])
    return [s for d in docs for s in d["Statement"]]


def actions(stmt: dict) -> list[str]:
    a = stmt["Action"]
    return [a] if isinstance(a, str) else list(a)


def text(node) -> str:
    return json.dumps(node, sort_keys=True)


# ---------------------------------------------------------------------------------------------- lint
@pytest.mark.parametrize("template", ["data-plane.yaml", "pipeline.yaml", "daily-orchestrator.yaml", "github-oidc.yaml",
                                      "daily-schedules.yaml", "amplify-trigger.yaml", "ui-verify.yaml"])
def test_templates_pass_cfn_lint(template):
    if not shutil.which("cfn-lint"):
        pytest.skip("cfn-lint is not installed")
    proc = subprocess.run(["cfn-lint", str(INFRA / template)], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------------------------- functions <-> code
def test_every_lambda_handler_exists_in_the_code():
    assert sorted(FUNCTIONS) == ["CheckResearchFunction", "PublishFunction", "ReportFailureFunction", "StartResearchFunction"]
    for name, props in FUNCTIONS.items():
        module, _, attr = props["Handler"].rpartition(".")
        assert module == "portal_pipeline.handlers" and callable(getattr(H, attr)), f"{name}: {props['Handler']}"
        assert props["Runtime"] == "python3.13" and props["Architectures"] == ["arm64"]
        assert props["Code"] == {"S3Bucket": {"Ref": "CodeBucket"}, "S3Key": {"Ref": "CodeKey"}}  # one bundle for all four
    assert sorted(p["Handler"].rsplit(".", 1)[1] for p in FUNCTIONS.values()) == ["check_research", "publish", "report_failure", "start_research"]


def test_the_environment_variables_are_the_ones_the_code_reads():
    source = (REPO / "pipeline" / "portal_pipeline" / "handlers.py").read_text(encoding="utf-8")
    known = set(re.findall(r'_env\("([A-Z_]+)"', source)) | set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    for name, props in FUNCTIONS.items():
        configured = set((props.get("Environment") or {}).get("Variables") or {})
        assert configured <= known, f"{name} sets variables the code never reads: {sorted(configured - known)}"
    env = {name: set((props.get("Environment") or {}).get("Variables") or {}) for name, props in FUNCTIONS.items()}
    assert {"AGENT_RUNTIME_ARN", "DATA_BUCKET", "RESEARCH_TIMEOUT_SECONDS"} <= env["StartResearchFunction"]
    assert {"DATA_BUCKET"} <= env["CheckResearchFunction"]
    assert {"DATA_BUCKET", "PUBLISH_MODE", "AUTO_MAX_OPS", "AUTO_ALLOW_REMOVE", "STATE_MACHINE_ARN"} <= env["PublishFunction"]
    # the three prefixes are fixed by the data plane template: nobody overrides them here
    assert not any({"PORTAL_PREFIX", "RESEARCH_PREFIX", "PIPELINE_PREFIX"} & e for e in env.values())


def test_every_role_can_carry_the_companys_permissions_boundary():
    roles = {name: res for name, res in PIPELINE["Resources"].items() if res["Type"] == "AWS::IAM::Role"}
    assert len(roles) == 6
    for name, res in roles.items():
        assert res["Properties"]["PermissionsBoundary"] == {"Fn::If": ["HasBoundary", {"Ref": "PermissionsBoundaryArn"}, {"Ref": "AWS::NoValue"}]}, name
    assert PIPELINE["Parameters"]["PermissionsBoundaryArn"]["Default"] == ""  # nothing changes unless it is asked for


def test_the_pipeline_never_learns_the_data_locations_url():
    """That URL carries the access token; notifications are forwarded around, so they name the portal's own address."""
    assert "DataBaseUrl" not in PIPELINE["Parameters"] and "PortalUrl" in PIPELINE["Parameters"]
    text = (REPO / "infra" / "pipeline.yaml").read_text(encoding="utf-8")
    assert "DATA_BASE_URL" not in text and "DataDistribution" not in text
    source = (REPO / "pipeline" / "portal_pipeline" / "handlers.py").read_text(encoding="utf-8") + \
        (REPO / "pipeline" / "portal_pipeline" / "notify.py").read_text(encoding="utf-8")
    assert "DATA_BASE_URL" not in source and "data_base_url" not in source


def test_log_groups_belong_to_the_functions():
    for name, props in FUNCTIONS.items():
        group = PIPELINE["Resources"][name.replace("Function", "LogGroup")]["Properties"]["LogGroupName"]
        assert group == {"Fn::Sub": "/aws/lambda/" + props["FunctionName"]["Fn::Sub"]}
        assert PIPELINE["Resources"][name]["DependsOn"] == name.replace("Function", "LogGroup")


def test_the_state_machine_gets_exactly_the_arns_its_definition_uses():
    definition, props = state_machine()
    used = set(re.findall(r"\$\{([A-Za-z]+)\}", text(props["Definition"])))
    assert used == set(props["DefinitionSubstitutions"])
    assert props["DefinitionSubstitutions"]["NotificationTopicArn"] == {"Ref": "NotificationTopic"}
    for key in ("StartResearch", "CheckResearch", "Publish", "ReportFailure"):
        assert props["DefinitionSubstitutions"][f"{key}FunctionArn"] == {"Fn::GetAtt": f"{key}Function.Arn"}
    # the publish step tells people which state machine to start: it must name this one
    arn = FUNCTIONS["PublishFunction"]["Environment"]["Variables"]["STATE_MACHINE_ARN"]["Fn::Sub"]
    assert arn.endswith(":stateMachine:${AWS::StackName}-update") and props["StateMachineName"] == {"Fn::Sub": "${AWS::StackName}-update"}


def test_the_timeouts_fit_inside_each_other():
    definition, _ = state_machine()
    agentcore = json.loads((REPO / "agentcore" / "agentcore.json").read_text(encoding="utf-8"))
    runtime = agentcore["runtimes"][0]
    agent_max = int(next(v["value"] for v in runtime["envVars"] if v["name"] == "MAX_RUNTIME_SECONDS"))
    wait = PIPELINE["Parameters"]["ResearchTimeoutSeconds"]
    assert agent_max + 300 <= wait["Default"], "the pipeline must outwait the longest research run"
    after_research = definition["States"]["Publish"]["TimeoutSeconds"] + 300  # publish, then notify with its retries
    assert wait["MaxValue"] + after_research < definition["TimeoutSeconds"], "every step must be able to finish (and report) before the execution is killed"
    assert agent_max < runtime["lifecycleConfiguration"]["maxLifetime"]
    for name, props in FUNCTIONS.items():
        task = definition["States"][name.replace("Function", "")]
        assert props["Timeout"] < task["TimeoutSeconds"], f"{name}: the Lambda must time out before its task does"


def test_the_schedule_starts_the_state_machine_with_a_request_the_first_step_understands():
    schedule = PIPELINE["Resources"]["UpdateSchedule"]["Properties"]
    assert schedule["Target"]["Arn"] == {"Ref": "UpdateStateMachine"} and schedule["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert PIPELINE["Parameters"]["ScheduleExpression"]["Default"] == "cron(0 9 ? * MON *)"
    assert PIPELINE["Parameters"]["ScheduleTimezone"]["Default"] == "Asia/Taipei"
    template = schedule["Target"]["Input"]["Fn::Sub"]
    execution_input = json.loads(template.replace("${ScheduleRequest}", PIPELINE["Parameters"]["ScheduleRequest"]["Default"]))
    assert execution_input == {"request": {"kind": "weekly"}, "trigger": "schedule"}
    assert H.request_of({"input": execution_input}) == {"kind": "weekly"} and H.actor_of({"input": execution_input}) == "schedule"
    role = statements(PIPELINE["Resources"]["SchedulerRole"])
    assert [(actions(s), s["Resource"]) for s in role] == [(["states:StartExecution"], {"Ref": "UpdateStateMachine"})]


def _nulls(node, path="") -> list[str]:
    if node is None:
        return [path]
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _nulls(v, f"{path}/{k}")]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _nulls(v, f"{path}/{i}")]
    return []


@pytest.mark.parametrize("template", ["data-plane.yaml", "pipeline.yaml"])
def test_no_template_contains_a_null(template):
    """CloudFormation refuses a template with a null anywhere ("'null' values are not allowed in templates"), also inside
    a state machine Definition - where ASL itself allows "ResultPath: null". cfn-lint and the ASL engines do not notice."""
    assert _nulls(load_template(template)) == []


def test_sending_the_notification_never_replaces_the_state_and_is_retried():
    definition, _ = state_machine()
    for name, after, caught in (("Notify", "Outcome", "NotificationFailed"), ("NotifyFailure", "Failed", "Failed"), ("NotifyUnreported", "Failed", "Failed")):
        step = definition["States"][name]
        assert step["ResultPath"] == "$.notified" and step["ResultSelector"] == {"messageId.$": "$.MessageId"}
        assert step["Retry"][0]["MaxAttempts"] >= 2 and "States.TaskFailed" in step["Retry"][0]["ErrorEquals"]
        assert step["Next"] == after and step["Catch"][0]["Next"] == caught
    # A message that cannot be sent ends the execution as failed (the alarm counts those): otherwise an update that went
    # live, or one that waits for approval, would have told nobody.
    failed = definition["States"]["NotificationFailed"]
    assert failed["Type"] == "Fail" and failed["Error"] == "PortalNotificationFailed" and "portalctl status" in failed["Cause"]
    assert "may or may not have been updated" in definition["States"]["Failed"]["Cause"]   # a failed Publish step is not "data unchanged"
    # When the step that writes the failure report fails, the state machine still tells a person - and for that message
    # it reads nothing out of the state (whatever broke the report is in there): a fixed subject, a fixed sentence and
    # the id of the execution, which the service itself supplies.
    report, last = definition["States"]["ReportFailure"], definition["States"]["NotifyUnreported"]["Parameters"]
    assert report["Catch"] == [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.reportError", "Next": "NotifyUnreported"}]
    assert sorted(last) == ["Message.$", "Subject", "TopicArn"] and last["Subject"].isascii() and len(last["Subject"]) < 100
    text = last["Message.$"]
    assert text.startswith("States.Format('") and text.endswith("{}', $$.Execution.Id)") and text.isascii()
    body = text[len("States.Format('"):-len("', $$.Execution.Id)")]
    assert not set(body.replace("{}", "")) & set("'{}\\\n") and body.count("{}") == 1 and "$." not in body  # nothing a parser could stumble over


def test_only_reading_steps_are_retried_after_a_timeout():
    definition, _ = state_machine()
    retried = {name: set(step["Retry"][0]["ErrorEquals"]) for name, step in definition["States"].items() if step.get("Retry")}
    assert "StartResearch" not in retried  # a second start is a second research run
    assert {"Sandbox.Timedout", "Lambda.Unknown"} <= retried["CheckResearch"]
    assert not {"Sandbox.Timedout", "Lambda.Unknown", "States.TaskFailed", "States.ALL"} & retried["Publish"]


def test_every_execution_that_does_not_succeed_raises_the_alarm():
    """Also the failed ones: an error no Catch can handle (States.Runtime, States.DataLimitExceeded) or a failure of the
    reporting steps ends the execution as FAILED without any message of its own."""
    alarm = PIPELINE["Resources"]["ExecutionProblemAlarm"]["Properties"]
    metrics = {m["Id"]: m for m in alarm["Metrics"]}
    parts = ("failed", "timedOut", "aborted")
    assert metrics["unsuccessful"]["Expression"] == "failed + timedOut + aborted" and metrics["unsuccessful"].get("ReturnData", True)
    assert {metrics[i]["MetricStat"]["Metric"]["MetricName"] for i in parts} == {"ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"}
    assert all(metrics[i]["MetricStat"]["Metric"]["Dimensions"] == [{"Name": "StateMachineArn", "Value": {"Ref": "UpdateStateMachine"}}] for i in parts)
    assert all(metrics[i]["ReturnData"] is False and metrics[i]["MetricStat"]["Metric"]["Namespace"] == "AWS/States" for i in parts)
    assert alarm["AlarmActions"] == [{"Ref": "NotificationTopic"}] and alarm["TreatMissingData"] == "notBreaching"
    retry = PIPELINE["Resources"]["UpdateSchedule"]["Properties"]["Target"]["RetryPolicy"]
    assert retry["MaximumRetryAttempts"] >= 1  # a throttled start is tried again instead of skipping the week


def test_functions_may_write_only_to_their_own_log_group():
    for name in FUNCTIONS:
        role = name.replace("Function", "Role")
        logs = next(s for s in statements(PIPELINE["Resources"][role]) if "logs:PutLogEvents" in actions(s))
        # the Arn attribute of a log group already ends with ":*"
        assert logs["Resource"] == {"Fn::GetAtt": name.replace("Function", "LogGroup") + ".Arn"}


# ---------------------------------------------------------------------------------------------- who may do what
def s3_write(stmt: dict) -> bool:
    return any(a in ("s3:PutObject", "s3:DeleteObject", "s3:*", "*") for a in actions(stmt))


def test_the_research_agent_can_read_the_data_but_never_write_it():
    stmts = statements(DATA["Resources"]["RuntimeDataPolicy"])
    writes = [s for s in stmts if s3_write(s)]
    assert len(writes) == 1 and actions(writes[0]) == ["s3:PutObject"]
    assert writes[0]["Resource"] == {"Fn::Sub": "${DataBucket.Arn}/research/*"}  # results only: never portal/, never pipeline/
    reads = [s for s in stmts if "s3:GetObject" in actions(s) and s["Effect"] == "Allow"]
    assert [s["Resource"] for s in reads] == [{"Fn::Sub": "${DataBucket.Arn}/portal/*"}]
    assert "pipeline/" not in text(stmts)
    # ...and of what is under portal/, the files that are none of its business are denied outright
    denied = [s for s in stmts if s["Effect"] == "Deny"]
    assert len(denied) == 1 and actions(denied[0]) == ["s3:GetObject"]
    assert [r["Fn::Sub"].split("/", 1)[1] for r in denied[0]["Resource"]] == ["portal/releases/*/data/internal/*", "portal/releases/*/runs/*"]


def test_only_the_publish_function_writes_what_the_ui_reads():
    for name in ("StartResearchRole", "CheckResearchRole", "ReportFailureRole", "StateMachineRole", "SchedulerRole"):
        assert not any(s3_write(s) for s in statements(PIPELINE["Resources"][name])), name
    publish = statements(PIPELINE["Resources"]["PublishRole"])
    puts = [s for s in publish if "s3:PutObject" in actions(s)]
    assert len(puts) == 1 and sorted(r["Fn::Sub"].split("/", 1)[1] for r in puts[0]["Resource"]) == ["pipeline/*", "portal/*"]
    assert "s3:GetObject" in actions(puts[0])  # S3 conditional puts (If-Match) need read access to the object
    # releases are immutable and the pointer is only ever replaced: the role cannot delete anything the UI reads
    deletes = [s for s in publish if "s3:DeleteObject" in actions(s)]
    assert len(deletes) == 1 and actions(deletes[0]) == ["s3:DeleteObject"] and deletes[0]["Resource"]["Fn::Sub"].endswith("/pipeline/*")
    assert "bedrock" not in text(publish).lower()  # the publisher has no way to call a model or the agent


def test_each_step_has_only_the_permissions_of_its_job():
    start = statements(PIPELINE["Resources"]["StartResearchRole"])
    assert sorted(a for s in start for a in actions(s)) == ["bedrock-agentcore:InvokeAgentRuntime", "logs:CreateLogStream", "logs:PutLogEvents"]
    invoke = next(s for s in start if "bedrock-agentcore:InvokeAgentRuntime" in actions(s))
    assert invoke["Resource"] == [{"Ref": "AgentRuntimeArn"}, {"Fn::Sub": "${AgentRuntimeArn}/runtime-endpoint/*"}]
    check = statements(PIPELINE["Resources"]["CheckResearchRole"])
    assert sorted(a for s in check for a in actions(s)) == ["logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:ListBucket"]
    assert next(s for s in check if "s3:GetObject" in actions(s))["Resource"]["Fn::Sub"].endswith("/research/runs/*")
    report = statements(PIPELINE["Resources"]["ReportFailureRole"])
    assert sorted(a for s in report for a in actions(s)) == ["logs:CreateLogStream", "logs:PutLogEvents"]
    machine = statements(PIPELINE["Resources"]["StateMachineRole"])
    assert sorted(a for s in machine for a in actions(s)) == ["lambda:InvokeFunction", "sns:Publish"]
    assert len(next(s for s in machine if "lambda:InvokeFunction" in actions(s))["Resource"]) == len(FUNCTIONS)


def test_the_operator_policy_covers_what_portalctl_does_through_the_pipeline_and_nothing_that_writes():
    """The people who approve and roll back get one policy.  It must be enough for every call portalctl makes when it
    works through the state machine - and must not be a way around the pipeline."""
    stmts = statements(PIPELINE["Resources"]["OperatorPolicy"])
    granted = sorted(a for s in stmts for a in actions(s))
    assert granted == ["s3:GetObject", "s3:ListBucket", "states:DescribeExecution", "states:DescribeStateMachine", "states:GetExecutionHistory",
                       "states:ListExecutions", "states:StartExecution"]
    assert not any(s3_write(s) for s in stmts) and all(s["Effect"] == "Allow" for s in stmts)
    # every Step Functions call in portalctl is one the policy allows, on the resource that call is authorised against
    source = (REPO / "scripts" / "portalctl.py").read_text(encoding="utf-8")
    called = set(re.findall(r"\.(start_execution|describe_execution|list_executions|get_execution_history|stop_execution|send_task_\w+)\(", source))
    assert called == {"start_execution", "describe_execution", "list_executions"}
    on_machine = next(s for s in stmts if "states:StartExecution" in actions(s))
    assert on_machine["Resource"] == {"Ref": "UpdateStateMachine"} and "states:ListExecutions" in actions(on_machine)
    on_execution = next(s for s in stmts if "states:DescribeExecution" in actions(s))
    assert on_execution["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}:execution:${UpdateStateMachine.Name}:*"}
    # reading: what is live, what is waiting, what a run found - with the listing that turns "not there" into 404
    read = next(s for s in stmts if "s3:GetObject" in actions(s))
    assert sorted(r["Fn::Sub"].split("/", 1)[1] for r in read["Resource"]) == ["pipeline/*", "portal/*", "research/*"]
    listing = next(s for s in stmts if "s3:ListBucket" in actions(s))
    assert listing["Condition"]["StringLike"]["s3:prefix"] == ["portal/*", "pipeline/*", "research/*"]
    assert PIPELINE["Outputs"]["OperatorPolicyArn"]["Value"] == {"Ref": "OperatorPolicy"}


def test_a_missing_object_is_not_found_rather_than_access_denied():
    """Without s3:ListBucket, S3 answers 403 for a key that does not exist - and "the run is not finished yet" would
    look like a permission problem to the waiting step."""
    for role, prefixes in (("CheckResearchRole", ["research/runs/*"]), ("PublishRole", ["portal/*", "pipeline/*", "research/*"])):
        listing = next(s for s in statements(PIPELINE["Resources"][role]) if "s3:ListBucket" in actions(s))
        allowed = listing["Condition"]["StringLike"]["s3:prefix"]
        assert (allowed if isinstance(allowed, list) else [allowed]) == prefixes


def test_no_statement_is_a_wildcard_grant():
    for template in (PIPELINE, DATA):
        for name, res in template["Resources"].items():
            if res["Type"] not in ("AWS::IAM::Role", "AWS::IAM::ManagedPolicy"):
                continue
            for stmt in statements(res):
                assert stmt["Effect"] in ("Allow", "Deny") and stmt["Resource"] != "*" and "*" not in actions(stmt), f"{name}: {stmt}"
                assert not any(a.endswith(":*") or a.startswith("iam:") for a in actions(stmt)), f"{name}: {stmt}"


# ---------------------------------------------------------------------------------------------- the data plane
def test_the_template_updates_the_stack_that_is_already_deployed_in_place():
    """infra/data-plane.yaml replaces the earlier support.yaml under the same stack: what exists must not be recreated."""
    resources = DATA["Resources"]
    assert {name: resources[name]["Type"] for name in ("DataBucket", "DataBucketPolicy", "RuntimeDataPolicy")} == {
        "DataBucket": "AWS::S3::Bucket", "DataBucketPolicy": "AWS::S3::BucketPolicy", "RuntimeDataPolicy": "AWS::IAM::ManagedPolicy"}
    bucket = resources["DataBucket"]
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    # the only bucket property whose change replaces the bucket: still "no name unless one is given"
    assert bucket["Properties"]["BucketName"] == {"Fn::If": ["NamedBucket", {"Ref": "BucketName"}, {"Ref": "AWS::NoValue"}]}
    assert DATA["Parameters"]["BucketName"]["Default"] == ""
    # a managed policy with a fixed name could not be updated in place; the runtime role refers to this one by ARN
    assert "ManagedPolicyName" not in resources["RuntimeDataPolicy"]["Properties"]
    sids = [s["Sid"] for s in resources["RuntimeDataPolicy"]["Properties"]["PolicyDocument"]["Statement"]]
    assert sids[:3] == ["ReadPortalData", "ListPortalData", "WriteResults"]  # what the deployed runtime relies on is still granted
    assert {"DataBucketName", "PortalDataUri", "OutputUri", "RuntimeDataPolicyArn"} <= set(DATA["Outputs"])  # what the runtime config uses
    # every new parameter has a default, so `aws cloudformation deploy` needs no overrides to update
    assert all("Default" in p for p in DATA["Parameters"].values())


def test_the_bucket_is_private_versioned_and_only_cloudfront_reads_portal():
    bucket = DATA["Resources"]["DataBucket"]
    assert bucket["DeletionPolicy"] == "Retain" and bucket["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert set(bucket["Properties"]["PublicAccessBlockConfiguration"].values()) == {True}
    policy = DATA["Resources"]["DataBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    allow = [s for s in policy if s["Effect"] == "Allow"]
    assert all(s["Principal"] == {"Service": "cloudfront.amazonaws.com"} for s in allow)
    assert all("distribution/${DataDistribution}" in s["Condition"]["StringEquals"]["AWS:SourceArn"]["Fn::Sub"] for s in allow)
    read = next(s for s in allow if s["Action"] == "s3:GetObject")
    assert read["Resource"] == {"Fn::Sub": "${DataBucket.Arn}/portal/*"}  # not research/, not pipeline/
    assert next(s for s in allow if s["Action"] == "s3:ListBucket")["Condition"]["StringLike"] == {"s3:prefix": "portal/*"}
    assert any(s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}} for s in policy)


def test_the_distribution_serves_portal_read_only_over_https():
    config = DATA["Resources"]["DataDistribution"]["Properties"]["DistributionConfig"]
    origin = config["Origins"][0]
    assert origin["OriginPath"] == "/portal" and origin["OriginAccessControlId"] == {"Fn::GetAtt": "DataOriginAccessControl.Id"}
    behavior = config["DefaultCacheBehavior"]
    assert behavior["ViewerProtocolPolicy"] == "https-only" and behavior["AllowedMethods"] == ["GET", "HEAD", "OPTIONS"]
    assert behavior["CachePolicyId"] == "658327ea-f89d-4fab-a63d-7e88639e58f6"  # Managed-CachingOptimized: honours the objects' Cache-Control
    cors = DATA["Resources"]["DataResponseHeaders"]["Properties"]["ResponseHeadersPolicyConfig"]["CorsConfig"]
    assert cors["AccessControlAllowMethods"]["Items"] == ["GET", "HEAD", "OPTIONS"] and cors["AccessControlAllowCredentials"] is False
    assert cors["AccessControlAllowOrigins"]["Items"] == {"Ref": "AllowedOrigins"}
    # the token function is attached unless the operator explicitly chose open data
    assert behavior["FunctionAssociations"]["Fn::If"][0] == "TokenRequired"
    assert DATA["Conditions"]["TokenRequired"] == {"Fn::Not": [{"Condition": "OpenData"}]}
    assert DATA["Conditions"]["OpenData"] == {"Fn::Equals": [{"Ref": "DataAccessToken"}, "none"]}
    outputs = DATA["Outputs"]
    assert {"DataBucketName", "PortalDataUri", "OutputUri", "PipelineStateUri", "RuntimeDataPolicyArn", "DataBaseUrl"} <= set(outputs)


@pytest.mark.parametrize("token", ["0f3a9c1e5b7d42f6a8c0e2d4b6f81357", "51af3dc0-da77-11e4-872e-1234567db123"])
def test_the_cloudfront_function_lets_only_token_paths_through(tmp_path, token):
    """Run the real function code (as CloudFormation would render it) in Node."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    source, variables = DATA["Resources"]["DataTokenFunction"]["Properties"]["FunctionCode"]["Fn::Sub"]
    assert set(variables) == {"Token"} and set(re.findall(r"\$\{(\w+)\}", source)) == {"Token"}
    code = source.replace("${Token}", token)
    cases = {**token_cases(token), "": 403}
    script = tmp_path / "run.js"
    script.write_text(code + "\nconst cases = " + json.dumps(list(cases)) + ";\n"
                      "const out = cases.map((uri) => { const r = handler({request: {uri, method: 'GET', headers: {}}});"
                      " return r.statusCode ? [r.statusCode, r.headers] : [r.uri, null]; });\n"
                      "console.log(JSON.stringify(out));\n", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert dict(zip(cases, [r[0] for r in results])) == cases
    for outcome, headers in results:
        if outcome == 403:  # never cached, and readable by a page so that it can say "403" instead of "network error"
            assert headers == {"cache-control": {"value": "no-store"}, "access-control-allow-origin": {"value": "*"}}


def test_the_token_parameter_only_accepts_url_safe_values():
    pattern = re.compile(DATA["Parameters"]["DataAccessToken"]["AllowedPattern"])
    for ok in ("", "none", "0f3a9c1e5b7d42f6a8c0e2d4b6f81357", "A-Za-z0-9_-long-enough"):
        assert pattern.match(ok), ok
    for bad in ("short", "has/slash/in-it-0123456789", "has'quote-0123456789abcdef", "spa ce-0123456789abcdef", "x" * 65):
        assert not pattern.match(bad), bad


# ---------------------------------------------------------------------------------------------- daily run (GitHub -> orchestrator)
ORCHESTRATOR = load_template("daily-orchestrator.yaml")
GITHUB = load_template("github-oidc.yaml")


def test_the_orchestrator_can_only_run_the_pipeline():
    role = ORCHESTRATOR["Resources"]["OrchestratorRole"]
    granted = sorted(a for s in statements(role) for a in actions(s))
    assert granted == ["events:DescribeRule", "events:PutRule", "events:PutTargets", "states:DescribeExecution", "states:StartExecution",
                       "states:StopExecution"]
    start = next(s for s in statements(role) if actions(s) == ["states:StartExecution"])
    assert start["Resource"] == {"Ref": "PipelineStateMachineArn"}
    rule = next(s for s in statements(role) if "events:PutRule" in actions(s))
    assert "StepFunctionsGetEventsForStepFunctionsExecutionRule" in text(rule["Resource"])
    definition = ORCHESTRATOR["Resources"]["DailyStateMachine"]["Properties"]["Definition"]
    tasks = [s for s in definition["States"].values() if s["Type"] == "Task"]
    assert tasks and all(s["Resource"] == "arn:aws:states:::states:startExecution.sync:2" for s in tasks)
    assert all(s["Parameters"]["StateMachineArn"] == "${PipelineStateMachineArn}" for s in tasks)
    # each child waits less than the whole run may take, and the pipeline's own limit (4500 s) fits in each wait
    assert all(4500 < s["TimeoutSeconds"] <= definition["TimeoutSeconds"] for s in tasks)


def test_the_fallback_schedule_is_off_unless_asked_for():
    assert ORCHESTRATOR["Parameters"]["ScheduleState"]["Default"] == "DISABLED"
    assert ORCHESTRATOR["Resources"]["FallbackSchedule"]["Properties"]["State"] == {"Ref": "ScheduleState"}


def test_github_can_start_the_daily_run_and_nothing_else():
    role = GITHUB["Resources"]["StartDailyRunRole"]["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1 and trust[0]["Action"] == "sts:AssumeRoleWithWebIdentity"
    cond = trust[0]["Condition"]
    assert cond["StringEquals"] == {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"}
    subjects = cond["StringLike"]["token.actions.githubusercontent.com:sub"]
    # one repository, one environment: no branch, pull request or other repository of the owner
    assert all(":environment:${GitHubEnvironment}" in s["Fn::Sub"] and "${GitHubRepository}" in s["Fn::Sub"] for s in subjects)
    granted = sorted(a for s in statements(GITHUB["Resources"]["StartDailyRunRole"]) for a in actions(s))
    assert granted == ["states:DescribeExecution", "states:StartExecution"]
    assert role["MaxSessionDuration"] == 14400  # the workflow's longest wait; the role can only start and read daily runs


def test_a_second_portal_rebuilds_after_every_publish_only_when_it_is_configured():
    trigger = load_template("amplify-trigger.yaml")
    res = trigger["Resources"]
    assert trigger["Parameters"]["SecondAmplifyWebhookUrl"]["Default"] == "" and trigger["Parameters"]["SecondAmplifyWebhookUrl"]["NoEcho"] is True
    assert res["SecondWebhookDestination"]["Condition"] == "HasSecondSite"
    targets = res["OnPipelineSucceeded"]["Properties"]["Targets"]
    assert targets[0]["Id"] == "amplify-webhook" and targets[1]["Fn::If"][0] == "HasSecondSite"
    granted = [s for s in statements(res["InvokeRole"]) if "events:InvokeApiDestination" in actions(s)]
    assert len(granted) == 1 and len(granted[0]["Resource"]) == 2
