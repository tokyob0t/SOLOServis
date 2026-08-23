import asyncio

import pytest

import config as cfg
from crawler import Crawler
from models import DataSource, FetchOutcome, ScraperConfig, ScrapeResult


class FakeScraper:
    """Scraper de mentira: sin red. Devuelve resultados predefinidos."""

    def __init__(self, pages, fail_urls=frozenset()):
        self.pages = pages
        self.fail_urls = set(fail_urls)

    async def fetch(self, url):
        if url in self.fail_urls:
            return FetchOutcome(status=None, error="connection refused")
        if url in self.pages:
            return FetchOutcome(status=200, final_url=url)
        return FetchOutcome(status=404, error="HTTP 404")

    def parse(self, outcome):
        return self.pages.get(outcome.final_url)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(cfg, "MAX_RETRIES", 1)
    monkeypatch.setattr(cfg, "BACKOFF_BASE_SECONDS", 0.01)


@pytest.fixture()
def env(tmp_path):
    from src import database as db

    path = str(tmp_path / "crawler.db")
    db.init_db(path)
    source_id = db.ensure_source(path, "https://site.test/")[0]

    jobs = db.get_active_jobs(path)
    config = jobs[0][1]
    run_id = db.create_scrape_run(path, config.id)
    return {"db_path": path, "source_id": source_id, "config": config, "run_id": run_id}


def _source(url="https://site.test/"):
    return DataSource(id=1, name="test", base_url=url, source_type="web")


def _config(rate_limit="1000/s"):
    return ScraperConfig(
        id=1,
        data_source_id=1,
        scraper_type="http",
        target_type="webpage",
        rate_limit=rate_limit,
        parser_config={},
    )


def _crawler(env, scraper, run_id=None, **kwargs):
    defaults = dict(max_depth=1, max_pages=10, concurrency=4)
    defaults.update(kwargs)
    return Crawler(
        _source(),
        _config(),
        run_id if run_id is not None else env["run_id"],
        db_path=env["db_path"],
        scraper=scraper,
        **defaults,
    )


def _run(crawler):
    return asyncio.run(crawler.run())


def _result(url, links=()):
    return ScrapeResult(None, url, "webpage", "{}", links=list(links))


class TestCrawlerRun:
    def test_crawls_within_depth_and_dedupes(self, env):
        pages = {
            "https://site.test/": _result("https://site.test/", ["/a", "/b", "/"]),
            "https://site.test/a": _result("https://site.test/a", ["/deep"]),  # depth 2
            "https://site.test/b": _result("https://site.test/b"),
        }
        summary = _run(_crawler(env, FakeScraper(pages)))

        assert summary.pages_found == 3  # seed + a + b (sin duplicados ni /deep)
        assert summary.pages_processed == 3
        assert summary.pages_failed == 0
        assert summary.status == "completed"
        assert summary.visited_urls == sorted(
            ["https://site.test/", "https://site.test/a", "https://site.test/b"]
        )

    def test_filters_other_domains(self, env):
        pages = {
            "https://site.test/": _result(
                "https://site.test/", ["https://other.org/x"]
            ),
        }
        summary = _run(_crawler(env, FakeScraper(pages), max_depth=2))
        assert summary.pages_found == 1
        assert all(u.startswith("https://site.test") for u in summary.visited_urls)

    def test_counts_failures_without_crashing(self, env):
        pages = {
            "https://site.test/": _result("https://site.test/", ["/dead"]),
        }
        scraper = FakeScraper(pages, fail_urls={"https://site.test/dead"})
        summary = _run(_crawler(env, scraper))

        assert summary.pages_processed == 1
        assert summary.pages_failed == 1
        assert "dead" in (summary.error_message or "")

    def test_respects_max_pages_cap(self, env):
        pages = {
            "https://site.test/": _result(
                "https://site.test/", [f"/p{i}" for i in range(20)]
            ),
            **{
                f"https://site.test/p{i}": _result(f"https://site.test/p{i}")
                for i in range(20)
            },
        }
        summary = _run(_crawler(env, FakeScraper(pages), max_pages=5))
        assert summary.pages_found <= 5

    def test_persists_run_and_scraped_rows(self, env):
        from src import database as db

        pages = {
            "https://site.test/": ScrapeResult(
                None, "https://site.test/", "webpage", '{"title":"home"}', links=["/x"]
            ),
            "https://site.test/x": ScrapeResult(
                None, "https://site.test/x", "webpage", '{"title":"x"}', links=[]
            ),
        }
        summary = _run(_crawler(env, FakeScraper(pages)))

        row = db.get_run(env["db_path"], summary.run_id)
        assert row["status"] == "completed"
        assert row["records_found"] == 2
        assert row["records_processed"] == 2
        assert row["finished_at"] is not None
        assert db.count_table(env["db_path"], "SCRAPED_DATA") == 2

    def test_all_failed_marks_run_failed(self, env):
        scraper = FakeScraper({}, fail_urls={"https://site.test/"})
        summary = _run(_crawler(env, scraper))
        assert summary.status == "failed"
        assert summary.pages_failed == 1


class TestAclose:
    def test_aclose_cancels_workers_and_is_idempotent(self, env):
        from src import database as db

        class SlowScraper:
            async def fetch(self, url):
                await asyncio.sleep(30)

            def parse(self, outcome):
                return None

        crawler = _crawler(env, SlowScraper())

        async def scenario():
            task = asyncio.create_task(crawler.run())
            await asyncio.sleep(0.15)  # workers bloqueados en fetch lento
            await crawler.aclose()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await crawler.aclose()  # segunda llamada: no-op seguro

        asyncio.run(scenario())
        assert crawler._workers == []
        row = db.get_run(env["db_path"], env["run_id"])
        assert row["status"] == "running"  # run() cancelado no llegó a finalizar
