"""URL fetching for resource ingestion. Two strategies:

1. Firecrawl (preferred when configured) — self-hosted instance or cloud API.
   Renders JavaScript and returns clean markdown, so SPAs and docs portals
   extract properly.
2. Built-in stdlib fetcher (always available, used as fallback) — plain HTTP
   GET + HTML-to-text. No JS rendering; static pages work well.

Supermemory's own web extractor isn't used: self-hosted it depends on
third-party services we can't rely on, so devagent always feeds it text."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 150_000
MAX_REDIRECTS = 5
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "iframe", "template"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2",
              "h3", "h4", "h5", "h6", "pre", "blockquote", "td", "th"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()
        elif self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, readable text) for an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML: keep what we got
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return parser.title, text.strip()


class FetchError(RuntimeError):
    pass


def _host_is_internal(host: str) -> bool:
    """True if `host` is (or resolves to) a non-routable address we must not
    fetch on a user's behalf — loopback, private, link-local (incl. the cloud
    metadata endpoint 169.254.169.254), reserved, multicast or unspecified.

    Unresolvable hosts are treated as safe to attempt: there is no IP to reach,
    so no SSRF, and the fetch will simply fail. A host that DNS-resolves to an
    internal IP (the real SSRF vector) is resolved here and blocked."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


async def _ensure_public_url(url: str) -> None:
    """Reject non-HTTP(S) URLs and any targeting internal infrastructure."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"URL has no host: {url}")
    if await asyncio.to_thread(_host_is_internal, host):
        raise FetchError(f"refusing to fetch internal/non-routable address: {host}")


async def _get_guarding_redirects(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET following redirects manually so every hop is SSRF-validated. httpx's
    own follow_redirects would jump straight to an internal redirect target."""
    for _ in range(MAX_REDIRECTS + 1):
        await _ensure_public_url(url)
        resp = await client.get(url)
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(str(resp.url), resp.headers["location"])
            continue
        return resp
    raise FetchError(f"too many redirects fetching {url}")


async def firecrawl_scrape(url: str, base_url: str, api_key: str | None,
                           timeout: float = 90.0) -> tuple[str, str]:
    """Scrape via a Firecrawl instance (v2 API, falling back to v1 for older
    self-hosted deployments). Returns (title, markdown)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_error = "no scrape endpoint found"
    async with httpx.AsyncClient(timeout=timeout) as client:
        for path in ("/v2/scrape", "/v1/scrape"):
            try:
                resp = await client.post(f"{base_url}{path}", headers=headers,
                                         json={"url": url, "formats": ["markdown"]})
            except httpx.HTTPError as e:
                raise FetchError(f"firecrawl unreachable at {base_url}: {e}") from e
            if resp.status_code == 404:
                continue  # older self-hosted versions only serve /v1
            if resp.status_code >= 300:
                last_error = f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}"
                break
            body = resp.json()
            data = body.get("data") or {}
            markdown = data.get("markdown") or ""
            if not markdown.strip():
                last_error = f"{path} returned no markdown for {url}"
                break
            title = (data.get("metadata") or {}).get("title") or ""
            return title, markdown[:MAX_CONTENT_CHARS]
    raise FetchError(f"firecrawl scrape failed: {last_error}")


async def fetch_page_text(url: str, timeout: float = 30.0,
                          firecrawl_url: str | None = None,
                          firecrawl_key: str | None = None) -> tuple[str, str]:
    """Fetch a URL and return (title, text). Prefers Firecrawl when configured,
    falling back to the built-in fetcher. Raises FetchError when both fail."""
    await _ensure_public_url(url)  # SSRF guard for user-submitted resource URLs
    if firecrawl_url:
        try:
            return await firecrawl_scrape(url, firecrawl_url, firecrawl_key)
        except FetchError as e:
            logger.warning("%s: falling back to built-in fetcher", e)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False,
            headers={"User-Agent": "sde-deepagent-resource-ingest/1.0"},
        ) as client:
            resp = await _get_guarding_redirects(client, url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise FetchError(f"could not fetch {url}: {e}") from e

    ctype = resp.headers.get("content-type", "")
    body = resp.text
    if "html" in ctype or body.lstrip()[:1] == "<":
        title, text = html_to_text(body)
    else:  # markdown, plain text, json, ...
        title, text = "", body
    if not text.strip():
        raise FetchError(f"no extractable text at {url} (content-type: {ctype})")
    return title, text[:MAX_CONTENT_CHARS]
