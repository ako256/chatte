"""Context & Token Management: real-time token counting, sliding context windows,
prompt caching, local model KV cache slot assignment, and idle model auto-unloading.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from chatte.storage.database import ChatSession, MessageNode, ProviderConfig

# Subword / token approximation regex matching BPE splits (words, numbers, punctuation, CJK)
_TOKEN_PATTERN = re.compile(
    r"'s|'t|'re|'ve|'m|'ll|'d| ?[A-Za-z]+| ?\d{1,3}| ?[^\sA-Za-z\d]+|\s+(?!\S)|\s+"
)


def count_tokens(text: str) -> int:
    """Count approximate BPE tokens for real-time UI feedback and sliding windows."""
    if not text:
        return 0
    matches = _TOKEN_PATTERN.findall(text)
    count = 0
    for token in matches:
        length = len(token)
        if length <= 4:
            count += 1
        else:
            # Longer identifiers/words split into ~1 token per 4 chars in cl100k/o200k
            count += (length + 3) // 4
    return max(1, count)


@dataclass
class ContextBreakdown:
    system_tokens: int = 0
    skills_tokens: int = 0
    history_tokens: int = 0
    draft_tokens: int = 0
    tools_tokens: int = 0
    total_tokens: int = 0
    max_tokens: int = 16384
    included_messages: int = 0
    excluded_messages: int = 0
    cached_tokens: int = 0
    window_messages: list[MessageNode] = field(default_factory=list)

    @property
    def usage_fraction(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return min(1.0, self.total_tokens / float(self.max_tokens))


def compute_kv_slot_id(chat_id: str, num_slots: int = 4) -> int:
    """Deterministic KV cache slot assignment for llama.cpp servers per chat."""
    digest = hashlib.sha256(chat_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % max(1, num_slots)


def build_sliding_window(
    chat: ChatSession,
    thread: list[MessageNode],
    *,
    skills_markdown: str = "",
    memories_text: str = "",
    draft_text: str = "",
    tools_schema: list[dict[str, Any]] | None = None,
) -> ContextBreakdown:
    """Apply customizable sliding context window (by max turns and max tokens)."""
    sys_tokens = count_tokens(chat.system_prompt) + count_tokens(memories_text) + 6
    sk_tokens = count_tokens(skills_markdown) + (4 if skills_markdown else 0)
    dr_tokens = count_tokens(draft_text) + (4 if draft_text else 0)
    tl_tokens = (
        count_tokens(json.dumps(tools_schema)) if tools_schema else 0
    )

    max_tokens = max(512, chat.sliding_window_tokens)
    max_turns = max(2, chat.sliding_window_turns)

    # Group messages into atomic turns so tool_calls and their tool results never get split
    groups: list[list[MessageNode]] = []
    current_group: list[MessageNode] = []
    for msg in thread:
        if msg.role == "tool" and current_group:
            current_group.append(msg)
        else:
            if current_group:
                groups.append(current_group)
            current_group = [msg]
    if current_group:
        groups.append(current_group)

    # First apply max_turns limit from the tail
    if len(groups) > max_turns:
        candidate_groups = groups[-max_turns:]
    else:
        candidate_groups = list(groups)

    reserved_tokens = sys_tokens + sk_tokens + dr_tokens + tl_tokens
    budget = max(256, max_tokens - reserved_tokens)

    selected_groups_rev: list[list[MessageNode]] = []
    hist_tokens = 0

    for grp in reversed(candidate_groups):
        grp_tokens = sum(
            count_tokens(m.content)
            + count_tokens(json.dumps(m.tool_calls) if m.tool_calls else "")
            + 4
            for m in grp
        )
        if selected_groups_rev and (hist_tokens + grp_tokens > budget):
            break
        selected_groups_rev.append(grp)
        hist_tokens += grp_tokens

    selected_groups = list(reversed(selected_groups_rev))
    window_msgs = [m for grp in selected_groups for m in grp]
    excluded_count = max(0, len(thread) - len(window_msgs))

    cached_sum = sum(m.cached_tokens for m in window_msgs)
    # If the latest assistant message reported exact prompt_tokens, keep cached_tokens visible
    for m in reversed(thread):
        if m.cached_tokens > 0:
            cached_sum = m.cached_tokens
            break

    total = sys_tokens + sk_tokens + tl_tokens + hist_tokens + dr_tokens
    return ContextBreakdown(
        system_tokens=sys_tokens,
        skills_tokens=sk_tokens,
        history_tokens=hist_tokens,
        draft_tokens=dr_tokens,
        tools_tokens=tl_tokens,
        total_tokens=total,
        max_tokens=max_tokens,
        included_messages=len(window_msgs),
        excluded_messages=excluded_count,
        cached_tokens=cached_sum,
        window_messages=window_msgs,
    )


def format_openai_payload(
    provider: ProviderConfig,
    chat: ChatSession,
    window_messages: list[MessageNode],
    *,
    skills_markdown: str = "",
    memories_text: str = "",
    web_search_context: str = "",
    tools_schema: list[dict[str, Any]] | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """Build OpenAI / Ollama / llama.cpp chat completions payload with prefix & KV caching."""
    system_parts = [chat.system_prompt.strip()]
    if memories_text.strip():
        system_parts.append(f"## User Local Memories\n{memories_text.strip()}")
    if skills_markdown.strip():
        system_parts.append(f"## Active On-Demand Skills\n{skills_markdown.strip()}")
    if web_search_context.strip():
        system_parts.append(
            "## Live Web Search Results (Cite sources inline using [1], [2], etc.)\n"
            + web_search_context.strip()
        )

    full_system = "\n\n".join(p for p in system_parts if p)
    messages_payload: list[dict[str, Any]] = []
    if full_system:
        messages_payload.append({"role": "system", "content": full_system})

    # Find the index of the last user message so reasoning_content is preserved
    # for intermediate tool-call assistant steps in the active turn (DeepSeek / OpenAI spec)
    last_user_idx = -1
    for idx, msg in enumerate(window_messages):
        if msg.role == "user":
            last_user_idx = idx

    for idx, msg in enumerate(window_messages):
        if msg.role == "assistant" and msg.tool_calls:
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": msg.tool_calls,
            }
            if idx > last_user_idx and msg.reasoning_content:
                entry["reasoning_content"] = msg.reasoning_content
            messages_payload.append(entry)
        elif msg.role == "tool":
            messages_payload.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "call_0",
                    "content": msg.content,
                }
            )
        else:
            messages_payload.append(
                {
                    "role": msg.role,
                    "content": msg.content,
                }
            )

    payload: dict[str, Any] = {
        "model": chat.model,
        "messages": messages_payload,
        "stream": stream,
    }
    if stream and provider.provider_type == "openai":
        payload["stream_options"] = {"include_usage": True}

    if tools_schema:
        payload["tools"] = tools_schema
        payload["tool_choice"] = "auto"

    # Local model KV cache reuse & keep_alive support
    if provider.provider_type == "llamacpp" and provider.enable_kv_cache:
        payload["cache_prompt"] = True
        payload["id_slot"] = compute_kv_slot_id(chat.id)
    elif provider.provider_type == "ollama":
        if provider.enable_kv_cache and provider.idle_unload_minutes > 0:
            payload["keep_alive"] = f"{provider.idle_unload_minutes}m"
        elif provider.idle_unload_minutes == 0:
            payload["keep_alive"] = "5m"

    return payload


def format_anthropic_payload(
    provider: ProviderConfig,
    chat: ChatSession,
    window_messages: list[MessageNode],
    *,
    skills_markdown: str = "",
    memories_text: str = "",
    web_search_context: str = "",
    tools_schema: list[dict[str, Any]] | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """Build Anthropic `/v1/messages` payload with `cache_control` prompt caching."""
    use_cache = provider.enable_prompt_cache
    system_blocks: list[dict[str, Any]] = []

    base_sys = chat.system_prompt.strip()
    if memories_text.strip():
        base_sys += f"\n\n## User Local Memories\n{memories_text.strip()}"
    if base_sys:
        block: dict[str, Any] = {"type": "text", "text": base_sys}
        if use_cache and not skills_markdown.strip():
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(block)

    if skills_markdown.strip():
        skill_block: dict[str, Any] = {
            "type": "text",
            "text": f"## Active On-Demand Skills\n{skills_markdown.strip()}",
        }
        if use_cache:
            skill_block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(skill_block)

    if web_search_context.strip():
        system_blocks.append(
            {
                "type": "text",
                "text": (
                    "## Live Web Search Results (Cite sources inline using [1], [2], etc.)\n"
                    + web_search_context.strip()
                ),
            }
        )

    anth_messages: list[dict[str, Any]] = []
    user_indices: list[int] = []

    for msg in window_messages:
        if msg.role == "user":
            content_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": msg.content or " "}
            ]
            user_indices.append(len(anth_messages))
            anth_messages.append({"role": "user", "content": content_blocks})
        elif msg.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_input = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    parsed_input = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", "toolu_0"),
                        "name": fn.get("name", "tool"),
                        "input": parsed_input,
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": " "})
            anth_messages.append({"role": "assistant", "content": blocks})
        elif msg.role == "tool":
            tool_res_block = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id or "toolu_0",
                "content": msg.content,
            }
            if anth_messages and anth_messages[-1]["role"] == "user":
                anth_messages[-1]["content"].append(tool_res_block)
            else:
                anth_messages.append({"role": "user", "content": [tool_res_block]})

    # Add ephemeral cache breakpoint on the penultimate user turn for multi-turn prefix caching
    if use_cache and len(user_indices) >= 2:
        penultimate_idx = user_indices[-2]
        content_list = anth_messages[penultimate_idx]["content"]
        if content_list and isinstance(content_list[-1], dict):
            content_list[-1]["cache_control"] = {"type": "ephemeral"}

    payload: dict[str, Any] = {
        "model": chat.model,
        "max_tokens": 4096,
        "messages": anth_messages,
        "stream": stream,
    }
    if system_blocks:
        payload["system"] = system_blocks

    if tools_schema:
        anth_tools = []
        for idx, t in enumerate(tools_schema):
            fn = t.get("function", {})
            tool_def: dict[str, Any] = {
                "name": fn.get("name", f"tool_{idx}"),
                "description": fn.get("description", ""),
                "input_schema": fn.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            if use_cache and idx == len(tools_schema) - 1:
                tool_def["cache_control"] = {"type": "ephemeral"}
            anth_tools.append(tool_def)
        payload["tools"] = anth_tools

    return payload


class IdleModelMonitor:
    """Tracks local model usage and automatically unloads idle models after timeout."""

    def __init__(
        self,
        unload_callback: Callable[[ProviderConfig, str], bool],
        on_unloaded_ui: Callable[[str], None] | None = None,
    ) -> None:
        self._unload_callback = unload_callback
        self._on_unloaded_ui = on_unloaded_ui
        # provider_id -> (ProviderConfig, model_name, last_active_timestamp, is_loaded)
        self._active_models: dict[str, tuple[ProviderConfig, str, float, bool]] = {}

    def record_activity(self, provider: ProviderConfig, model: str) -> None:
        """Mark a local provider model as active right now."""
        if provider.provider_type in ("ollama", "llamacpp"):
            self._active_models[provider.id] = (provider, model, time.time(), True)

    def mark_unloaded(self, provider_id: str) -> None:
        if provider_id in self._active_models:
            prov, model, ts, _ = self._active_models[provider_id]
            self._active_models[provider_id] = (prov, model, ts, False)

    def get_status(self, provider: ProviderConfig) -> dict[str, Any]:
        """Return current load/idle countdown status for UI display."""
        if provider.provider_type not in ("ollama", "llamacpp"):
            return {"local": False, "loaded": False, "remaining_seconds": 0}
        entry = self._active_models.get(provider.id)
        if not entry or not entry[3]:
            return {"local": True, "loaded": False, "remaining_seconds": 0}
        prov, model, last_ts, loaded = entry
        timeout_sec = max(0, prov.idle_unload_minutes * 60)
        if timeout_sec == 0:
            return {"local": True, "loaded": True, "model": model, "remaining_seconds": -1}
        elapsed = time.time() - last_ts
        remaining = max(0, int(timeout_sec - elapsed))
        return {
            "local": True,
            "loaded": loaded,
            "model": model,
            "remaining_seconds": remaining,
        }

    def check_and_unload_expired(self, now: float | None = None) -> list[str]:
        """Check all tracked local models and unload any that exceeded idle_unload_minutes."""
        current_time = now if now is not None else time.time()
        unloaded_ids: list[str] = []

        for pid, (prov, model, last_ts, loaded) in list(self._active_models.items()):
            if not loaded or prov.idle_unload_minutes <= 0:
                continue
            timeout_sec = prov.idle_unload_minutes * 60
            if current_time - last_ts >= timeout_sec:
                try:
                    self._unload_callback(prov, model)
                except Exception:
                    pass
                self._active_models[pid] = (prov, model, last_ts, False)
                unloaded_ids.append(pid)
                if self._on_unloaded_ui:
                    self._on_unloaded_ui(f"Unloaded idle local model '{model}' ({prov.name})")

        return unloaded_ids

