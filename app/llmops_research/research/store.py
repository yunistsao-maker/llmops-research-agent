"""Persist run results (S3 or a local directory) so scheduled runs leave something durable behind."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .validate.grounding import whole_json


def _dump(obj: Any) -> bytes:
    # whole_json: a record that holds half a surrogate pair somewhere (a tool argument the model wrote, say) is still saved
    return (json.dumps(whole_json(obj), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def run_location(output_uri: str, run_id: str) -> str:
    """Where ``save_result`` puts the full record of ``run_id`` (used to tell a background caller where to look)."""
    if output_uri.startswith("s3://"):
        bucket, _, prefix = output_uri[5:].partition("/")
        prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        return f"s3://{bucket}/{prefix}runs/{run_id}.json"
    return str(Path(output_uri) / "runs" / f"{run_id}.json")


def save_result(output_uri: str, result: dict, *, client: Any = None) -> str | None:
    """Write ``runs/<runId>.json`` (everything) and, for an accepted proposal, ``proposals/<id>.json``.

    Returns the location of the proposal (or of the run record when there is none), or None if ``output_uri`` is empty.
    """
    if not output_uri:
        return None
    run_id = result.get("runId", "run")
    items: list[tuple[str, dict]] = [(f"runs/{run_id}.json", result)]
    proposal = result.get("proposal")
    if proposal:
        items.append((f"proposals/{proposal['id']}.json", proposal))
    if output_uri.startswith("s3://"):
        import boto3

        bucket, _, prefix = output_uri[5:].partition("/")
        prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        s3 = client or boto3.client("s3")
        for key, body in items:
            s3.put_object(Bucket=bucket, Key=prefix + key, Body=_dump(body), ContentType="application/json")
        return f"s3://{bucket}/{prefix}{items[-1][0]}"
    root = Path(output_uri)
    for key, body in items:
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dump(body))
    return str(root / items[-1][0])
