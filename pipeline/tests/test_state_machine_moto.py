"""A second opinion on the state machine: moto's Step Functions engine (an independent ASL implementation) executes the
same definition with the same real handlers, and must take the same path and produce the same output as the small
interpreter in asl.py that the other state-machine tests rely on.

Only the Wait is shortened (60 s -> 1 s) so the test does not sleep for minutes.
"""

from __future__ import annotations

import copy
import json
import time

import pytest
from asl import Machine
from cfn import state_machine
from conftest import BUCKET, T1, Plane, accepted_run

from portal_pipeline import handlers as H
from portal_pipeline import releases as R
from portal_pipeline.storage import S3Store

moto = pytest.importorskip("moto")
ACCOUNT, REGION = "123456789012", "us-east-1"
FUNCTIONS = {"StartResearchFunctionArn": ("p-start-research", "start_research"), "CheckResearchFunctionArn": ("p-check-research", "check_research"),
             "PublishFunctionArn": ("p-publish", "publish"), "ReportFailureFunctionArn": ("p-report-failure", "report_failure")}


def _scrub(state):
    """Drop what legitimately differs between two runs of the same scenario."""
    state = copy.deepcopy(state)
    state.pop("execution", None)
    if isinstance(state.get("notified"), dict):
        state["notified"] = {"messageId": "…"}  # each engine's SNS hands out its own message ids
    if isinstance(state.get("run"), dict):
        state["run"].pop("sessionId", None)
    for key in ("notifyError", "reportError", "error"):
        if isinstance(state.get(key), dict):
            state[key] = {"Error": state[key].get("Error")}
    note = state.get("notification")
    if isinstance(note, dict) and "Execution：" in note.get("message", ""):
        note["message"] = "\n".join(line for line in note["message"].splitlines() if not line.startswith("Execution："))
    return state


class Scenario:
    """One world (data plane + simulated runtime) that can be driven by either engine."""

    def __init__(self, monkeypatch, s3, prefix: str, *, record=None, finish_on_check=2, agent_events=None, env=None, report_breaks=False):
        from conftest import ARN, SFN, FakeAgentCore

        self.plane = Plane(S3Store(BUCKET, f"{prefix}/portal/", s3), S3Store(BUCKET, f"{prefix}/pipeline/", s3),
                           S3Store(BUCKET, f"{prefix}/research/", s3), "s3")
        self.plane.bootstrap()
        self.agent = FakeAgentCore(events=agent_events, result_uri=f"s3://{BUCKET}/{prefix}/research/runs/{accepted_run()['runId']}.json")
        self.record, self.finish_on_check, self.checks = record, finish_on_check, 0
        self.monkeypatch, self.prefix, self.report_breaks = monkeypatch, prefix, report_breaks
        self.env = {"AGENT_RUNTIME_ARN": ARN, "DATA_BUCKET": BUCKET, "PUBLISH_MODE": "auto", "STATE_MACHINE_ARN": SFN,
                    "RESEARCH_PREFIX": f"{prefix}/research/", "RESEARCH_TIMEOUT_SECONDS": "2400", "PORTAL_URL": "", **(env or {})}

    def activate(self) -> None:
        mp = self.monkeypatch
        mp.setattr(H, "data_store", lambda: self.plane.data)
        mp.setattr(H, "control_store", lambda: self.plane.control)
        mp.setattr(H, "research_store", lambda: self.plane.research)
        mp.setitem(H.clients, "agentcore", lambda arn: self.agent)
        mp.setattr(H, "clock", lambda: T1)
        for name, value in self.env.items():
            mp.setenv(name, value)

    def check_research(self, event):
        self.checks += 1
        if self.record is not None and self.checks == self.finish_on_check:
            self.plane.save_run(self.record)
        return H.check_research(event)

    def report_failure(self, event):
        if self.report_breaks:
            raise RuntimeError("the report itself fails")
        return H.report_failure(event)

    def handlers(self) -> dict:
        return {"start_research": H.start_research, "check_research": self.check_research, "publish": H.publish, "report_failure": self.report_failure}


@pytest.fixture()
def aws(monkeypatch):
    import boto3
    from moto.awslambda_simple.models import LambdaSimpleBackend

    with moto.mock_aws(config={"lambda": {"use_docker": False}, "stepfunctions": {"execute_state_machine": True}}):
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        registry: dict = {}

        def invoke(self, function_name, qualifier, body, headers, response_headers):
            """Run the real handler in-process, answering the way the Lambda service does (errors included)."""
            name = str(function_name).rsplit(":", 1)[-1]
            try:
                out = registry[name](json.loads(body))
            except Exception as e:  # noqa: BLE001 - this is what "Unhandled" function errors look like on the wire
                response_headers["x-amz-function-error"] = "Unhandled"
                return json.dumps({"errorMessage": str(e), "errorType": type(e).__name__, "stackTrace": []}).encode("utf-8")
            return json.dumps(out).encode("utf-8")

        monkeypatch.setattr(LambdaSimpleBackend, "invoke", invoke)
        yield {"s3": s3, "registry": registry, "sfn": boto3.client("stepfunctions", region_name=REGION),
               "sns": boto3.client("sns", region_name=REGION)}


def run_in_moto(aws, scenario: Scenario, execution_input) -> dict:
    from moto.sns import sns_backends

    scenario.activate()
    topic = aws["sns"].create_topic(Name=f"topic-{scenario.prefix}")["TopicArn"]
    values = {key: f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}-{scenario.prefix}" for key, (name, _) in FUNCTIONS.items()}
    for key, (name, handler) in FUNCTIONS.items():
        aws["registry"][f"{name}-{scenario.prefix}"] = scenario.handlers()[handler]
    definition, _ = state_machine({**values, "NotificationTopicArn": topic})
    definition["States"]["WaitForResearch"]["Seconds"] = 1
    machine = aws["sfn"].create_state_machine(name=f"m-{scenario.prefix}", definition=json.dumps(definition),
                                              roleArn=f"arn:aws:iam::{ACCOUNT}:role/states")["stateMachineArn"]
    execution = aws["sfn"].start_execution(stateMachineArn=machine, input=json.dumps(execution_input))["executionArn"]
    deadline = time.time() + 60
    while True:
        described = aws["sfn"].describe_execution(executionArn=execution)
        if described["status"] != "RUNNING":
            break
        assert time.time() < deadline, "the execution did not finish"
        time.sleep(0.1)
    events = aws["sfn"].get_execution_history(executionArn=execution, maxResults=1000)["events"]
    path = [next(v for k, v in e.items() if k.endswith("EventDetails") and isinstance(v, dict) and "name" in v)["name"]
            for e in events if e["type"].endswith("StateEntered")]
    sent = [{"Subject": n[2], "Message": n[1]} for n in sns_backends[ACCOUNT][REGION].topics[topic].sent_notifications]
    return {"status": described["status"], "error": described.get("error"), "path": path, "sent": sent,
            "output": json.loads(described["output"]) if described.get("output") else None}


def run_in_interpreter(scenario: Scenario, execution_input) -> dict:
    scenario.activate()
    names = {key: f"fn:{key}" for key in FUNCTIONS}
    definition, _ = state_machine({**names, "NotificationTopicArn": "arn:aws:sns:us-east-1:123456789012:t"})
    sent: list[dict] = []
    lambdas = {names[key]: scenario.handlers()[handler] for key, (_, handler) in FUNCTIONS.items()}
    ex = Machine(definition, lambdas=lambdas, sns=lambda **kw: sent.append({"Subject": kw["Subject"], "Message": kw["Message"]})).run(execution_input)
    return {"status": ex.status, "error": ex.error, "path": ex.path, "sent": sent, "output": ex.output}


def _rejected_record():
    record = accepted_run()
    record["proposal"]["portal"]["sample"] = True
    return record


SCENARIOS = {
    "published": dict(make=lambda: dict(record=accepted_run(), finish_on_check=2), input={"request": {"kind": "weekly"}, "trigger": "schedule"},
                      status="SUCCEEDED", ends=["Publish", "Notify", "Outcome", "Done"]),
    "manual-empty-input": dict(make=lambda: dict(record=accepted_run(), finish_on_check=1), input={}, status="SUCCEEDED", ends=["Notify", "Outcome", "Done"]),
    "no-proposal": dict(make=lambda: dict(record={**accepted_run(), "status": "no_proposal", "proposal": None, "report": {"errors": ["no evidence"]}},
                                          finish_on_check=1), input={}, status="SUCCEEDED", ends=["ResearchDone", "Notify", "Outcome", "Done"]),
    "rejected": dict(make=lambda: dict(record=_rejected_record(), finish_on_check=1), input={}, status="FAILED", error="PortalUpdateRejected",
                     ends=["Notify", "Outcome", "NotPublished"]),
    "review-holds": dict(make=lambda: dict(record=accepted_run(), finish_on_check=1, env={"PUBLISH_MODE": "review"}), input={"kind": "weekly"},
                         status="SUCCEEDED", ends=["Publish", "Notify", "Outcome", "Done"]),
    "agent-refuses": dict(make=lambda: dict(agent_events=[{"type": "result", "status": "error", "error": "busy"}]), input={},
                          status="FAILED", error="PortalUpdateFailed", ends=["StartResearch", "ReportFailure", "NotifyFailure", "Failed"]),
    "research-fails": dict(make=lambda: dict(record={**accepted_run(), "status": "error", "proposal": None, "error": "model error"}, finish_on_check=1),
                           input={}, status="FAILED", error="PortalUpdateFailed", ends=["CheckResearch", "ReportFailure", "NotifyFailure", "Failed"]),
    "bad-action": dict(make=lambda: dict(), input={"action": "delete-everything"}, status="FAILED", error="PortalUpdateFailed",
                       ends=["Route", "Publish", "ReportFailure", "NotifyFailure", "Failed"]),
    "pause": dict(make=lambda: dict(), input={"action": "pause", "by": "eunice", "reason": "holiday"}, status="SUCCEEDED",
                  ends=["Route", "Publish", "Notify", "Outcome", "Done"]),
    # the report of the failure fails too: the state machine writes the message itself (States.Format, the context object)
    "report-fails": dict(make=lambda: dict(report_breaks=True), input={"action": "delete-everything"}, status="FAILED", error="PortalUpdateFailed",
                         ends=["Route", "Publish", "ReportFailure", "NotifyUnreported", "Failed"]),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_both_engines_agree_on_path_output_and_messages(aws, monkeypatch, name):
    spec = SCENARIOS[name]
    in_moto = run_in_moto(aws, Scenario(monkeypatch, aws["s3"], f"moto-{name}", **spec["make"]()), spec["input"])
    in_mine = run_in_interpreter(Scenario(monkeypatch, aws["s3"], f"mine-{name}", **spec["make"]()), spec["input"])

    assert in_moto["status"] == spec["status"] and in_moto.get("error") == spec.get("error"), in_moto
    assert in_moto["path"][-len(spec["ends"]):] == spec["ends"], in_moto["path"]
    assert in_mine["status"] == in_moto["status"] and in_mine["error"] == in_moto["error"]
    assert in_mine["path"] == in_moto["path"]
    assert len(in_moto["sent"]) == 1 and in_moto["sent"][0]["Subject"] == in_mine["sent"][0]["Subject"]
    if name == "report-fails":  # the same sentence from both engines, each with its own execution at the end
        theirs, mine = in_moto["sent"][0]["Message"], in_mine["sent"][0]["Message"]
        assert theirs.rsplit(": ", 1)[0] == mine.rsplit(": ", 1)[0] and "{}" not in theirs and "\\" not in theirs
        assert theirs.rsplit(": ", 1)[1].startswith(f"arn:aws:states:{REGION}:{ACCOUNT}:execution:m-moto-report-fails:")
        assert in_moto["sent"][0]["Subject"] == "[LLMOps portal] FAILED: the failure report could not be written"
    if in_moto["output"] is not None:
        mine, theirs = _scrub(in_mine["output"]), _scrub(in_moto["output"])
        # the two worlds live under different prefixes of the bucket: compare everything but the locations
        text = lambda doc, prefix: json.dumps(doc, sort_keys=True, ensure_ascii=False).replace(prefix, "X")  # noqa: E731
        assert text(mine, f"mine-{name}") == text(theirs, f"moto-{name}")


def test_the_engine_under_moto_really_published(aws, monkeypatch):
    scenario = Scenario(monkeypatch, aws["s3"], "moto-live", record=accepted_run(), finish_on_check=3)
    result = run_in_moto(aws, scenario, {"trigger": "schedule"})
    assert result["status"] == "SUCCEEDED" and result["path"].count("WaitForResearch") == 3
    current = R.load_current(scenario.plane.data)
    assert current.release_id == result["output"]["publish"]["releaseId"] and current.manifest["source"]["by"] == "schedule"
    assert len(scenario.agent.calls) == 1 and result["sent"][0]["Subject"].startswith("[LLMOps portal] published ")
