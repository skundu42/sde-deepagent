import httpx
import pytest

from sde_deepagent.webfetch import FetchError, fetch_page_text, html_to_text

SAMPLE = """<!DOCTYPE html><html><head><title>Arch Docs</title>
<style>body{color:red}</style><script>var x=1;</script></head>
<body><h1>Architecture</h1><p>The API gateway routes to services.</p>
<div>Deploys use <b>ArgoCD</b>.</div><noscript>enable js</noscript></body></html>"""


def test_html_to_text_strips_chrome():
    title, text = html_to_text(SAMPLE)
    assert title == "Arch Docs"
    assert "API gateway routes" in text
    assert "ArgoCD" in text
    assert "var x=1" not in text and "color:red" not in text
    assert "enable js" not in text


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def patched(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    # keep these tests hermetic: skip the real DNS-based SSRF check
    monkeypatch.setattr("sde_deepagent.webfetch._host_is_internal", lambda host: False)


async def test_fetch_page_text_html(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(
        200, text=SAMPLE, headers={"content-type": "text/html"}))
    title, text = await fetch_page_text("https://docs.example.com/arch")
    assert title == "Arch Docs" and "ArgoCD" in text


async def test_fetch_page_text_failure(monkeypatch):
    _patch_transport(monkeypatch, lambda req: httpx.Response(404))
    with pytest.raises(FetchError):
        await fetch_page_text("https://nope.example.com/")


# ---- firecrawl integration ----

FC_OK = {"success": True, "data": {"markdown": "# Arch\nGateway routes to services.",
                                   "metadata": {"title": "Arch (rendered)"}}}


async def test_firecrawl_preferred_when_configured(monkeypatch):
    def handler(req):
        if req.url.path == "/v2/scrape":
            assert req.headers.get("authorization") == "Bearer fc-key"
            return httpx.Response(200, json=FC_OK)
        raise AssertionError(f"unexpected request: {req.url}")

    _patch_transport(monkeypatch, handler)
    title, text = await fetch_page_text("https://spa.example.com/",
                                        firecrawl_url="http://fc:3002",
                                        firecrawl_key="fc-key")
    assert title == "Arch (rendered)" and "Gateway routes" in text


async def test_firecrawl_v1_fallback_for_old_self_hosted(monkeypatch):
    def handler(req):
        if req.url.path == "/v2/scrape":
            return httpx.Response(404)
        if req.url.path == "/v1/scrape":
            assert "authorization" not in req.headers  # self-hosted: no key needed
            return httpx.Response(200, json=FC_OK)
        raise AssertionError(f"unexpected request: {req.url}")

    _patch_transport(monkeypatch, handler)
    title, _ = await fetch_page_text("https://x.example.com/",
                                     firecrawl_url="http://fc:3002")
    assert title == "Arch (rendered)"


async def test_firecrawl_failure_falls_back_to_builtin(monkeypatch):
    def handler(req):
        if req.url.path in ("/v2/scrape", "/v1/scrape"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=SAMPLE,
                              headers={"content-type": "text/html"})

    _patch_transport(monkeypatch, handler)
    title, text = await fetch_page_text("https://docs.example.com/arch",
                                        firecrawl_url="http://fc:3002")
    assert title == "Arch Docs" and "ArgoCD" in text  # built-in fetcher result


# ---- SSRF guard ----


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://[::1]/",
    "http://0.0.0.0/",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
])
async def test_fetch_rejects_internal_hosts(url):
    with pytest.raises(FetchError):
        await fetch_page_text(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://h/f"])
async def test_fetch_rejects_non_http_scheme(url):
    with pytest.raises(FetchError):
        await fetch_page_text(url)


async def test_fetch_rejects_redirect_to_internal(monkeypatch):
    # a public URL that 302-redirects to an internal host must be blocked at the hop
    real_client = httpx.AsyncClient

    def patched(**kw):
        kw["transport"] = httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"location": "http://127.0.0.1/"}))
        return real_client(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr("sde_deepagent.webfetch._host_is_internal",
                        lambda host: host == "127.0.0.1")
    with pytest.raises(FetchError):
        await fetch_page_text("https://safe.example.com/start")


def test_firecrawl_url_resolution():
    from sde_deepagent.settings import Settings

    assert Settings().firecrawl_url is None
    assert Settings(firecrawl_api_key="fc-k").firecrawl_url == "https://api.firecrawl.dev"
    assert Settings(firecrawl_api_url="http://localhost:3002/",
                    ).firecrawl_url == "http://localhost:3002"
