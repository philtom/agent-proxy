"""
fetch-proxy: a small web-fetching service for AI agents.

Agents call  GET /fetch?url=...  instead of fetching sites directly. The service:
  * sends normal browser request headers (many sites reject default library user agents)
  * falls back to a real headless Chromium when a page needs JavaScript or the
    plain request is refused
  * returns raw HTML or clean extracted text/markdown (much cheaper for LLM context)
  * caches responses, rate-limits per host, and blocks requests to private networks
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
import urllib.robotparser
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from cachetools import TTLCache
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------- config ----
TOKEN = os.environ.get("FETCH_PROXY_TOKEN", "")            # empty = no auth (local use only)
UPSTREAM_PROXY = os.environ.get("UPSTREAM_PROXY") or None  # e.g. http://user:pass@host:port
CACHE_TTL = int(os.environ.get("CACHE_TTL", "900"))        # seconds
MIN_HOST_INTERVAL = float(os.environ.get("MIN_HOST_INTERVAL", "1.0"))  # seconds between hits per host
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(10 * 1024 * 1024)))
TIMEOUT = float(os.environ.get("TIMEOUT", "30"))
BROWSER_CONCURRENCY = int(os.environ.get("BROWSER_CONCURRENCY", "3"))
RESPECT_ROBOTS = os.environ.get("RESPECT_ROBOTS", "false").lower() == "true"
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "false").lower() == "true"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
RETRY_WITH_BROWSER = {401, 403, 429, 503}
JS_HINTS = ("enable javascript", "javascript is required", "please turn javascript on",
            "checking your browser", "just a moment")

# ----------------------------------------------------------------- state ----
cache: TTLCache = TTLCache(maxsize=2000, ttl=CACHE_TTL)
robots_cache: TTLCache = TTLCache(maxsize=500, ttl=3600)
host_last_hit: dict[str, float] = {}
host_locks: dict[str, asyncio.Lock] = {}
state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["http"] = httpx.AsyncClient(
        headers=BROWSER_HEADERS, timeout=TIMEOUT, proxy=UPSTREAM_PROXY,
        follow_redirects=False, http2=False,
    )
    state["browser_sem"] = asyncio.Semaphore(BROWSER_CONCURRENCY)
    state["pw"] = state["browser"] = None
    yield
    await state["http"].aclose()
    if state["browser"]:
        await state["browser"].close()
    if state["pw"]:
        await state["pw"].stop()


app = FastAPI(title="fetch-proxy", lifespan=lifespan)


def check_auth(authorization: str | None = Header(default=None)):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "missing or bad bearer token")


# --------------------------------------------------------------- helpers ----
def assert_public_url(url: str) -> None:
    """Reject non-http(s) URLs and hosts that resolve to private/internal addresses (SSRF guard)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "only absolute http(s) URLs are allowed")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(502, f"cannot resolve host {p.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(403, f"refusing to fetch non-public address {ip}")


async def polite_wait(host: str) -> None:
    lock = host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        wait = MIN_HOST_INTERVAL - (time.monotonic() - host_last_hit.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        host_last_hit[host] = time.monotonic()


async def robots_allows(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    rp = robots_cache.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = await state["http"].get(base + "/robots.txt", follow_redirects=True)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        robots_cache[base] = rp
    return rp.can_fetch(UA, url)


async def fetch_plain(url: str) -> dict:
    client: httpx.AsyncClient = state["http"]
    for _ in range(10):  # follow redirects manually so every hop gets the SSRF check
        assert_public_url(url)
        await polite_wait(urlparse(url).hostname)
        async with client.stream("GET", url) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers["location"])
                continue
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise HTTPException(413, "response too large")
            return {
                "url": str(r.url), "status": r.status_code,
                "content_type": r.headers.get("content-type", ""),
                "html": body.decode(r.encoding or "utf-8", errors="replace"),
                "via": "http",
            }
    raise HTTPException(508, "too many redirects")


async def get_browser():
    if state["browser"] is None:
        from playwright.async_api import async_playwright
        state["pw"] = await async_playwright().start()
        launch = {"proxy": {"server": UPSTREAM_PROXY}} if UPSTREAM_PROXY else {}
        exe = os.environ.get("CHROMIUM_PATH")
        if exe:
            launch["executable_path"] = exe
        state["browser"] = await state["pw"].chromium.launch(**launch)
    return state["browser"]


async def fetch_browser(url: str, wait_ms: int) -> dict:
    assert_public_url(url)
    await polite_wait(urlparse(url).hostname)
    async with state["browser_sem"]:
        browser = await get_browser()
        ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                        viewport={"width": 1366, "height": 900})

        # Re-check every request the page makes so JS can't pivot to internal hosts.
        async def guard(route):
            try:
                assert_public_url(route.request.url)
                await route.continue_()
            except HTTPException:
                await route.abort()
        await ctx.route(lambda u: u.startswith("http"), guard)
        # Skip heavy assets we never read.
        await ctx.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,mp4,webm}", lambda r: r.abort())

        try:
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(wait_ms + 5000, 15000))
            except Exception:
                pass
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            return {
                "url": page.url, "status": resp.status if resp else 0,
                "content_type": (resp.headers.get("content-type", "") if resp else "text/html"),
                "html": await page.content(), "via": "browser",
            }
        finally:
            await ctx.close()


def looks_js_dependent(result: dict) -> bool:
    if "html" not in result["content_type"]:
        return False
    html = result["html"]
    text = trafilatura.extract(html) or ""
    low = html[:20000].lower()
    return len(text) < 200 or any(h in low for h in JS_HINTS)


def is_blocked(result: dict) -> bool:
    return result["status"] in RETRY_WITH_BROWSER or (
        result["status"] == 200 and any(h in result["html"][:20000].lower() for h in JS_HINTS[3:])
    )


# ------------------------------------------------------------------- API ----
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/fetch", dependencies=[Depends(check_auth)])
async def fetch(
    url: str = Query(..., description="Absolute http(s) URL"),
    format: Literal["markdown", "text", "html"] = "markdown",
    render: Literal["auto", "never", "always"] = "auto",
    wait_ms: int = Query(0, ge=0, le=15000, description="Extra wait after load (browser only)"),
    no_cache: bool = False,
):
    key = (url, format, render)
    if not no_cache and key in cache:
        return {**cache[key], "cached": True}

    if RESPECT_ROBOTS and not await robots_allows(url):
        raise HTTPException(403, "disallowed by robots.txt")

    result = None
    if render != "always":
        try:
            result = await fetch_plain(url)
        except httpx.HTTPError as e:
            if render == "never":
                raise HTTPException(502, f"fetch failed: {e!r}")
    if render == "always" or (
        render == "auto" and (result is None or is_blocked(result) or looks_js_dependent(result))
    ):
        try:
            result = await fetch_browser(url, wait_ms)
        except HTTPException:
            raise
        except Exception as e:
            if result is None:
                raise HTTPException(502, f"browser fetch failed: {e}")

    out = {k: result[k] for k in ("url", "status", "content_type", "via")}
    out["blocked"] = is_blocked(result)
    ctype = result["content_type"]
    if format == "html" or "html" not in ctype:
        out["content"] = result["html"]
    else:
        meta = trafilatura.extract_metadata(result["html"])
        out["title"] = meta.title if meta else None
        out["content"] = trafilatura.extract(
            result["html"], url=result["url"], output_format="markdown" if format == "markdown" else "txt",
            include_links=format == "markdown", include_tables=True, favor_recall=True,
        ) or ""

    if result["status"] < 400 and not out["blocked"]:
        cache[key] = out
    return JSONResponse({**out, "cached": False}, status_code=200)
