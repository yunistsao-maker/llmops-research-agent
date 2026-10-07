"""Render JavaScript-heavy pages through the AgentCore Browser tool (managed Chromium).

Some official pages (for example Google's Agent Platform model-version tables) are built client-side
and come back almost empty from a plain HTTP fetch.  This renderer starts an AgentCore Browser
session, drives it with Playwright over CDP, and returns the rendered HTML in the same shape as the
normal fetcher so the rest of the pipeline (parsing, trust tier, quotes) is identical.

It is optional: ``ENABLE_BROWSER=true`` and ``pip install playwright`` (the AgentCore Browser is the
remote Chromium; no local browser is downloaded).  The AWS-facing calls are not exercised by the unit
tests -- they use an injected fake -- so treat the first real run as the integration test.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..clock import iso_now
from .safe_http import FetchError, FetchResult, validate_url

log = logging.getLogger(__name__)


def _default_driver(region: str, timeout_ms: int) -> Callable[[str], tuple[str, str]]:
    """Return ``render(url) -> (html, final_url)`` backed by AgentCore Browser + Playwright."""

    def render(url: str) -> tuple[str, str]:
        from bedrock_agentcore.tools.browser_client import browser_session  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore

        with browser_session(region) as client:
            ws_url, headers = client.generate_ws_headers()
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(ws_url, headers=headers)
                try:
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    return page.content(), page.url
                finally:
                    browser.close()

    return render


class AgentCoreBrowserRenderer:
    def __init__(self, region: str, *, timeout_ms: int = 45_000, driver: Callable[[str], tuple[str, str]] | None = None):
        self._render = driver or _default_driver(region, timeout_ms)
        self.region = region

    def render(self, url: str) -> FetchResult:
        validate_url(url, resolver=None)  # scheme / port / host-name rules; the managed browser does its own networking
        try:
            html, final_url = self._render(url)
        except ImportError as e:
            raise FetchError(f"browser rendering needs the 'playwright' package ({e})") from e
        except Exception as e:
            log.warning("browser render failed for %s: %s", url, e)
            raise FetchError(f"browser rendering failed: {type(e).__name__}: {e}") from e
        validate_url(final_url, resolver=None)
        return FetchResult(url, final_url, 200, "text/html; charset=utf-8", html.encode("utf-8"), False, iso_now(), [])
