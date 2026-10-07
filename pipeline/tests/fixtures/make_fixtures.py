#!/usr/bin/env python3
"""Regenerate fixtures/accepted_run.json: a real run record, produced by the real agent stack with a scripted model.

Needs the agent's dependencies (run it with the agent's environment):

    cd app/llmops_research && uv run --extra dev python ../../pipeline/tests/fixtures/make_fixtures.py

The run is researched against the release that ``conftest.snapshot_tree()`` bootstraps at ``conftest.T0``, so the tests can
recreate exactly the data the proposal was made for.  Regenerate after changing the bundled snapshot or the proposal format.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
os.environ["RESEARCH_NOW"] = "2026-10-02T03:00:00Z"  # the same pinned clock as the agent's own tests (the script cites this day)
os.environ["RESEARCH_TODAY"] = "2026-10-02"
sys.path[:0] = [str(HERE.parent), str(REPO / "pipeline"), str(REPO / "app" / "llmops_research"), str(REPO / "app" / "llmops_research" / "tests")]

import conftest as C  # noqa: E402
from e2e_helpers import FOUNDRY_HTML, FOUNDRY_URL, Site, foundry_script, settings  # noqa: E402
from portal_pipeline.storage import DirStore  # noqa: E402
from research.agent import run_once  # noqa: E402
from research.offline.scripted_model import ScriptedModel  # noqa: E402
from research.web.search_backend import FakeSearchBackend  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plane = C.Plane(DirStore(Path(tmp) / "portal"), DirStore(Path(tmp) / "pipeline"), DirStore(Path(tmp) / "research"), "folder")
        release = plane.bootstrap().release_id
        st = settings(portal_data=str(Path(tmp) / "portal"), output_uri=str(Path(tmp) / "research"))
        site = Site().add(FOUNDRY_URL, FOUNDRY_HTML)
        res = asyncio.run(run_once({"prompt": "Check the Foundry retirement date of claude-sonnet-4-5", "kind": "prompt"}, settings=st,
                                   model=ScriptedModel(foundry_script()), fetcher=site.fetcher(), search=FakeSearchBackend({})))
        assert res["status"] == "accepted", res.get("error") or res.get("report")
        saved = json.loads((Path(tmp) / "research" / "runs" / f"{res['runId']}.json").read_text(encoding="utf-8"))
        assert saved["proposal"]["portal"]["manifest"]["releaseId"] == release, saved["proposal"]["portal"]
        text = json.dumps(saved, ensure_ascii=False, indent=1).replace(tmp, "/data")
        (HERE / "accepted_run.json").write_text(text + "\n", encoding="utf-8")
        print(f"wrote accepted_run.json: run {saved['runId']}, proposal {saved['proposal']['id']}, based on release {release}")
        print("the proposal id is spelled out in the pipeline tests (conftest.PROPOSAL_ID checks it): when it changed, replace the old id in pipeline/tests/*.py")


if __name__ == "__main__":
    main()
