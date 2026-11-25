"""Drop-in helper + tool definition for your agents."""
import os, httpx

PROXY = os.environ.get("FETCH_PROXY_URL", "http://localhost:8787")
TOKEN = os.environ.get("FETCH_PROXY_TOKEN", "")

def fetch_page(url: str, format: str = "markdown", render: str = "auto") -> str:
    r = httpx.get(f"{PROXY}/fetch", params={"url": url, "format": format, "render": render},
                  headers={"Authorization": f"Bearer {TOKEN}"}, timeout=90)
    if r.status_code != 200:
        return f"ERROR {r.status_code}: {r.json().get('detail')}"
    d = r.json()
    if d["blocked"]:
        return f"BLOCKED (HTTP {d['status']}): this site refused automated access. Try another source."
    return f"# {d.get('title') or d['url']}\nSource: {d['url']}\n\n{d['content']}"

# JSON-schema tool definition (works with Claude tool use / most agent frameworks)
FETCH_TOOL = {
    "name": "fetch_page",
    "description": "Fetch a web page and return its main content as markdown. "
                   "Handles JavaScript-rendered pages automatically.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL"},
            "format": {"type": "string", "enum": ["markdown", "text", "html"], "default": "markdown"},
        },
        "required": ["url"],
    },
}
