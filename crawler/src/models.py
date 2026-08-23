"""Typed data models shared across the crawler, scraper and persistence layers."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DataSource:
    id: int
    name: str
    base_url: str
    source_type: str
    active: bool = True


@dataclass(slots=True)
class ScraperConfig:
    id: int
    data_source_id: int
    scraper_type: str
    target_type: str
    schedule: str | None = None
    rate_limit: str | None = None
    parser_config: dict = field(default_factory=dict)
    active: bool = True


@dataclass(slots=True)
class ScrapeRun:
    id: int
    scraper_config_id: int
    started_at: str
    finished_at: str | None = None
    records_found: int = 0
    records_processed: int = 0
    records_failed: int = 0
    status: str = "pending"
    error_message: str | None = None


@dataclass(slots=True)
class FetchOutcome:
    """Result of a single HTTP fetch attempt."""

    status: int | None = None
    final_url: str | None = None
    document: Any | None = None  # Scrapling Response (Adaptor-like)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


@dataclass(slots=True)
class ScrapeResult:
    """Structured payload produced by the scraper for one page."""

    external_id: str | None
    external_url: str
    entity_type: str
    raw_data: str  # JSON string payload
    links: list[str] = field(default_factory=list)
