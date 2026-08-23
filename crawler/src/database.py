"""SQLite persistence layer.

All database access is isolated here. Functions are synchronous; the crawler
invokes them through ``asyncio.to_thread`` so the event loop is never blocked.
"""

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing

import config as cfg
from models import DataSource, ScraperConfig, ScrapeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS DATA_SOURCE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS SCRAPER_CONFIG (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_source_id INTEGER NOT NULL,
    scraper_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    schedule TEXT,
    rate_limit TEXT,
    parser_config TEXT, -- JSON string containing CSS/XPath selectors or options
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (data_source_id) REFERENCES DATA_SOURCE(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS SCRAPE_RUN (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper_config_id INTEGER NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    records_found INTEGER DEFAULT 0,
    records_processed INTEGER DEFAULT 0,
    records_failed INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT,
    FOREIGN KEY (scraper_config_id) REFERENCES SCRAPER_CONFIG(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS SCRAPED_DATA (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_run_id INTEGER NOT NULL,
    external_id TEXT,
    external_url TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    raw_data TEXT,
    extracted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scrape_run_id) REFERENCES SCRAPE_RUN(id) ON DELETE CASCADE
);
"""


def connect(db_path: str = cfg.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = cfg.DB_PATH) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.executescript(SCHEMA)


def _insert_source_with_config(conn: sqlite3.Connection, name: str,
                               base_url: str, source_type: str) -> int:
    cursor = conn.execute(
        "INSERT INTO DATA_SOURCE (name, base_url, source_type) VALUES (?, ?, ?)",
        (name, base_url, source_type),
    )
    source_id = cursor.lastrowid
    conn.execute(
        """
        INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type,
                                    rate_limit, parser_config)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            source_id,
            "http",
            "webpage",
            cfg.DEFAULT_RATE_LIMIT,
            json.dumps(cfg.DEFAULT_PARSER_CONFIG),
        ),
    )
    return source_id


def ensure_source(
    db_path: str,
    base_url: str,
    name: str | None = None,
    source_type: str = "web",
) -> tuple[int, bool]:
    """Idempotently register a seed URL as DATA_SOURCE + default SCRAPER_CONFIG.

    Returns ``(source_id, created)``. When a row with the same ``base_url``
    already exists it is left untouched.
    """
    with closing(connect(db_path)) as conn, conn:
        row = conn.execute("SELECT id FROM DATA_SOURCE WHERE base_url = ?",
                           (base_url, )).fetchone()
        if row:
            return int(row["id"]), False
        source_id = _insert_source_with_config(conn, name
                                               or f"Source {base_url}",
                                               base_url, source_type)
        return source_id, True


def get_active_jobs(
        db_path: str = cfg.DB_PATH) -> list[tuple[DataSource, ScraperConfig]]:
    """Return every active (DATA_SOURCE, SCRAPER_CONFIG) pair."""
    query = """
        SELECT s.id AS sid, s.name AS s_name, s.base_url, s.source_type,
               s.active AS source_active,
               c.id AS cid, c.data_source_id, c.scraper_type, c.target_type,
               c.schedule, c.rate_limit, c.parser_config, c.active AS config_active
        FROM DATA_SOURCE s
        JOIN SCRAPER_CONFIG c ON c.data_source_id = s.id
        WHERE s.active = 1 AND c.active = 1
    """
    jobs: list[tuple[DataSource, ScraperConfig]] = []
    with closing(connect(db_path)) as conn, conn:
        for row in conn.execute(query):
            source = DataSource(
                id=row["sid"],
                name=row["s_name"],
                base_url=row["base_url"],
                source_type=row["source_type"],
                active=bool(row["source_active"]),
            )
            config = ScraperConfig(
                id=row["cid"],
                data_source_id=row["data_source_id"],
                scraper_type=row["scraper_type"],
                target_type=row["target_type"],
                schedule=row["schedule"],
                rate_limit=row["rate_limit"],
                parser_config=_decode_parser_config(row["parser_config"]),
                active=bool(row["config_active"]),
            )
            jobs.append((source, config))
    return jobs


def _decode_parser_config(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def create_scrape_run(db_path: str, scraper_config_id: int) -> int:
    with closing(connect(db_path)) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO SCRAPE_RUN (scraper_config_id, started_at, status)"
            " VALUES (?, CURRENT_TIMESTAMP, 'running')",
            (scraper_config_id, ),
        )
        return int(cursor.lastrowid)


def update_run_stats(
    db_path: str,
    run_id: int,
    records_found: int | None = None,
    records_processed: int | None = None,
    records_failed: int | None = None,
) -> None:
    fields: list[str] = []
    values: list[int] = []
    if records_found is not None:
        fields.append("records_found = ?")
        values.append(records_found)
    if records_processed is not None:
        fields.append("records_processed = ?")
        values.append(records_processed)
    if records_failed is not None:
        fields.append("records_failed = ?")
        values.append(records_failed)
    if not fields:
        return
    values.append(run_id)
    with closing(connect(db_path)) as conn, conn:
        conn.execute(f"UPDATE SCRAPE_RUN SET {', '.join(fields)} WHERE id = ?",
                     values)


def finish_scrape_run(
    db_path: str,
    run_id: int,
    status: str,
    error_message: str | None = None,
    records_found: int | None = None,
    records_processed: int | None = None,
    records_failed: int | None = None,
) -> None:
    stats_sql = ""
    values: list = [status]
    counters = {
        "records_found": records_found,
        "records_processed": records_processed,
        "records_failed": records_failed,
    }
    for column, value in counters.items():
        if value is not None:
            stats_sql += f", {column} = ?"
            values.append(value)

    error_sql = ", error_message = ?" if error_message is not None else ""
    if error_message is not None:
        values.append(error_message[:2000])

    values.append(run_id)
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE SCRAPE_RUN SET status = ?, finished_at = CURRENT_TIMESTAMP"
            f"{stats_sql}{error_sql} WHERE id = ?",
            values,
        )


def save_scraped_data(
        db_path: str,
        scrape_run_id: int,
        result: ScrapeResult,
        extra_rows: Iterable[ScrapeResult] = (),
) -> int:
    rows = [result, *extra_rows]
    with closing(connect(db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO SCRAPED_DATA (scrape_run_id, external_id, external_url,"
            " entity_type, raw_data) VALUES (?, ?, ?, ?, ?)",
            [(scrape_run_id, r.external_id, r.external_url, r.entity_type,
              r.raw_data) for r in rows],
        )
    return len(rows)


def get_run(db_path: str, run_id: int) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn, conn:
        return conn.execute("SELECT * FROM SCRAPE_RUN WHERE id = ?",
                            (run_id, )).fetchone()


def count_table(db_path: str, table: str) -> int:
    assert table in {
        "DATA_SOURCE", "SCRAPER_CONFIG", "SCRAPE_RUN", "SCRAPED_DATA"
    }
    with closing(connect(db_path)) as conn, conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])
