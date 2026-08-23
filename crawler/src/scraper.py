"""Scraper: fetches pages with Scrapling and extracts structured payloads.

The scraper is stateless regarding queues, retries and persistence — it only
turns a URL into (a) a structured :class:`ScrapeResult` payload and
(b) the raw links discovered on the page.
"""

import json
from typing import Any

from scrapling.fetchers import AsyncFetcher

import config as cfg
from models import FetchOutcome, ScrapeResult
from utils import normalize_url


def _selector_text(document: Any,
                   selector: str | None,
                   default: str | None = None) -> str | None:
    """Run an optional CSS selector returning its first text node."""
    if not selector:
        return default
    try:
        values = document.css(f"{selector} ::text").getall()
    except Exception:
        values = []
    for value in values:
        value = value.strip()
        if value:
            return value
    return default


def _meta_content(document: Any, css_selector: str) -> str | None:
    try:
        elements = document.css(css_selector)
    except Exception:
        return None
    for element in elements:
        content = element.attrib.get("content")
        if content:
            return content.strip()
    return None


class Scraper:
    """Fetches and parses pages according to ``parser_config``."""

    def __init__(self, parser_config: dict | None = None):
        parser_config = parser_config or {}
        self.entity_type: str = parser_config.get("entity_type", "webpage")
        self.title_selector: str | None = parser_config.get("title_selector")
        self.external_id_selector: str | None = parser_config.get(
            "external_id_selector")

    async def fetch(self, url: str) -> FetchOutcome:
        """Perform one HTTP GET using Scrapling's async fetcher."""
        try:
            response = await AsyncFetcher.get(
                url,
                timeout=cfg.REQUEST_TIMEOUT,
                follow_redirects=True,
                retries=1,
                retry_delay=1,
                headers=cfg.BROWSER_LIKE_HEADERS,
            )
        except Exception as exc:  # network errors, DNS, timeouts...
            return FetchOutcome(status=None,
                                final_url=None,
                                document=None,
                                error=str(exc))

        status = int(response.status)
        error = None
        if status >= 400:
            error = f"HTTP {status}"
        return FetchOutcome(
            status=status,
            final_url=normalize_url(str(response.url)) or url,
            document=response if error is None else None,
            error=error,
        )

    def parse(self, outcome: FetchOutcome) -> ScrapeResult | None:
        """Extract a structured payload plus raw links from a fetched page."""
        if not outcome.ok or outcome.document is None or outcome.final_url is None:
            return None

        document = outcome.document

        title = _selector_text(document, self.title_selector)
        if title is None:
            title = _selector_text(document, "title")

        description = _meta_content(document, 'meta[name="description"]')
        og_url = _meta_content(document, 'meta[property="og:url"]')

        external_id: str | None = None
        if self.external_id_selector:
            external_id = _meta_content(document, self.external_id_selector)

        favicon: str | None = None
        try:
            link_elements = document.css("link")
        except Exception:
            link_elements = []
        for element in link_elements:
            rel = (element.attrib.get("rel") or "").lower()
            if "icon" in rel:
                href = element.attrib.get("href")
                if href:
                    favicon = normalize_url(href, outcome.final_url)
                    break
        if favicon is None:
            favicon = normalize_url("/favicon.ico", outcome.final_url)

        h1_values: list[str] = []
        try:
            h1_values = [
                t.strip() for t in document.css("h1 ::text").getall()
                if t.strip()
            ]
        except Exception:
            pass

        payload = {
            "title": title,
            "description": description,
            "favicon": favicon,
            "og_url": og_url,
            "h1": h1_values[:5],
            "http_status": outcome.status,
            "final_url": outcome.final_url,
        }

        raw_links: list[str] = []
        try:
            raw_links = document.css("a ::attr(href)").getall()
        except Exception:
            pass

        return ScrapeResult(
            external_id=external_id,
            external_url=outcome.final_url,
            entity_type=self.entity_type,
            raw_data=json.dumps(payload, ensure_ascii=False),
            links=raw_links,
        )
