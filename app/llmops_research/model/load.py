"""Model factory.  ``MODEL_PROVIDER`` = bedrock (default) | anthropic | scripted (offline test double)."""

from __future__ import annotations

import os

from research.config import Settings


def load_model(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    provider = settings.model_provider

    if provider == "scripted":
        from research.offline.scripted_model import ScriptedModel, load_script_file, smoke_script

        path = os.environ.get("SCRIPT_FILE")
        return ScriptedModel(load_script_file(path) if path else smoke_script())

    if provider == "anthropic":
        try:
            from strands.models.anthropic import AnthropicModel
        except ImportError as e:  # the SDK extra is optional
            raise RuntimeError("MODEL_PROVIDER=anthropic needs `pip install 'strands-agents[anthropic]'` and ANTHROPIC_API_KEY") from e
        return AnthropicModel(
            client_args={"api_key": os.environ["ANTHROPIC_API_KEY"]}, model_id=settings.model_id, max_tokens=settings.max_tokens,
            **({"params": dict(settings.model_extra_fields)} if settings.model_extra_fields else {}),
        )

    if provider != "bedrock":
        raise ValueError(f"unknown MODEL_PROVIDER {provider!r} (bedrock | anthropic | scripted)")

    from botocore.config import Config
    from strands.models import BedrockModel
    from strands.models.model import CacheConfig

    # Claude 5.5 on Bedrock: use an inference-profile id (global.* / us.* ...); do NOT set temperature/top_p
    # (non-default values return 400) and do not force tool_choice (400) -- the agent never needs either.
    kwargs: dict = {
        "model_id": settings.model_id,
        "max_tokens": settings.max_tokens,
        "region_name": settings.region,
        "boto_client_config": Config(read_timeout=600, connect_timeout=15, retries={"max_attempts": 4, "mode": "adaptive"},
                                     user_agent_extra="llmops-research-agent/0.1"),
    }
    if settings.model_extra_fields:
        kwargs["additional_request_fields"] = dict(settings.model_extra_fields)
    if settings.prompt_cache != "off":
        kwargs["cache_config"] = CacheConfig(strategy="auto")
    return BedrockModel(**kwargs)
