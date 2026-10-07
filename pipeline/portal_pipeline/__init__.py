"""Publishing pipeline for the LLMOps portal data.

The research agent (``app/llmops_research``) only *proposes* changes.  This package is the deterministic other half:
it re-validates an accepted proposal against the data the UI is currently serving, writes the result as an immutable
release, and switches a single pointer file so the UI picks it up - no model, no web access, no UI deployment.

    storage     an S3 prefix (AWS) or a folder (local runs, tests) behind one small interface
    releases    the layout the UI reads: manifest.json -> releases/<id>/...
    publish     proposal -> release (gates, approval, rollback, manual pushes from a portal checkout)
    notify      the messages a person receives
    agentcore   starting a background research run on AgentCore Runtime
    handlers    AWS Lambda entry points used by the Step Functions state machine (infra/pipeline.yaml)

The portal data model (schema + cross-file checks, JSON patching) is shared with the agent and lives in
``app/llmops_research/research``; ``scripts/build_lambdas.py`` copies the modules that are needed into the Lambda bundle.
"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"


def _make_research_importable() -> None:
    """In a source checkout the shared modules live under app/llmops_research; in the Lambda bundle they sit next to us."""
    try:
        import research.portal.data  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    app_dir = Path(__file__).resolve().parents[2] / "app" / "llmops_research"
    if (app_dir / "research" / "portal" / "data.py").is_file() and str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))


_make_research_importable()
