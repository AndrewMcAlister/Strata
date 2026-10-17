#!/usr/bin/env python3
"""
A web search MCP server for the Strata model's chat page.

WHAT THIS IS

`serve/mcp.py` lets the Strata model call tools from MCP servers listed in
`strata-<model>.json` under `mcp_servers`. This is one such server: two tools, a
web search and a page reader, spoken over stdio as newline-delimited JSON-RPC
2.0 - one message per line, never anything else on stdout, logs on stderr.

Standard library only, on purpose. Strata ships its own MCP server the same way
(`tools/strata_mcp.py`) so it works before `.venv` exists, and a tool that needs
a package installed is a tool that fails the first time it is called.

TOOLS

  web_search    ranked results - title, url, snippet - from DuckDuckGo's HTML
                endpoint, which needs no key. Optionally Brave or Tavily when a
                key is present in the environment.
  fetch_page    the readable text of one page, so a search result can actually be
                read rather than guessed at from its snippet.

WHY fetch_page IS HERE TOO

Search alone returns snippets, and a model that can only read snippets will write
confident answers from them. The pair is what makes the tool honest: search to
find, fetch to check.

    python tools/web_search_mcp.py             # run as a server (stdio)
    python tools/web_search_mcp.py --selftest  # exercise the tools and exit
"""

from __future__ import annotations

import argparse
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"          # the revision Strata asks for
SERVER_NAME = "web-search"
SERVER_VERSION = "1.0.0"


def _force_utf8() -> None:
    """Make stdio UTF-8 regardless of the machine's locale.

    On Windows Python opens stdio with the ANSI code page - cp1252 here - so a
    search result containing an accented letter, a CJK character or an emoji
    raises UnicodeEncodeError and the tool call fails. It fails intermittently,
    on whichever query happens to return such a page, which is the worst way for
    a bug to behave. The MCP wire format is UTF-8, so say so explicitly.

    reconfigure() is 3.7+ and this server needs 3.10+, so it is always present.
    """
    for stream, kwargs in ((sys.stdin, {"errors": "replace"}),
                           (sys.stdout, {"errors": "replace"}),
                           (sys.stderr, {"errors": "replace"})):
        try:
            stream.reconfigure(encoding="utf-8", **kwargs)
        except (AttributeError, ValueError, OSError):
            # A stream that cannot be reconfigured (pytest capture, a pipe
            # wrapper) is not a reason to refuse to run.
            pass


_force_utf8()

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Politeness. The HTML endpoint is not an API and will start refusing if it is
# hammered; one request every few seconds is invisible in a chat and keeps the
# tool working. A single search is one request, so this only ever matters when
# the model calls the tool repeatedly in one answer.
MIN_INTERVAL_S = 1.5
_last_request = [0.0]

DEFAULT_TIMEOUT_S = 25


class BackendUnavailable(RuntimeError):
    """A provider refused this request - a block, a challenge page, an HTTP error.

    Distinct from a bug: the chain moves on to the next provider for this, and
    only reports it if every provider refuses.
    """

MAX_FETCH_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 12_000       # Strata cuts at max_result_chars (20,000) anyway


def log(message: str) -> None:
    """stderr is the server's log. stdout is reserved for JSON-RPC."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _throttle() -> None:
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT_S,
             headers: dict | None = None) -> tuple[int, str, str]:
    """Returns (status, body, content_type). Follows redirects, decodes gzip."""
    _throttle()
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(MAX_FETCH_BYTES)
        if response.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except OSError:
                pass
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, raw.decode(charset, "replace"), \
            response.headers.get("Content-Type", "")


# --------------------------------------------------------------------------
# Search backends
# --------------------------------------------------------------------------

def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<encoded>. Return the real target."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("uddg"):
            return urllib.parse.unquote(query["uddg"][0])
    return href


def search_duckduckgo(query: str, count: int) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    status, body, _ = http_get(url)
    if status != 200:
        raise BackendUnavailable(f"DuckDuckGo HTTP {status}")
    if "anomaly" in body.lower() or "<title>Captcha" in body:
        raise BackendUnavailable("DuckDuckGo served a challenge page")

    # Each result is an <a class="result__a" href="...">title</a> followed
    # somewhere by the snippet. Pair them up by walking the document once.
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'(?P<rest>.*?)(?=<a[^>]+class="result__a"|</body>)', re.S)
    results = []
    for match in pattern.finditer(body):
        title = _strip_tags(match.group("title"))
        link = _unwrap_ddg(html.unescape(match.group("href")))
        snippet_match = re.search(
            r'class="result__snippet"[^>]*>(.*?)</a>', match.group("rest"), re.S)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= count:
            break
    return results


def search_mojeek(query: str, count: int) -> list[dict]:
    url = "https://www.mojeek.com/search?" + urllib.parse.urlencode({"q": query})
    status, body, _ = http_get(url)
    if status != 200:
        raise BackendUnavailable(f"Mojeek HTTP {status}")
    if "<title>Captcha" in body:
        raise BackendUnavailable("Mojeek served a challenge page")

    results = []
    for block in re.findall(r'<li>\s*<h2>.*?</li>', body, re.S):
        link_match = re.search(r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
                               block, re.S)
        if not link_match:
            continue
        snippet_match = re.search(r'<p class="s">(.*?)</p>', block, re.S)
        results.append({
            "title": _strip_tags(link_match.group("title")),
            "url": html.unescape(link_match.group("href")),
            "snippet": _strip_tags(snippet_match.group(1)) if snippet_match else "",
        })
        if len(results) >= count:
            break
    return results


def search_wikipedia(query: str, count: int) -> list[dict]:
    """A last resort with no rate limit and no pretence of being a web index."""
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query,
         "format": "json", "srlimit": max(1, min(count, 20))})
    status, body, _ = http_get(url)
    if status != 200:
        raise BackendUnavailable(f"Wikipedia HTTP {status}")
    data = json.loads(body)
    out = []
    for item in (data.get("query", {}) or {}).get("search", []):
        title = item.get("title", "")
        out.append({
            "title": title,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
                title.replace(" ", "_")),
            "snippet": _strip_tags(item.get("snippet", "")),
        })
    return out


def search_brave(query: str, count: int, key: str) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": max(1, min(count, 20))})
    status, body, _ = http_get(url, headers={"X-Subscription-Token": key,
                                             "Accept": "application/json"})
    if status != 200:
        raise BackendUnavailable(f"Brave HTTP {status}")
    data = json.loads(body)
    results = []
    for item in (data.get("web", {}) or {}).get("results", [])[:count]:
        results.append({"title": _strip_tags(item.get("title", "")),
                        "url": item.get("url", ""),
                        "snippet": _strip_tags(item.get("description", ""))})
    return results


def search_tavily(query: str, count: int, key: str) -> list[dict]:
    payload = json.dumps({"api_key": key, "query": query,
                          "max_results": max(1, min(count, 20))}).encode()
    _throttle()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    return [{"title": item.get("title", ""), "url": item.get("url", ""),
             "snippet": item.get("content", "")}
            for item in data.get("results", [])[:count]]


def provider_chain() -> list[tuple[str, object]]:
    """The order to try. A keyed API first when one exists, then the keyless ones.

    The keyless backends are HTML endpoints, not APIs. They work, and then they
    stop working - a captcha page, or an HTTP 202 challenge - for a while, for
    everyone behind the same address. That is not a bug in this file and cannot
    be fixed in it, so the honest design is to try several and to say plainly
    when all of them are refusing rather than to return nothing.
    """
    chain: list[tuple[str, object]] = []
    brave = os.environ.get("BRAVE_API_KEY", "").strip()
    tavily = os.environ.get("TAVILY_API_KEY", "").strip()
    if brave:
        chain.append(("brave", lambda q, c: search_brave(q, c, brave)))
    if tavily:
        chain.append(("tavily", lambda q, c: search_tavily(q, c, tavily)))
    chain.append(("duckduckgo", search_duckduckgo))
    chain.append(("mojeek", search_mojeek))
    chain.append(("wikipedia", search_wikipedia))
    return chain


def web_search(query: str, count: int = 5) -> tuple[list[dict], str, list[str]]:
    """Search, trying each provider until one answers with results."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is empty")
    count = max(1, min(int(count or 5), 20))

    refusals: list[str] = []
    for name, fn in provider_chain():
        try:
            results = fn(query, count)
        except BackendUnavailable as exc:
            refusals.append(f"{name}: {exc}")
            continue
        except urllib.error.URLError as exc:
            refusals.append(f"{name}: network error ({exc.reason})")
            continue
        except Exception as exc:  # noqa: BLE001 - try the next provider
            refusals.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if results:
            return results, name, refusals
        refusals.append(f"{name}: no results")

    raise RuntimeError(
        "every search provider refused. "
        + "; ".join(refusals)
        + ". The keyless backends are HTML endpoints, not APIs, and they block an "
          "address that asks too often - often for several minutes. Setting "
          "BRAVE_API_KEY (free tier) or TAVILY_API_KEY makes the first provider a "
          "real API and this stops happening.")


# --------------------------------------------------------------------------
# Page reading
# --------------------------------------------------------------------------

def html_to_text(markup: str) -> str:
    """Readable text from a page, without pretending to be a browser engine."""
    markup = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?is)<!--.*?-->", " ", markup)
    # Keep paragraph and heading breaks, or every page becomes one line.
    markup = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", markup)
    text = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def fetch_page(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    url = (url or "").strip()
    if not url:
        raise ValueError("url is empty")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not a fetchable URL: {url}")

    status, body, content_type = http_get(url)
    if status != 200:
        raise RuntimeError(f"HTTP {status} for {url}")

    if "json" in content_type:
        text = body
    elif "html" in content_type or body.lstrip()[:1] == "<":
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
        title = _strip_tags(title_match.group(1)) if title_match else ""
        text = html_to_text(body)
        if title:
            text = f"{title}\n\n{text}"
    else:
        text = body

    max_chars = max(500, min(int(max_chars or DEFAULT_MAX_CHARS), 60_000))
    truncated = len(text) > max_chars
    return {"url": url, "text": text[:max_chars], "truncated": truncated,
            "chars": len(text)}


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web and return ranked results with title, url and snippet. "
            "Use it for anything not in your training data: current events, prices, "
            "versions, documentation, or a fact you are unsure of. Snippets are "
            "short - call fetch_page on a result before relying on its detail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "count": {"type": "integer",
                          "description": "How many results, 1-20. Default 5.",
                          "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": (
            "Fetch one web page and return its readable text. Use it after "
            "web_search when a snippet is not enough, or when given a URL to read. "
            "Long pages are truncated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The page to fetch."},
                "max_chars": {"type": "integer",
                              "description": "Truncate the text at this length. "
                                             "Default 12000."},
            },
            "required": ["url"],
        },
    },
]


def call_tool(name: str, arguments: dict) -> str:
    arguments = arguments or {}
    if name == "web_search":
        results, provider, refusals = web_search(arguments.get("query", ""),
                                                 arguments.get("count", 5))
        lines = [f"{len(results)} result(s) from {provider}:"]
        for index, item in enumerate(results, 1):
            lines.append(f"\n{index}. {item['title']}\n   {item['url']}")
            if item["snippet"]:
                lines.append(f"   {item['snippet']}")
        if refusals:
            # Worth telling the model: it explains a thin result set, and a thin
            # result set is a reason to be less confident rather than more.
            lines.append("\n(providers that refused before this one: "
                         + "; ".join(refusals) + ")")
        return "\n".join(lines)

    if name == "fetch_page":
        page = fetch_page(arguments.get("url", ""), arguments.get("max_chars",
                                                                  DEFAULT_MAX_CHARS))
        note = "\n\n[truncated]" if page["truncated"] else ""
        return f"{page['url']}\n\n{page['text']}{note}"

    raise ValueError(f"unknown tool: {name}")


# --------------------------------------------------------------------------
# JSON-RPC over stdio: one message per line
# --------------------------------------------------------------------------

def send(message: dict) -> None:
    # ensure_ascii stays False so UTF-8 goes out as UTF-8, and the stream is
    # reconfigured at import. json.dumps would otherwise escape every non-ASCII
    # character into \uXXXX, which is legal JSON but hides the bug above rather
    # than fixing it, and makes the wire log unreadable.
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_result(request_id, result) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def send_error(request_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code,
                                                        "message": message}})


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # A notification carries no id and must never be answered.
    if request_id is None and method != "ping":
        return

    if method == "initialize":
        send_result(request_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return

    if method == "ping":
        send_result(request_id, {})
        return

    if method == "tools/list":
        send_result(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            text = call_tool(name, arguments)
            send_result(request_id, {"content": [{"type": "text", "text": text}],
                                     "isError": False})
        except Exception as exc:  # noqa: BLE001 - a failed tool is a result, not a crash
            log(f"{name} failed: {exc}")
            # Returned as a result with isError, which is what the model reads.
            # Raising here would end the chat instead of letting it try again.
            send_result(request_id, {"content": [{"type": "text",
                                                  "text": f"error: {exc}"}],
                                     "isError": True})
        return

    if request_id is not None:
        send_error(request_id, -32601, f"method not found: {method}")


def serve() -> int:
    log(f"ready on stdio (protocol {PROTOCOL}), "
        f"{len(TOOLS)} tools: {', '.join(t['name'] for t in TOOLS)}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            log(f"ignoring a line that is not JSON: {line[:80]!r}")
            continue
        try:
            handle(message)
        except Exception as exc:  # noqa: BLE001 - one bad message must not kill the server
            log(f"error handling {message.get('method')}: {exc}")
    return 0


# --------------------------------------------------------------------------

def selftest() -> int:
    """Exercise both tools for real, so a failure is found here and not in chat."""
    failures = 0

    print("  initialize:", json.dumps({"protocolVersion": PROTOCOL}))
    print(f"  tools: {[t['name'] for t in TOOLS]}")

    print("\n  web_search('python asyncio tutorial', 3)")
    try:
        text = call_tool("web_search", {"query": "python asyncio tutorial", "count": 3})
        print("   ", text.replace("\n", "\n    ")[:600])
    except Exception as exc:
        print(f"    FAILED: {exc}")
        failures += 1

    print("\n  fetch_page('https://example.com', 400)")
    try:
        text = call_tool("fetch_page", {"url": "https://example.com", "max_chars": 400})
        print("   ", text.replace("\n", "\n    ")[:400])
    except Exception as exc:
        print(f"    FAILED: {exc}")
        failures += 1

    print("\n  error handling: fetch_page('not a url')")
    try:
        call_tool("fetch_page", {"url": "ftp://nope"})
        print("    FAILED: should have raised")
        failures += 1
    except Exception as exc:
        print(f"    correctly raised: {exc}")

    print("\n  unknown tool")
    try:
        call_tool("nope", {})
        print("    FAILED: should have raised")
        failures += 1
    except Exception as exc:
        print(f"    correctly raised: {exc}")

    print("\n  provider availability right now")
    for name, fn in provider_chain():
        try:
            r = fn("test", 1)
            print(f"    {name:<12}ok ({len(r)} result(s))")
        except Exception as exc:
            print(f"    {name:<12}unavailable: {str(exc)[:70]}")

    print(f"\n  {'ALL SELFTESTS PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A web search MCP server for Strata.")
    parser.add_argument("--selftest", action="store_true",
                        help="exercise the tools and exit")
    args = parser.parse_args(argv)
    return selftest() if args.selftest else serve()


if __name__ == "__main__":
    raise SystemExit(main())
