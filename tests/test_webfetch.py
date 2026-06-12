import httpx
import pytest

from devagent.webfetch import FetchError, fetch_page_text, html_to_text

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


def test_firecrawl_url_resolution():
    from devagent.settings import Settings

    assert Settings().firecrawl_url is None
    assert Settings(firecrawl_api_key="fc-k").firecrawl_url == "https://api.firecrawl.dev"
    assert Settings(firecrawl_api_url="http://localhost:3002/",
                    ).firecrawl_url == "http://localhost:3002"
