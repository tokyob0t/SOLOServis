import sqlite3

import pytest

import config as cfg
import database as db
from models import RUN_STATUSES


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


def _insert_source(db_path, name="Src", url="https://example.com"):
    conn = db.connect(db_path)
    cursor = conn.execute(
        "INSERT INTO DATA_SOURCE (name, base_url, source_type) VALUES (?, ?, 'web')",
        (name, url),
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return source_id


class TestSchema:
    def test_all_tables_created(self, db_path):
        for table in ("DATA_SOURCE", "SCRAPER_CONFIG", "SCRAPE_RUN", "SCRAPED_DATA"):
            assert db.count_table(db_path, table) == 0

    def test_init_db_idempotent(self, db_path):
        db.init_db(db_path)
        db.init_db(db_path)


class TestEnsureSource:
    def test_creates_source_and_config(self, db_path):
        source_id, created = db.ensure_source(db_path, "https://x.com/")
        assert created is True
        jobs = db.get_active_jobs(db_path)
        assert len(jobs) == 1
        source, config = jobs[0]
        assert source.id == source_id
        assert config.rate_limit == cfg.DEFAULT_RATE_LIMIT
        assert config.parser_config == cfg.DEFAULT_PARSER_CONFIG

    def test_idempotent_by_base_url(self, db_path):
        first_id, first_created = db.ensure_source(db_path, "https://x.com/")
        second_id, second_created = db.ensure_source(
            db_path, "https://x.com/", name="Otro nombre"
        )
        assert first_created and not second_created
        assert first_id == second_id
        assert db.count_table(db_path, "DATA_SOURCE") == 1

    def test_different_urls_create_different_sources(self, db_path):
        id_a, _ = db.ensure_source(db_path, "https://a.com/")
        id_b, _ = db.ensure_source(db_path, "https://b.com/")
        assert id_a != id_b
        assert len(db.get_active_jobs(db_path)) == 2


class TestJobs:
    def test_returns_active_pairs_only(self, db_path):
        active_source = _insert_source(db_path, "active", "https://a.com")
        inactive_source = _insert_source(db_path, "inactive", "https://b.com")
        conn = db.connect(db_path)
        conn.execute(
            "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type,"
            " rate_limit, parser_config)"
            " VALUES (?, 'http', 'webpage', '30/min', '{\"entity_type\": \"webpage\"}')",
            (active_source,),
        )
        conn.execute(
            "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type, active)"
            " VALUES (?, 'http', 'webpage', 0)",
            (inactive_source,),
        )
        conn.execute("UPDATE DATA_SOURCE SET active = 0 WHERE id = ?", (inactive_source,))
        conn.commit()
        conn.close()

        jobs = db.get_active_jobs(db_path)
        assert len(jobs) == 1
        source, config = jobs[0]
        assert source.base_url == "https://a.com"
        assert config.rate_limit == "30/min"
        assert config.parser_config == {"entity_type": "webpage"}


class TestRunLifecycle:
    def test_full_run_flow(self, db_path):
        source_id = _insert_source(db_path)
        conn = db.connect(db_path)
        cursor = conn.execute(
            "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type)"
            " VALUES (?, 'http', 'webpage')",
            (source_id,),
        )
        config_id = cursor.lastrowid
        conn.commit()
        conn.close()

        run_id = db.create_scrape_run(db_path, config_id)
        row = db.get_run(db_path, run_id)
        assert row["status"] == "running"
        assert row["started_at"] is not None
        assert row["finished_at"] is None

        db.update_run_stats(
            db_path, run_id, records_found=3, records_processed=2, records_failed=1
        )
        db.finish_scrape_run(
            db_path,
            run_id,
            "success",
            error_message=None,
            records_found=3,
            records_processed=2,
            records_failed=1,
        )

        row = db.get_run(db_path, run_id)
        assert row["status"] == "success"
        assert row["finished_at"] is not None
        assert (row["records_found"], row["records_processed"], row["records_failed"]) == (3, 2, 1)

    def test_scraped_data_and_cascade(self, db_path):
        source_id = _insert_source(db_path)
        conn = db.connect(db_path)
        config_id = conn.execute(
            "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type)"
            " VALUES (?, 'http', 'webpage')",
            (source_id,),
        ).lastrowid
        run_id = conn.execute(
            "INSERT INTO SCRAPE_RUN (scraper_config_id, started_at, status)"
            " VALUES (?, CURRENT_TIMESTAMP, 'running')",
            (config_id,),
        ).lastrowid
        conn.commit()
        conn.close()

        from src.models import ScrapeResult

        result = ScrapeResult(
            external_id="42",
            external_url="https://example.com/p",
            entity_type="product",
            raw_data='{"title": "x"}',
        )
        saved = db.save_scraped_data(db_path, run_id, result)
        assert saved == 1
        assert db.count_table(db_path, "SCRAPED_DATA") == 1

        conn = db.connect(db_path)
        conn.execute("DELETE FROM SCRAPE_RUN WHERE id = ?", (run_id,))
        conn.commit()
        conn.close()

        assert db.count_table(db_path, "SCRAPED_DATA") == 0


class TestRunStatusVocabulary:
    """The status values must stay inside the production schema's CHECK.

    ``scrape_run.status`` is ``CHECK (status IN ('pending', 'running',
    'success', 'failed'))`` in the target PostgreSQL schema. A value outside
    that set is rejected at ingestion time, so it is enforced locally too.
    """

    def test_the_vocabulary_matches_the_schema(self):
        assert RUN_STATUSES == {"pending", "running", "success", "failed"}

    def test_the_old_completed_value_is_not_in_the_vocabulary(self):
        assert "completed" not in RUN_STATUSES

    def test_finish_rejects_a_foreign_status(self, db_path):
        source_id = _insert_source(db_path)
        conn = db.connect(db_path)
        config_id = conn.execute(
            "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type)"
            " VALUES (?, 'http', 'webpage')",
            (source_id,),
        ).lastrowid
        conn.commit()
        conn.close()
        run_id = db.create_scrape_run(db_path, config_id)

        with pytest.raises(ValueError, match="invalid scrape_run status"):
            db.finish_scrape_run(db_path, run_id, "completed")
        assert db.get_run(db_path, run_id)["status"] == "running"

    def test_the_sqlite_check_constraint_also_rejects_it(self, db_path):
        source_id = _insert_source(db_path)
        conn = db.connect(db_path)
        config_id = conn.execute(
            "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type)"
            " VALUES (?, 'http', 'webpage')",
            (source_id,),
        ).lastrowid
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO SCRAPE_RUN (scraper_config_id, started_at, status)"
                " VALUES (?, CURRENT_TIMESTAMP, 'completed')",
                (config_id,),
            )
        conn.close()
