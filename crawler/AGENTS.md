# AGENTS.md

## Project Overview

This project is an asynchronous web crawling and scraping system written in Python.
The system must be designed around two clearly separated responsibilities:

* **Crawler** — discovers, schedules, deduplicates, and manages URLs to visit.
* **Scraper** — fetches pages and extracts structured information from them.

The initial goal is to build a small, reliable foundation that can later evolve into a larger crawler without requiring a major architectural rewrite.

---

## Objectives

### Primary Objectives

* Crawl a configurable list of seed URLs / Data Sources.
* Fetch pages asynchronously.
* Extract basic metadata and custom raw payloads from each webpage.
* Store crawl results and operational tracking data in SQLite using a structured relational model.
* Deduplicate URLs.
* Support configurable concurrency.
* Handle HTTP errors and timeouts gracefully.
* Respect per-domain rate limits and configurations.
* Keep crawler logic separate from scraping/parsing logic.
* Use browser-like HTTP headers where appropriate.
* Normalize URLs before processing them.
* Make the system easy to extend with additional extracted fields and entity types.

---

## Technology Stack

### Language

**Python**

* Use modern Python (Target: **Python 3.12+**).
* Prefer:
* type hints
* dataclasses / Pydantic models where appropriate
* `async` / `await`
* clear module boundaries
* small, testable functions


* Avoid unnecessary abstractions.

### HTTP / Fetching

**Scrapling**

* Scrapling is the primary scraping/fetching library.
* Use it for:
* HTTP fetching
* asynchronous fetching
* HTML parsing
* CSS/XPath selection
* browser-like request headers
* optional advanced fetching when required


* Prefer Scrapling's asynchronous fetcher for normal HTTP crawling.
* Do not introduce another HTTP library unless there is a concrete technical reason.
* The system should initially use normal HTTP fetching rather than browser automation.
* Browser-based fetching should only be introduced for websites that genuinely require JavaScript execution.

### Async Runtime

**asyncio**

* `asyncio` is the concurrency foundation of the project.
* Use it for:
* URL scheduling
* concurrent fetching
* worker management
* queues
* rate limiting
* retry scheduling


* Prefer `asyncio.Queue` for communication between crawler workers.
* Do not create an unbounded number of concurrent requests.
* Concurrency must always be configurable (e.g., `MAX_CONCURRENCY = 10`).

### Database

**SQLite**

* SQLite is the initial local persistence layer.
* Must implement the specific relational schema defined below (`DATA_SOURCE`, `SCRAPER_CONFIG`, `SCRAPE_RUN`, `SCRAPED_DATA`).
* The database should be treated as a persistence layer, not as part of the crawler's business logic.
* Database access must be isolated in a dedicated module (`database.py`).

### HTML Parsing

* Scrapling should be preferred for parsing fetched documents.
* BeautifulSoup should not be introduced unless Scrapling cannot reasonably provide the required functionality.

---

## Database Schema Design

The local database must strictly implement the following relational model to separate data sources, scraper configurations, crawl execution runs, and scraped payloads:

```sql
-- 1. Source definitions
CREATE TABLE DATA_SOURCE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. Scraper configurations per data source
CREATE TABLE SCRAPER_CONFIG (
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

-- 3. Execution logs for each scrape run
CREATE TABLE SCRAPE_RUN (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper_config_id INTEGER NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    records_found INTEGER DEFAULT 0,
    records_processed INTEGER DEFAULT 0,
    records_failed INTEGER DEFAULT 0,
    status TEXT NOT NULL, -- e.g., 'pending', 'running', 'completed', 'failed'
    error_message TEXT,
    FOREIGN KEY (scraper_config_id) REFERENCES SCRAPER_CONFIG(id) ON DELETE CASCADE
);

-- 4. Extracted data records
CREATE TABLE SCRAPED_DATA (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_run_id INTEGER NOT NULL,
    external_id TEXT,
    external_url TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    raw_data TEXT, -- JSON string or HTML snippet containing extracted payload
    extracted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scrape_run_id) REFERENCES SCRAPE_RUN(id) ON DELETE CASCADE
);

```

---

## Architecture

The project should follow this conceptual architecture:

```text
                  Data Sources & Configs (SQLite)
                                |
                                v
                        +---------------+
                        |    Crawler    |
                        |---------------|
                        | URL queue     |
                        | deduplication |
                        | depth control |
                        | rate limiting |
                        +-------+-------+
                                |
                                v
                        +---------------+
                        |   Scrapling   |
                        |---------------|
                        | Async fetch   |
                        | HTTP handling |
                        | HTML parsing  |
                        +-------+-------+
                                |
                                v
                        +---------------+
                        |    Scraper    |
                        |---------------|
                        | Parse payload |
                        | Extract data  |
                        | Extract links |
                        +-------+-------+
                                |
                                v
                        +---------------+
                        |   Processor   |
                        |---------------|
                        | URL normalize |
                        | Data cleaning |
                        +-------+-------+
                                |
                                v
             +-------------------------------------+
             |     SQLite Persistence Layer        |
             |-------------------------------------|
             | SCRAPE_RUN  |  SCRAPED_DATA         |
             +-------------------------------------+

```

---

## Recommended Project Structure

```text
project/
│
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .gitignore
│
├── src/
│   └── crawler/
│       ├── __init__.py
│       ├── main.py
│       ├── crawler.py
│       ├── scraper.py
│       ├── processor.py
│       ├── database.py
│       ├── models.py
│       ├── config.py
│       └── utils.py
│
└── tests/
    ├── test_crawler.py
    ├── test_scraper.py
    ├── test_processor.py
    └── test_database.py

```

---

## Responsibilities

### Crawler Responsibilities

* Receiving seed URLs and configuration from `DATA_SOURCE` and `SCRAPER_CONFIG`.
* Creating a `SCRAPE_RUN` execution entry in SQLite.
* Maintaining the URL queue.
* Tracking visited URLs and preventing duplicate crawling.
* Controlling crawl depth and domain restrictions.
* Applying concurrency and rate limits (using `SCRAPER_CONFIG.rate_limit`).
* Invoking the scraper/fetcher.
* Updating `SCRAPE_RUN` stats (`records_found`, `records_processed`, `records_failed`, `status`, `finished_at`).

### Scraper Responsibilities

* Extracting information from a fetched document using Scrapling based on `parser_config`.
* Returning structured dictionary payload objects containing:
* `external_id` (if available in page)
* `external_url` (canonical/target URL)
* `entity_type` (e.g., page title, article, product)
* `raw_data` (JSON string or extracted content string)
* Discovered links for the crawler queue


* The scraper does not manage queues, retries, database connections, or global state.

### URL Handling & Processor Responsibilities

* Normalizing URLs before processing (`processor.py` / `utils.py`).
* Resolving relative URLs to absolute URLs.
* Stripping fragments (`#section`).
* Validating schemes (`http`, `https`).

---

## Concurrency & Error Handling

* Use `asyncio.Queue` and configurable workers (`MAX_CONCURRENCY`).
* Support exponential backoff retries for transient errors (`408`, `429`, `500`, `502`, `503`, `504`).
* Record failures in the `SCRAPE_RUN.error_message` column without crashing the process.

---

## Definition of Done — Initial Version

The initial version is considered complete when it can:

1. Automatically create and initialize SQLite database tables (`DATA_SOURCE`, `SCRAPER_CONFIG`, `SCRAPE_RUN`, `SCRAPED_DATA`).
2. Read an active `DATA_SOURCE` and `SCRAPER_CONFIG` to start a crawl job.
3. Log the start and finish of a job in `SCRAPE_RUN`.
4. Fetch pages asynchronously using Scrapling under configurable concurrency.
5. Extract page payload, status, and links using Scrapling.
6. Normalize discovered URLs and avoid duplicate crawls within the same run.
7. Save extracted payloads into `SCRAPED_DATA` mapped to the corresponding `scrape_run_id`.
8. Safely record progress (`records_found`, `records_processed`, `records_failed`) and errors.
9. Shut down cleanly without database corruption.
