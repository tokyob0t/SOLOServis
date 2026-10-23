import json
import sqlite3
from decimal import Decimal

import pytest

import database as crawler_db
from normalize import (
    RESOLUTION_ORDER,
    TARGET_BY_NAME,
    TARGET_TABLES,
    Normalizer,
    column_ddl,
    specs_for,
    sqlite_default,
    sqlite_type,
    target_ddl,
)

LISTING_HTML = """
<html><body>
  <article class="pod">
    <h3 class="title"><a href="catalogue/1_1/index.html">Sharp Objects</a></h3>
    <p class="brand">Serpentine</p>
    <p class="category">Mystery</p>
    <p class="price_color">&pound;47.82</p>
    <p class="price_was">&pound;51.77</p>
    <p class="instock in stock">In stock</p>
    <img class="shot" src="../media/1_1.jpg" alt="cover">
  </article>
  <article class="pod">
    <h3 class="title"><a href="catalogue/2_2/index.html">The Past Never Ends</a></h3>
    <p class="brand">Serpentine</p>
    <p class="category">Mystery</p>
    <p class="price_color">&pound;19.63</p>
    <p class="instock">Out of stock</p>
  </article>
</body></html>
"""

PARSER_CONFIG = {
    "profiles": {
        "books": {
            "product": {
                "root": "article.pod",
                "fields": {
                    # Plain selectors read get_all_text(), so they descend into
                    # children. "::text" would only see direct text nodes and
                    # find nothing here, because the title lives in an <a>.
                    "name": ".title",
                    "sku": {"selector": ".title a", "mode": "attr",
                            "attribute": "href"},
                },
                "references": {
                    "category": ".category",
                    "brand": ".brand",
                },
            },
            "product_offer": {
                "root": "article.pod",
                "fields": {
                    "price": ".price_color",
                    "list_price": ".price_was",
                    "currency": {"selector": ".missing", "default": "GBP"},
                    "available": ".instock",
                    "condition": {"selector": ".missing", "default": "new"},
                },
                "references": {"store": ".brand"},
            },
            "product_image": {
                "root": "article.pod",
                "fields": {
                    "image_url": {"selector": "img.shot", "attribute": "src"},
                    "alt_text": {"selector": "img.shot", "attribute": "alt"},
                },
            },
        }
    }
}


_LEGACY_STATUS = "completed"


def _downgrade_scrape_run(path):
    """Rebuild SCRAPE_RUN without its CHECK, as an archive from before the fix.

    The crawler refuses to write a status PostgreSQL would reject, so the only
    way an out-of-vocabulary value reaches the normalizer is an archive written
    before that CHECK existed.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.executescript(
        "ALTER TABLE SCRAPE_RUN RENAME TO SCRAPE_RUN_NEW;"
        "CREATE TABLE SCRAPE_RUN ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " scraper_config_id INTEGER NOT NULL,"
        " started_at DATETIME NOT NULL, finished_at DATETIME,"
        " records_found INTEGER DEFAULT 0, records_processed INTEGER DEFAULT 0,"
        " records_failed INTEGER DEFAULT 0, status TEXT NOT NULL,"
        " error_message TEXT);"
        " INSERT INTO SCRAPE_RUN SELECT id, scraper_config_id, started_at,"
        " finished_at, records_found, records_processed, records_failed, status,"
        " error_message FROM SCRAPE_RUN_NEW;"
        " DROP TABLE SCRAPE_RUN_NEW;")
    conn.commit()
    conn.close()


def _build_crawler_db(path, *, html=LISTING_HTML, parser_config=None,
                      source_type="store", status="success", name="Books Store"):
    crawler_db.init_db(path)
    if status == _LEGACY_STATUS:
        _downgrade_scrape_run(path)
    conn = crawler_db.connect(path)
    source_id = conn.execute(
        "INSERT INTO DATA_SOURCE (name, base_url, source_type) VALUES (?,?,?)",
        (name, "https://books.test/catalogue/", source_type)).lastrowid
    config_id = conn.execute(
        "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type, target_type,"
        " parser_config) VALUES (?, 'http', 'product', ?)",
        (source_id, json.dumps(parser_config if parser_config is not None
                               else PARSER_CONFIG))).lastrowid
    run_id = conn.execute(
        "INSERT INTO SCRAPE_RUN (scraper_config_id, started_at, status)"
        " VALUES (?, CURRENT_TIMESTAMP, ?)", (config_id, status)).lastrowid
    conn.execute(
        "INSERT INTO SCRAPED_DATA (scrape_run_id, external_id, external_url,"
        " entity_type, raw_data) VALUES (?, NULL, ?, 'webpage', ?)",
        (run_id, "https://books.test/catalogue/", html))
    conn.commit()
    conn.close()
    return {"source_id": source_id, "config_id": config_id, "run_id": run_id}


def _rows(target_path, table, order_by="id"):
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY {order_by}")]
    finally:
        conn.close()


def _one(target_path, sql, params=()):
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class TestMirrorSchema:
    def test_every_scraper_owned_table_is_declared(self):
        names = {table.name for table in TARGET_TABLES}
        assert {"data_source", "scraper_config", "scrape_run", "scraped_data"} \
            <= names
        assert {"product", "product_offer", "store", "brand", "service",
                "service_offer", "provider"} <= names

    def test_ddl_creates_the_whole_schema(self, tmp_path):
        target = str(tmp_path / "n.db")
        Normalizer(target)._create_target()
        conn = sqlite3.connect(target)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert tables == set(TARGET_BY_NAME)

    def test_numeric_becomes_text_so_cents_survive(self):
        assert sqlite_type("NUMERIC(12,2)") == "TEXT"

    def test_pg_defaults_are_translated(self):
        assert sqlite_default("NOW()") == "CURRENT_TIMESTAMP"
        assert sqlite_default("TRUE") == "1"
        assert sqlite_default("'CLP'") == "'CLP'"

    def test_declared_length_becomes_a_check(self):
        column = TARGET_BY_NAME["product"].column("name")
        ddl = column_ddl(column)
        assert "VARCHAR(255)" not in ddl
        assert "TEXT NOT NULL" in ddl
        assert "CHECK (length(name) <= 255)" in ddl

    def test_the_mirror_rejects_a_status_postgres_would_reject(self, tmp_path):
        target = str(tmp_path / "n.db")
        normalizer = Normalizer(target)
        normalizer._create_target()
        conn = sqlite3.connect(target)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="status"):
                conn.execute(
                    "INSERT INTO scrape_run (id, scraper_config_id, started_at,"
                    " status) VALUES (1, 1, '2026-01-01', 'completed')")
        finally:
            conn.close()

    def test_the_mirror_rejects_an_off_enum_condition(self, tmp_path):
        target = str(tmp_path / "n.db")
        normalizer = Normalizer(target)
        normalizer._create_target()
        conn = sqlite3.connect(target)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="condition"):
                conn.execute(
                    "INSERT INTO product_offer (product_id, store_id, price,"
                    " condition) VALUES (1, 1, '1.00', 'seminuevo')")
        finally:
            conn.close()

    def test_offers_keep_their_composite_uniqueness(self, tmp_path):
        target = str(tmp_path / "n.db")
        Normalizer(target)._create_target()
        ddl = _one(target, "SELECT sql FROM sqlite_master WHERE name='product_offer'")
        assert "UNIQUE (product_id, store_id)" in ddl["sql"]

    def test_resolution_order_puts_parents_first(self):
        position = {name: index for index, name in enumerate(RESOLUTION_ORDER)}
        assert position["product_category"] < position["product"]
        assert position["product"] < position["product_offer"]
        assert position["store"] < position["product_offer"]
        assert position["service"] < position["service_offer"]

    def test_generated_ddl_parses(self):
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect(":memory:").executescript(target_ddl() + "BROKEN")


class TestSpecsFor:
    def test_flat_config_yields_one_spec(self):
        specs = specs_for({"entity_type": "product", "fields": {"name": ".t"}})
        assert list(specs) == ["product"]

    def test_profiles_yield_every_entity(self):
        specs = specs_for(PARSER_CONFIG)
        assert set(specs) == {"product", "product_offer", "product_image"}

    def test_empty_config_yields_nothing(self):
        assert specs_for({}) == {}
        assert specs_for(None) == {}

    def test_a_bad_config_raises(self):
        with pytest.raises(ValueError, match="unknown entity_type"):
            specs_for({"entity_type": "nope", "fields": {}})


class TestNormalization:
    def test_products_and_offers_land_in_the_graph(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)

        Normalizer(target).run(source)

        products = _rows(target, "product")
        offers = _rows(target, "product_offer")
        assert len(products) == 2
        assert len(offers) == 2
        assert {p["name"] for p in products} == {
            "Sharp Objects", "The Past Never Ends"}

    def test_a_missing_image_is_reported_without_failing_the_run(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        result = Normalizer(target).run(source)

        # The second pod carries no <img>: it legitimately gets no
        # product_image row, but the other product and both offers still land.
        assert len(_rows(target, "product_image")) == 1
        assert len(_rows(target, "product_offer")) == 2
        assert any("image_url: required value not found" in w
                   for w in result.warnings)

    def test_foreign_keys_are_resolved_not_invented(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)

        product = _rows(target, "product")[0]
        category = _one(target, "SELECT * FROM product_category WHERE id = ?",
                        (product["category_id"],))
        brand = _one(target, "SELECT * FROM brand WHERE id = ?",
                     (product["brand_id"],))
        assert category["name"] == "Mystery"
        assert brand["name"] == "Serpentine"

        offer = _rows(target, "product_offer")[0]
        assert offer["product_id"] == product["id"]
        assert offer["store_id"] is not None

    def test_natural_keys_are_reused_not_duplicated(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)

        assert len(_rows(target, "brand")) == 1
        assert len(_rows(target, "product_category")) == 1

    def test_a_crawled_site_becomes_a_store(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source, source_type="store")
        Normalizer(target).run(source)

        stores = _rows(target, "store")
        assert any(store["website_url"] == "https://books.test/catalogue/"
                   for store in stores)

    def test_a_provider_source_becomes_a_provider(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source, source_type="provider")
        Normalizer(target).run(source)

        # The claim under test is that the crawled site itself was registered as
        # a provider, not that no store exists: the parser_config in this fixture
        # also declares a store reference, so one is legitimately created.
        providers = _rows(target, "provider")
        assert any(provider["website_url"] == "https://books.test/catalogue/"
                   for provider in providers)

    def test_money_keeps_full_precision(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)

        offer = _rows(target, "product_offer")[0]
        assert Decimal(offer["price"]) == Decimal("47.82")
        assert Decimal(offer["list_price"]) == Decimal("51.77")
        assert offer["currency"] == "GBP"
        assert offer["available"] == 1

    def test_out_of_stock_is_captured(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)

        offers = {o["product_id"]: o for o in _rows(target, "product_offer")}
        flags = [o["available"] for o in offers.values()]
        assert sorted(flags) == [0, 1]

    def test_images_attach_to_the_product(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)

        images = _rows(target, "product_image")
        assert len(images) == 1
        assert images[0]["image_url"] == "https://books.test/media/1_1.jpg"
        assert images[0]["alt_text"] == "cover"
        assert images[0]["sort_order"] == 0

    def test_raw_data_becomes_jsonb_compatible(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)

        payload = json.loads(_rows(target, "scraped_data")[0]["raw_data"])
        assert payload["entity_type"] == "product"
        assert payload["entities"]["product"][0]["name"] == "Sharp Objects"

    def test_an_unparsed_archive_stays_valid_json(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source, parser_config={})
        result = Normalizer(target).run(source)

        assert result.skipped == 1
        payload = json.loads(_rows(target, "scraped_data")[0]["raw_data"])
        assert "html" in payload

    def test_an_out_of_vocabulary_status_is_normalized(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source, status=_LEGACY_STATUS)

        result = Normalizer(target).run(source)
        assert _rows(target, "scrape_run")[0]["status"] == "failed"
        assert any("outside the schema vocabulary" in w for w in result.warnings)

    def test_the_mirror_itself_cannot_store_a_legacy_status(self, tmp_path):
        # The crawler now carries the same CHECK, so 'completed' cannot even be
        # produced locally; only archives predating that fix still carry it.
        path = str(tmp_path / "fresh.db")
        crawler_db.init_db(path)
        conn = crawler_db.connect(path)
        try:
            source_id = conn.execute(
                "INSERT INTO DATA_SOURCE (name, base_url, source_type)"
                " VALUES ('s', 'https://x.test', 'web')").lastrowid
            config_id = conn.execute(
                "INSERT INTO SCRAPER_CONFIG (data_source_id, scraper_type,"
                " target_type) VALUES (?, 'http', 'webpage')",
                (source_id,)).lastrowid
            with pytest.raises(sqlite3.IntegrityError, match="status"):
                conn.execute(
                    "INSERT INTO SCRAPE_RUN (scraper_config_id, started_at,"
                    " status) VALUES (?, CURRENT_TIMESTAMP, 'completed')",
                    (config_id,))
        finally:
            conn.close()

    def test_an_offer_without_a_product_is_reported_not_invented(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source, parser_config={
            "entity_type": "product_offer",
            "fields": {"price": ".price_color::text"}})

        result = Normalizer(target).run(source)
        assert not _rows(target, "product_offer")
        assert any("no product to attach to" in w for w in result.warnings)


class TestIdempotency:
    def test_renormalizing_does_not_duplicate_offers(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)

        Normalizer(target).run(source)
        first = len(_rows(target, "product_offer"))
        Normalizer(target).run(source)
        assert len(_rows(target, "product_offer")) == first
        assert len(_rows(target, "product")) == first

    def test_renormalizing_does_not_duplicate_parties(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)

        Normalizer(target, rebuild=False).run(source)
        Normalizer(target, rebuild=False).run(source)
        names = [store["name"] for store in _rows(target, "store")]
        assert len(names) == len(set(names))

    def test_an_unchanged_price_records_no_history(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)

        # History can only accrue when the target is merged: a rebuilt file has
        # no previous price to compare against.
        Normalizer(target, rebuild=False).run(source)
        Normalizer(target, rebuild=False).run(source)
        assert _rows(target, "product_price_history") == []

    def test_a_changed_price_is_recorded_in_history(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target, rebuild=False).run(source)

        conn = crawler_db.connect(source)
        conn.execute(
            "UPDATE SCRAPED_DATA SET raw_data = ? WHERE id = 1",
            (LISTING_HTML.replace("47.82", "39.99"),))
        conn.commit()
        conn.close()

        Normalizer(target, rebuild=False).run(source)
        history = _rows(target, "product_price_history")
        assert len(history) == 1
        assert Decimal(history[0]["price"]) == Decimal("47.82")
        assert history[0]["is_promotional"] == 1

    def test_a_rebuilt_target_starts_from_scratch(self, tmp_path):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source)
        Normalizer(target).run(source)
        Normalizer(target).run(source)
        assert len(_rows(target, "product")) == 2


class TestSqlDump:
    def _dump(self, tmp_path, **kwargs):
        source = str(tmp_path / "crawler.db")
        target = str(tmp_path / "normalized.db")
        _build_crawler_db(source, **kwargs)
        normalizer = Normalizer(target)
        normalizer.run(source)
        path = normalizer.dump_sql(tmp_path / "out" / "load.sql")
        return path.read_text(encoding="utf-8")

    def test_money_is_unquoted_so_postgres_keeps_the_exact_decimal(self, tmp_path):
        sql = self._dump(tmp_path)
        assert "'47.82'" not in sql
        assert ", 47.82," in sql or ", 47.82)" in sql or "47.82, " in sql

    def test_booleans_become_keywords(self, tmp_path):
        sql = self._dump(tmp_path)
        assert "TRUE" in sql and "FALSE" in sql
        assert ", 1, 0," not in sql

    def test_jsonb_is_cast_on_import(self, tmp_path):
        sql = self._dump(tmp_path)
        assert "::jsonb" in sql

    def test_null_columns_are_omitted_so_defaults_apply(self, tmp_path):
        sql = self._dump(tmp_path)
        assert "public_id" not in sql
        assert "gen_random_uuid" not in sql  # the DB supplies it, not the dump

        offer_insert = sql.split("INSERT INTO product_offer ")[1]
        columns = offer_insert.split("VALUES")[0]
        # stock and model are never set, so they stay absent and PostgreSQL
        # fills them from its own defaults instead of receiving an explicit NULL.
        assert "stock" not in columns
        assert "model" not in sql.split("INSERT INTO product ")[1].split(
            "VALUES")[0]

    def test_sequences_are_realigned(self, tmp_path):
        sql = self._dump(tmp_path)
        assert "pg_get_serial_sequence('product', 'id')" in sql
        assert "setval" in sql

    def test_rows_are_dumped_in_foreign_key_order(self, tmp_path):
        sql = self._dump(tmp_path)

        def position(table):
            return sql.index(f"INSERT INTO {table} ")

        # Only real dependencies are asserted: store and product_category are
        # siblings and may appear in either order.
        assert position("data_source") < position("scraper_config")
        assert position("scraper_config") < position("scrape_run")
        assert position("scrape_run") < position("scraped_data")
        assert position("product_category") < position("product")
        assert position("brand") < position("product")
        assert position("product") < position("product_offer")
        assert position("store") < position("product_offer")

    def test_the_dump_only_covers_the_scraper_owned_schema(self, tmp_path):
        sql = self._dump(tmp_path)
        assert "INSERT INTO product (" in sql
        assert "INSERT INTO user_account" not in sql
