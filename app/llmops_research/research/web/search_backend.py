"""Web search backends.

* ``GatewaySearchBackend`` -- AgentCore Gateway *web-search connector* (managed search, IAM/SigV4,
  no third-party API key).  Offered in us-east-1, eu-west-1 and ap-northeast-1.
* ``SeedSearchBackend``    -- no network: ranks the portal's own source URLs plus a curated list of
  official pages.  Used when no gateway is configured, so the agent still has somewhere to start.
* ``FakeSearchBackend``    -- canned results for tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

from .tiers import host_of

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    published: str | None = None
    backend: str = ""


class SearchBackend(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        max_results: int = 6,
        include_domains: Sequence[str] | None = None,
        published_after: str | None = None,
    ) -> list[SearchHit]: ...


class SearchUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
class GatewaySearchBackend:
    name = "agentcore-gateway-web-search"

    def __init__(self, client: Any):
        self._client = client

    @classmethod
    def from_settings(cls, settings) -> "GatewaySearchBackend":
        from bedrock_agentcore.tools import WebSearchClient

        gateway_id = settings.search_gateway_id
        if not (gateway_id or settings.search_gateway_arn or settings.search_gateway_endpoint):
            if not settings.search_gateway_name:
                raise SearchUnavailable("no web-search gateway configured")
            gateway_id = resolve_gateway_id(settings.search_gateway_name, settings.region)
        kwargs: dict[str, Any] = {"region": settings.region}
        if settings.search_gateway_arn:
            kwargs["gateway_arn"] = settings.search_gateway_arn
        elif settings.search_gateway_endpoint:
            kwargs["gateway_endpoint"] = settings.search_gateway_endpoint
        else:
            kwargs["gateway_id"] = gateway_id
        if settings.search_target:
            kwargs["target_name"] = settings.search_target
        return cls(WebSearchClient(**kwargs))

    def search(self, query, *, max_results=6, include_domains=None, published_after=None) -> list[SearchHit]:
        try:
            resp = self._client.search(
                query[:200],
                max_results=max(1, min(int(max_results), 25)),
                include_domains=list(include_domains) if include_domains else None,
                published_after=published_after,
            )
        except Exception as e:  # WebSearchError, botocore errors, ValueError
            raise SearchUnavailable(f"{type(e).__name__}: {e}") from e
        hits = []
        for r in resp.results:
            if not r.url:
                continue
            hits.append(SearchHit(r.title or r.url, r.url, (r.text or "")[:600], r.published_date, self.name))
        return hits


def resolve_gateway_id(name: str, region: str) -> str:
    """Find a gateway id by (partial) name.  Needs bedrock-agentcore:ListGateways."""
    import boto3

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    matches: list[str] = []
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        page = client.list_gateways(**kwargs)
        for g in page.get("items", []):
            if name.lower() in (g.get("name", "") or "").lower():
                matches.append(g["gatewayId"])
        token = page.get("nextToken")
        if not token:
            break
    if len(matches) != 1:
        raise SearchUnavailable(f"expected exactly one gateway matching {name!r}, found {len(matches)}")
    return matches[0]


# --------------------------------------------------------------------------------------
#: Curated official starting points (title, url, keywords).  Complements the portal's sources.json.
SEED_PAGES: list[tuple[str, str, str]] = [
    ("Amazon Bedrock model lifecycle", "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html",
     "bedrock lifecycle legacy eol end of life retirement deprecation extended access"),
    ("Amazon Bedrock model lifecycle (Legacy / EOL table)", "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html",
     "bedrock legacy eol table retirement date model id region"),
    ("Supported foundation models in Amazon Bedrock", "https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html",
     "bedrock models supported regions model id"),
    ("Amazon Bedrock pricing", "https://aws.amazon.com/bedrock/pricing/", "bedrock pricing price per 1000 tokens"),
    ("AWS What's New", "https://aws.amazon.com/about-aws/whats-new/recent/feed/", "aws whats new announcement launch generally available bedrock"),
    ("Microsoft Foundry model retirement schedule",
     "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule",
     "azure foundry openai retirement schedule deprecated retire gpt model version"),
    ("Foundry Models sold directly by Azure",
     "https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure", "azure foundry models region availability"),
    ("Claude models in Microsoft Foundry", "https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models",
     "claude foundry azure anthropic availability"),
    ("Anthropic model deprecations", "https://platform.claude.com/docs/en/about-claude/model-deprecations",
     "anthropic claude deprecation retirement not before date replacement"),
    ("Anthropic pricing", "https://platform.claude.com/docs/en/about-claude/pricing", "anthropic claude pricing price per million tokens cache"),
    ("Anthropic models overview", "https://platform.claude.com/docs/en/about-claude/models/overview", "anthropic claude models overview context window"),
    ("Claude Platform release notes", "https://platform.claude.com/docs/en/release-notes/overview", "anthropic claude release notes api changes"),
    ("OpenAI API deprecations", "https://developers.openai.com/api/docs/deprecations", "openai deprecations shutdown date replacement"),
    ("OpenAI models", "https://developers.openai.com/api/docs/models", "openai models gpt pricing context"),
    ("Gemini API deprecations", "https://ai.google.dev/gemini-api/docs/deprecations", "gemini api deprecations shutdown date google"),
    ("Gemini API changelog", "https://ai.google.dev/gemini-api/docs/changelog", "gemini api changelog release google"),
    ("Gemini API models", "https://ai.google.dev/gemini-api/docs/models", "gemini models pricing context google"),
    ("Gemini Enterprise Agent Platform model versions and lifecycle",
     "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions",
     "google cloud agent platform vertex gemini model versions lifecycle retirement"),
    ("LiteLLM release notes", "https://docs.litellm.ai/release_notes/", "litellm release notes version stable"),
    ("LiteLLM on PyPI (JSON)", "https://pypi.org/pypi/litellm/json", "litellm pypi release version latest"),
    ("LiteLLM blog", "https://docs.litellm.ai/blog", "litellm blog day 0 support security advisory incident"),
    ("MODA AI governance", "https://moda.gov.tw/major-policies/ai/governance/19248", "taiwan moda ai basic act risk framework regulation 數發部"),
]

_WORD = re.compile(r"[a-z0-9\.\-]+|[一-鿿]+")


def _tokens(s: str) -> set[str]:
    return {t for t in _WORD.findall(s.lower()) if len(t) > 1}


class SeedSearchBackend:
    name = "seed-pages"

    def __init__(self, portal_sources: Iterable[dict] | None = None):
        self._pages: list[tuple[str, str, str]] = list(SEED_PAGES)
        known = {u for _, u, _ in self._pages}
        for s in portal_sources or []:
            if s.get("url") and s["url"] not in known:
                self._pages.append((s.get("title", s["url"]), s["url"], f"{s.get('publisher', '')} {s.get('type', '')} {s.get('title', '')}"))

    def search(self, query, *, max_results=6, include_domains=None, published_after=None) -> list[SearchHit]:
        q = _tokens(query)
        scored = []
        for title, url, kw in self._pages:
            if include_domains and not any(host_of(url) == d or host_of(url).endswith("." + d) for d in include_domains):
                continue
            overlap = len(q & _tokens(f"{title} {kw} {url}"))
            if overlap:
                scored.append((overlap, title, url))
        scored.sort(key=lambda x: (-x[0], x[2]))
        return [SearchHit(t, u, "(seed page - not a live search result; fetch it to read the current content)", None, self.name)
                for _, t, u in scored[:max_results]]


class FakeSearchBackend:
    name = "fake"

    def __init__(self, results: dict[str, list[SearchHit]] | None = None, default: list[SearchHit] | None = None):
        self.results = results or {}
        self.default = default or []
        self.calls: list[dict] = []

    def search(self, query, *, max_results=6, include_domains=None, published_after=None) -> list[SearchHit]:
        self.calls.append({"query": query, "max_results": max_results, "include_domains": include_domains, "published_after": published_after})
        for key, hits in self.results.items():
            if key.lower() in query.lower():
                return hits[:max_results]
        return self.default[:max_results]


class CompositeSearchBackend:
    """Try the live backend; fall back to seed pages when it is unavailable or empty."""

    def __init__(self, primary: SearchBackend | None, fallback: SearchBackend):
        self.primary, self.fallback = primary, fallback
        self.name = f"{primary.name}+{fallback.name}" if primary else fallback.name
        self.last_error: str | None = None

    def search(self, query, *, max_results=6, include_domains=None, published_after=None) -> list[SearchHit]:
        if self.primary is not None:
            try:
                hits = self.primary.search(query, max_results=max_results, include_domains=include_domains, published_after=published_after)
                if hits:
                    return hits
            except SearchUnavailable as e:
                self.last_error = str(e)
                log.warning("live search unavailable, using seed pages: %s", e)
        return self.fallback.search(query, max_results=max_results, include_domains=include_domains, published_after=published_after)


def build_search_backend(settings, portal_sources: Iterable[dict] | None = None) -> SearchBackend:
    seed = SeedSearchBackend(portal_sources)
    primary: SearchBackend | None = None
    if settings.search_gateway_id or settings.search_gateway_arn or settings.search_gateway_endpoint or settings.search_gateway_name:
        try:
            primary = GatewaySearchBackend.from_settings(settings)
        except Exception as e:  # misconfiguration must not stop the run; seed pages still work
            log.warning("web-search gateway not usable (%s); falling back to seed pages", e)
    return CompositeSearchBackend(primary, seed)
