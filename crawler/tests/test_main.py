import asyncio
import threading

from test_crawler import FakeScraper

import config as cfg
from crawler import Crawler, CrawlSummary, start_run
from database import (
    count_table,
    create_scrape_run,
    ensure_source,
    get_active_jobs,
    get_run,
    init_db,
)
from main import distribute, run_job
from models import DataSource, ScrapeResult


class TestDistribute:
    def test_balanced_round_robin(self):
        jobs = list(range(7))
        chunks = distribute(jobs, 3)
        assert [len(chunk) for chunk in chunks] == [3, 2, 2]
        assert sorted(j for c in chunks for j in c) == jobs

    def test_more_parts_than_jobs(self):
        chunks = distribute([1, 2], 5)
        assert len(chunks) == 2  # chunks vacíos se descartan

    def test_single_job_single_part(self):
        assert distribute([42], 4) == [[42]]

    def test_empty_jobs(self):
        assert distribute([], 3) == []


class TestParseSeedUrls:
    def test_parses_normalizes_and_dedupes(self):
        raw = " https://a.com , b.com/path#frag , https://A.com/ , ftp://no "
        assert cfg.parse_seed_urls(raw) == ["https://a.com/", "https://b.com/path"]

    def test_bare_domains_assumed_https(self):
        assert cfg.parse_seed_urls("midominio.com/cat") == [
            "https://midominio.com/cat"
        ]

    def test_empty_string(self):
        assert cfg.parse_seed_urls("") == []

    def test_seeds_loaded_from_env_at_import(self, monkeypatch):
        import importlib

        monkeypatch.setenv("SCRAPER_SEEDS", "one.test, https://two.test/x")
        import src.config as config_module

        reloaded = importlib.reload(config_module)
        try:
            assert reloaded.SEED_URLS == ["https://one.test/", "https://two.test/x"]
        finally:
            monkeypatch.delenv("SCRAPER_SEEDS")
            importlib.reload(config_module)


class TestConcurrentSources:
    """Dos crawlers (una seed cada uno) corriendo en paralelo en un mismo loop."""

    def _setup(self, tmp_path):
        path = str(tmp_path / "multi.db")
        init_db(path)
        id_a, _ = ensure_source(path, "https://a.test/")
        id_b, _ = ensure_source(path, "https://b.test/")
        jobs = get_active_jobs(path)
        run_a = create_scrape_run(path, jobs[0][1].id)
        run_b = create_scrape_run(path, jobs[1][1].id)
        return path, (id_a, run_a), (id_b, run_b)

    @staticmethod
    def _result(url, links=()):
        return ScrapeResult(None, url, "webpage", "{}", links=list(links))

    def test_gather_of_two_crawlers_isolates_runs(self, tmp_path):
        pages_a = {
            "https://a.test/": self._result("https://a.test/", ["/x"]),
            "https://a.test/x": self._result("https://a.test/x"),
        }
        pages_b = {
            "https://b.test/": self._result("https://b.test/", ["/y"]),
            "https://b.test/y": self._result("https://b.test/y"),
        }

        path, (id_a, run_a), (id_b, run_b) = self._setup(tmp_path)

        def source_for(url):
            return DataSource(id=0, name=url, base_url=url, source_type="web")

        crawler_a = Crawler(
            source_for("https://a.test/"), get_active_jobs(path)[0][1], run_a,
            db_path=path, scraper=FakeScraper(pages_a),
            max_depth=1, max_pages=10, concurrency=2,
        )
        crawler_b = Crawler(
            source_for("https://b.test/"), get_active_jobs(path)[1][1], run_b,
            db_path=path, scraper=FakeScraper(pages_b),
            max_depth=1, max_pages=10, concurrency=2,
        )

        async def runner():
            return await asyncio.gather(crawler_a.run(), crawler_b.run())

        summary_a, summary_b = asyncio.run(runner())

        assert summary_a.pages_processed == 2
        assert summary_b.pages_processed == 2
        assert summary_a.run_id != summary_b.run_id

        import sqlite3

        conn = sqlite3.connect(path)
        urls = {row[0] for row in conn.execute("SELECT external_url FROM SCRAPED_DATA")}
        conn.close()
        assert any("/a.test/" in u for u in urls)
        assert any("/b.test/" in u for u in urls)
        assert count_table(path, "SCRAPE_RUN") == 2


class TestRunPerSource:
    def test_start_run_creates_independent_rows(self, tmp_path):
        path = str(tmp_path / "runs.db")
        init_db(path)
        _, created = ensure_source(path, "https://s.test/")
        assert created
        source, config_ = get_active_jobs(path)[0]

        first = start_run(source, config_, db_path=path)
        second = start_run(source, config_, db_path=path)

        assert isinstance(first, Crawler)
        assert first.run_id != second.run_id


class TestImmediateCancellation:
    """Ctrl+C: las tasks se cancelan al instante y el run queda finalizado."""

    class SlowScraper:
        def __init__(self):
            self.fetches_started = 0

        async def fetch(self, url):
            self.fetches_started += 1
            await asyncio.sleep(30)
            raise AssertionError("should have been cancelled")

        def parse(self, outcome):
            return None

    def test_cancelled_job_finalizes_run_as_failed(self, tmp_path):
        path = str(tmp_path / "cancel.db")
        init_db(path)
        ensure_source(path, "https://slow.test/")
        source, scraper_config = get_active_jobs(path)[0]

        async def scenario():
            stop_event = threading.Event()
            task = asyncio.create_task(
                run_job(source, scraper_config, path, stop_event)
            )
            await asyncio.sleep(0.3)  # workers dentro del fetch lento
            start = asyncio.get_running_loop().time()
            task.cancel()
            summary = await task
            return summary, asyncio.get_running_loop().time() - start

        summary, elapsed = asyncio.run(scenario())

        assert isinstance(summary, CrawlSummary)
        assert summary.status == "failed"
        row = get_run(path, 1)
        assert row is not None
        assert row["status"] == "failed"
        assert "interrupted" in (row["error_message"] or "")
        assert row["finished_at"] is not None
        assert elapsed < 1.0  # la cancelación es inmediata, no espera el sleep(30)

    def test_stop_event_skips_new_jobs(self, tmp_path):
        path = str(tmp_path / "skip.db")
        init_db(path)
        ensure_source(path, "https://skip.test/")
        source, scraper_config = get_active_jobs(path)[0]
        stop_event = threading.Event()
        stop_event.set()

        result = asyncio.run(run_job(source, scraper_config, path, stop_event))
        assert result is None
        assert count_table(path, "SCRAPE_RUN") == 0  # ni siquiera crea el run
