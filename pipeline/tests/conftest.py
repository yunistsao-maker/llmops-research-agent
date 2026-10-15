"""Shared fixtures: a portal checkout, and the data plane on a folder and on (moto's) S3."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app" / "llmops_research"
for extra in (REPO / "pipeline", REPO / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# nothing here may reach real AWS or depend on the wall clock
for name, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                    "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true"}.items():
    os.environ[name] = value
for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL"):
    os.environ.pop(name, None)

from portal_pipeline import publish as P  # noqa: E402  (also puts app/llmops_research on sys.path)
from portal_pipeline.storage import DirStore, S3Store  # noqa: E402

SNAPSHOT = APP / "research" / "portal" / "snapshot"
FIXTURES = Path(__file__).parent / "fixtures"
PORTAL_FILES = FIXTURES / "portal"
COMPAT, PERF, INVENTORY = "runs/compat/example-mock.json", "runs/perf/example-mock.json", "data/internal/platform-inventory.example.json"
BUCKET = "portal-data-bucket"
#: the fixture run in fixtures/accepted_run.json was researched against the release made from snapshot_tree() at this time
T0 = dt.datetime(2026, 10, 2, 1, 0, 0, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2026, 10, 2, 3, 30, 0, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2026, 10, 3, 9, 0, 0, tzinfo=dt.timezone.utc)


def squash(text: str) -> str:
    """``text`` without white space and without the marks of a continued line: what a message says, however the lines
    that hold text from elsewhere were wrapped (``notify.wrap``)."""
    return "".join(str(text).replace("┆", "").split())


def snapshot_tree() -> dict[str, bytes]:
    """A small but real portal checkout: the agent's bundled sample data plus the files only people and tools own
    (fixtures/portal: the portal repo's own example test runs, inventory example and gateway config)."""
    files = {p.relative_to(SNAPSHOT).as_posix(): p.read_bytes() for p in sorted(SNAPSHOT.rglob("*"))
             if p.is_file() and p.name != "snapshot_manifest.json"}
    files.update({p.relative_to(PORTAL_FILES).as_posix(): p.read_bytes() for p in sorted(PORTAL_FILES.rglob("*")) if p.is_file()})
    return files


def tool_file(rel: str, **changes) -> bytes:
    """A valid test run / inventory, made from one of the example files with a few fields changed."""
    doc = json.loads((PORTAL_FILES / rel).read_text(encoding="utf-8"))
    doc.update(changes)
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def token_cases(token: str, release: str = "20261002T010000Z-992cd00d") -> dict[str, object]:
    """Request path -> the path the origin is asked for, or 403: what the distribution's access-token check must do.

    One list for the real CloudFront Function (run in Node) and for scripts/serve_data.py, so the local stand-in cannot
    drift into being more permissive than the real thing.
    """
    return {
        f"/{token}/manifest.json": "/manifest.json",
        f"/{token}/releases/{release}/data/catalog.json": f"/releases/{release}/data/catalog.json",
        f"/{token}/": "/",
        "/manifest.json": 403, "/": 403, f"/{token}": 403, f"/{token}x/manifest.json": 403, f"/x{token}/manifest.json": 403,
        f"/releases/{token}/manifest.json": 403, "/wrong-token/manifest.json": 403, f"//{token}/manifest.json": 403,
        f"/{token}%2Fmanifest.json": 403, f"/{token.upper()}/manifest.json": 403,
    }


def write_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


#: The fixture proposal's id.  It is spelled out in the tests (they read better with the real text of a notification
#: or a command in them); this is the one place that notices when the fixture was regenerated with another id.
PROPOSAL_ID = "prop-20261002T030000Z-7dd4e9c6"


def accepted_run() -> dict:
    record = json.loads((FIXTURES / "accepted_run.json").read_text(encoding="utf-8"))
    assert record["proposal"]["id"] == PROPOSAL_ID, (
        f"fixtures/accepted_run.json now holds {record['proposal']['id']}: replace {PROPOSAL_ID} in pipeline/tests/*.py (see make_fixtures.py)")
    return record


class Plane:
    """The three prefixes of the data bucket."""

    def __init__(self, data, control, research, kind):
        self.data, self.control, self.research, self.kind = data, control, research, kind

    def bootstrap(self, tree=None, now=T0):
        outcome = P.publish_tree(self.data, tree or snapshot_tree(), bootstrap=True, actor="test", now=now)
        assert outcome.status == "published", outcome.reasons
        return outcome

    def save_run(self, record: dict) -> str:
        key = f"runs/{record['runId']}.json"
        self.research.put(key, json.dumps(record, ensure_ascii=False).encode("utf-8"), content_type="application/json")
        return key


@pytest.fixture(params=["folder", "s3"])
def plane(request, tmp_path):
    if request.param == "folder":
        yield Plane(DirStore(tmp_path / "portal"), DirStore(tmp_path / "pipeline"), DirStore(tmp_path / "research"), "folder")
        return
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield Plane(S3Store(BUCKET, "portal/", s3), S3Store(BUCKET, "pipeline/", s3), S3Store(BUCKET, "research/", s3), "s3")


@pytest.fixture()
def folder_plane(tmp_path):
    return Plane(DirStore(tmp_path / "portal"), DirStore(tmp_path / "pipeline"), DirStore(tmp_path / "research"), "folder")


# ---------------------------------------------------------------------------------------------- the Lambda steps, wired for tests
from portal_pipeline import handlers as H  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/llmops_research-AbCdEf1234"
SFN = "arn:aws:states:us-east-1:123456789012:stateMachine:llmops-portal-update"
RUN = accepted_run()["runId"]  # the fixture run: its record is what the fake runtime "writes"


class FakeAgentCore:
    """bedrock-agentcore data plane: records the call, answers like the runtime's background mode."""

    def __init__(self, events=None, error=None, result_uri=None):
        self.calls: list[dict] = []
        self.events, self.error, self.result_uri = events, error, result_uri

    def invoke_agent_runtime(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        events = self.events if self.events is not None else [
            {"type": "accepted", "runId": RUN, "resultUri": self.result_uri or f"s3://{BUCKET}/research/runs/{RUN}.json",
             "event": {"contentBlockDelta": {"delta": {"text": "started\n"}}}}]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")
        return {"contentType": "text/event-stream", "response": io.BytesIO(body)}


@pytest.fixture()
def wired(plane, monkeypatch):
    """Handlers wired to the test data plane, a fake runtime and a clock we control."""
    agent = FakeAgentCore()
    monkeypatch.setattr(H, "data_store", lambda: plane.data)
    monkeypatch.setattr(H, "control_store", lambda: plane.control)
    monkeypatch.setattr(H, "research_store", lambda: plane.research)
    monkeypatch.setitem(H.clients, "agentcore", lambda arn: agent)
    now = {"t": T1}
    monkeypatch.setattr(H, "clock", lambda: now["t"])
    for name, value in {"AGENT_RUNTIME_ARN": ARN, "DATA_BUCKET": BUCKET, "PUBLISH_MODE": "auto", "STATE_MACHINE_ARN": SFN,
                        "PORTAL_URL": "https://main.d111.amplifyapp.com", "RESEARCH_TIMEOUT_SECONDS": "2400"}.items():
        monkeypatch.setenv(name, value)
    for name in ("AGENT_QUALIFIER", "AUTO_MAX_OPS", "AUTO_ALLOW_REMOVE", "RESEARCH_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    plane.agent, plane.now = agent, now
    return plane
