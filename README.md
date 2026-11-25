# fetch-proxy

A small self-hosted web-fetching service for AI agents. Agents call `GET /fetch?url=…`, or the
`fetch_page` tool on the MCP endpoint at `/mcp`, and get back clean markdown, so they don't need
to fetch sites directly.

## How it helps with blocked fetches

Most of the time an agent is "blocked" for one of three reasons:

| Cause | What this service does |
|---|---|
| Library user agent (`python-requests`, `node-fetch`, …) gets a 403 | Sends normal browser headers |
| Page is a JavaScript app, so the HTML is an empty shell | Falls back to headless Chromium automatically (`render=auto`) |
| Site distrusts datacenter/cloud IP ranges | Run the service on a machine whose IP the site accepts, or set `UPSTREAM_PROXY` (see below) |

It does **not** solve CAPTCHAs or disguise the browser fingerprint. When a site still refuses,
the response says `"blocked": true` so the agent can move on to another source.

## Run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
FETCH_PROXY_TOKEN=change-me .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8787
```

Or with Docker: `docker build -t fetch-proxy . && docker run -p 8787:8787 -e FETCH_PROXY_TOKEN=change-me fetch-proxy`

## API

`GET /fetch` with header `Authorization: Bearer <token>`

| Param | Default | |
|---|---|---|
| `url` | required | Absolute http(s) URL |
| `format` | `markdown` | `markdown`, `text` or `html` |
| `render` | `auto` | `auto` (browser only if needed), `never`, `always` |
| `wait_ms` | `0` | Extra wait after load, for slow JavaScript apps (browser only) |
| `no_cache` | `false` | Skip the cache |

Response: `{url, status, content_type, via: "http"|"browser", blocked, title, content, cached}`

## MCP endpoint (Claude connectors, Cowork)

The service also exposes the fetcher as an MCP server at `/mcp`, with one tool:
`fetch_page(url, format="markdown", render="auto", wait_ms=0)`. Failed fetches, blocked sites
and HTTP 4xx/5xx come back as tool errors, so the agent can tell and move on.

- **Protocol:** MCP **2026-07-28 or newer only** (stateless Streamable HTTP, JSON responses).
  Clients using the older `initialize` handshake (2025-11-25 and earlier) get a `-32022 Unsupported
  protocol version` error listing the supported versions. Set `MCP_ALLOW_LEGACY=true` to also
  accept them, for example if a client you use hasn't moved to 2026-07-28 yet.
- **Auth:** the same `Authorization: Bearer <FETCH_PROXY_TOKEN>` header as the REST API.
- **Stateless:** there are no sessions, so you can run several instances behind a load balancer.

To add it to Claude (Cowork):
1. Deploy behind HTTPS on port 443. Claude won't connect to a URL with a custom port.
2. **Settings → Connectors → Add custom connector**. URL: `https://<your-host>/mcp`.
   Under **Request headers**, add `Authorization: Bearer <your token>`.
3. Enable the connector in Cowork. Optionally add to your instructions: "When you need to read a
   web page, use the fetch_page tool."

Test it locally with the SDK client:

```python
import asyncio, httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    http = httpx2.AsyncClient(headers={"Authorization": "Bearer change-me"}, timeout=90)
    async with Client(streamable_http_client("http://localhost:8787/mcp", http_client=http),
                      mode="2026-07-28") as c:
        r = await c.call_tool("fetch_page", {"url": "https://example.com"})
        print(r.content[0].text)

asyncio.run(main())
```

## Other agent integration

`agent_tool.py` has a `fetch_page()` helper and a JSON-schema tool definition that call the REST
API, for agents you run yourself.

## Configuration (environment variables)

| Var | Default | |
|---|---|---|
| `FETCH_PROXY_TOKEN` | empty (no auth) | Set this whenever the service is reachable by anything other than you |
| `UPSTREAM_PROXY` | none | Send all traffic (HTTP and browser) through this proxy, e.g. `http://user:pass@host:port` |
| `CACHE_TTL` | `900` | Seconds to cache successful results |
| `MIN_HOST_INTERVAL` | `1.0` | Minimum seconds between requests to the same host |
| `BROWSER_CONCURRENCY` | `3` | Concurrent headless pages |
| `RESPECT_ROBOTS` | `false` | Refuse URLs disallowed by robots.txt |
| `TIMEOUT` / `MAX_BYTES` | `30` / 10 MB | Limits per request |
| `ALLOW_PRIVATE` | `false` | Allow private/internal addresses. Leave off: this is the SSRF guard |
| `CHROMIUM_PATH` | bundled | Use a specific Chromium binary |
| `MCP_ALLOW_LEGACY` | `false` | Also serve MCP clients older than 2026-07-28 |

## Security notes

- Never expose it without a token. It is a URL fetcher, and an open one is abusable.
- Private, loopback, link-local and cloud-metadata addresses are refused on every redirect hop
  and for every request the headless browser makes.
