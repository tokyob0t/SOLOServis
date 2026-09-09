from scrapling.parser import Adaptor

from models import FetchOutcome
from scraper import Scraper

HTML = """
<html>
  <head>
    <title>Mi Página</title>
    <meta name="description" content="Una descripción">
    <meta property="og:url" content="https://example.com/canonical">
    <link rel="icon" href="/static/favicon.png">
    <link rel="apple-touch-icon" href="/touch.png">
  </head>
  <body>
    <h1>Encabezado</h1>
    <a href="/otra">enlace interno</a>
    <a href="https://externo.org/x">enlace externo</a>
    <a href="javascript:void(0)">no crawlable</a>
  </body>
</html>
"""


def _outcome(html=HTML, final_url="https://example.com/pagina"):
    return FetchOutcome(status=200, final_url=final_url, document=Adaptor(html))


class TestParse:
    def test_raw_data_is_full_html(self):
        result = Scraper().parse(_outcome())
        assert result is not None
        assert result.raw_data == HTML

    def test_extracts_raw_links(self):
        result = Scraper().parse(_outcome())
        links = result.links
        assert "/otra" in links
        assert "https://externo.org/x" in links
        assert len(links) == 3

    def test_entity_type_from_config(self):
        scraper = Scraper({"entity_type": "product"})
        result = scraper.parse(_outcome())
        assert result.entity_type == "product"

    def test_external_id_selector(self):
        html = HTML.replace(
            '<meta name="description"',
            '<meta name="article-id" content="abc-123"><meta name="description"',
        )
        scraper = Scraper({"external_id_selector": 'meta[name="article-id"]'})
        result = scraper.parse(_outcome(html))
        assert result.external_id == "abc-123"

    def test_returns_none_on_bad_outcome(self):
        assert Scraper().parse(FetchOutcome(status=None, error="boom")) is None
        assert Scraper().parse(FetchOutcome(status=404, error="HTTP 404")) is None


class TestFetchOutcome:
    def test_ok_property(self):
        assert FetchOutcome(status=200).ok
        assert not FetchOutcome(status=500, error="HTTP 500").ok
        assert not FetchOutcome(status=None, error="timeout").ok