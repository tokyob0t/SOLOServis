"""Entry point: initialize the database, load active sources and crawl them.

Sources are crawled in parallel at two levels (see AGENTS.md):

* one OS thread per group of sources (``SCRAPER_MAX_THREADS``), each running
  its own ``asyncio`` event loop;
* within every loop, each source gets its own crawler instance with an
  independent ``asyncio.Queue`` worker pool (``SCRAPER_MAX_CONCURRENCY``).

Pressing Ctrl+C cancels every crawl task immediately: worker pools are shut
down, interrupted runs are finalized in SQLite and all resources are released
before exiting.
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time

import config as cfg
from crawler import CrawlSummary, start_run
from database import (
    ensure_source,
    finish_scrape_run,
    get_active_jobs,
    init_db,
)
from models import DataSource, ScraperConfig
from utils import configure_logging, get_logger

_log_init = get_logger("INIT")
_log_seed = get_logger("SEED")
_log_task = get_logger("TASK")
_log_shutdown = get_logger("SHUTDOWN")
_log_summary = get_logger("SUMMARY")

JOIN_TIMEOUT_SECONDS = 10


def distribute(jobs: list, n_parts: int) -> list[list]:
    """Round-robin distribution of jobs into ``n_parts`` balanced chunks."""
    parts = max(1, n_parts)
    chunks: list[list] = [[] for _ in range(parts)]
    for index, job in enumerate(jobs):
        chunks[index % parts].append(job)
    return [chunk for chunk in chunks if chunk]


def _finalize(crawler, status: str, error_message: str | None) -> None:
    """Persist the final state of a SCRAPE_RUN row."""
    finish_scrape_run(
        crawler.db_path,
        crawler.run_id,
        status,
        error_message=error_message,
        records_found=crawler.records_found,
        records_processed=crawler.records_processed,
        records_failed=crawler.records_failed,
    )


async def _finalize_interrupted(crawler) -> None:
    await crawler.aclose()
    _finalize(
        crawler,
        "failed",
        "interrupted by user (Ctrl+C); partial stats persisted",
    )
    _log_task.warning("run #%d interrupted before completion", crawler.run_id)


async def run_job(
    source: DataSource,
    scraper_config: ScraperConfig,
    db_path: str,
    stop_event: threading.Event,
) -> CrawlSummary | None:
    """Crawl one source inside the current event loop; never raises."""
    if stop_event.is_set():
        return None

    crawler = start_run(source, scraper_config, db_path=db_path)
    started = time.monotonic()
    _log_task.info("run #%d started: %s (%s)", crawler.run_id, source.name,
                   source.base_url)
    try:
        summary = await crawler.run()
    except asyncio.CancelledError:
        await _finalize_interrupted(crawler)
        return CrawlSummary(
            run_id=crawler.run_id,
            status="failed",
            pages_found=crawler.records_found,
            pages_processed=crawler.records_processed,
            pages_failed=crawler.records_failed,
        )
    except Exception as exc:  # a broken source must not kill its siblings
        await crawler.aclose()
        _finalize(crawler, "failed", f"{type(exc).__name__}: {exc}")
        _log_task.error("run #%d crashed: %s: %s", crawler.run_id,
                        type(exc).__name__, exc)
        return None

    elapsed = time.monotonic() - started
    level = logging.INFO if summary.status == "completed" else logging.WARNING
    _log_task.log(
        level,
        "run #%d finished: found=%d processed=%d failed=%d in %.1fs [%s]",
        summary.run_id, summary.pages_found, summary.pages_processed,
        summary.pages_failed, elapsed, summary.status,
    )
    return summary


class _ThreadRunner(threading.Thread):
    """Runs this thread's sources concurrently on a private event loop."""

    def __init__(
        self,
        label: int,
        jobs: list[tuple[DataSource, ScraperConfig]],
        db_path: str,
        results: list,
        lock: threading.Lock,
    ):
        super().__init__(name=f"crawler-{label}", daemon=True)
        self.jobs = jobs
        self.db_path = db_path
        self.results = results
        self.lock = lock
        self.stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []

    async def _runner(self) -> list:
        self._tasks = [
            asyncio.create_task(run_job(source, config_, self.db_path,
                                        self.stop_event),
                                name=f"task-{source.name}")
            for source, config_ in self.jobs
        ]
        return await asyncio.gather(*self._tasks, return_exceptions=True)

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            outcomes = self._loop.run_until_complete(self._runner())
            summaries = [
                o for o in outcomes
                if isinstance(o, CrawlSummary)  # drops CancelledError/etc.
            ]
            with self.lock:
                self.results.extend(summaries)
        except Exception as exc:  # loop-level failure
            _log_task.error("thread crashed: %s: %s", type(exc).__name__, exc)
        finally:
            self._release_loop()

    def request_stop(self) -> None:
        """Cancel every running task immediately from another thread."""
        self.stop_event.set()
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _cancel_all() -> None:
            pending = [t for t in self._tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                _log_shutdown.info("cancelled %d task(s) in %s", len(pending),
                                   self.name)

        try:
            loop.call_soon_threadsafe(_cancel_all)
        except RuntimeError:
            pass  # loop already closed between check and call

    def _release_loop(self) -> None:
        """Flush async generators/executor threads and close the loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(self._shutdown_executor(loop))
        except Exception:
            pass
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    @staticmethod
    async def _shutdown_executor(loop: asyncio.AbstractEventLoop) -> None:
        try:
            await asyncio.wait_for(loop.shutdown_default_executor(),
                                   timeout=JOIN_TIMEOUT_SECONDS)
        except (TimeoutError, TypeError):
            pass


def _register_seed_sources() -> int:
    """Register every ``SCRAPER_SEEDS`` URL as an active DATA_SOURCE row."""
    created = 0
    for url in cfg.SEED_URLS:
        _, was_created = ensure_source(cfg.DB_PATH, url)
        if was_created:
            created += 1
            _log_seed.info("registered new seed source: %s", url)
        else:
            _log_seed.info("seed source already exists: %s", url)
    return created


def _install_signal_handlers() -> None:
    """Force our own SIGINT/SIGTERM handlers.

    Background processes may inherit SIGINT as *ignored*, in which case CPython
    never installs its KeyboardInterrupt handler. Registering ours explicitly
    guarantees Ctrl+C / ``kill`` always triggers the immediate-shutdown path.
    """

    def _raise_interrupt(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _raise_interrupt)
        except (ValueError, OSError):
            pass  # not on the main thread / unsupported platform


def cli() -> int:
    configure_logging()
    _silence_third_party_logging()
    _install_signal_handlers()
    _log_init.info("initializing database at %s", cfg.DB_PATH)
    init_db(cfg.DB_PATH)

    if cfg.SEED_URLS:
        registered = _register_seed_sources()
        _log_init.info("%d/%d seed(s) newly registered",
                       registered, len(cfg.SEED_URLS))

    jobs = get_active_jobs(cfg.DB_PATH)
    if not jobs:
        _log_init.warning("no active DATA_SOURCE/SCRAPER_CONFIG rows; nothing to do")
        return 0

    n_threads = max(1, min(cfg.MAX_THREADS, len(jobs)))
    chunks = distribute(jobs, n_threads)
    _log_init.info("%d source(s) scheduled across %d thread(s), "
                   "%d worker(s) per source",
                   len(jobs), len(chunks), cfg.MAX_CONCURRENCY)

    results: list = []
    lock = threading.Lock()
    runners = [
        _ThreadRunner(index, chunk, cfg.DB_PATH, results, lock)
        for index, chunk in enumerate(chunks)
    ]

    exit_code = 0
    try:
        for runner in runners:
            runner.start()
        for runner in runners:
            runner.join()
    except KeyboardInterrupt:
        exit_code = _handle_interrupt(runners)

    _report(results)

    _log_shutdown.info("all database connections closed; resources released")
    return exit_code


def _handle_interrupt(runners: list[_ThreadRunner]) -> int:
    """Cancel every task immediately and wait briefly for clean teardown."""
    _log_shutdown.error("Ctrl+C received; cancelling all crawl tasks...")
    for runner in runners:
        runner.request_stop()
    try:
        for runner in runners:
            runner.join(timeout=JOIN_TIMEOUT_SECONDS)
    except KeyboardInterrupt:
        _log_shutdown.error("second Ctrl+C received; forcing immediate exit")
        os._exit(130)

    alive = [runner for runner in runners if runner.is_alive()]
    if alive:
        _log_shutdown.warning("%d thread(s) did not stop in time; forcing exit",
                              len(alive))
        os._exit(130)
    _log_shutdown.info("all threads stopped cleanly")
    return 130


def _report(results: list) -> None:
    completed = sum(1 for s in results if s.status == "completed")
    failed = sum(1 for s in results if s.status != "completed")
    totals_found = sum(s.pages_found for s in results)
    totals_ok = sum(s.pages_processed for s in results)
    totals_bad = sum(s.pages_failed for s in results)
    _log_summary.info(
        "sources=%d completed=%d failed=%d | pages found=%d processed=%d errors=%d",
        len(results), completed, failed, totals_found, totals_ok, totals_bad,
    )
    if failed:
        _log_summary.warning("%d run(s) did not complete successfully", failed)


def _silence_third_party_logging() -> None:
    """Scrapling emits per-fetch noise through loguru AND stdlib logging."""
    # 1) stdlib channel: drop "scrapling" records entirely.
    noisy = logging.getLogger("scrapling")
    noisy.propagate = False
    noisy.addHandler(logging.NullHandler())
    noisy.setLevel(logging.CRITICAL)
    # 2) loguru channel: detach its default sink; our own logs cover visibility.
    try:
        from loguru import logger as loguru_logger

        loguru_logger.disable("scrapling")
        loguru_logger.remove()
    except ImportError:
        pass


if __name__ == "__main__":
    sys.exit(cli())
