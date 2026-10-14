"""The update state machine, executed: the real definition from infra/pipeline.yaml, the real Lambda handlers, a data
plane on a folder and on (moto) S3, and a simulated AgentCore runtime that "finishes" its research after a few waits.

What this cannot show is the behaviour of the real services (IAM, the Lambda runtime, SNS delivery, EventBridge
Scheduler): see docs/delivery-plan.md for the first-deployment checks.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from asl import SNS_PUBLISH, Machine, ServiceError, lint
from cfn import state_machine
from conftest import BUCKET, RUN, accepted_run

from portal_pipeline import handlers as H
from portal_pipeline import publish as P
from portal_pipeline import releases as R

ARNS = {"StartResearchFunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:p-start-research",
        "CheckResearchFunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:p-check-research",
        "PublishFunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:p-publish",
        "ReportFailureFunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:p-report-failure",
        "NotificationTopicArn": "arn:aws:sns:us-east-1:123456789012:p-NotificationTopic"}


class World:
    """The deployed pipeline in miniature."""

    def __init__(self, plane):
        self.plane, self.sent, self.waits, self._finish, self.executions = plane, [], 0, None, 0
        self.definition, self.props = state_machine(ARNS)
        self.lambdas = {ARNS["StartResearchFunctionArn"]: H.start_research, ARNS["CheckResearchFunctionArn"]: H.check_research,
                        ARNS["PublishFunctionArn"]: H.publish, ARNS["ReportFailureFunctionArn"]: H.report_failure}
        self.sns_error = None

    def research_finishes(self, record: dict, after_waits: int = 3) -> None:
        self._finish = (after_waits, record)

    def _on_wait(self, seconds: int) -> None:
        self.waits += 1
        self.plane.now["t"] += dt.timedelta(seconds=seconds)
        if self._finish and self.waits == self._finish[0]:
            self.plane.save_run(self._finish[1])

    def _sns(self, **kw):
        if self.sns_error:
            raise ServiceError(self.sns_error)
        self.sent.append(kw)

    def start(self, execution_input, execution_id=None, **kw):
        self.executions += 1  # every start is a new execution, with its own id - as in the service
        execution_id = execution_id or f"arn:aws:states:us-east-1:123456789012:execution:machine:test-{self.executions}"
        return Machine(self.definition, lambdas=self.lambdas, sns=self._sns, on_wait=self._on_wait, **kw).run(execution_input, execution_id)

    @property
    def live(self) -> R.Current:
        return R.load_current(self.plane.data)


@pytest.fixture()
def world(wired):
    wired.bootstrap()
    return World(wired)


def test_the_definition_is_structurally_sound_and_uses_only_what_is_tested():
    definition, props = state_machine()
    assert lint(definition) == []
    assert props["StateMachineType"] == "STANDARD" and definition["TimeoutSeconds"] == 4500
    assert "${" not in json.dumps(state_machine(ARNS)[0])
    # One intrinsic function, in one place: the message the state machine writes itself when the failure report could
    # not be written (asl.py models States.Format, and nothing else that begins with "States.").
    text = json.dumps(definition)
    used = [name for name, state in definition["States"].items() if "States." in json.dumps(state.get("Parameters", {}))]
    assert used == ["NotifyUnreported"] and text.count("States.Format(") == 1
    assert not [w for w in ("States.Array", "States.JsonToString", "States.StringToJson", "States.UUID", "States.Hash", "States.Math") if w in text]


# ---------------------------------------------------------------------------------------------- research -> publish
def test_a_scheduled_run_researches_publishes_and_notifies(world):
    world.research_finishes(accepted_run(), after_waits=3)
    ex = world.start({"request": {"kind": "weekly"}, "trigger": "schedule"})
    assert ex.status == "SUCCEEDED", (ex.error, ex.cause)
    assert ex.path == ["Init", "Route", "StartResearch",
                       "WaitForResearch", "CheckResearch", "ResearchDone", "WaitForResearch", "CheckResearch", "ResearchDone",
                       "WaitForResearch", "CheckResearch", "ResearchDone", "Publish", "Notify", "Outcome", "Done"]
    assert ex.waited == 180
    # the agent was asked exactly once, in the background
    assert [json.loads(c["payload"]) for c in world.plane.agent.calls] == [{"kind": "weekly", "background": True}]
    # the data is live, attributed to the schedule
    assert ex.output["publish"]["status"] == "published" and world.live.release_id == ex.output["publish"]["releaseId"]
    assert world.live.manifest["source"]["by"] == "schedule" and world.live.manifest["sequence"] == 2
    # one message, to the topic, SNS-safe
    assert len(world.sent) == 1 and world.sent[0]["TopicArn"] == ARNS["NotificationTopicArn"]
    assert world.sent[0]["Subject"].startswith("[LLMOps portal] published ") and "data/lifecycle.json" in world.sent[0]["Message"]
    assert ex.output["execution"].startswith("arn:aws:states:") and ex.output["input"]["trigger"] == "schedule"


@pytest.mark.parametrize("execution_input, request_sent", [
    ({}, {"kind": "weekly"}),
    ({"kind": "focus", "focus": ["new-models", "lifecycle"]}, {"kind": "focus", "focus": ["new-models", "lifecycle"]}),
])
def test_a_manual_start_takes_any_research_request(world, execution_input, request_sent):
    world.research_finishes(accepted_run(), after_waits=1)
    ex = world.start(execution_input)
    assert ex.status == "SUCCEEDED" and json.loads(world.plane.agent.calls[0]["payload"]) == {**request_sent, "background": True}
    assert world.live.manifest["source"]["by"] == "manual"


@pytest.mark.parametrize("execution_input", ["weekly", ["rollback"], 7, True])
def test_an_input_that_is_not_an_object_starts_nothing_and_says_so(world, execution_input):
    """It used to be read as "no input" - which starts (and bills) a weekly research nobody asked for."""
    base = world.live.release_id
    ex = world.start(execution_input)
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed") and world.plane.agent.calls == []
    assert world.sent[0]["Subject"] == "[LLMOps portal] FAILED: BadRequest" and "input 必須是 JSON 物件" in world.sent[0]["Message"]
    assert "portal 資料沒有變更" in world.sent[0]["Message"] and world.live.release_id == base


def test_a_result_stuffed_with_text_cannot_push_the_state_over_the_limit(world):
    """Above 256 KB the service ends the execution with an error no Catch handles: no message would be sent at all."""
    huge = "長" * 120_000
    record = accepted_run()
    record.update(stopReason=huge, finishedAt=huge)
    record["proposal"].update(summary=huge)
    for op in record["proposal"]["ops"]:
        op["rationale"] = huge
    for ev in record["proposal"]["evidence"].values():
        ev.update(quote=huge, publisher=huge, title=huge, note=huge)
    world.research_finishes(record, after_waits=1)
    ex = world.start({"request": {"kind": "weekly", "focus": ["x" * 3000]}, "trigger": "schedule", "by": "名" * 3000})
    assert ex.status == "SUCCEEDED", (ex.error, ex.cause)
    assert ex.output["publish"]["status"] == "published" and len(world.sent) == 1
    assert len(json.dumps(ex.output, ensure_ascii=False).encode("utf-8")) < 150_000
    assert len(world.sent[0]["Message"].encode("utf-8")) < 262_144 and world.sent[0]["Subject"].isascii()


def test_a_run_without_a_proposal_ends_well_and_changes_nothing(world):
    base = world.live.release_id
    world.research_finishes({**accepted_run(), "status": "no_proposal", "proposal": None,
                             "report": {"accepted": False, "errors": ["digest: date 2026-12-01 is not in any recorded evidence quote"]}}, after_waits=2)
    ex = world.start({})
    assert ex.status == "SUCCEEDED" and ex.path[-4:] == ["ResearchDone", "Notify", "Outcome", "Done"] and "Publish" not in ex.path
    assert world.live.release_id == base
    assert world.sent[0]["Subject"] == "[LLMOps portal] research finished without a proposal (no_proposal)"


def test_a_refused_update_notifies_and_fails_the_execution(world):
    base = world.live.release_id
    record = accepted_run()
    record["proposal"]["portal"]["sample"] = True  # the runtime's PORTAL_DATA_URI was never set
    world.research_finishes(record, after_waits=1)
    ex = world.start({})
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateRejected") and ex.path[-3:] == ["Notify", "Outcome", "NotPublished"]
    assert world.live.release_id == base
    assert "update NOT published" in world.sent[0]["Subject"] and "PORTAL_DATA_URI" in world.sent[0]["Message"]


# ---------------------------------------------------------------------------------------------- approval, rollback
def test_review_mode_holds_the_proposal_until_an_approve_execution(world, monkeypatch):
    monkeypatch.setenv("PUBLISH_MODE", "review")
    base = world.live.release_id
    world.research_finishes(accepted_run(), after_waits=1)
    held = world.start({"trigger": "schedule", "request": {"kind": "weekly"}})
    assert held.status == "SUCCEEDED" and held.output["publish"]["status"] == "pending" and world.live.release_id == base
    assert world.sent[0]["Subject"] == "[LLMOps portal] approval needed: prop-20261002T030000Z-7dd4e9c6"
    assert '"action": "approve"' in world.sent[0]["Message"]

    approve = world.start({"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "eunice"})
    assert approve.status == "SUCCEEDED" and approve.path == ["Init", "Route", "Publish", "Notify", "Outcome", "Done"]
    assert world.live.manifest["source"]["approvedBy"] == "eunice" and world.live.manifest["sequence"] == 2
    assert len(world.plane.agent.calls) == 1  # approving does not research again
    assert world.sent[1]["Subject"].startswith("[LLMOps portal] published ")


def test_discard_and_rollback_are_executions_too(world, monkeypatch):
    base = world.live.release_id
    world.research_finishes(accepted_run(), after_waits=1)
    published = world.start({})
    assert published.output["publish"]["status"] == "published"
    back = world.start({"action": "rollback", "by": "eunice", "reason": "日期要再確認"})
    assert back.status == "SUCCEEDED" and back.path == ["Init", "Route", "Publish", "Notify", "Outcome", "Done"]
    assert world.live.release_id == base and world.sent[-1]["Subject"] == f"[LLMOps portal] rolled back to {base}"

    monkeypatch.setenv("PUBLISH_MODE", "review")
    record = accepted_run()
    assert P.publish_proposal(world.plane.data, record, policy=P.Policy(mode="review"), control=world.plane.control).status == "pending"
    dropped = world.start({"action": "discard", "proposalId": record["proposal"]["id"], "by": "eunice"})
    assert dropped.status == "SUCCEEDED" and dropped.output["publish"]["status"] == "discarded" and P.list_pending(world.plane.control) == []


def test_an_existing_result_can_be_published_without_new_research(world):
    world.plane.save_run(accepted_run())
    ex = world.start({"action": "publish", "runKey": f"runs/{RUN}.json", "by": "eunice"})
    assert ex.status == "SUCCEEDED" and ex.output["publish"]["status"] == "published" and world.plane.agent.calls == []


# ---------------------------------------------------------------------------------------------- failures
def test_a_run_the_agent_refuses_is_reported_and_fails(world):
    world.plane.agent.events = [{"type": "result", "status": "error", "error": "busy: this session is already running 2 research run(s)"}]
    ex = world.start({})
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed")
    assert ex.path == ["Init", "Route", "StartResearch", "ReportFailure", "NotifyFailure", "Failed"]
    assert world.sent[0]["Subject"] == "[LLMOps portal] FAILED: ResearchFailed"
    assert "busy" in world.sent[0]["Message"] and "步驟：啟動研究" in world.sent[0]["Message"] and "arn:aws:states:" in world.sent[0]["Message"]


def test_research_that_never_finishes_times_out_inside_the_execution_limit(world):
    ex = world.start({})  # nobody ever writes the run record
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed") and ex.path[-3:] == ["ReportFailure", "NotifyFailure", "Failed"]
    assert world.sent[0]["Subject"] == "[LLMOps portal] FAILED: ResearchTimeout" and "步驟：等待研究結果" in world.sent[0]["Message"]
    # the step gives up (RESEARCH_TIMEOUT_SECONDS = 2400) well before Step Functions would kill the execution silently
    assert 2400 < ex.waited <= 2400 + 120 and ex.waited < world.definition["TimeoutSeconds"]


def test_a_failed_research_run_is_reported_with_the_agents_error(world):
    world.research_finishes({**accepted_run(), "status": "error", "proposal": None, "error": "ModelErrorException: on-demand throughput exceeded"}, after_waits=2)
    ex = world.start({})
    assert ex.status == "FAILED" and "on-demand throughput exceeded" in world.sent[0]["Message"]


@pytest.mark.parametrize("execution_input, error", [
    ({"action": "delete-everything"}, "BadRequest"),
    ({"action": "approve"}, "BadRequest"),
    ({"action": "approve", "proposalId": "prop-nobody-held-this"}, "BadRequest"),
    ({"action": "discard", "proposalId": "prop-nobody-held-this"}, "BadRequest"),
    ({"action": "approve", "proposalId": "prop-x", "sha256": "not-a-hash"}, "BadRequest"),
    ({"action": "rollback", "releaseId": "not-a-release"}, "ReleaseError"),
    ({"kind": "weekly", "dryRun": True}, "ResearchFailed"),
])
def test_requests_that_cannot_be_carried_out_fail_with_a_message(world, execution_input, error):
    base = world.live.release_id
    ex = world.start(execution_input)
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed")
    assert world.sent[0]["Subject"] == f"[LLMOps portal] FAILED: {error}" and world.live.release_id == base


def test_a_misconfigured_output_location_is_caught_at_the_start(world):
    world.plane.agent.result_uri = f"s3://some-other-bucket/out/runs/{RUN}.json"
    ex = world.start({})
    assert ex.status == "FAILED" and ex.path[:4] == ["Init", "Route", "StartResearch", "ReportFailure"]
    assert "OUTPUT_URI" in world.sent[0]["Message"] and f"s3://{BUCKET}/research/" in world.sent[0]["Message"]


# ---------------------------------------------------------------------------------------------- retries and resilience
def flaky(fn, error, times=1, after_running=False):
    """A Lambda whose first ``times`` invocations fail with a Lambda service error (optionally after doing the work)."""
    left = {"n": times}

    def wrapper(event):
        if left["n"] > 0:
            left["n"] -= 1
            if after_running:
                fn(event)
            raise ServiceError(error)
        return fn(event)

    return wrapper


def test_starting_the_research_is_never_retried(world):
    """A retry could start (and bill) a second research run, so a service error at this step ends the execution."""
    world.lambdas[ARNS["StartResearchFunctionArn"]] = flaky(H.start_research, "Lambda.ServiceException", after_running=True)
    ex = world.start({})
    assert ex.status == "FAILED" and len(world.plane.agent.calls) == 1
    assert [s for s, _ in ex.invocations].count("StartResearch") == 1


def test_transient_lambda_errors_while_waiting_are_retried(world):
    world.lambdas[ARNS["CheckResearchFunctionArn"]] = flaky(H.check_research, "Lambda.TooManyRequestsException", times=2)
    world.research_finishes(accepted_run(), after_waits=1)
    ex = world.start({})
    assert ex.status == "SUCCEEDED" and [s for s, _ in ex.invocations].count("CheckResearch") == 3


def test_a_publish_step_that_is_retried_after_it_already_published_does_not_publish_twice(world):
    world.lambdas[ARNS["PublishFunctionArn"]] = flaky(H.publish, "Lambda.SdkClientException", after_running=True)
    world.research_finishes(accepted_run(), after_waits=1)
    ex = world.start({})
    assert ex.status == "SUCCEEDED" and [s for s, _ in ex.invocations].count("Publish") == 2
    assert world.live.manifest["sequence"] == 2 and len(R.list_release_ids(world.plane.data)) == 2
    # ... and the one message that goes out is the one the first attempt would have sent: what was published, the
    # difference, how to take it back - not "no change", which is what the second attempt found
    publish = ex.output["publish"]
    assert publish["status"] == "published" and publish["releaseId"] == world.live.release_id and publish["previousReleaseId"] == world.live.manifest["previousReleaseId"]
    assert [m["Subject"] for m in world.sent] == [f"[LLMOps portal] published {world.live.release_id} (2 files)"]
    message = world.sent[0]["Message"]
    assert "實際變更" in message and '"2026-11-15" → "2026-11-30"' in message and '"action": "rollback"' in message and "提案的理由與引文" in message
    # the same result published once more by somebody else (another execution) is a repeat, and is told so
    world.lambdas[ARNS["PublishFunctionArn"]] = H.publish
    world.plane.save_run(accepted_run())
    again = world.start({"action": "publish", "runKey": f"runs/{RUN}.json", "by": "amy"})
    assert again.output["publish"]["status"] == "already_published" and world.live.manifest["sequence"] == 2


def test_a_rollback_step_that_is_retried_reports_the_rollback_and_does_not_undo_it(world):
    """The response of the first attempt is lost, the service retries: the second attempt must neither roll forward
    again (it used to: a rollback with no target was a toggle) nor report a refusal."""
    base = world.live.release_id
    world.research_finishes(accepted_run(), after_waits=1)
    published = world.start({})
    world.lambdas[ARNS["PublishFunctionArn"]] = flaky(H.publish, "Lambda.SdkClientException", after_running=True)
    ex = world.start({"action": "rollback", "by": "eunice"}, execution_id="arn:aws:states:us-east-1:123456789012:execution:machine:rollback-1")
    assert ex.status == "SUCCEEDED" and [s for s, _ in ex.invocations].count("Publish") == 2
    assert ex.output["publish"]["status"] == "rolled_back" and ex.output["publish"]["releaseId"] == base
    assert ex.output["publish"]["previousReleaseId"] == published.output["publish"]["releaseId"]
    assert world.live.release_id == base and world.live.manifest["sequence"] == 3
    assert "arn:aws" not in json.dumps(world.live.manifest) and "123456789012" not in json.dumps(world.live.manifest)
    # a new request (another execution) for the same thing is told what the situation is
    world.lambdas[ARNS["PublishFunctionArn"]] = H.publish
    again = world.start({"action": "rollback", "by": "amy"}, execution_id="arn:aws:states:us-east-1:123456789012:execution:machine:rollback-2")
    assert (again.status, again.error) == ("FAILED", "PortalUpdateRejected") and world.live.release_id == base


def test_a_resume_step_that_is_retried_still_reports_the_resume(world):
    world.start({"action": "pause", "by": "eunice", "reason": "連假"})
    world.lambdas[ARNS["PublishFunctionArn"]] = flaky(H.publish, "Lambda.SdkClientException", after_running=True)
    ex = world.start({"action": "resume", "by": "eunice"}, execution_id="arn:aws:states:us-east-1:123456789012:execution:machine:resume-1")
    assert ex.status == "SUCCEEDED" and ex.output["publish"]["status"] == "resumed" and P.get_hold(world.plane.data) is None
    world.lambdas[ARNS["PublishFunctionArn"]] = H.publish
    nothing = world.start({"action": "resume", "by": "eunice"}, execution_id="arn:aws:states:us-east-1:123456789012:execution:machine:resume-2")
    assert nothing.output["publish"]["status"] == "unchanged"


def test_errors_that_keep_happening_end_in_a_reported_failure(world):
    world.lambdas[ARNS["CheckResearchFunctionArn"]] = flaky(H.check_research, "Lambda.ServiceException", times=99)
    ex = world.start({})
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed")
    assert [s for s, _ in ex.invocations].count("CheckResearch") == 4  # 1 try + MaxAttempts 3
    assert world.sent[0]["Subject"] == "[LLMOps portal] FAILED: Lambda.ServiceException"


def test_a_notification_that_cannot_be_sent_fails_the_execution_and_undoes_nothing(world, monkeypatch):
    """Nobody is told that an update went live (or waits for approval): the alarm on failed executions is the only
    signal left, so the execution must not end as a success."""
    world.sns_error = "SNS.AuthorizationErrorException"
    world.research_finishes(accepted_run(), after_waits=1)
    ex = world.start({})
    assert (ex.status, ex.error) == ("FAILED", "PortalNotificationFailed") and ex.path[-2:] == ["Notify", "NotificationFailed"]
    assert "may have been published or may be waiting for approval" in ex.cause and "portalctl status" in ex.cause
    assert world.live.manifest["sequence"] == 2 and world.sent == []        # the update itself stays
    monkeypatch.setenv("PUBLISH_MODE", "review")
    world.research_finishes(accepted_run(), after_waits=1)
    P.rollback(world.plane.data, actor="test")
    held = world.start({})
    assert (held.status, held.error) == ("FAILED", "PortalNotificationFailed") and len(P.list_pending(world.plane.control)) == 1


def test_when_even_the_failure_report_fails_a_person_is_still_told_which_execution_to_open(world):
    """The report used to be the end of the line: when its function failed (or could not be called) the execution ended
    red with no message at all.  The state machine now sends one itself - from nothing but its own context."""
    world.plane.agent.events = []
    world.lambdas[ARNS["ReportFailureFunctionArn"]] = flaky(H.report_failure, "Lambda.ServiceException", times=99)
    ex = world.start({}, execution_id="arn:aws:states:us-east-1:123456789012:execution:machine:monday-run")
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed") and ex.path[-3:] == ["ReportFailure", "NotifyUnreported", "Failed"]
    [note] = world.sent
    assert note["Subject"] == "[LLMOps portal] FAILED: the failure report could not be written" and note["Subject"].isascii() and len(note["Subject"]) < 100
    assert note["Message"].endswith("this execution: arn:aws:states:us-east-1:123456789012:execution:machine:monday-run")
    assert "portalctl status" in note["Message"] and "{}" not in note["Message"] and note["Message"].isascii()
    # ... and when the message cannot be sent either, the execution is still a failed one (the alarm is what is left)
    world.sent.clear()
    world.sns_error = "SNS.InternalErrorException"
    ex = world.start({})
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed") and ex.path[-3:] == ["ReportFailure", "NotifyUnreported", "Failed"] and world.sent == []
    world.lambdas[ARNS["ReportFailureFunctionArn"]] = H.report_failure
    ex = world.start({})
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed") and ex.path[-2:] == ["NotifyFailure", "Failed"]


def test_an_input_no_step_can_encode_is_still_answered(world, monkeypatch):
    """Half an emoji in a reason ("\\ud83d": the other half was cut off somewhere) cannot be encoded, measured or stored.
    Every step failed on it, the report of the failure included: a red execution, and nobody was told why."""
    monkeypatch.setenv("PUBLISH_MODE", "review")
    world.research_finishes(accepted_run(), after_waits=1)
    assert world.start({}).status == "SUCCEEDED" and len(P.list_pending(world.plane.control)) == 1
    world.sent.clear()
    half = "日期要再確認 \ud83d"
    ex = world.start({"action": "discard", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "reason": half, "by": "eunice"})
    assert ex.status == "SUCCEEDED" and ex.path[-2:] == ["Outcome", "Done"], (ex.error, ex.cause)
    assert P.list_pending(world.plane.control) == [] and len(world.sent) == 1
    decided = json.loads(world.plane.control.get("decided/prop-20261002T030000Z-7dd4e9c6.json").data)
    assert decided["reason"] == "日期要再確認 \ufffd"  # what could be kept of it, kept
    # the same inside a request that IS refused: the refusal is reported like any other
    world.sent.clear()
    ex = world.start({"action": "approve", "proposalId": "prop-\ud83d"})
    assert (ex.status, ex.error) == ("FAILED", "PortalUpdateFailed") and ex.path[-3:] == ["ReportFailure", "NotifyFailure", "Failed"]
    assert world.sent[0]["Subject"] == "[LLMOps portal] FAILED: BadRequest"


def test_the_state_stays_far_below_the_step_functions_payload_limit(world):
    record = accepted_run()
    record["proposal"]["summary"] = "很長的摘要。" * 5000
    record["proposal"]["unverified"] = [{"claim": "x" * 500, "reason": "y" * 500}] * 200
    world.research_finishes(record, after_waits=1)
    ex = world.start({"kind": "weekly"})
    assert ex.status == "SUCCEEDED" and len(json.dumps(ex.output, ensure_ascii=False).encode("utf-8")) < 30_000


# ---------------------------------------------------------------------------------------------- pause / resume
def test_after_a_rollback_the_next_scheduled_update_waits_for_a_person(world):
    base = world.live.release_id
    world.research_finishes(accepted_run(), after_waits=1)
    assert world.start({"trigger": "schedule"}).output["publish"]["status"] == "published"

    back = world.start({"action": "rollback", "by": "eunice", "reason": "日期有疑問"})
    assert back.status == "SUCCEEDED" and back.output["publish"]["autoPublishPaused"] is True and world.live.release_id == base
    assert "自動發布目前是暫停的" in world.sent[-1]["Message"] and '"action": "resume"' in world.sent[-1]["Message"]

    world.waits = 0
    world.research_finishes(accepted_run(), after_waits=1)  # next week the agent finds the same thing again
    held = world.start({"trigger": "schedule"})
    assert held.status == "SUCCEEDED" and held.output["publish"]["status"] == "pending" and world.live.release_id == base
    assert "approval needed" in world.sent[-1]["Subject"] and "自動發布已暫停（eunice" in world.sent[-1]["Message"]

    resumed = world.start({"action": "resume", "by": "eunice"})
    assert resumed.output["publish"]["status"] == "resumed" and resumed.output["publish"]["autoPublishPaused"] is False
    assert world.sent[-1]["Subject"] == "[LLMOps portal] automatic publishing resumed"
    approved = world.start({"action": "approve", "proposalId": "prop-20261002T030000Z-7dd4e9c6", "by": "eunice"})
    assert approved.output["publish"]["status"] == "published"


def test_publishing_can_be_paused_and_resumed_on_demand(world):
    paused = world.start({"action": "pause", "by": "eunice", "reason": "連假"})
    assert paused.status == "SUCCEEDED" and paused.output["publish"] ["status"] == "paused"
    assert world.sent[-1]["Subject"] == "[LLMOps portal] automatic publishing paused" and "連假" in world.sent[-1]["Message"]
    world.research_finishes(accepted_run(), after_waits=1)
    assert world.start({}).output["publish"]["status"] == "pending"
    assert world.start({"action": "resume"}).output["publish"]["status"] == "resumed"
