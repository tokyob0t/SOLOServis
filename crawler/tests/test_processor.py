from processor import clean_payload, process_links
from utils import normalize_url, parse_rate_limit, same_site


class TestNormalizeUrl:
    def test_resolves_relative(self):
        assert (
            normalize_url("/a/b?q=1", "https://example.com/x/y")
            == "https://example.com/a/b?q=1"
        )

    def test_strips_fragment(self):
        assert normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_lowercases_scheme_and_host(self):
        assert normalize_url("HTTP://EXAMPLE.COM/Path") == "http://example.com/Path"

    def test_drops_default_port(self):
        assert normalize_url("https://example.com:443/") == "https://example.com/"
        assert normalize_url("http://example.com:8080/") == "http://example.com:8080/"

    def test_rejects_non_http_schemes(self):
        for url in ["ftp://example.com", "javascript:void(0)", "mailto:a@b.c", "#anchor", ""]:
            assert normalize_url(url) is None

    def test_empty_path_becomes_slash(self):
        assert normalize_url("https://example.com") == "https://example.com/"

    def test_trailing_slash_dropped_on_non_root(self):
        assert normalize_url("https://example.com/a/") == "https://example.com/a"
        assert normalize_url("https://example.com/") == "https://example.com/"


class TestSameSite:
    def test_same_domain(self):
        assert same_site("https://example.com/a", "https://example.com/b")

    def test_www_is_normalized(self):
        assert same_site("https://www.example.com/", "https://example.com/")

    def test_different_domains(self):
        assert not same_site("https://example.com/", "https://other.org/")

    def test_subdomains_are_not_merged(self):
        assert not same_site("https://sub.example.com/", "https://example.com/")


class TestParseRateLimit:
    def test_per_minute(self):
        assert parse_rate_limit("30/min") == 2.0

    def test_per_second(self):
        assert parse_rate_limit("10/s") == 0.1

    def test_per_hour(self):
        assert parse_rate_limit("3600/hour") == 1.0

    def test_invalid_values_return_zero(self):
        for value in [None, "", "abc", "/min", "0/min", "-5/s", "10 parsecs"]:
            assert parse_rate_limit(value) == 0.0


class TestProcessLinks:
    def test_resolves_and_dedupes(self):
        base = "https://example.com/"
        raw = ["/a", "/a#frag", "https://EXAMPLE.com/a/", "b", "mailto:x@y.z"]
        result = process_links(raw, base)
        assert result == ["https://example.com/a", "https://example.com/b"]

    def test_preserves_order(self):
        result = process_links(["/z", "/a"], "https://example.com/")
        assert result == ["https://example.com/z", "https://example.com/a"]


class TestCleanPayload:
    def test_trims_and_drops_empty_strings(self):
        cleaned = clean_payload({"title": "  hola  ", "desc": "   ", "n": 5})
        assert cleaned == {"title": "hola", "n": 5}

    def test_cleans_nested_and_lists(self):
        cleaned = clean_payload({"meta": {"k": " v "}, "h1": [" a ", "", "b"], "empty_list": []})
        assert cleaned == {"meta": {"k": "v"}, "h1": ["a", "b"]}
