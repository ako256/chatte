"""Direct HTTPX streaming client for OpenAI, Anthropic, Ollama, and llama.cpp endpoints."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
import httpx

from chatte.core.context_manager import (
    compute_kv_slot_id,
    count_tokens,
    format_anthropic_payload,
    format_openai_payload,
)
from chatte.storage.database import ChatSession, MessageNode, ProviderConfig


@dataclass
class StreamResult:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    stopped: bool = False
    error: str | None = None


def split_think_tags(text: str) -> tuple[str, str]:
    """Extract `<think>...</think>` blocks into `(reasoning, visible_content)`."""
    if "<think>" not in text:
        return "", text

    reasoning_parts: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        reasoning_parts.append(match.group(1).strip())
        return ""

    cleaned = re.sub(r"<think>(.*?)</think>", _repl, text, flags=re.DOTALL)
    # Handle an unclosed `<think>` while streaming is still in progress
    if "<think>" in cleaned:
        before, after = cleaned.split("<think>", 1)
        reasoning_parts.append(after.strip())
        cleaned = before
    return "\n\n".join(p for p in reasoning_parts if p), cleaned.lstrip()


class DirectAIClient:
    """Connects directly to OpenAI, Anthropic, Ollama, or llama.cpp servers."""

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    @staticmethod
    def _build_headers(provider: ProviderConfig) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        api_key = provider.api_key
        if provider.provider_type == "anthropic":
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
            if provider.enable_prompt_cache:
                headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        else:
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        for k, v in (provider.custom_headers or {}).items():
            if k and v is not None:
                headers[str(k)] = str(v)
        return headers

    @staticmethod
    def _resolve_endpoint(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith(path):
            return base
        if path.startswith("/v1/") and base.endswith("/v1"):
            return base + path[3:]
        return base + path

    def list_models(self, provider: ProviderConfig) -> list[str]:
        """Fetch available models directly from the configured provider endpoint."""
        headers = self._build_headers(provider)
        headers["Accept"] = "application/json"
        url = self._resolve_endpoint(provider.base_url, "/v1/models")
        models: list[str] = []

        try:
            with httpx.Client(timeout=12.0, follow_redirects=True, headers=headers) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("data", []):
                        if isinstance(item, dict) and item.get("id"):
                            models.append(str(item["id"]))
        except Exception:
            pass

        # Fallback for Ollama native `/api/tags` if `/v1/models` wasn't used
        if not models and provider.provider_type == "ollama":
            try:
                root = re.sub(r"/v1/?$", "", provider.base_url.rstrip("/"))
                with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
                    resp = client.get(f"{root}/api/tags")
                    if resp.status_code == 200:
                        data = resp.json()
                        for m in data.get("models", []):
                            if isinstance(m, dict) and m.get("name"):
                                models.append(str(m["name"]))
            except Exception:
                pass

        if provider.default_model and provider.default_model not in models:
            models.insert(0, provider.default_model)
        return models

    def unload_local_model(self, provider: ProviderConfig, model: str) -> bool:
        """Unload a local model from VRAM/RAM on Ollama or llama.cpp servers."""
        headers = self._build_headers(provider)
        headers["Accept"] = "application/json"
        root = re.sub(r"/v1/?$", "", provider.base_url.rstrip("/"))

        if provider.provider_type == "ollama":
            try:
                with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
                    resp = client.post(
                        f"{root}/api/generate",
                        json={"model": model or provider.default_model, "keep_alive": 0},
                    )
                    return resp.status_code in (200, 204)
            except Exception:
                return False

        if provider.provider_type == "llamacpp":
            try:
                with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
                    for slot_id in range(4):
                        client.post(f"{root}/slots/{slot_id}?action=erase")
                    return True
            except Exception:
                return False

        return False

    def stream_chat(
        self,
        provider: ProviderConfig,
        chat: ChatSession,
        window_messages: list[MessageNode],
        *,
        skills_markdown: str = "",
        memories_text: str = "",
        web_search_context: str = "",
        tools_schema: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> StreamResult:
        """Stream a chat completion from OpenAI/Ollama/llama.cpp or Anthropic."""
        if provider.provider_type == "anthropic":
            return self._stream_anthropic(
                provider=provider,
                chat=chat,
                window_messages=window_messages,
                skills_markdown=skills_markdown,
                memories_text=memories_text,
                web_search_context=web_search_context,
                tools_schema=tools_schema,
                on_delta=on_delta,
                stop_event=stop_event,
            )
        return self._stream_openai(
            provider=provider,
            chat=chat,
            window_messages=window_messages,
            skills_markdown=skills_markdown,
            memories_text=memories_text,
            web_search_context=web_search_context,
            tools_schema=tools_schema,
            on_delta=on_delta,
            stop_event=stop_event,
        )

    def _stream_openai(
        self,
        provider: ProviderConfig,
        chat: ChatSession,
        window_messages: list[MessageNode],
        *,
        skills_markdown: str = "",
        memories_text: str = "",
        web_search_context: str = "",
        tools_schema: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> StreamResult:
        result = StreamResult()
        headers = self._build_headers(provider)
        url = self._resolve_endpoint(provider.base_url, "/v1/chat/completions")
        payload = format_openai_payload(
            provider=provider,
            chat=chat,
            window_messages=window_messages,
            skills_markdown=skills_markdown,
            memories_text=memories_text,
            web_search_context=web_search_context,
            tools_schema=tools_schema,
            stream=True,
        )

        raw_content_parts: list[str] = []
        raw_reasoning_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        err_body = resp.read().decode("utf-8", errors="replace")
                        result.error = f"HTTP {resp.status_code}: {err_body[:500]}"
                        return result

                    content_type = resp.headers.get("content-type", "")
                    if "application/json" in content_type and "text/event-stream" not in content_type:
                        # Non-streaming JSON response fallback
                        body_json = json.loads(resp.read().decode("utf-8", errors="replace"))
                        choices = body_json.get("choices", [])
                        if choices:
                            msg = choices[0].get("message", {})
                            raw_content_parts.append(msg.get("content") or "")
                            if msg.get("reasoning_content"):
                                raw_reasoning_parts.append(str(msg["reasoning_content"]))
                            if msg.get("tool_calls"):
                                result.tool_calls = list(msg["tool_calls"])
                        usage = body_json.get("usage", {})
                        result.prompt_tokens = int(usage.get("prompt_tokens") or 0)
                        result.completion_tokens = int(usage.get("completion_tokens") or 0)
                        details = usage.get("prompt_tokens_details") or {}
                        result.cached_tokens = int(
                            details.get("cached_tokens") or usage.get("cached_tokens") or 0
                        )
                    else:
                        for line in resp.iter_lines():
                            if stop_event and stop_event.is_set():
                                result.stopped = True
                                break
                            if not line:
                                continue
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                            else:
                                continue
                            if data_str == "[DONE]":
                                break
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            # Usage metrics (OpenAI / llama.cpp / DeepSeek)
                            usage = event.get("usage")
                            if isinstance(usage, dict):
                                result.prompt_tokens = int(
                                    usage.get("prompt_tokens") or result.prompt_tokens
                                )
                                result.completion_tokens = int(
                                    usage.get("completion_tokens") or result.completion_tokens
                                )
                                details = usage.get("prompt_tokens_details") or {}
                                result.cached_tokens = int(
                                    details.get("cached_tokens")
                                    or usage.get("prompt_cache_hit_tokens")
                                    or usage.get("cached_tokens")
                                    or result.cached_tokens
                                )

                            choices = event.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}

                            r_chunk = delta.get("reasoning_content") or delta.get("reasoning")
                            if r_chunk:
                                raw_reasoning_parts.append(str(r_chunk))

                            c_chunk = delta.get("content")
                            if c_chunk:
                                raw_content_parts.append(str(c_chunk))

                            for tc_delta in delta.get("tool_calls") or []:
                                idx = int(tc_delta.get("index", 0))
                                entry = tool_calls_by_index.setdefault(
                                    idx,
                                    {
                                        "id": tc_delta.get("id") or f"call_{idx}",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                if tc_delta.get("id"):
                                    entry["id"] = tc_delta["id"]
                                fn_delta = tc_delta.get("function") or {}
                                if fn_delta.get("name"):
                                    entry["function"]["name"] += fn_delta["name"]
                                if fn_delta.get("arguments"):
                                    entry["function"]["arguments"] += fn_delta["arguments"]

                            if on_delta and (c_chunk or r_chunk):
                                joined_raw = "".join(raw_content_parts)
                                think_part, visible_part = split_think_tags(joined_raw)
                                full_reasoning = "\n\n".join(
                                    p for p in ["".join(raw_reasoning_parts), think_part] if p
                                )
                                on_delta(visible_part, full_reasoning)

        except Exception as exc:
            result.error = str(exc)

        joined_raw = "".join(raw_content_parts)
        think_part, visible_part = split_think_tags(joined_raw)
        result.content = visible_part
        result.reasoning_content = "\n\n".join(
            p for p in ["".join(raw_reasoning_parts), think_part] if p
        )
        if tool_calls_by_index and not result.tool_calls:
            result.tool_calls = [
                tool_calls_by_index[k] for k in sorted(tool_calls_by_index.keys())
            ]

        if result.completion_tokens == 0 and (result.content or result.reasoning_content):
            result.completion_tokens = count_tokens(result.content + result.reasoning_content)
        if result.prompt_tokens == 0:
            result.prompt_tokens = sum(count_tokens(m.content) for m in window_messages)

        return result

    def _stream_anthropic(
        self,
        provider: ProviderConfig,
        chat: ChatSession,
        window_messages: list[MessageNode],
        *,
        skills_markdown: str = "",
        memories_text: str = "",
        web_search_context: str = "",
        tools_schema: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> StreamResult:
        result = StreamResult()
        headers = self._build_headers(provider)
        url = self._resolve_endpoint(provider.base_url, "/v1/messages")
        payload = format_anthropic_payload(
            provider=provider,
            chat=chat,
            window_messages=window_messages,
            skills_markdown=skills_markdown,
            memories_text=memories_text,
            web_search_context=web_search_context,
            tools_schema=tools_schema,
            stream=True,
        )

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_blocks: dict[int, dict[str, Any]] = {}

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
                with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        err_body = resp.read().decode("utf-8", errors="replace")
                        result.error = f"HTTP {resp.status_code}: {err_body[:500]}"
                        return result

                    for line in resp.iter_lines():
                        if stop_event and stop_event.is_set():
                            result.stopped = True
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        ev_type = event.get("type")
                        if ev_type == "message_start":
                            msg_obj = event.get("message") or {}
                            usage = msg_obj.get("usage") or {}
                            result.prompt_tokens = int(usage.get("input_tokens") or 0)
                            result.cached_tokens = int(
                                usage.get("cache_read_input_tokens") or 0
                            )
                        elif ev_type == "content_block_start":
                            idx = int(event.get("index", 0))
                            cb = event.get("content_block") or {}
                            if cb.get("type") == "tool_use":
                                tool_blocks[idx] = {
                                    "id": cb.get("id") or f"toolu_{idx}",
                                    "type": "function",
                                    "function": {
                                        "name": cb.get("name") or "",
                                        "arguments": "",
                                    },
                                }
                        elif ev_type == "content_block_delta":
                            idx = int(event.get("index", 0))
                            delta = event.get("delta") or {}
                            d_type = delta.get("type")
                            if d_type == "text_delta":
                                text_parts.append(str(delta.get("text") or ""))
                            elif d_type == "thinking_delta":
                                thinking_parts.append(str(delta.get("thinking") or ""))
                            elif d_type == "input_json_delta" and idx in tool_blocks:
                                tool_blocks[idx]["function"]["arguments"] += str(
                                    delta.get("partial_json") or ""
                                )

                            if on_delta and d_type in ("text_delta", "thinking_delta"):
                                on_delta("".join(text_parts), "".join(thinking_parts))
                        elif ev_type == "message_delta":
                            usage = event.get("usage") or {}
                            result.completion_tokens = int(
                                usage.get("output_tokens") or result.completion_tokens
                            )

        except Exception as exc:
            result.error = str(exc)

        result.content = "".join(text_parts)
        result.reasoning_content = "".join(thinking_parts)
        if tool_blocks:
            result.tool_calls = [tool_blocks[k] for k in sorted(tool_blocks.keys())]
        if result.completion_tokens == 0 and result.content:
            result.completion_tokens = count_tokens(result.content)
        return result

