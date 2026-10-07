"""Runtime settings, read from environment variables (set via agentcore.json envVars)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5-5"


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(x.strip().lower() for x in value.split(",") if x.strip())


def discover_gateway_url(env: Mapping[str, str], name_hint: str = "") -> str:
    """The MCP endpoint of the project's gateway, which the AgentCore CDK constructs inject into the runtime as
    ``AGENTCORE_GATEWAY_<NAME>_URL`` (``<NAME>`` = the gateway name upper-cased, non-alphanumerics as ``_``)."""
    urls = {k: v for k, v in env.items() if k.startswith("AGENTCORE_GATEWAY_") and k.endswith("_URL") and v}
    if name_hint:
        return urls.get("AGENTCORE_GATEWAY_" + re.sub(r"[^A-Za-z0-9]+", "_", name_hint).upper() + "_URL", "")
    return next(iter(urls.values())) if len(urls) == 1 else ""


@dataclass(frozen=True)
class Settings:
    # --- model -------------------------------------------------------------
    model_provider: str = "bedrock"  # bedrock | anthropic | scripted
    model_id: str = DEFAULT_MODEL_ID
    region: str = "ap-northeast-1"
    max_tokens: int = 32000
    model_extra_fields: Mapping[str, Any] | None = None  # BedrockModel additional_request_fields
    # --- portal data (read-only) ---------------------------------------------
    portal_data: str = "bundled"  # bundled | /path/to/portal-repo | s3://bucket/prefix/
    # --- web search (AgentCore Gateway web-search connector) ----------------
    search_gateway_id: str = ""
    search_gateway_arn: str = ""
    search_gateway_endpoint: str = ""
    search_gateway_name: str = ""  # resolved to an id at runtime via ListGateways
    search_target: str = ""
    # --- fetching -----------------------------------------------------------
    enable_browser: bool = False
    fetch_timeout: float = 20.0
    fetch_max_bytes: int = 3_000_000
    extra_allow_domains: tuple[str, ...] = ()
    extra_official_domains: tuple[str, ...] = ()  # hosts the operator vouches for as a vendor's own documentation
    # --- budgets (per invocation) ---------------------------------------------
    max_searches: int = 30
    max_fetches: int = 40
    max_tool_calls: int = 200
    max_turns: int = 120  # model calls per run (Strands loop limit)
    max_total_tokens: int = 6_000_000  # input+output tokens summed over the run (spend guard)
    max_runtime_seconds: int = 1500
    # --- output -------------------------------------------------------------
    output_uri: str = ""  # s3://bucket/prefix/ or a directory: where accepted proposals are saved ("" = only returned)
    prompt_cache: str = "auto"  # auto | off  (Bedrock prompt caching of system prompt + tools)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        extra = e.get("MODEL_EXTRA_FIELDS")
        extra_fields = None
        if extra:
            try:
                parsed = json.loads(extra)
                extra_fields = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                extra_fields = None
        return cls(
            model_provider=(e.get("MODEL_PROVIDER") or "bedrock").strip().lower(),
            model_id=e.get("MODEL_ID") or DEFAULT_MODEL_ID,
            region=e.get("AWS_REGION") or e.get("AWS_DEFAULT_REGION") or "ap-northeast-1",
            max_tokens=_int(e.get("MAX_TOKENS"), 32000),
            model_extra_fields=extra_fields,
            portal_data=e.get("PORTAL_DATA_URI") or "bundled",
            search_gateway_id=e.get("WEB_SEARCH_GATEWAY_ID", ""),
            search_gateway_arn=e.get("WEB_SEARCH_GATEWAY_ARN", ""),
            # explicit id / arn / endpoint win; otherwise the URL the deployment injected; otherwise look the gateway up by name
            search_gateway_endpoint=e.get("WEB_SEARCH_GATEWAY_ENDPOINT") or (
                "" if e.get("WEB_SEARCH_GATEWAY_ID") or e.get("WEB_SEARCH_GATEWAY_ARN")
                else discover_gateway_url(e, e.get("WEB_SEARCH_GATEWAY_NAME", ""))),
            search_gateway_name=e.get("WEB_SEARCH_GATEWAY_NAME", ""),
            search_target=e.get("WEB_SEARCH_TARGET", ""),
            enable_browser=_flag(e.get("ENABLE_BROWSER")),
            fetch_timeout=_float(e.get("FETCH_TIMEOUT"), 20.0),
            fetch_max_bytes=_int(e.get("FETCH_MAX_BYTES"), 3_000_000),
            extra_allow_domains=_csv(e.get("EXTRA_ALLOW_DOMAINS")),
            extra_official_domains=_csv(e.get("EXTRA_OFFICIAL_DOMAINS")),
            max_searches=_int(e.get("MAX_SEARCHES"), 30),
            max_fetches=_int(e.get("MAX_FETCHES"), 40),
            max_tool_calls=_int(e.get("MAX_TOOL_CALLS"), 200),
            max_turns=_int(e.get("MAX_TURNS"), 120),
            max_total_tokens=_int(e.get("MAX_TOTAL_TOKENS"), 6_000_000),
            max_runtime_seconds=_int(e.get("MAX_RUNTIME_SECONDS"), 1500),
            output_uri=e.get("OUTPUT_URI", ""),
            prompt_cache=(e.get("PROMPT_CACHE") or "auto").strip().lower(),
        )
