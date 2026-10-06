from decimal import Decimal
from pathlib import Path

import pytest
from scrapling.parser import Adaptor

from extractor import (
    TABLES,
    Column,
    EntitySpec,
    Extractor,
    export_jsonl,
    to_boolean,
    to_currency,
    to_integer,
    to_number,
    to_text,
    to_url,
)

PRODUCT_HTML = """
<html><body>
  <div class="card" data-sku="FAL-12345">
    <h2 class="title">Refrigerador Samsung No Frost</h2>
    <span class="brand">Samsung</span>
    <div class="breadcrumb">Electrodomesticos &gt; Refrigeradores</div>
    <p class="price"><span class="current">$549.990</span>
       <span class="was">$599.990</span></p>
    <span class="stock">Disponible</span>
    <span class="condition">Nuevo</span>
    <img class="shot" src="/img/refrigerador.jpg" alt="Vista frontal">
    <img class="shot" src="/img/refrigerador-2.jpg">
    <p class="desc">  Nevera   de 380 litros </p>
  </div>
  <div class="card" data-sku="FAL-99999">
    <h2 class="title">Horno Electrolux</h2>
    <span class="brand">Electrolux</span>
    <p class="price"><span class="current">$129.990</span></p>
    <span class="condition">Usado</span>
  </div>
</body></html>
"""


def _product_spec():
    return EntitySpec.from_parser_config({
        "entity_type": "product",
        "root": ".card",
        "fields": {
            "name": ".title::text",
            "sku": {"selector": ".card", "mode": "attr", "attribute": "data-sku"},
            "description": ".desc::text",
            "model": {"selector": ".missing::text", "default": "sin-modelo"},
        },
        "references": {
            "category": ".breadcrumb::text",
            "brand": ".brand::text",
        },
    })


class TestTransforms:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("$549.990", Decimal("549990")),
        ("549.990,50", Decimal("549990.50")),
        ("1,234.56", Decimal("1234.56")),
        ("5499.90", Decimal("5499.90")),
        ("$0", Decimal("0")),
        ("-12", Decimal("-12")),
    ])
    def test_to_number(self, raw, expected):
        assert to_number(raw, Column("x", "NUMERIC(12,2)"), None) == expected

    def test_to_number_rejects_nonsense(self):
        assert to_number("consultar", Column("x", "NUMERIC(12,2)"), None) is None

    def test_to_integer_rejects_fractions(self):
        with pytest.raises(ValueError):
            to_integer("12.5", Column("x", "INTEGER"), None)

    def test_to_integer_accepts_trailing_zeroes(self):
        assert to_integer("12.0", Column("x", "INTEGER"), None) == 12

    @pytest.mark.parametrize(("raw", "expected"), [
        ("Disponible", True),
        ("sí", True),
        ("Agotado", False),
        ("sin stock", False),
    ])
    def test_to_boolean(self, raw, expected):
        assert to_boolean(raw, Column("x", "BOOLEAN"), None) is expected

    def test_to_boolean_unknown_word(self):
        assert to_boolean("quizá", Column("x", "BOOLEAN"), None) is None

    def test_to_currency(self):
        assert to_currency("usd $10", Column("x", "CHAR(3)"), None) == "USD"
        assert to_currency("10 pesos", Column("x", "CHAR(3)"), None) is None

    def test_to_text_collapses_whitespace(self):
        assert to_text("  Nevera\n  de 380  ", Column("x", "TEXT"), None) \
            == "Nevera de 380"

    def test_to_url_absolutizes(self):
        column = Column("image_url", "TEXT")
        assert to_url("/img/a.jpg", column, "https://falabella.com/p/1") \
            == "https://falabella.com/img/a.jpg"
        assert to_url("mailto:a@b.cl", column, "https://falabella.com") is None


class TestColumnCoercion:
    def test_required_missing_is_an_error(self):
        column = Column("name", "VARCHAR(255)", required=True, transform="text")
        value, errors = column.coerce("   ")
        assert value is None
        assert errors == ["name: required value not found"]

    def test_default_silences_missing_value(self):
        column = Column("condition", "VARCHAR(20)", default="new")
        assert column.coerce("")[0] == "new"

    def test_overflow_is_dropped_and_reported(self):
        column = Column("price", "NUMERIC(12,2)", transform="price")
        value, errors = column.coerce("99999999999999")
        assert value is None
        assert any("overflows" in e for e in errors)

    def test_negative_price_violates_check_constraint(self):
        column = Column("price", "NUMERIC(12,2)", minimum=Decimal(0),
                        transform="price")
        value, errors = column.coerce("-5")
        assert value is None
        assert any("minimum" in e for e in errors)

    def test_overlong_value_is_truncated_and_reported(self):
        column = Column("name", "VARCHAR(10)", transform="text")
        value, errors = column.coerce("Refrigerador Samsung")
        assert value == "Refrigerad"
        assert any("truncated" in e for e in errors)

    def test_scale_comes_from_the_declared_type(self):
        column = TABLES["location"][4]  # latitude NUMERIC(9,6)
        value, _ = column.coerce("-33,426000")
        assert value == Decimal("-33.426000")
        assert str(value) == "-33.426000"

    def test_condition_outside_the_check_constraint_is_rejected(self):
        column = TABLES["product_offer"][8]
        value, errors = column.coerce("seminuevo")
        assert value is None
        assert errors

    def test_spec_default_outranks_the_column_default(self):
        currency = next(c for c in TABLES["product_offer"] if c.name == "currency")
        assert currency.default == "CLP"
        # A site that never prints its currency must not be silently relabelled
        # with the schema-wide default when the spec says otherwise.
        value, errors = currency.coerce("£47.82", fallback="GBP")
        assert value == "GBP"
        assert errors == []

    def test_unreadable_value_falls_back_without_inventing_an_error(self):
        column = Column("name", "VARCHAR(255)", required=True, transform="text")
        value, errors = column.coerce("", fallback="desconocido")
        assert value == "desconocido"
        assert errors == []


class TestEntitySpec:
    def test_bare_string_selector_uses_column_defaults(self):
        spec = _product_spec()
        name = next(s for s in spec.selectors if s.column.name == "name")
        assert name.mode == "text"
        assert name.column.transform == "text"

    def test_unknown_entity_type_is_rejected(self):
        with pytest.raises(ValueError, match="unknown entity_type"):
            EntitySpec.from_parser_config({"entity_type": "widget"})

    def test_unknown_column_is_rejected(self):
        with pytest.raises(ValueError, match="unknown column 'colour'"):
            EntitySpec.from_parser_config({
                "entity_type": "product", "fields": {"colour": ".c"}})

    def test_unknown_transform_is_rejected(self):
        with pytest.raises(ValueError, match="unknown transform"):
            EntitySpec.from_parser_config({
                "entity_type": "product",
                "fields": {"name": {"selector": ".t", "transform": "magic"}}})

    def test_attr_mode_requires_an_attribute(self):
        with pytest.raises(ValueError, match="needs an attribute"):
            EntitySpec.from_parser_config({
                "entity_type": "product",
                "fields": {"sku": {"selector": ".c", "mode": "attr"}}})

    def test_unknown_reference_is_rejected(self):
        with pytest.raises(ValueError, match="unknown references"):
            EntitySpec.from_parser_config({
                "entity_type": "product", "references": {"vendor": ".v"}})

    def test_required_columns_are_exposed(self):
        assert _product_spec().required_columns == ("name",)


class TestExtractor:
    def test_extracts_a_typed_row(self):
        row = Extractor(_product_spec()).extract(Adaptor(PRODUCT_HTML))
        assert row.values["name"] == "Refrigerador Samsung No Frost"
        assert row.values["sku"] == "FAL-12345"
        assert row.values["model"] == "sin-modelo"
        assert row.ok

    def test_natural_keys_become_references_not_ids(self):
        row = Extractor(_product_spec()).extract(Adaptor(PRODUCT_HTML))
        assert row.references == {
            "category": "Electrodomesticos > Refrigeradores",
            "brand": "Samsung",
        }
        assert "category_id" not in row.values

    def test_extract_all_walks_every_root(self):
        rows = Extractor(_product_spec()).extract_all(Adaptor(PRODUCT_HTML))
        assert [r.values["name"] for r in rows] == [
            "Refrigerador Samsung No Frost", "Horno Electrolux"]

    def test_without_a_root_it_returns_one_row(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product", "fields": {"name": ".title::text"}})
        assert len(Extractor(spec).extract_all(Adaptor(PRODUCT_HTML))) == 1

    def test_offer_row_matches_the_schema_contract(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product_offer",
            "fields": {
                "price": ".current::text",
                "list_price": ".was::text",
                "currency": {"selector": ".price", "mode": "attr",
                             "attribute": "data-currency", "default": "CLP"},
                "shipping_free": {"selector": ".missing::text", "default": True},
                "available": ".stock::text",
                "condition": ".condition::text",
            },
            "references": {"store": ".brand::text"},
        })
        document = Adaptor(
            '<div class="price" data-currency="CLP">'
            '<span class="current">$549.990</span>'
            '<span class="was">$599.990</span></div>'
            '<span class="stock">Disponible</span>'
            '<span class="condition">Nuevo</span>'
            '<span class="brand">Falabella</span>')
        row = Extractor(spec).extract(document, "https://falabella.com/p/1")
        assert row.values["price"] == Decimal("549990.00")
        assert row.values["list_price"] == Decimal("599990.00")
        assert row.values["currency"] == "CLP"
        assert row.values["shipping_free"] is True
        assert row.values["available"] is True
        assert row.values["condition"] == "new"
        assert row.references == {"store": "Falabella"}

    def test_missing_required_value_fails_the_row(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product", "fields": {"name": ".nope::text"}})
        row = Extractor(spec).extract(Adaptor(PRODUCT_HTML))
        assert not row.ok
        assert row.values == {}

    def test_list_mode_collects_every_match(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product_image",
            "root": ".card",
            "fields": {
                "image_url": {"selector": "img.shot", "mode": "list",
                              "attribute": "src"},
            }})
        rows = Extractor(spec).extract_all(
            Adaptor(PRODUCT_HTML), "https://falabella.com/p/1")
        assert rows[0].values["image_url"] == [
            "https://falabella.com/img/refrigerador.jpg",
            "https://falabella.com/img/refrigerador-2.jpg",
        ]

    def test_list_mode_defaults_to_text_when_no_attribute(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product_image",
            "root": ".card",
            "fields": {"alt_text": {"selector": ".tag", "mode": "list"}}})
        document = Adaptor(
            '<div class="card"><span class="tag">frontal</span>'
            '<span class="tag">lateral</span></div>')
        rows = Extractor(spec).extract_all(document)
        assert rows[0].values["alt_text"] == ["frontal", "lateral"]

    def test_declared_varchar_length_is_enforced_without_config(self):
        sku = next(c for c in TABLES["product"] if c.name == "sku")
        value, errors = sku.coerce("X" * 250)
        assert len(value) == 100
        assert any("truncated" in e for e in errors)

    def test_bad_selector_never_raises(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product", "fields": {"name": ">>>broken::text"}})
        assert not Extractor(spec).extract(Adaptor(PRODUCT_HTML)).ok


class TestExport:
    def test_decimal_is_serialised_as_text_not_float(self):
        spec = EntitySpec.from_parser_config({
            "entity_type": "product_offer", "fields": {"price": ".c::text"}})
        row = Extractor(spec).extract(Adaptor('<span class="c">549990</span>'))
        assert '"price": "549990.00"' in row.as_jsonb()

    def test_export_jsonl_appends_one_row_per_line(self, tmp_path):
        target = tmp_path / "out" / "products.jsonl"
        spec = _product_spec()
        rows = Extractor(spec).extract_all(Adaptor(PRODUCT_HTML))
        assert export_jsonl(rows, target) == 2
        assert export_jsonl(rows[:1], target) == 1
        lines = Path(target).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("{") and lines[0].endswith("}")

    def test_payload_carries_entity_type_for_ingestion(self):
        row = Extractor(_product_spec()).extract(Adaptor(PRODUCT_HTML))
        payload = row.to_dict()
        assert payload["entity_type"] == "product"
        assert payload["values"]["name"].startswith("Refrigerador")
