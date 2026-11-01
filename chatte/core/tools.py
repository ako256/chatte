"""Function Calling / Tool Definitions and Safe Execution Engine for Chatte."""

from __future__ import annotations

import ast
import concurrent.futures
import datetime
import json
import math
import operator
import platform
from typing import Any

from chatte.core.web_search import (
    Citation,
    DEFAULT_WEB_TIMEOUT,
    _extract_domain,
    fetch_webpage_text,
    format_citations_for_prompt,
    search_web,
)

TOOL_TIMEOUT_SECONDS = 10.0


BUILTIN_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo and Wikipedia for current facts, "
                "documentation, news, or references. Returns numbered sources [1], [2], etc. "
                "Always cite sources inline using [1], [2] in your answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query keywords or question.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "Fetch and read the text content of a specific HTTP/HTTPS URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full https:// or http:// URL to read.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_math",
            "description": (
                "Evaluate a mathematical or scientific expression accurately using Python's "
                "math library (supports sin, cos, tan, sqrt, log, exp, pi, e, factorial, comb, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Mathematical expression, e.g. 'sqrt(144) + sin(pi / 2)' or '2**32'.",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Get current local date, time, timezone, and Linux/GNOME platform info.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_MATH_NAMES: dict[str, Any] = {
    name: getattr(math, name)
    for name in dir(math)
    if not name.startswith("_")
}
_SAFE_MATH_NAMES.update(
    {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "sum": sum,
    }
)


def safe_eval_math(expression: str) -> float | int:
    """Safely evaluate a mathematical AST expression without `eval()`."""
    expr = (expression or "").strip().replace("^", "**")
    tree = ast.parse(expr, mode="eval")

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPERATORS:
            left = _eval(node.left)
            right = _eval(node.right)
            return _SAFE_OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPERATORS:
            return _SAFE_OPERATORS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name) and node.id in _SAFE_MATH_NAMES:
            return _SAFE_MATH_NAMES[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn_name = node.func.id
            if fn_name in _SAFE_MATH_NAMES and callable(_SAFE_MATH_NAMES[fn_name]):
                args = [_eval(a) for a in node.args]
                return _SAFE_MATH_NAMES[fn_name](*args)
        if isinstance(node, (ast.Tuple, ast.List)):
            return [_eval(elt) for elt in node.elts]
        raise ValueError(f"Unsupported mathematical construct: {ast.dump(node)}")

    return _eval(tree)


def _normalize_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    return u.lower()


def _execute_tool_call_inner(
    tool_name: str,
    arguments_raw: str | dict[str, Any],
    *,
    existing_citations: list[Citation] | None = None,
    timeout: float = TOOL_TIMEOUT_SECONDS,
) -> tuple[str, list[Citation]]:
    if isinstance(arguments_raw, str):
        try:
            args = json.loads(arguments_raw) if arguments_raw.strip() else {}
        except json.JSONDecodeError:
            args = {"query": arguments_raw}
    else:
        args = dict(arguments_raw or {})

    existing_list = list(existing_citations or [])
    url_to_existing: dict[str, Citation] = {
        _normalize_url(c.url): c for c in existing_list if c.url
    }
    next_index = len(existing_list) + 1

    if tool_name == "web_search":
        query = str(args.get("query") or "").strip()
        max_results = int(args.get("max_results") or 5)
        raw_citations = search_web(
            query,
            max_results=max_results,
            start_index=1,
            timeout=min(timeout, DEFAULT_WEB_TIMEOUT),
        )
        if not raw_citations:
            return f"No web results found for query: '{query}'.", []

        new_unique: list[Citation] = []
        prompt_citations: list[Citation] = []
        for rc in raw_citations:
            norm = _normalize_url(rc.url)
            if norm in url_to_existing:
                prompt_citations.append(url_to_existing[norm])
            else:
                dedup_cit = Citation(
                    index=next_index,
                    title=rc.title,
                    url=rc.url,
                    snippet=rc.snippet,
                    domain=rc.domain,
                    source_engine=rc.source_engine,
                )
                next_index += 1
                url_to_existing[norm] = dedup_cit
                new_unique.append(dedup_cit)
                prompt_citations.append(dedup_cit)

        formatted = format_citations_for_prompt(prompt_citations)
        return (
            f"Web search results for '{query}' (cite inline with [1], [2], etc. — do not repeat the same citation number twice in one sentence):\n\n{formatted}",
            new_unique,
        )

    if tool_name == "fetch_webpage":
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return f"Invalid URL '{url}': must start with http:// or https://.", []
        try:
            title, text = fetch_webpage_text(
                url,
                timeout=min(timeout, DEFAULT_WEB_TIMEOUT),
            )
            norm = _normalize_url(url)
            if norm in url_to_existing:
                cit = url_to_existing[norm]
                return f"[{cit.index}] Content of {title} ({url}):\n\n{text}", []
            cit = Citation(
                index=next_index,
                title=title,
                url=url,
                snippet=text[:240].replace("\n", " "),
                domain=_extract_domain(url),
                source_engine="Webpage",
            )
            return f"[{cit.index}] Content of {title} ({url}):\n\n{text}", [cit]
        except Exception as exc:
            return f"Error fetching webpage {url}: {exc}", []

    if tool_name == "calculate_math":
        expr = str(args.get("expression") or "").strip()
        try:
            val = safe_eval_math(expr)
            return json.dumps({"expression": expr, "result": val}), []
        except Exception as exc:
            return json.dumps({"expression": expr, "error": str(exc)}), []

    if tool_name == "get_system_info":
        now = datetime.datetime.now().astimezone()
        info = {
            "iso_datetime": now.isoformat(),
            "local_date": now.strftime("%Y-%m-%d (%A)"),
            "local_time": now.strftime("%H:%M:%S %Z"),
            "os": f"{platform.system()} {platform.release()}",
            "desktop": "GNOME 50 (GTK 4.22 / Libadwaita 1.9)",
            "app_id": "earth.dlc.Chatte",
        }
        return json.dumps(info, indent=2), []

    return f"Unknown tool '{tool_name}'.", []


def execute_tool_call(
    tool_name: str,
    arguments_raw: str | dict[str, Any],
    *,
    existing_citations: list[Citation] | None = None,
    timeout: float = TOOL_TIMEOUT_SECONDS,
) -> tuple[str, list[Citation]]:
    """Execute a tool call within a strict 10-second limit and return (result_text, newly_added_unique_citations)."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        _execute_tool_call_inner,
        tool_name,
        arguments_raw,
        existing_citations=existing_citations,
        timeout=timeout,
    )
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return f"Tool '{tool_name}' timed out after {timeout:.0f} seconds.", []
    except Exception as exc:
        return f"Tool '{tool_name}' failed: {exc}", []
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

