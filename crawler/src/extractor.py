"""Extractor: reads typed fields out of a fetched document.

The crawler archives whole pages; this module pulls *fields* out of them. A
declarative spec -- the ``fields`` block of ``SCRAPER_CONFIG.parser_config`` --
says where each value lives, while the column contracts in :data:`TABLES` are
transcribed from the production schema in
``backend/database/migrations/001_schema.sql``. The extractor therefore knows
the target type before it reads a single character, so a scraped price arrives
as ``NUMERIC(12,2)`` and a condition as one of the three values the ``CHECK``
constraint allows.

Three rules keep the boundary honest:

* Foreign keys are never invented. ``product.category_id`` is a server-side
  identity; a scraper can only emit the natural key (``category``) for the
  ingestion layer to resolve. Those are declared as :class:`Reference`.
* No HTTP, no queue, no database. It runs against a live document or a fixture.
* Failures are collected, never raised: one unreadable field must not cost a
  whole page.
"""

import json
import re
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from utils import normalize_url

# ---------------------------------------------------------------------------
# Column contracts, transcribed from 001_schema.sql
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One target column exactly as the production schema declares it."""

    name: str
    sql_type: str
    required: bool = False
    max_length: int | None = None
    choices: tuple[str, ...] = ()
    default: Any = None
    transform: str | None = None
    minimum: Decimal | None = None

    @property
    def scale(self) -> int | None:
        """Decimal places for a ``NUMERIC(p,s)`` column, else ``None``."""
        match = _NUMERIC_RE.search(self.sql_type)
        return int(match.group(2)) if match else None

    @property
    def precision(self) -> int | None:
        """Total digits for a ``NUMERIC(p,s)`` column, else ``None``."""
        match = _NUMERIC_RE.search(self.sql_type)
        return int(match.group(1)) if match else None

    @property
    def length_limit(self) -> int | None:
        """Character budget, taken from ``VARCHAR(n)``/``CHAR(n)`` itself."""
        if self.max_length is not None:
            return self.max_length
        match = _SIZED_TEXT_RE.search(self.sql_type)
        return int(match.group(1)) if match else None

    def coerce(self, raw: str, base_url: str | None = None,
               fallback: Any = None) -> tuple[Any, list[str]]:
        """Turn a raw string into a value this column accepts.

        Returns the value (``None`` when unusable) plus the problems found.
        ``fallback`` is the spec's own default for this field and outranks the
        column default: it is the more specific declaration, and quietly
        swapping a site's real ``GBP`` for the schema-wide ``CLP`` would be
        worse than no value at all.
        """
        errors: list[str] = []

        def _absent() -> Any:
            if fallback is not None:
                return fallback
            return self.default

        text = (raw or "").strip()
        if not text:
            if (value := _absent()) is not None:
                return value, errors
            if self.required:
                errors.append(f"{self.name}: required value not found")
            return None, errors

        transform = TRANSFORMS.get(self.transform or "", _identity)
        try:
            value = transform(text, self, base_url)
        except ValueError as exc:
            value = None
            errors.append(f"{self.name}: {exc}")

        if value is None:
            if (fallback := _absent()) is not None:
                return fallback, errors
            if not errors:
                errors.append(
                    f"{self.name}: could not read {text!r} as {self.sql_type}")
            return None, errors

        if isinstance(value, Decimal):
            value, decimal_errors = self._check_decimal(value)
            errors.extend(decimal_errors)
        elif isinstance(value, str):
            value, length_errors = self._check_length(value)
            errors.extend(length_errors)

        return value, errors

    def _check_decimal(self, value: Decimal) -> tuple[Decimal, list[str]]:
        errors: list[str] = []
        scale = self.scale
        if scale is not None:
            quantum = Decimal(1).scaleb(-scale)
            rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
            if rounded != value:
                errors.append(
                    f"{self.name}: {value} rounded to {rounded} to fit "
                    f"{self.sql_type}")
            value = rounded
        precision = self.precision
        if precision is not None and abs(value) >= Decimal(10) ** (precision - (scale or 0)):
            errors.append(f"{self.name}: {value} overflows {self.sql_type}")
            return None, errors
        if self.minimum is not None and value < self.minimum:
            errors.append(f"{self.name}: {value} is below {self.sql_type} minimum")
            return None, errors
        return value, errors

    def _check_length(self, value: str) -> tuple[str, list[str]]:
        limit = self.length_limit
        if limit is None or len(value) <= limit:
            return value, []
        # PostgreSQL rejects an over-long VARCHAR outright, so the value is cut
        # and the truncation is reported: lossy, but never silent.
        return value[:limit], [
            f"{self.name}: truncated to {limit} chars to fit {self.sql_type}"]


_NUMERIC_RE = re.compile(r"NUMERIC\((\d+)\s*,\s*(\d+)\)", re.I)
_SIZED_TEXT_RE = re.compile(r"(?:VAR)?CHAR\((\d+)\)", re.I)

_ZERO = Decimal(0)


TABLES: dict[str, tuple[Column, ...]] = {
    "product": (
        Column("name", "VARCHAR(255)", required=True, transform="text"),
        Column("model", "VARCHAR(150)", transform="text"),
        Column("sku", "VARCHAR(100)", transform="text"),
        Column("description", "TEXT", transform="text"),
    ),
    "product_image": (
        Column("image_url", "TEXT", required=True, transform="url"),
        Column("alt_text", "VARCHAR(255)", transform="text"),
        Column("sort_order", "INTEGER", default=0, minimum=_ZERO,
               transform="integer"),
    ),
    "brand": (
        Column("name", "VARCHAR(150)", required=True, transform="text"),
        Column("logo_url", "TEXT", transform="url"),
        Column("website_url", "TEXT", transform="url"),
    ),
    "product_category": (
        Column("name", "VARCHAR(150)", required=True, transform="text"),
        Column("description", "TEXT", transform="text"),
    ),
    "product_offer": (
        Column("price", "NUMERIC(12,2)", required=True, minimum=_ZERO,
               transform="price"),
        Column("list_price", "NUMERIC(12,2)", minimum=_ZERO, transform="price"),
        Column("currency", "CHAR(3)", default="CLP", max_length=3,
               transform="currency"),
        Column("shipping_cost", "NUMERIC(12,2)", default=_ZERO, minimum=_ZERO,
               transform="price"),
        Column("shipping_free", "BOOLEAN", default=False, transform="boolean"),
        Column("available", "BOOLEAN", default=True, transform="boolean"),
        Column("stock", "INTEGER", minimum=_ZERO, transform="integer"),
        Column("condition", "VARCHAR(20)", default="new",
               choices=("new", "used", "refurbished"), transform="condition"),
        Column("product_url", "TEXT", transform="url"),
    ),
    "service": (
        Column("name", "VARCHAR(255)", required=True, transform="text"),
        Column("description", "TEXT", transform="text"),
        Column("image_url", "TEXT", transform="url"),
    ),
    "service_category": (
        Column("name", "VARCHAR(150)", required=True, transform="text"),
        Column("description", "TEXT", transform="text"),
    ),
    "service_offer": (
        Column("price", "NUMERIC(12,2)", required=True, minimum=_ZERO,
               transform="price"),
        Column("currency", "CHAR(3)", default="CLP", max_length=3,
               transform="currency"),
        Column("billing_period", "VARCHAR(30)", transform="billing_period"),
        Column("installation_cost", "NUMERIC(12,2)", minimum=_ZERO,
               transform="price"),
        Column("contract_period", "VARCHAR(50)", transform="text"),
        Column("available", "BOOLEAN", default=True, transform="boolean"),
        Column("coverage_summary", "TEXT", transform="text"),
        Column("additional_costs_summary", "TEXT", transform="text"),
        Column("service_url", "TEXT", transform="url"),
    ),
    "store": (
        Column("name", "VARCHAR(150)", required=True, transform="text"),
        Column("website_url", "TEXT", transform="url"),
        Column("logo_url", "TEXT", transform="url"),
        Column("rating", "NUMERIC(3,2)", minimum=_ZERO, transform="number"),
        Column("reputation", "VARCHAR(100)", transform="text"),
        Column("shipping_information", "TEXT", transform="text"),
        Column("general_conditions", "TEXT", transform="text"),
    ),
    "provider": (
        Column("name", "VARCHAR(150)", required=True, transform="text"),
        Column("website_url", "TEXT", transform="url"),
        Column("logo_url", "TEXT", transform="url"),
        Column("rating", "NUMERIC(3,2)", minimum=_ZERO, transform="number"),
        Column("reputation", "VARCHAR(100)", transform="text"),
        Column("general_conditions", "TEXT", transform="text"),
    ),
    "location": (
        Column("country", "VARCHAR(100)", required=True, transform="text"),
        Column("region", "VARCHAR(100)", transform="text"),
        Column("city", "VARCHAR(100)", transform="text"),
        Column("commune", "VARCHAR(100)", transform="text"),
        Column("latitude", "NUMERIC(9,6)", transform="coordinate"),
        Column("longitude", "NUMERIC(9,6)", transform="coordinate"),
    ),
}

# Natural keys a scraper can see but cannot resolve to a server-side id.
REFERENCES: dict[str, tuple[tuple[str, str], ...]] = {
    "product": (("category", "product_category"), ("brand", "brand")),
    "product_offer": (("store", "store"),),
    "service": (("category", "service_category"),),
    "service_offer": (("provider", "provider"),),
}

# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_NOISE_RE = re.compile(r"[^\d,.\-+]")
# Three letters not followed by another letter: matches "USD10" and "$usd",
# but never the first three characters of a spelled-out name like "pesos".
_CURRENCY_RE = re.compile(r"\b([A-Za-z]{3})(?![A-Za-z])")

_TRUE_WORDS = frozenset({
    "1", "true", "t", "yes", "y", "si", "sí", "available", "disponible",
    "activo", "active", "en stock", "instantaneo", "instantáneo",
})
_FALSE_WORDS = frozenset({
    "0", "false", "f", "no", "n", "agotado", "sin stock", "unavailable",
    "indisponible", "inactivo", "disabled", "ultimas unidades",
    "últimas unidades",
})

_ENUM_ALIASES: dict[str, dict[str, str]] = {
    "condition": {
        "nuevo": "new", "nueva": "new", "new": "new",
        "usado": "used", "usada": "used", "used": "used",
        "segunda mano": "used", "reacondicionado": "refurbished",
        "reacondicionada": "refurbished", "refurbished": "refurbished",
        "remate": "refurbished",
    },
    "billing_period": {
        "mensual": "monthly", "monthly": "monthly", "m": "monthly",
        "mes": "monthly", "anual": "yearly", "yearly": "yearly",
        "annual": "yearly", "y": "yearly", "anio": "yearly", "año": "yearly",
        "unico": "one_time", "único": "one_time", "one_time": "one_time",
        "one time": "one_time", "pago unico": "one_time", "pago único": "one_time",
    },
}


def _identity(raw: str, column: Column, base_url: str | None) -> Any:
    return raw


def to_text(raw: str, column: Column, base_url: str | None) -> str:
    """Collapse runs of whitespace and trim."""
    return _WHITESPACE_RE.sub(" ", raw).strip()


def to_number(raw: str, column: Column, base_url: str | None) -> Decimal | None:
    """Parse a human-formatted number, tolerating CLP thousands separators.

    ``"$549.990"`` and ``"549.990,00"`` both become ``549990``. When a lone
    separator is followed by exactly three digits it is read as a thousands
    group (the Chilean convention), otherwise as a decimal point.
    """
    text = _NOISE_RE.sub("", raw)
    if not text or text in {"-", "+", ".", ","}:
        return None

    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        text = (text.replace(".", "").replace(",", ".")
                if text.rfind(",") > text.rfind(".") else text.replace(",", ""))
    elif has_comma:
        text = _resolve_separator(text, ",")
    elif has_dot:
        text = _resolve_separator(text, ".")

    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _resolve_separator(text: str, separator: str) -> str:
    head, _, tail = text.rpartition(separator)
    if head and len(tail) == 3 and head.lstrip("+-").isdigit():
        return text.replace(separator, "")
    return f"{head}.{tail}" if head else tail


def to_integer(raw: str, column: Column, base_url: str | None) -> int | None:
    """Parse a whole number; a fractional part is a read error, not a round."""
    number = to_number(raw, column, base_url)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise ValueError(f"{raw!r} is not a whole number")
    return int(number)


def to_boolean(raw: str, column: Column, base_url: str | None) -> bool | None:
    """Interpret the Spanish and English spellings a storefront actually uses."""
    text = _WHITESPACE_RE.sub(" ", raw).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def to_currency(raw: str, column: Column, base_url: str | None) -> str | None:
    """Upper-case an ISO-4217 alphabetic code (``"$usd"`` -> ``"USD"``)."""
    match = _CURRENCY_RE.search(raw)
    if not match:
        return None
    code = match.group(0).upper()
    return None if len(code) != 3 else code


def to_url(raw: str, column: Column, base_url: str | None) -> str | None:
    """Absolutize a possibly relative URL against the page it was found on."""
    return normalize_url(raw, base_url)


def to_enum(raw: str, column: Column, base_url: str | None) -> str | None:
    """Map a localized label onto the values the target ``CHECK`` allows."""
    text = _WHITESPACE_RE.sub(" ", raw).strip().lower()
    aliases = _ENUM_ALIASES.get(column.name, {})
    if text in aliases:
        return aliases[text]
    if column.choices and text in column.choices:
        return text
    return None


TRANSFORMS = {
    "text": to_text,
    "number": to_number,
    "price": to_number,
    "integer": to_integer,
    "boolean": to_boolean,
    "currency": to_currency,
    "url": to_url,
    "condition": to_enum,
    "billing_period": to_enum,
    "coordinate": to_number,
}

# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reference:
    """A natural key that ingestion must resolve into a server-side id."""

    name: str
    table: str
    selector: str
    mode: str = "text"
    attribute: str | None = None


@dataclass(frozen=True, slots=True)
class Selector:
    """Where one column's value lives in the document."""

    column: Column
    selector: str
    mode: str = "text"
    attribute: str | None = None
    transform: str | None = None
    default: Any = None

    def read(self, nodes: Any) -> list[str]:
        """Pull the raw strings this selector points at.

        ``list`` mode takes every match, the other modes take the first. An
        ``attribute`` always wins over the text, so ``{"mode": "list",
        "attribute": "src"}`` collects every ``src`` under the node.
        """
        if not len(nodes):
            return []
        selected = nodes if self.mode == "list" else nodes[:1]
        if self.attribute:
            return [value for node in selected
                    if (value := node.attrib.get(self.attribute))]
        if self.mode == "html":
            return [node.html_content for node in selected]
        return [node.get_all_text() for node in selected]


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """A table, plus the selectors that fill each of its columns."""

    table: str
    selectors: tuple[Selector, ...]
    root: str | None = None
    references: tuple[Reference, ...] = ()

    @property
    def entity_type(self) -> str:
        return self.table

    @property
    def required_columns(self) -> tuple[str, ...]:
        return tuple(s.column.name for s in self.selectors if s.column.required)

    @classmethod
    def from_parser_config(cls, parser_config: dict | None) -> "EntitySpec":
        """Build a spec from ``SCRAPER_CONFIG.parser_config``.

        ``fields`` maps a column name to either a bare CSS selector or a dict of
        ``selector``/``mode``/``attribute``/``transform``/``default``. Unknown
        columns and unknown tables are rejected loudly: silently ignoring them
        would ship a spec that quietly drops data.
        """
        config = parser_config or {}
        table = str(config.get("entity_type") or "").strip()
        columns = {column.name: column for column in TABLES.get(table, ())}
        if not columns:
            known = ", ".join(sorted(TABLES))
            raise ValueError(
                f"unknown entity_type {table!r}; expected one of: {known}")

        declared = config.get("fields") or {}
        if not isinstance(declared, dict):
            raise ValueError("parser_config['fields'] must be an object")

        selectors: list[Selector] = []
        for name, raw in declared.items():
            column = columns.get(name)
            if column is None:
                known = ", ".join(sorted(columns))
                raise ValueError(
                    f"unknown column {name!r} for {table}; expected: {known}")
            selectors.append(_build_selector(column, raw))

        unknown_refs = set(config.get("references") or ()) - {
            name for name, _ in REFERENCES.get(table, ())}
        if unknown_refs:
            known = ", ".join(name for name, _ in REFERENCES.get(table, ())) or "none"
            raise ValueError(
                f"unknown references {sorted(unknown_refs)} for {table}; "
                f"expected: {known}")

        references = tuple(
            Reference(name=name, table=target,
                      selector=(config["references"][name]))
            for name, target in REFERENCES.get(table, ())
            if name in (config.get("references") or {})
        )

        return cls(
            table=table,
            selectors=tuple(selectors),
            root=_clean_selector(config.get("root")),
            references=references,
        )


def _build_selector(column: Column, raw: Any) -> Selector:
    if isinstance(raw, str):
        return Selector(column=column, selector=raw.strip())
    if not isinstance(raw, dict):
        raise ValueError(
            f"field {column.name!r} must be a selector string or an object")

    selector = _clean_selector(raw.get("selector"))
    if not selector:
        raise ValueError(f"field {column.name!r} is missing a selector")

    mode = str(raw.get("mode") or "text").strip().lower()
    if mode not in {"text", "html", "attr", "list"}:
        raise ValueError(f"field {column.name!r} has unknown mode {mode!r}")
    if mode == "attr" and not raw.get("attribute"):
        raise ValueError(f"field {column.name!r} in attr mode needs an attribute")

    transform = raw.get("transform")
    if transform is not None and transform not in TRANSFORMS:
        known = ", ".join(sorted(TRANSFORMS))
        raise ValueError(
            f"field {column.name!r} has unknown transform {transform!r}; "
            f"expected: {known}")

    return Selector(
        column=column,
        selector=selector,
        mode=mode,
        attribute=raw.get("attribute"),
        transform=transform,
        default=raw.get("default"),
    )


def _clean_selector(raw: Any) -> str:
    return str(raw or "").strip()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Make a value safe for JSONB.

    ``Decimal`` becomes a string: PostgreSQL casts a numeric literal from text
    exactly, whereas a float would already have lost cents by the time it is
    written.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(slots=True)
class Extraction:
    """The outcome of applying one :class:`EntitySpec` to one document."""

    entity_type: str
    values: dict[str, Any] = field(default_factory=dict)
    references: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing was reported against this row."""
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe mapping, ready for ``scraped_data.raw_data``."""
        payload: dict[str, Any] = {"entity_type": self.entity_type}
        payload["values"] = _jsonable(self.values)
        if self.references:
            payload["references"] = _jsonable(self.references)
        if self.errors:
            payload["errors"] = list(self.errors)
        return payload

    def as_jsonb(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class Extractor:
    """Applies an :class:`EntitySpec` to Scrapling documents."""

    def __init__(self, spec: EntitySpec):
        self.spec = spec

    def extract(self, document: Any, base_url: str | None = None) -> Extraction:
        """Extract one row from a document or from a single ``root`` match."""
        return self._extract_from(document, base_url)

    def extract_all(self, document: Any,
                    base_url: str | None = None) -> list[Extraction]:
        """Extract one row per ``root`` match, for listing pages.

        Falls back to a single row when no ``root`` is declared, so the same
        spec serves a detail page and a category page.
        """
        roots = self._roots(document)
        if roots is None:
            return [self._extract_from(document, base_url)]
        return [self._extract_from(node, base_url) for node in roots]

    def _roots(self, document: Any) -> list[Any] | None:
        if not self.spec.root:
            return None
        try:
            return list(document.css(self.spec.root))
        except Exception:
            return None

    def _extract_from(self, node: Any, base_url: str | None) -> Extraction:
        result = Extraction(entity_type=self.spec.entity_type)

        for selector in self.spec.selectors:
            column = self._resolve_column(selector)
            raw_values = self._read(node, selector)
            if not raw_values:
                if selector.default is not None:
                    result.values[column.name] = selector.default
                elif column.required:
                    result.errors.append(f"{column.name}: required value not found")
                continue
            if len(raw_values) > 1 and selector.mode == "list":
                items: list[Any] = []
                for raw in raw_values:
                    value, errors = column.coerce(raw, base_url)
                    result.errors.extend(f"[{raw}] {e}" for e in errors)
                    if value is not None:
                        items.append(value)
                if items:
                    result.values[column.name] = items
                continue

            value, errors = column.coerce(
                raw_values[0], base_url, fallback=selector.default)
            result.errors.extend(errors)
            if value is not None:
                result.values[column.name] = value

        for reference in self.spec.references:
            value = self._read_reference(node, reference)
            if value:
                result.references[reference.name] = value

        return result

    def _resolve_column(self, selector: Selector) -> Column:
        if selector.transform is None:
            return selector.column
        return replace(selector.column, transform=selector.transform)

    def _read(self, node: Any, selector: Selector) -> list[str]:
        try:
            nodes = node.css(selector.selector)
        except Exception:
            return []
        return [value for value in selector.read(nodes) if value]

    def _read_reference(self, node: Any, reference: Reference) -> str | None:
        try:
            nodes = node.css(reference.selector)
        except Exception:
            return None
        if not len(nodes):
            return None
        raw = nodes[0].attrib.get(reference.attribute) if reference.mode == "attr" \
            else nodes[0].get_all_text()
        return to_text(raw, Column(reference.name, "TEXT"), None) or None


def export_jsonl(extractions: list[Extraction], path: str | Path) -> int:
    """Write one JSON object per row, newline delimited.

    This is the handoff format the architecture calls for: JSONL is trivially
    appendable, replayable and bulk-loadable, and it keeps the raw scrape
    recoverable even if a downstream mapping has to be rewritten.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("a", encoding="utf-8") as handle:
        for extraction in extractions:
            handle.write(extraction.as_jsonb() + "\n")
            written += 1
    return written
