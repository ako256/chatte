"""Built-in Web Search (DuckDuckGo + Wikipedia) and Webpage Extraction with Citations."""

from __future__ import annotations

import concurrent.futures
import html
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any
import html2text
import httpx

DEFAULT_WEB_TIMEOUT = 10.0


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chatte/1.0 Safari/537.36"
)


@dataclass
class Citation:
    index: int
    title: str
    url: str
    snippet: str
    domain: str
    source_engine: str = "DuckDuckGo"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_domain(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return "web"


def _unwrap_ddg_url(raw_url: str) -> str:
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    try:
        parsed = urllib.parse.urlparse(raw_url)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            qs = urllib.parse.parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                return qs["uddg"][0]
    except Exception:
        pass
    return raw_url


def _strip_tags(raw_html: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", raw_html)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def search_duckduckgo(query: str, max_results: int = 5, timeout: float = 8.0) -> list[dict[str, str]]:
    """Query DuckDuckGo HTML & Instant Answer API without requiring any API key."""
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # 1. DuckDuckGo HTML Search
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "us-en"},
            )
            if resp.status_code == 200:
                body = resp.text
                link_matches = re.findall(
                    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    body,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                snippet_matches = re.findall(
                    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|'
                    r'<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>',
                    body,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                for idx, (raw_href, raw_title) in enumerate(link_matches):
                    url = _unwrap_ddg_url(html.unescape(raw_href))
                    if not url.startswith("http") or url in seen_urls:
                        continue
                    title = _strip_tags(raw_title)
                    snippet = ""
                    if idx < len(snippet_matches):
                        s_pair = snippet_matches[idx]
                        snippet = _strip_tags(s_pair[0] or s_pair[1])
                    seen_urls.add(url)
                    results.append(
                        {
                            "title": title or url,
                            "url": url,
                            "snippet": snippet,
                            "source_engine": "DuckDuckGo",
                        }
                    )
                    if len(results) >= max_results:
                        break
    except Exception:
        pass

    # 2. DuckDuckGo Instant Answer JSON API fallback / enrichment
    if len(results) < max_results:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
                resp = client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    abs_url = data.get("AbstractURL") or ""
                    abs_text = data.get("AbstractText") or ""
                    heading = data.get("Heading") or query
                    if abs_url and abs_text and abs_url not in seen_urls:
                        seen_urls.add(abs_url)
                        results.append(
                            {
                                "title": heading,
                                "url": abs_url,
                                "snippet": abs_text,
                                "source_engine": "DuckDuckGo",
                            }
                        )
                    for topic in data.get("RelatedTopics", []):
                        if len(results) >= max_results:
                            break
                        if isinstance(topic, dict) and topic.get("FirstURL") and topic.get("Text"):
                            u = topic["FirstURL"]
                            if u not in seen_urls:
                                seen_urls.add(u)
                                results.append(
                                    {
                                        "title": topic["Text"][:70],
                                        "url": u,
                                        "snippet": topic["Text"],
                                        "source_engine": "DuckDuckGo",
                                    }
                                )
        except Exception:
            pass

    return results[:max_results]


def search_wikipedia(query: str, max_results: int = 3, timeout: float = 8.0) -> list[dict[str, str]]:
    """Query Wikipedia Search API for authoritative encyclopedia references."""
    results: list[dict[str, str]] = []
    headers = {"User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": max_results,
                    "format": "json",
                    "utf8": 1,
                },
            )
            if resp.status_code == 200:
                items = resp.json().get("query", {}).get("search", [])
                for item in items[:max_results]:
                    title = item.get("title", "")
                    snippet = _strip_tags(item.get("snippet", ""))
                    if not title:
                        continue
                    slug = urllib.parse.quote(title.replace(" ", "_"))
                    url = f"https://en.wikipedia.org/wiki/{slug}"
                    results.append(
                        {
                            "title": f"{title} — Wikipedia",
                            "url": url,
                            "snippet": snippet,
                            "source_engine": "Wikipedia",
                        }
                    )
    except Exception:
        pass
    return results


def search_web(
    query: str,
    max_results: int = 6,
    start_index: int = 1,
    timeout: float = DEFAULT_WEB_TIMEOUT,
) -> list[Citation]:
    """Perform combined DuckDuckGo + Wikipedia search within a strict timeout (default 10s)."""
    cleaned_q = (query or "").strip()
    if not cleaned_q:
        return []

    per_req_timeout = min(timeout, 6.0)
    ddg_items: list[dict[str, str]] = []
    wiki_items: list[dict[str, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_ddg = pool.submit(
            search_duckduckgo,
            cleaned_q,
            max(3, max_results - 2),
            per_req_timeout,
        )
        fut_wiki = pool.submit(
            search_wikipedia,
            cleaned_q,
            3,
            per_req_timeout,
        )
        done, not_done = concurrent.futures.wait(
            [fut_ddg, fut_wiki],
            timeout=timeout,
        )
        for f in not_done:
            f.cancel()
        if fut_ddg in done:
            try:
                ddg_items = fut_ddg.result(timeout=0)
            except Exception:
                ddg_items = []
        if fut_wiki in done:
            try:
                wiki_items = fut_wiki.result(timeout=0)
            except Exception:
                wiki_items = []

    combined: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in ddg_items + wiki_items:
        u = item.get("url", "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        combined.append(item)
        if len(combined) >= max_results:
            break

    citations: list[Citation] = []
    for offset, item in enumerate(combined):
        url = item["url"]
        citations.append(
            Citation(
                index=start_index + offset,
                title=item.get("title") or url,
                url=url,
                snippet=item.get("snippet") or "",
                domain=_extract_domain(url),
                source_engine=item.get("source_engine", "Web"),
            )
        )
    return citations


def format_citations_for_prompt(citations: list[Citation]) -> str:
    """Format search citations as numbered blocks for LLM grounding."""
    if not citations:
        return ""
    lines: list[str] = []
    for c in citations:
        lines.append(
            f"[{c.index}] Title: {c.title}\n"
            f"    URL: {c.url} ({c.source_engine})\n"
            f"    Excerpt: {c.snippet}"
        )
    return "\n\n".join(lines)


def fetch_webpage_text(
    url: str,
    max_chars: int = 8000,
    timeout: float = DEFAULT_WEB_TIMEOUT,
) -> tuple[str, str]:
    """Fetch a webpage within a strict 10-second limit and convert its HTML into Markdown."""
    headers = {"User-Agent": USER_AGENT}
    deadline = time.monotonic() + timeout
    http_timeout = httpx.Timeout(timeout, connect=min(5.0, timeout))
    chunks: list[bytes] = []
    total_bytes = 0
    max_bytes = 512_000  # 512 KB cap to prevent huge downloads stalling

    with httpx.Client(timeout=http_timeout, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(chunk_size=16384):
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes >= max_bytes or time.monotonic() >= deadline:
                    break
            encoding = resp.encoding or "utf-8"

    raw_html = b"".join(chunks).decode(encoding, errors="replace")

    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    title = _strip_tags(title_match.group(1)) if title_match else _extract_domain(url)

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0
    md_text = converter.handle(raw_html).strip()
    if len(md_text) > max_chars:
        md_text = md_text[:max_chars] + "\n\n...[Content truncated]..."
    return title, md_text

