"""Central configuration for the crawler.

Every value can be overridden through environment variables so runs can be
tuned without touching the code (e.g. ``SCRAPER_MAX_PAGES=5``).
"""

import os

from utils import normalize_url

DB_PATH: str = os.getenv("SCRAPER_DB_PATH", "scraper.db")

# Concurrency / politeness -----------------------------------------------------
MAX_CONCURRENCY: int = int(os.getenv("SCRAPER_MAX_CONCURRENCY", "10"))
MAX_DEPTH: int = int(os.getenv("SCRAPER_MAX_DEPTH", "1"))
MAX_PAGES_PER_RUN: int = int(os.getenv("SCRAPER_MAX_PAGES", "50"))
DEFAULT_RATE_LIMIT: str = os.getenv("SCRAPER_RATE_LIMIT", "60/min")
MAX_THREADS: int = int(os.getenv("SCRAPER_MAX_THREADS", "4"))

# HTTP fetching ----------------------------------------------------------------
REQUEST_TIMEOUT: float = float(os.getenv("SCRAPER_TIMEOUT", "15"))
MAX_RETRIES: int = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
BACKOFF_BASE_SECONDS: float = float(os.getenv("SCRAPER_BACKOFF_BASE", "1.0"))
TRANSIENT_STATUS_CODES: frozenset[int] = frozenset(
    {408, 429, 500, 502, 503, 504})

BROWSER_LIKE_HEADERS: dict[str, str] = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,"
               "application/xml;q=0.9,image/avif,image/webp,"
               "image/apng,*/*;q=0.8"),
    "Accept-Language":
    "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding":
    "gzip, deflate, br",
    "DNT":
    "1",
    "Upgrade-Insecure-Requests":
    "1",
}


def parse_seed_urls(raw: str) -> list[str]:
    """Parse a comma-separated seed list into normalized unique URLs.

    Bare domains are assumed to be https (``b.com/x`` -> ``https://b.com/x``).
    Invalid or non-http entries are silently dropped.
    """

    def _candidate(item: str) -> str | None:
        url = normalize_url(item)
        if url is None and "." in item and "://" not in item:
            url = normalize_url(f"https://{item}")
        return url

    seeds: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        url = _candidate(item.strip())
        if url and url not in seen:
            seen.add(url)
            seeds.append(url)
    return seeds


# Seed sources -----------------------------------------------------------------
# Comma-separated list of seed URLs (SCRAPER_SEEDS). Each URL becomes its own
# DATA_SOURCE row with a default SCRAPER_CONFIG and is crawled concurrently by
# its own worker pool. Ready to use as a plain list[str] at import time.
SEED_URLS: list[str] = parse_seed_urls(os.getenv("SCRAPER_SEEDS", ""))

DEFAULT_PARSER_CONFIG: dict = {
    "entity_type": "webpage",
    "title_selector": None,
    "external_id_selector": None,
}
