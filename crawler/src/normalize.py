"""Normalizer: turns a crawler database into a database shaped like production.

The crawler and the product schema disagree, and they disagree in ways that
matter. The crawler stores whole pages as HTML in a ``TEXT`` column, keys its
operational tables its own way, and has no product, store or offer concept at
all. The target schema (``backend/database/migrations/001_schema.sql``) wants
``JSONB`` payloads, exact ``NUMERIC`` money, ``UUID`` public ids and a graph of
foreign keys.

This module bridges the two in one pass:

1. Mirrors the target schema into a new file, declaring the same ``CHECK``,
   ``NOT NULL`` and ``UNIQUE`` constraints so the normalized data self-validates
   here instead of failing at import time.
2. Copies the operational tables (``data_source``, ``scraper_config``,
   ``scrape_run``, ``scraped_data``) across, normalizing the status vocabulary.
3. Re-runs the extractor over the HTML already sitting in ``scraped_data``, so
   an old archive can be normalized months later without crawling again, then
   resolves every foreign key from the natural keys extraction emitted.
4. Optionally dumps ``INSERT`` statements ready for ``psql -f``.

Four fidelity rules drive the design:

* ``NUMERIC(p,s)`` is stored as ``TEXT`` in the SQLite mirror. SQLite has no
  exact decimal, so a ``NUMERIC(12,2)`` price routed through a float loses cents
  before it is ever written. The SQL dump re-emits it unquoted.
* ``BOOLEAN`` is stored as ``0``/``1`` because that is all SQLite has; the dump
  turns it back into ``TRUE``/``FALSE``.
* Columns that are ``NULL`` are omitted from the dump entirely, so PostgreSQL
  applies its own defaults -- which is what makes ``public_id`` pick up
  ``gen_random_uuid()`` instead of being inserted as ``NULL``.
* ``SERIAL`` sequences are not copied, so the dump ends with ``setval`` calls.
  Without them the backend's first insert would collide.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from scrapling.parser import Adaptor

from extractor import EntitySpec, Extractor
from models import RUN_STATUSES

# ---------------------------------------------------------------------------
# The target schema, declared once
# ---------------------------------------------------------------------------

_SERIAL = "SERIAL PRIMARY KEY"
_TEXT_LENGTH_RE = re.compile(r"(?:VAR)?CHAR\((\d+)\)", re.I)


@dataclass(frozen=True, slots=True)
class TargetColumn:
    """One column of the target schema, exactly as PostgreSQL declares it."""

    name: str
    pg_type: str
    primary: bool = False
    nullable: bool = True
    unique: bool = False
    references: str | None = None
    default: str | None = None
    enum: tuple[str, ...] = ()

    @property
    def is_boolean(self) -> bool:
        return self.pg_type.upper() == "BOOLEAN"

    @property
    def is_exact_numeric(self) -> bool:
        return self.pg_type.upper().startswith("NUMERIC")

    @property
    def char_limit(self) -> int | None:
        match = _TEXT_LENGTH_RE.search(self.pg_type)
        return int(match.group(1)) if match else None


@dataclass(frozen=True, slots=True)
class TargetTable:
    """A target table: its columns, in declaration order."""

    name: str
    columns: tuple[TargetColumn, ...]
    unique_together: tuple[tuple[str, ...], ...] = ()

    def column(self, name: str) -> TargetColumn:
        for candidate in self.columns:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.name} has no column {name!r}")

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def unique_columns(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns if column.unique)


def _c(name: str, pg_type: str, **kwargs: Any) -> TargetColumn:
    return TargetColumn(name, pg_type, **kwargs)


def _t(name: str, *columns: TargetColumn,
      unique_together: tuple[tuple[str, ...], ...] = ()) -> TargetTable:
    return TargetTable(name, tuple(columns), unique_together)


TARGET_TABLES: tuple[TargetTable, ...] = (
    # -- operational, carried over from the crawler --------------------------
    _t("data_source",
       _c("id", _SERIAL, primary=True),
       _c("name", "VARCHAR(150)", nullable=False, unique=True),
       _c("base_url", "TEXT"),
       _c("source_type", "VARCHAR(50)"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("scraper_config",
       _c("id", _SERIAL, primary=True),
       _c("data_source_id", "INTEGER", nullable=False, references="data_source"),
       _c("scraper_type", "VARCHAR(50)"),
       _c("target_type", "VARCHAR(50)"),
       _c("schedule", "VARCHAR(100)"),
       _c("rate_limit", "VARCHAR(50)"),
       _c("parser_config", "JSONB"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("scrape_run",
       _c("id", _SERIAL, primary=True),
       _c("scraper_config_id", "INTEGER", nullable=False,
          references="scraper_config"),
       _c("started_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("finished_at", "TIMESTAMP"),
       _c("records_found", "INTEGER", default="0"),
       _c("records_processed", "INTEGER", default="0"),
       _c("records_failed", "INTEGER", default="0"),
       _c("status", "VARCHAR(20)", nullable=False, default="'pending'",
          enum=("pending", "running", "success", "failed")),
       _c("error_message", "TEXT")),
    _t("scraped_data",
       _c("id", _SERIAL, primary=True),
       _c("scrape_run_id", "INTEGER", nullable=False, references="scrape_run"),
       _c("external_id", "VARCHAR(255)"),
       _c("external_url", "TEXT"),
       _c("entity_type", "VARCHAR(50)"),
       _c("raw_data", "JSONB"),
       _c("extracted_at", "TIMESTAMP", nullable=False, default="NOW()")),
    # -- catalog lookups, resolved from natural keys -------------------------
    _t("brand",
       _c("id", _SERIAL, primary=True),
       _c("name", "VARCHAR(150)", nullable=False, unique=True),
       _c("logo_url", "TEXT"),
       _c("website_url", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE")),
    _t("product_category",
       _c("id", _SERIAL, primary=True),
       _c("parent_category_id", "INTEGER", references="product_category"),
       _c("name", "VARCHAR(150)", nullable=False),
       _c("description", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("product_category_specification",
       _c("id", _SERIAL, primary=True),
       _c("category_id", "INTEGER", nullable=False,
          references="product_category"),
       _c("name", "VARCHAR(150)", nullable=False),
       _c("data_type", "VARCHAR(50)", nullable=False),
       _c("unit", "VARCHAR(50)"),
       _c("required", "BOOLEAN", nullable=False, default="FALSE"),
       _c("comparable", "BOOLEAN", nullable=False, default="TRUE"),
       _c("display_order", "INTEGER", nullable=False, default="0")),
    _t("store",
       _c("id", _SERIAL, primary=True),
       _c("name", "VARCHAR(150)", nullable=False),
       _c("website_url", "TEXT"),
       _c("logo_url", "TEXT"),
       _c("rating", "NUMERIC(3,2)"),
       _c("reputation", "VARCHAR(100)"),
       _c("shipping_information", "TEXT"),
       _c("general_conditions", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("provider",
       _c("id", _SERIAL, primary=True),
       _c("name", "VARCHAR(150)", nullable=False),
       _c("website_url", "TEXT"),
       _c("logo_url", "TEXT"),
       _c("rating", "NUMERIC(3,2)"),
       _c("reputation", "VARCHAR(100)"),
       _c("general_conditions", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("service_type",
       _c("id", _SERIAL, primary=True),
       _c("name", "VARCHAR(100)", nullable=False, unique=True)),
    _t("service_category",
       _c("id", _SERIAL, primary=True),
       _c("service_type_id", "INTEGER", nullable=False, references="service_type"),
       _c("parent_category_id", "INTEGER", references="service_category"),
       _c("name", "VARCHAR(150)", nullable=False),
       _c("description", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("service_category_specification",
       _c("id", _SERIAL, primary=True),
       _c("category_id", "INTEGER", nullable=False, references="service_category"),
       _c("name", "VARCHAR(150)", nullable=False),
       _c("data_type", "VARCHAR(50)", nullable=False),
       _c("unit", "VARCHAR(50)"),
       _c("required", "BOOLEAN", nullable=False, default="FALSE"),
       _c("comparable", "BOOLEAN", nullable=False, default="TRUE"),
       _c("display_order", "INTEGER", nullable=False, default="0")),
    _t("location",
       _c("id", _SERIAL, primary=True),
       _c("country", "VARCHAR(100)", nullable=False),
       _c("region", "VARCHAR(100)"),
       _c("city", "VARCHAR(100)"),
       _c("commune", "VARCHAR(100)"),
       _c("latitude", "NUMERIC(9,6)"),
       _c("longitude", "NUMERIC(9,6)")),
    # -- entities produced by extraction -------------------------------------
    _t("product",
       _c("id", _SERIAL, primary=True),
       _c("public_id", "UUID", unique=True),
       _c("category_id", "INTEGER", nullable=False, references="product_category"),
       _c("brand_id", "INTEGER", references="brand"),
       _c("name", "VARCHAR(255)", nullable=False),
       _c("model", "VARCHAR(150)"),
       _c("sku", "VARCHAR(100)", unique=True),
       _c("description", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("product_image",
       _c("id", _SERIAL, primary=True),
       _c("product_id", "INTEGER", nullable=False, references="product"),
       _c("image_url", "TEXT", nullable=False),
       _c("alt_text", "VARCHAR(255)"),
       _c("sort_order", "INTEGER", nullable=False, default="0"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE")),
    _t("product_specification_value",
       _c("id", _SERIAL, primary=True),
       _c("product_id", "INTEGER", nullable=False, references="product"),
       _c("specification_id", "INTEGER", nullable=False,
          references="product_category_specification"),
       _c("value", "VARCHAR(255)", nullable=False),
       unique_together=(("product_id", "specification_id"), )),
    _t("product_offer",
       _c("id", _SERIAL, primary=True),
       _c("product_id", "INTEGER", nullable=False, references="product"),
       _c("store_id", "INTEGER", nullable=False, references="store"),
       _c("price", "NUMERIC(12,2)", nullable=False),
       _c("list_price", "NUMERIC(12,2)"),
       _c("currency", "CHAR(3)", nullable=False, default="'CLP'"),
       _c("shipping_cost", "NUMERIC(12,2)", default="0"),
       _c("shipping_free", "BOOLEAN", nullable=False, default="FALSE"),
       _c("available", "BOOLEAN", nullable=False, default="TRUE"),
       _c("stock", "INTEGER"),
       _c("condition", "VARCHAR(20)", nullable=False, default="'new'",
          enum=("new", "used", "refurbished")),
       _c("product_url", "TEXT"),
       _c("last_updated", "TIMESTAMP", nullable=False, default="NOW()"),
       unique_together=(("product_id", "store_id"), )),
    _t("product_price_history",
       _c("id", _SERIAL, primary=True),
       _c("product_offer_id", "INTEGER", nullable=False,
          references="product_offer"),
       _c("price", "NUMERIC(12,2)", nullable=False),
       _c("is_promotional", "BOOLEAN", nullable=False, default="FALSE"),
       _c("recorded_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("service",
       _c("id", _SERIAL, primary=True),
       _c("public_id", "UUID", unique=True),
       _c("category_id", "INTEGER", nullable=False, references="service_category"),
       _c("name", "VARCHAR(255)", nullable=False),
       _c("description", "TEXT"),
       _c("image_url", "TEXT"),
       _c("active", "BOOLEAN", nullable=False, default="TRUE"),
       _c("created_at", "TIMESTAMP", nullable=False, default="NOW()"),
       _c("updated_at", "TIMESTAMP", nullable=False, default="NOW()")),
    _t("service_specification_value",
       _c("id", _SERIAL, primary=True),
       _c("service_id", "INTEGER", nullable=False, references="service"),
       _c("specification_id", "INTEGER", nullable=False,
          references="service_category_specification"),
       _c("value", "VARCHAR(255)", nullable=False),
       unique_together=(("service_id", "specification_id"), )),
    _t("service_offer",
       _c("id", _SERIAL, primary=True),
       _c("service_id", "INTEGER", nullable=False, references="service"),
       _c("provider_id", "INTEGER", nullable=False, references="provider"),
       _c("price", "NUMERIC(12,2)", nullable=False),
       _c("currency", "CHAR(3)", nullable=False, default="'CLP'"),
       _c("billing_period", "VARCHAR(30)"),
       _c("installation_cost", "NUMERIC(12,2)"),
       _c("contract_period", "VARCHAR(50)"),
       _c("available", "BOOLEAN", nullable=False, default="TRUE"),
       _c("coverage_summary", "TEXT"),
       _c("additional_costs_summary", "TEXT"),
       _c("service_url", "TEXT"),
       _c("last_updated", "TIMESTAMP", nullable=False, default="NOW()"),
       unique_together=(("service_id", "provider_id"), )),
    _t("service_price_history",
       _c("id", _SERIAL, primary=True),
       _c("service_offer_id", "INTEGER", nullable=False, references="service_offer"),
       _c("price", "NUMERIC(12,2)", nullable=False),
       _c("is_promotional", "BOOLEAN", nullable=False, default="FALSE"),
       _c("recorded_at", "TIMESTAMP", nullable=False, default="NOW()")),
)

TARGET_BY_NAME: dict[str, TargetTable] = {t.name: t for t in TARGET_TABLES}

# A table must be normalized after everything it points at, and after whatever
# produces the parent id its children attach to.
RESOLUTION_ORDER: tuple[str, ...] = (
    "brand",
    "product_category",
    "service_type",
    "service_category",
    "store",
    "provider",
    "location",
    "product",
    "product_image",
    "service",
    "product_offer",
    "service_offer",
)

# Which context key each entity leaves behind for its children to attach to.
PARENT_KEYS: dict[str, str] = {
    "product": "product_id",
    "service": "service_id",
}


# ---------------------------------------------------------------------------
# DDL generation
# ---------------------------------------------------------------------------


def sqlite_type(pg_type: str) -> str:
    """Translate a PostgreSQL type into the closest SQLite storage class."""
    upper = pg_type.upper()
    if upper.startswith("SERIAL"):
        return "INTEGER"
    if upper.startswith(("NUMERIC", "DECIMAL")):
        return "TEXT"
    if upper in {"UUID", "JSONB", "JSON", "TEXT"}:
        return "TEXT"
    if upper == "BOOLEAN":
        return "BOOLEAN"
    if upper.startswith(("VARCHAR", "CHAR")):
        return "TEXT"
    if upper.startswith("TIMESTAMP") or upper == "DATE":
        return "TEXT"
    return "INTEGER"


def sqlite_default(default: str | None) -> str | None:
    """Translate a PostgreSQL default into SQLite syntax."""
    if not default:
        return None
    upper = default.upper()
    if upper == "NOW()":
        return "CURRENT_TIMESTAMP"
    if upper == "TRUE":
        return "1"
    if upper == "FALSE":
        return "0"
    return default


def column_ddl(column: TargetColumn) -> str:
    """Render one column, carrying over every constraint SQLite understands."""
    parts = [column.name, sqlite_type(column.pg_type)]
    if column.primary:
        parts.append("PRIMARY KEY")
    elif not column.nullable:
        parts.append("NOT NULL")

    if column.unique:
        parts.append("UNIQUE")
    if column.references:
        parts.append(f"REFERENCES {column.references}(id)")
    if (default := sqlite_default(column.default)):
        parts.append(f"DEFAULT {default}")
    if column.enum:
        allowed = ", ".join(f"'{value}'" for value in column.enum)
        parts.append(f"CHECK ({column.name} IN ({allowed}))")
    elif column.is_boolean:
        parts.append(f"CHECK ({column.name} IN (0, 1))")
    elif not column.is_exact_numeric and column.char_limit is not None:
        # PostgreSQL rejects an over-long value; the mirror should too, so a
        # violation surfaces here instead of as a failed import.
        parts.append(f"CHECK (length({column.name}) <= {column.char_limit})")
    return " ".join(parts)


def table_ddl(table: TargetTable) -> str:
    body = ",\n    ".join(column_ddl(column) for column in table.columns)
    for group in table.unique_together:
        body += f",\n    UNIQUE ({', '.join(group)})"
    return f"CREATE TABLE IF NOT EXISTS {table.name} (\n    {body}\n);"


def target_ddl() -> str:
    """Full DDL for the normalized file.

    Foreign keys are declared for documentation but the mirror is written with
    ``PRAGMA foreign_keys = OFF``: resolution order already guarantees parents
    exist, and deferring keeps a partially-normalized archive loadable.
    """
    return "\n\n".join(table_ddl(table) for table in TARGET_TABLES) + "\n"


def pg_ddl() -> str:
    """The same schema rendered back as PostgreSQL, for diffing the mirror."""
    blocks = []
    for table in TARGET_TABLES:
        lines = []
        for column in table.columns:
            parts = [column.name, column.pg_type]
            if column.unique:
                parts.append("UNIQUE")
            if column.references:
                parts.append(f"REFERENCES {column.references}(id)")
            if column.default:
                parts.append(f"DEFAULT {column.default}")
            if column.enum:
                allowed = ", ".join(f"'{value}'" for value in column.enum)
                parts.append(f"CHECK ({column.name} IN ({allowed}))")
            lines.append("    " + " ".join(parts))
        for group in table.unique_together:
            lines.append(f"    UNIQUE ({', '.join(group)})")
        blocks.append(f"CREATE TABLE {table.name} (\n" + ",\n".join(lines) + "\n);")
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Specs declared by parser_config
# ---------------------------------------------------------------------------


def specs_for(parser_config: dict | None) -> dict[str, EntitySpec]:
    """Build every :class:`EntitySpec` a ``parser_config`` declares.

    Two shapes are accepted. The flat one describes a single entity and is what
    the crawler stores today::

        {"entity_type": "product_offer", "fields": {...}}

    The ``profiles`` shape describes several entities at once, which is what
    normalization actually needs: an offer is meaningless without the product it
    belongs to::

        {"profiles": {"books": {"product": {...}, "product_offer": {...}}}}
    """
    config = parser_config or {}
    profiles = config.get("profiles")
    if not profiles:
        if not config.get("entity_type"):
            return {}
        flat = dict(config)
        entity_type = flat.pop("entity_type")
        return {entity_type: EntitySpec.from_parser_config(
            {"entity_type": entity_type, **flat})}

    specs: dict[str, EntitySpec] = {}
    for profile in profiles.values():
        for entity_type, block in (profile or {}).items():
            specs[entity_type] = EntitySpec.from_parser_config(
                {"entity_type": entity_type, **(block or {})})
    return specs


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NormalizationResult:
    """What one normalization pass produced."""

    target_path: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def rows(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        written = ", ".join(f"{name}={count}"
                            for name, count in sorted(self.counts.items()) if count)
        tail = f" | {len(self.warnings)} warning(s)" if self.warnings else ""
        skipped = f" | {self.skipped} skipped" if self.skipped else ""
        return (f"{self.rows} rows -> {Path(self.target_path).name}"
                f" ({written}){tail}{skipped}")


def _to_storage(value: Any) -> Any:
    """Render a Python value the way the SQLite mirror has to store it."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class Normalizer:
    """Builds a normalized database out of a crawler database."""

    def __init__(self, target_path: str, *,
                 default_service_type: str = "suscripción",
                 track_price_history: bool = True,
                 rebuild: bool = True):
        self.target_path = target_path
        self.default_service_type = default_service_type
        self.track_price_history = track_price_history
        self.rebuild = rebuild
        self.result = NormalizationResult(target_path=target_path)
        self._lookups: dict[str, dict[str, int]] = {}
        self._source_names: dict[int, str] = {}

    # -- public API ---------------------------------------------------------

    def run(self, source_path: str) -> NormalizationResult:
        """Normalize the crawler database at ``source_path``."""
        self.result = NormalizationResult(target_path=self.target_path)
        self._lookups = {}
        self._source_names = {}

        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            self._create_target()
            with sqlite3.connect(self.target_path) as target:
                target.execute("PRAGMA foreign_keys = OFF")
                target.row_factory = sqlite3.Row
                self._copy_sources(target, source)
                self._copy_configs(target, source)
                self._copy_runs(target, source)
                self._prime_lookups(target)
                self._normalize_payloads(target, source)
                target.commit()
        finally:
            source.close()
        return self.result

    def dump_sql(self, path: str | Path) -> Path:
        """Write ``INSERT`` statements ready for ``psql -f``."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.target_path)
        conn.row_factory = sqlite3.Row
        try:
            with target.open("w", encoding="utf-8") as handle:
                handle.write("-- Generated from a SOLOServis crawler archive.\n")
                handle.write("-- Apply on top of 001_schema.sql, which stays the\n"
                             "-- authority for the DDL. Rows are dumped in foreign\n"
                             "-- key order, and NULL columns are omitted so\n"
                             "-- PostgreSQL applies its own defaults.\n")
                for table in TARGET_TABLES:
                    self._dump_table(conn, handle, table)
                handle.write("\n-- SERIAL sequences were not copied, so realign them\n"
                             "-- before the backend writes anything.\n")
                for table in TARGET_TABLES:
                    if any(column.primary for column in table.columns):
                        handle.write(
                            "SELECT setval(pg_get_serial_sequence("
                            f"'{table.name}', 'id'),"
                            f" COALESCE((SELECT MAX(id) FROM {table.name}), 1),"
                            f" EXISTS (SELECT 1 FROM {table.name}));\n")
        finally:
            conn.close()
        return target

    # -- target construction ------------------------------------------------

    def _create_target(self) -> None:
        """Prepare the mirror.

        ``rebuild`` gives a deterministic file for a one-shot import. Turning it
        off merges into whatever is already there, which is the only way price
        history can accrue: a rebuilt file has no previous price to compare
        against.
        """
        if self.rebuild:
            Path(self.target_path).unlink(missing_ok=True)
        Path(self.target_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.target_path) as conn:
            conn.executescript(target_ddl())

    # -- operational tables -------------------------------------------------

    def _copy_sources(self, target: sqlite3.Connection,
                      source: sqlite3.Connection) -> None:
        rows = source.execute(
            "SELECT id, name, base_url, source_type, active, created_at,"
            " updated_at FROM DATA_SOURCE").fetchall()
        for row in rows:
            self._source_names[row["id"]] = row["name"]
            self._insert(target, "data_source", dict(row))
        self._seed_parties(target)

    def _seed_parties(self, target: sqlite3.Connection) -> None:
        """Give every crawled site its ``store``/``provider`` row.

        ``data_source.source_type`` is exactly the hook the schema provides -- the
        seed data registers Falabella as ``'store'`` and Movistar as
        ``'provider'`` -- so a site joins the graph as a real party the first time
        it is crawled, with an id ready for its offers to reference.

        ``store.name`` carries no UNIQUE constraint in the schema, so the row is
        matched on website first: without that, every merge would append another
        copy of the same store.
        """
        for row in target.execute(
                "SELECT id, name, base_url, source_type FROM data_source"):
            kind = (row["source_type"] or "").strip().lower()
            table = {"store": "store", "provider": "provider"}.get(kind)
            if table is None:
                continue
            new_id = self._ensure_row(
                target, table, row["base_url"], row["name"],
                {"name": row["name"], "website_url": row["base_url"]})
            self._remember(table, row["name"], new_id)
            if row["base_url"]:
                self._remember(table, row["base_url"], new_id)

    def _ensure_row(self, target: sqlite3.Connection, table: str,
                    website: str | None, name: str,
                    payload: dict[str, Any]) -> int:
        """Return the id of this party, creating the row the first time."""
        found = target.execute(
            f"SELECT id FROM {table}"
            f" WHERE (? IS NOT NULL AND website_url = ?)"
            f"    OR lower(name) = lower(?)",
            (website, website, name)).fetchone()
        if found:
            return int(found["id"])
        return self._insert(target, table, payload)

    def _copy_configs(self, target: sqlite3.Connection,
                      source: sqlite3.Connection) -> None:
        rows = source.execute(
            "SELECT id, data_source_id, scraper_type, target_type, schedule,"
            " rate_limit, parser_config, active, created_at, updated_at"
            " FROM SCRAPER_CONFIG").fetchall()
        for row in rows:
            payload = dict(row)
            payload["parser_config"] = _as_json(payload["parser_config"])
            self._insert(target, "scraper_config", payload)

    def _copy_runs(self, target: sqlite3.Connection,
                   source: sqlite3.Connection) -> None:
        rows = source.execute(
            "SELECT id, scraper_config_id, started_at, finished_at,"
            " records_found, records_processed, records_failed, status,"
            " error_message FROM SCRAPE_RUN").fetchall()
        for row in rows:
            payload = dict(row)
            status = payload.get("status")
            if status not in RUN_STATUSES:
                self.result.warnings.append(
                    f"scrape_run {payload['id']}: status {status!r} is outside "
                    f"the schema vocabulary; stored as 'failed'")
                payload["status"] = "failed"
            self._insert(target, "scrape_run", payload)

    # -- payloads -----------------------------------------------------------

    def _normalize_payloads(self, target: sqlite3.Connection,
                            source: sqlite3.Connection) -> None:
        rows = source.execute(
            "SELECT sd.id AS id, sd.scrape_run_id AS scrape_run_id,"
            " sd.external_id AS external_id, sd.external_url AS external_url,"
            " sd.entity_type AS entity_type, sd.raw_data AS raw_data,"
            " sd.extracted_at AS extracted_at, sc.parser_config AS parser_config,"
            " sc.data_source_id AS data_source_id, ds.base_url AS base_url"
            " FROM SCRAPED_DATA sd"
            " JOIN SCRAPE_RUN run ON run.id = sd.scrape_run_id"
            " JOIN SCRAPER_CONFIG sc ON sc.id = run.scraper_config_id"
            " JOIN DATA_SOURCE ds ON ds.id = sc.data_source_id"
            " ORDER BY sd.id").fetchall()

        for row in rows:
            self._normalize_row(target, row)

    def _normalize_row(self, target: sqlite3.Connection, row: sqlite3.Row) -> None:
        url = row["external_url"]
        specs = self._specs_for(row["parser_config"], row["id"])
        if not specs:
            self.result.warnings.append(
                f"scraped_data {row['id']}: no usable parser_config; archived "
                f"without entities")
            self._write_scraped_row(target, row, None)
            self.result.skipped += 1
            return

        document, preloaded = _document_for(row["raw_data"])
        if document is None:
            self.result.warnings.append(
                f"scraped_data {row['id']}: raw_data holds neither HTML nor an "
                f"extractor payload; archived without entities")
            self._write_scraped_row(target, row, preloaded)
            self.result.skipped += 1
            return

        base_url = row["base_url"] or url
        payload = self._extract_entities(
            target, row, specs, document, base_url, url)
        if not payload.get("entities"):
            self.result.skipped += 1
        self._write_scraped_row(target, row, payload)

    def _specs_for(self, parser_config: Any, scraped_id: int) -> dict[str, EntitySpec]:
        try:
            return specs_for(_as_json(parser_config))
        except ValueError as exc:
            self.result.warnings.append(f"scraped_data {scraped_id}: {exc}")
            return {}

    def _extract_entities(self, target: sqlite3.Connection, row: sqlite3.Row,
                          specs: dict[str, EntitySpec], document: Any,
                          base_url: str | None,
                          url: str | None) -> dict[str, Any]:
        """Write every declared entity for one archived page.

        Returns the payload stored back into ``scraped_data.raw_data``.

        A listing page yields N products and N offers from the same container
        selector, so children are paired to their parent *by position* rather
        than stacked onto whichever parent happened to be written first.
        """
        extractions: dict[str, list[Any]] = {}
        for entity_type in RESOLUTION_ORDER:
            if entity_type not in specs:
                continue
            try:
                extractions[entity_type] = Extractor(
                    specs[entity_type]).extract_all(document, base_url)
            except Exception as exc:
                self.result.warnings.append(
                    f"{entity_type} @ {url}: extraction crashed "
                    f"({type(exc).__name__}: {exc})")
                extractions[entity_type] = []

        base_context: dict[str, Any] = {
            "source_name": self._source_names.get(row["data_source_id"]),
            "external_url": url,
        }
        parent_ids: dict[str, list[int]] = {}
        payload: dict[str, Any] = {"entities": {}}

        for entity_type in RESOLUTION_ORDER:
            rows = extractions.get(entity_type)
            if rows is None:
                continue

            written: list[dict] = []
            for index, extraction in enumerate(rows):
                context = dict(base_context)
                for key, ids in parent_ids.items():
                    if index < len(ids):
                        context[key] = ids[index]
                try:
                    new_id = self._write_entity(
                        target, entity_type, extraction, context, url)
                except sqlite3.Error as exc:
                    self.result.warnings.append(f"{entity_type} @ {url}: {exc}")
                    continue
                if new_id is None:
                    continue
                # to_dict() runs the values through the extractor's JSON
                # coercion, which turns Decimal into text; raw values would not
                # serialize.
                written.append({"id": new_id, **extraction.to_dict()["values"]})

            if written:
                payload["entities"][entity_type] = written
                parent_key = PARENT_KEYS.get(entity_type)
                if parent_key:
                    parent_ids[parent_key] = [entry["id"] for entry in written]

        for key, ids in parent_ids.items():
            orphans = len(extractions.get(key.removesuffix("_id"), [])) - len(ids)
            if orphans > 0:
                self.result.warnings.append(
                    f"{key}: {orphans} child row(s) had no parent to attach to "
                    f"and were dropped")
        return payload

    def _write_entity(self, target: sqlite3.Connection, entity_type: str,
                      extraction: Any, context: dict[str, Any],
                      url: str | None) -> int | None:
        writer = getattr(self, f"_write_{entity_type}", None)
        if writer is None:
            self.result.warnings.append(
                f"no normalizer for entity_type {entity_type!r}; rows dropped")
            return None

        for error in extraction.errors:
            self.result.warnings.append(f"{entity_type} @ {url}: {error}")
        return writer(target, extraction, context)

    # -- natural key resolution --------------------------------------------

    def _remember(self, table: str, key: str, row_id: int) -> None:
        if key:
            self._lookups.setdefault(table, {})[str(key).strip().lower()] = row_id

    def _resolve(self, target: sqlite3.Connection, table: str, column: str,
                 key: Any) -> int | None:
        """Find a row by natural key, creating it when it does not exist yet."""
        if key is None:
            return None
        text = str(key).strip()
        if not text:
            return None

        known = self._lookups.get(table, {})
        if text.lower() in known:
            return known[text.lower()]

        found = target.execute(
            f"SELECT id FROM {table} WHERE lower({column}) = lower(?)",
            (text,)).fetchone()
        if found:
            self._remember(table, text, found["id"])
            return int(found["id"])

        new_id = self._insert(target, table, {column: text})
        self._remember(table, text, new_id)
        return new_id

    def _prime_lookups(self, target: sqlite3.Connection) -> None:
        """Cache rows that already exist, including those imported ahead of time."""
        for table in ("brand", "product_category", "service_type",
                      "service_category", "store", "provider"):
            for row in target.execute(f"SELECT id, name FROM {table}"):
                self._remember(table, row["name"], row["id"])

    # -- entity writers -----------------------------------------------------

    def _write_brand(self, target, extraction, context) -> int | None:
        return self._resolve(target, "brand", "name", extraction.values["name"])

    def _write_store(self, target, extraction, context) -> int | None:
        return self._resolve(target, "store", "name", extraction.values["name"])

    def _write_provider(self, target, extraction, context) -> int | None:
        return self._resolve(target, "provider", "name", extraction.values["name"])

    def _write_product_category(self, target, extraction, context) -> int | None:
        return self._resolve(target, "product_category", "name",
                             extraction.values["name"])

    def _write_service_type(self, target, extraction, context) -> int | None:
        return self._resolve(target, "service_type", "name",
                             extraction.values["name"])

    def _write_service_category(self, target, extraction, context) -> int | None:
        service_type = self._resolve(target, "service_type", "name",
                                     self.default_service_type)
        category_id = self._resolve(target, "service_category", "name",
                                    extraction.values["name"])
        if category_id is None or service_type is None:
            return None
        target.execute(
            "UPDATE service_category SET service_type_id = ?"
            " WHERE id = ? AND service_type_id IS NULL", (service_type, category_id))
        return category_id

    def _write_location(self, target, extraction, context) -> int | None:
        values = dict(extraction.values)
        keys = ("country", "region", "city", "commune")
        found = target.execute(
            "SELECT id FROM location WHERE "
            + " AND ".join(f"{key} IS ?" for key in keys),
            tuple(values.get(key) for key in keys)).fetchone()
        if found:
            return int(found["id"])
        return self._insert(target, "location", values)

    def _write_product(self, target, extraction, context) -> int | None:
        values = extraction.values
        category = self._resolve(
            target, "product_category", "name",
            extraction.references.get("category") or context.get("source_name"))
        if category is None:
            self.result.warnings.append(
                f"product @ {context.get('external_url')}: no category could be "
                f"resolved; add a 'category' reference to the parser_config")
            return None
        brand = self._resolve(target, "brand", "name",
                              extraction.references.get("brand"))
        return self._insert(target, "product", {
            "category_id": category,
            "brand_id": brand,
            "name": values["name"],
            "model": values.get("model"),
            "sku": values.get("sku"),
            "description": values.get("description"),
        })

    def _write_service(self, target, extraction, context) -> int | None:
        values = extraction.values
        category = self._resolve(
            target, "service_category", "name",
            extraction.references.get("category") or context.get("source_name"))
        if category is None:
            self.result.warnings.append(
                f"service @ {context.get('external_url')}: no category could be "
                f"resolved")
            return None
        return self._insert(target, "service", {
            "category_id": category,
            "name": values["name"],
            "description": values.get("description"),
            "image_url": values.get("image_url"),
        })

    def _write_product_image(self, target, extraction, context) -> int | None:
        product_id = context.get("product_id")
        urls = extraction.values.get("image_url")
        if product_id is None or not urls:
            return None
        urls = [urls] if isinstance(urls, str) else list(urls)
        alt = extraction.values.get("alt_text")
        alt = [alt] if isinstance(alt, str) else list(alt or [])

        for order, url in enumerate(urls):
            self._insert(target, "product_image", {
                "product_id": product_id,
                "image_url": url,
                "alt_text": alt[order] if order < len(alt) else None,
                "sort_order": order,
            })
        return product_id

    def _write_product_offer(self, target, extraction, context) -> int | None:
        product_id = context.get("product_id")
        if product_id is None:
            self.result.warnings.append(
                f"product_offer @ {context.get('external_url')}: no product to "
                f"attach to; declare a 'product' spec in the same profile")
            return None
        store_id = self._resolve(
            target, "store", "name",
            extraction.references.get("store") or context.get("source_name"))
        if store_id is None:
            return None

        values = extraction.values
        offer_id, previous = self._upsert_offer(
            target, "product_offer", "product_id", product_id, {
                "store_id": store_id,
                "price": values["price"],
                "list_price": values.get("list_price"),
                "currency": values.get("currency"),
                "shipping_cost": values.get("shipping_cost"),
                "shipping_free": values.get("shipping_free"),
                "available": values.get("available"),
                "stock": values.get("stock"),
                "condition": values.get("condition"),
                "product_url": (values.get("product_url")
                                or context.get("external_url")),
            })
        self._record_price_history(target, "product_price_history",
                                   "product_offer_id", offer_id, previous, values)
        return offer_id

    def _write_service_offer(self, target, extraction, context) -> int | None:
        service_id = context.get("service_id")
        if service_id is None:
            self.result.warnings.append(
                f"service_offer @ {context.get('external_url')}: no service to "
                f"attach to; declare a 'service' spec in the same profile")
            return None
        provider_id = self._resolve(
            target, "provider", "name",
            extraction.references.get("provider") or context.get("source_name"))
        if provider_id is None:
            return None

        values = extraction.values
        offer_id, previous = self._upsert_offer(
            target, "service_offer", "service_id", service_id, {
                "provider_id": provider_id,
                "price": values["price"],
                "currency": values.get("currency"),
                "billing_period": values.get("billing_period"),
                "installation_cost": values.get("installation_cost"),
                "contract_period": values.get("contract_period"),
                "available": values.get("available"),
                "coverage_summary": values.get("coverage_summary"),
                "additional_costs_summary": values.get(
                    "additional_costs_summary"),
                "service_url": (values.get("service_url")
                                or context.get("external_url")),
            })
        self._record_price_history(target, "service_price_history",
                                   "service_offer_id", offer_id, previous, values)
        return offer_id

    # -- shared write helpers ----------------------------------------------

    def _upsert_offer(self, target: sqlite3.Connection, table: str,
                      party_column: str, party_id: int,
                      payload: dict[str, Any]) -> tuple[int | None, Any]:
        """Insert an offer, or refresh the existing one for the same party pair.

        ``product_offer`` is ``UNIQUE (product_id, store_id)``, so re-normalizing
        an archive must update the row rather than duplicate the offer. Returns
        the offer id and the price it held before, which is what price history is
        a record of.
        """
        other = "store_id" if party_column == "product_id" else "provider_id"
        existing = target.execute(
            f"SELECT id, price FROM {table}"
            f" WHERE {party_column} = ? AND {other} = ?",
            (party_id, payload[other])).fetchone()
        if existing is None:
            return self._insert(
                target, table, {party_column: party_id, **payload}), None

        changed = {name: value for name, value in payload.items()
                   if name not in {party_column, other} and value is not None}
        assignments = [f"{name} = ?" for name in changed]
        assignments.append("last_updated = CURRENT_TIMESTAMP")
        target.execute(
            f"UPDATE {table} SET " + ", ".join(assignments) + " WHERE id = ?",
            (*[_to_storage(value) for value in changed.values()], existing["id"]))
        return int(existing["id"]), existing["price"]

    def _record_price_history(self, target: sqlite3.Connection, table: str,
                              key_column: str, offer_id: int | None,
                              previous: Any, values: dict[str, Any]) -> None:
        """Append history only when a price actually moved.

        A price that has never changed is not history, so the first sighting of
        an offer records nothing; only a later, different price does.
        """
        if not self.track_price_history or offer_id is None or previous is None:
            return
        price = values.get("price")
        if price is None or _as_decimal(previous) == _as_decimal(price):
            return
        list_price = values.get("list_price")
        self._insert(target, table, {
            key_column: offer_id,
            "price": previous,
            "is_promotional": bool(
                list_price is not None
                and _as_decimal(previous) < _as_decimal(list_price)),
        })

    def _write_scraped_row(self, target: sqlite3.Connection, row: sqlite3.Row,
                           payload: dict[str, Any] | None) -> None:
        entity_type = row["entity_type"]
        external_id = row["external_id"]
        if payload is not None:
            entities = payload.get("entities") or {}
            if entities:
                entity_type = next(iter(entities))
                payload["entity_type"] = entity_type
                external_id = external_id or _infer_external_id(entities)

        self._insert(target, "scraped_data", {
            "id": row["id"],
            "scrape_run_id": row["scrape_run_id"],
            "external_id": external_id,
            "external_url": row["external_url"],
            "entity_type": entity_type,
            "raw_data": (json.dumps(payload, ensure_ascii=False)
                         if payload is not None else _wrap_html(row["raw_data"])),
            "extracted_at": row["extracted_at"],
        })

    def _insert(self, target: sqlite3.Connection, table: str,
                payload: dict[str, Any]) -> int:
        spec = TARGET_BY_NAME[table]
        names: list[str] = []
        values: list[Any] = []
        for name, value in payload.items():
            if name not in spec.column_names:
                self.result.warnings.append(
                    f"{table}: unknown column {name!r}; value dropped")
                continue
            if value is None:
                continue
            names.append(name)
            values.append(_to_storage(value))

        if not names:
            raise sqlite3.IntegrityError(f"{table}: nothing to insert")

        cursor = target.execute(
            f"INSERT INTO {table} ({', '.join(names)})"
            f" VALUES ({', '.join('?' * len(names))}) ON CONFLICT DO NOTHING",
            values)
        if cursor.rowcount:
            self.result.counts[table] = self.result.counts.get(table, 0) + 1
            return int(cursor.lastrowid)

        # The row lost a UNIQUE/PRIMARY KEY race, so return the winner's id.
        existing = self._find_existing(target, spec, names, values)
        if existing is None:
            raise sqlite3.IntegrityError(
                f"{table}: conflict on {', '.join(names)}")
        return existing

    def _find_existing(self, target: sqlite3.Connection, spec: TargetTable,
                       names: list[str], values: list[Any]) -> int | None:
        """Locate the row a failed insert collided with."""
        provided = set(names)
        groups: list[tuple[str, ...]] = []
        groups.extend(spec.unique_together)
        groups.extend((name,) for name in spec.unique_columns)
        groups.extend((column.name,) for column in spec.columns if column.primary)

        for group in groups:
            if not group or not set(group) <= provided:
                continue
            indexes = [names.index(name) for name in group]
            found = target.execute(
                f"SELECT id FROM {spec.name} WHERE "
                + " AND ".join(f"{name} IS ?" for name in group),
                tuple(values[index] for index in indexes)).fetchone()
            if found:
                return int(found["id"])
        return None

    # -- SQL dump -----------------------------------------------------------

    def _dump_table(self, conn: sqlite3.Connection, handle: Any,
                    table: TargetTable) -> None:
        rows = conn.execute(f"SELECT * FROM {table.name}").fetchall()
        if not rows:
            return
        handle.write(f"\n-- {table.name} ({len(rows)} rows)\n")
        for row in rows:
            # NULL columns are omitted so PostgreSQL fills them from its own
            # defaults: that is how public_id gets gen_random_uuid() instead of
            # arriving as an explicit NULL and tripping NOT NULL.
            present = [name for name in table.column_names if row[name] is not None]
            if not present:
                continue
            rendered = ", ".join(
                _sql_literal(row[name], table.column(name)) for name in present)
            handle.write(
                f"INSERT INTO {table.name} ({', '.join(present)})"
                f" VALUES ({rendered});\n")


def _sql_literal(value: Any, column: TargetColumn) -> str:
    """Render one value as a PostgreSQL literal."""
    if value is None:
        return "NULL"
    if column.is_exact_numeric:
        # Unquoted, so the exact decimal PostgreSQL received survives intact.
        return str(value)
    if column.is_boolean:
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    if column.pg_type.upper() in {"JSONB", "JSON"}:
        return f"'{text}'::jsonb"
    return f"'{text}'"


def _as_json(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _document_for(raw: Any) -> tuple[Any, dict[str, Any] | None]:
    """Return an ``Adaptor``, and the payload when the row already holds one.

    The crawler archives HTML, so that is the normal case. A row that already
    contains an extractor payload passes through untouched: re-parsing JSON as
    markup would corrupt it.
    """
    if raw is None:
        return None, None
    text = raw if isinstance(raw, str) else str(raw)
    if text.lstrip().startswith("{"):
        payload = _as_json(text)
        if payload is not None:
            return None, payload
    try:
        return Adaptor(text), None
    except Exception:
        return None, None


def _wrap_html(raw: Any) -> str:
    """Park an unparsed page inside a JSON object so the column stays JSONB."""
    text = raw if isinstance(raw, str) else str(raw or "")
    return json.dumps({"html": text}, ensure_ascii=False)


def _infer_external_id(entities: dict[str, list[dict]]) -> str | None:
    """Prefer a scraped identifier over inventing one.

    The crawler only fills ``external_id`` when a meta selector is configured,
    so the product sku or the offer URL is the next best stable key.
    """
    for entries in entities.values():
        for entry in entries:
            for key in ("sku", "external_id"):
                if entry.get(key):
                    return str(entry[key])
    return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
