"""Processor: URL normalization pipeline and payload cleaning.

Sits between the scraper output and the persistence layer, so neither of them
needs to know about URL canonicalization rules.
"""

from utils import normalize_url


def process_links(raw_links: list[str], base_url: str) -> list[str]:
    """Resolve, normalize and deduplicate discovered links (order preserved)."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in raw_links:
        normalized = normalize_url(raw, base_url)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def clean_payload(payload: dict) -> dict:
    """Trim whitespace on string values and drop empty top-level entries."""
    cleaned: dict = {}
    for key, value in payload.items():
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        elif isinstance(value, dict):
            value = clean_payload(value)
        elif isinstance(value, (list, tuple)):
            items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if isinstance(value, tuple):
                value = tuple(items)
            else:
                value = items
        if value in ({}, (), []):
            continue
        cleaned[key] = value
    return cleaned
