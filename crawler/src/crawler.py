"""Crawler: URL discovery, scheduling, deduplication and concurrency control.

The crawler owns the ``asyncio.Queue`` worker pool, per-domain rate limiting,
depth/domain restrictions and retry scheduling. Extraction is delegated to the
scraper and persistence to :mod:`crawler.database`.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import config as cfg
from database import create_scrape_run, finish_scrape_run, save_scraped_data, update_run_stats
from models import DataSource, FetchOutcome, ScraperConfig, ScrapeResult
from processor import clean_payload, process_links
from scraper import Scraper
from utils import get_logger, parse_rate_limit, same_site

_retry_log = get_logger("RETRY")


@dataclass(slots=True)
class CrawlSummary:
    run_id: int
    pages_found: int = 0
    pages_processed: int = 0
    pages_failed: int = 0
    status: str = "completed"
    error_message: str | None = None
    visited_urls: list[str] = field(default_factory=list)


class RateLimiter:
    """Async minimum-interval limiter built from a ``"<count>/<unit>"`` string."""

    def __init__(self, rate_limit: str | None):
        self.interval = parse_rate_limit(rate_limit or cfg.DEFAULT_RATE_LIMIT)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class _DomainRateLimiters:

    def __init__(self, default_rate_limit: str):
        self._default = default_rate_limit
        self._limiters: dict[str, RateLimiter] = {}

    def for_url(self, url: str) -> RateLimiter:
        try:
            domain = (urlparse(url).hostname or "").lower()
        except ValueError:
            domain = ""
        limiter = self._limiters.get(domain)
        if limiter is None:
            limiter = RateLimiter(self._default)
            self._limiters[domain] = limiter
        return limiter


class Crawler:
    """Crawls one (DATA_SOURCE, SCRAPER_CONFIG) pair inside a SCRAPE_RUN."""

    def __init__(
        self,
        source: DataSource,
        config: ScraperConfig,
        run_id: int,
        db_path: str = cfg.DB_PATH,
        scraper: Scraper | None = None,
        max_depth: int | None = None,
        max_pages: int | None = None,
        concurrency: int | None = None,
    ):
        self.source = source
        self.config = config
        self.run_id = run_id
        self.db_path = db_path
        self.scraper = scraper or Scraper(config.parser_config)
        self.max_depth = cfg.MAX_DEPTH if max_depth is None else max_depth
        self.max_pages = cfg.MAX_PAGES_PER_RUN if max_pages is None else max_pages
        self.concurrency = min(cfg.MAX_CONCURRENCY, concurrency
                               or cfg.MAX_CONCURRENCY)

        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.seen: set[str] = set()
        self.visited: set[str] = set()
        self.limiters = _DomainRateLimiters(config.rate_limit
                                            or cfg.DEFAULT_RATE_LIMIT)

        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._workers: list[asyncio.Task] = []
        self.records_found = 0
        self.records_processed = 0
        self.records_failed = 0
        self.errors: list[str] = []

    # -- scheduling ----------------------------------------------------------

    def _submit(self, url: str, depth: int) -> bool:
        normalized = self.seen  # readability
        if url in normalized:
            return False
        if len(self.seen) >= self.max_pages:
            return False
        self.seen.add(url)
        self._active += 1
        self._idle.clear()
        self.queue.put_nowait((url, depth))
        self.records_found = len(self.seen)
        return True

    async def _worker(self) -> None:
        while True:
            url, depth = await self.queue.get()
            try:
                await self._process(url, depth)
            finally:
                self.queue.task_done()
                self._active -= 1
                if self._active == 0:
                    self._idle.set()

    async def _process(self, url: str, depth: int) -> None:
        await self.limiters.for_url(url).acquire()

        try:
            outcome = await self._fetch_with_retries(url)

            result: ScrapeResult | None = None
            if outcome.ok:
                result = await asyncio.to_thread(self.scraper.parse, outcome)

            if result is None:
                error = outcome.error or "unparseable response"
                self.records_failed += 1
                self.errors.append(f"{url}: {error}")
                return

            result.links = process_links(result.links, result.external_url)
            payload = json.loads(result.raw_data)
            result.raw_data = json.dumps(clean_payload(payload),
                                         ensure_ascii=False)
            await asyncio.to_thread(save_scraped_data, self.db_path,
                                    self.run_id, result)
            self.visited.add(url)
            self.records_processed += 1

            if depth < self.max_depth:
                base = self.source.base_url
                for link in result.links:
                    if len(self.seen) >= self.max_pages:
                        break
                    if not same_site(link, base):
                        continue
                    self._submit(link, depth + 1)
        except Exception as exc:  # never let one page kill the worker
            self.records_failed += 1
            self.errors.append(f"{url}: {type(exc).__name__}: {exc}")
        finally:
            await self._persist_stats()

    async def _fetch_with_retries(self, url: str) -> FetchOutcome:
        last_outcome = FetchOutcome(error="not attempted")
        for attempt in range(max(1, cfg.MAX_RETRIES)):
            outcome = await self.scraper.fetch(url)
            if outcome.ok:
                return outcome

            transient = outcome.status in cfg.TRANSIENT_STATUS_CODES or outcome.status is None
            last_outcome = outcome
            if transient and attempt < cfg.MAX_RETRIES - 1:
                delay = cfg.BACKOFF_BASE_SECONDS * (2**attempt)
                reason = outcome.error or f"HTTP {outcome.status}"
                _retry_log.warning(
                    "attempt %d/%d failed for %s (%s); retrying in %.1fs",
                    attempt + 1, cfg.MAX_RETRIES, url, reason, delay,
                )
                await asyncio.sleep(delay)
            else:
                break
        return last_outcome

    async def _persist_stats(self) -> None:
        await asyncio.to_thread(
            update_run_stats,
            self.db_path,
            self.run_id,
            records_found=self.records_found,
            records_processed=self.records_processed,
            records_failed=self.records_failed,
        )

    # -- public API ------------------------------------------------------------

    async def run(self) -> CrawlSummary:
        """Run the crawl and finalize its SCRAPE_RUN row."""
        seeds = [self.source.base_url]
        for seed in seeds:
            self._submit(seed, depth=0)

        self._workers = [
            asyncio.create_task(self._worker(), name=f"crawler-worker-{i}")
            for i in range(self.concurrency)
        ]

        await self._idle.wait()
        await self.aclose()

        status = "completed"
        error_message: str | None = None
        if self.records_failed > 0 and self.records_processed == 0:
            status = "failed"
            error_message = "; ".join(self.errors[:10]) or None
        elif self.errors:
            error_message = "; ".join(self.errors[:10])

        summary = CrawlSummary(
            run_id=self.run_id,
            pages_found=self.records_found,
            pages_processed=self.records_processed,
            pages_failed=self.records_failed,
            status=status,
            error_message=error_message,
            visited_urls=sorted(self.visited),
        )

        await asyncio.to_thread(
            finish_scrape_run,
            self.db_path,
            self.run_id,
            summary.status,
            error_message,
            summary.pages_found,
            summary.pages_processed,
            summary.pages_failed,
        )
        return summary

    async def aclose(self) -> None:
        """Cancel and reap every worker task; safe to call multiple times."""
        workers, self._workers = self._workers, []
        for worker in workers:
            if not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


def start_run(
    source: DataSource,
    config: ScraperConfig,
    db_path: str = cfg.DB_PATH,
) -> Crawler:
    """Create a SCRAPE_RUN row and bind it to a fresh crawler instance."""
    run_id = create_scrape_run(db_path, config.id)
    return Crawler(source, config, run_id, db_path=db_path)
