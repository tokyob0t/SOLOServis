"""Asynchronous web crawling and scraping system.

The package is split into two clearly separated responsibilities:

* ``crawler.Crawler`` — discovers, schedules, deduplicates and manages URLs.
* ``scraper.Scraper`` — fetches pages (Scrapling) and extracts structured data.
"""

from crawler import Crawler, CrawlSummary
from models import DataSource, FetchOutcome, ScraperConfig, ScrapeResult
from scraper import Scraper

__all__ = [
    "Crawler",
    "CrawlSummary",
    "DataSource",
    "FetchOutcome",
    "Scraper",
    "ScrapeResult",
    "ScraperConfig",
]

__version__ = "0.1.0"
