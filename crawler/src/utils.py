"""Low-level URL helpers and logging utilities."""

import logging
import re
import sys
from urllib.parse import urljoin, urlparse, urlunparse

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

LOG_FORMAT = "%(asctime)s [%(threadName)s] %(levelname)-7s [%(name)s] %(message)s"
LOG_DATEFMT = "%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once; safe to call multiple times."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATEFMT))
        root.addHandler(handler)
        root.setLevel(level)


def get_logger(tag: str) -> logging.Logger:
    """Return a logger whose name renders as a ``[TAG]`` prefix in records."""
    return logging.getLogger(tag)


_RATE_LIMIT_RE = re.compile(
    r"^\s*(\d+)\s*/\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours)\s*$",
    re.I,
)

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
}


def normalize_url(raw_url: str, base_url: str | None = None) -> str | None:
    """Normalize *raw_url* and return it, or ``None`` when it is not crawlable.

    Steps:
      * resolve relative URLs against ``base_url``
      * strip fragments (``#section``)
      * lowercase scheme and host
      * drop default ports (80/443)
      * drop trailing slash on non-root paths
      * only allow ``http``/``https`` schemes
    """
    if not raw_url:
        return None

    candidate = raw_url.strip()
    if base_url:
        candidate = urljoin(base_url, candidate)

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None

    host = parsed.hostname
    if not host:
        return None

    netloc = host.lower()
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        netloc = f"{parsed.username}:{parsed.password}@{netloc}" if parsed.username else netloc
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https"
                                                         and port == 443)
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def same_site(url_a: str, url_b: str) -> bool:
    """Return True when both URLs belong to the same site (ignoring ``www.``)."""

    def host_of(value: str) -> str | None:
        try:
            host = urlparse(value).hostname
        except ValueError:
            return None
        if not host:
            return None
        return host[4:] if host.startswith("www.") else host

    a, b = host_of(url_a), host_of(url_b)
    return a is not None and a == b


def parse_rate_limit(rate_limit: str | None) -> float:
    """Convert a ``"<count>/<unit>"`` string into the minimum interval (seconds).

    Examples: ``"30/min"`` -> ``2.0``, ``"10/s"`` -> ``0.1``.
    Invalid or missing values return ``0.0`` (no limiting).
    """
    if not rate_limit:
        return 0.0
    match = _RATE_LIMIT_RE.match(rate_limit)
    if not match:
        return 0.0
    count, unit = int(match.group(1)), match.group(2).lower()
    if count <= 0:
        return 0.0
    return _UNIT_SECONDS[unit] / count
