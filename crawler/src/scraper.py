"""Scraper: fetches pages with Scrapling and extracts structured payloads.

The scraper is stateless regarding queues, retries and persistence — it only
turns a URL into (a) a structured :class:`ScrapeResult` payload and
(b) the raw links discovered on the page.
"""

from typing import Any

from scrapling.fetchers import AsyncFetcher

import config as cfg
from models import FetchOutcome, ScrapeResult
from utils import normalize_url


def _document_html(document: Any) -> str:
    """Return the full raw HTML source of a fetched document."""
    body = getattr(document, "body", None)
    if isinstance(body, bytes):
        encoding = getattr(document, "encoding", None) or "utf-8"
        return body.decode(encoding, errors="replace")
    if isinstance(body, str):
        return body
    return str(document)


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
        """Turn a fetched page into a payload plus the raw links discovered."""
        if not outcome.ok or outcome.document is None or outcome.final_url is None:
            return None

        document = outcome.document

        external_id: str | None = None
        if self.external_id_selector:
            external_id = _meta_content(document, self.external_id_selector)

        raw_links: list[str] = []
        try:
            raw_links = document.css("a ::attr(href)").getall()
        except Exception:
            pass

        return ScrapeResult(
            external_id=external_id,
            external_url=outcome.final_url,
            entity_type=self.entity_type,
            raw_data=_document_html(document),
            links=raw_links,
        )