"""Asynchronous web crawling and scraping system.

The package is split into clearly separated responsibilities:

* ``crawler.Crawler`` — discovers, schedules, deduplicates and manages URLs.
* ``scraper.Scraper`` — fetches pages (Scrapling) and extracts structured data.
* ``extractor.Extractor`` — reads typed fields out of a fetched document,
  shaped for the production schema in ``backend/database/migrations``.
"""

from crawler import Crawler, CrawlSummary
from extractor import EntitySpec, Extraction, Extractor, export_jsonl
from models import DataSource, FetchOutcome, ScraperConfig, ScrapeResult
from scraper import Scraper

__all__ = [
    "Crawler",
    "CrawlSummary",
    "DataSource",
    "EntitySpec",
    "Extraction",
    "Extractor",
    "FetchOutcome",
    "Scraper",
    "ScrapeResult",
    "ScraperConfig",
    "export_jsonl",
]

__version__ = "0.1.0"
