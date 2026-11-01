"""Unit tests for Context & Token Management, Sliding Windows, Prompt Caching,
Local KV Cache Slot Reuse, and Idle Model Auto-Unloading.
"""

from __future__ import annotations

import time
import unittest

from chatte.core.context_manager import (
    IdleModelMonitor,
    build_sliding_window,
    count_tokens,
    format_anthropic_payload,
    format_openai_payload,
)
from chatte.storage.database import ChatSession, MessageNode, ProviderConfig


class TestContextAndCache(unittest.TestCase):
    def test_token_counting_and_sliding_window(self) -> None:
        self.assertGreater(count_tokens("Hello world, this is Chatte on GNOME 50!"), 5)

        chat = ChatSession(
            id="chat-1",
            title="Window Test",
            provider_id="openai-default",
            model="gpt-4o",
            system_prompt="You are a concise assistant.",
            sliding_window_tokens=120,
            sliding_window_turns=3,
        )

        messages = [
            MessageNode(id=f"m{i}", chat_id="chat-1", parent_id=None, selected_child_id=None, role="user" if i % 2 == 0 else "assistant", content=f"Turn {i} content with several words.")
            for i in range(6)
        ]

        bd = build_sliding_window(chat, messages, draft_text="New draft question")
        self.assertLessEqual(bd.included_messages, 3)
        self.assertGreaterEqual(bd.excluded_messages, 3)
        self.assertEqual(bd.window_messages[-1].id, "m5")

    def test_anthropic_prompt_caching_breakpoints(self) -> None:
        prov = ProviderConfig(
            id="anth",
            name="Anthropic",
            provider_type="anthropic",
            base_url="https://api.anthropic.com/v1",
            default_model="claude-3-7-sonnet-latest",
            enable_prompt_cache=True,
        )
        chat = ChatSession(
            id="c1",
            title="Test",
            provider_id="anth",
            model="claude-3-7-sonnet-latest",
            system_prompt="Base system prompt",
        )
        msgs = [
            MessageNode(id="1", chat_id="c1", parent_id=None, selected_child_id="2", role="user", content="Turn 1"),
            MessageNode(id="2", chat_id="c1", parent_id="1", selected_child_id="3", role="assistant", content="Resp 1"),
            MessageNode(id="3", chat_id="c1", parent_id="2", selected_child_id=None, role="user", content="Turn 2"),
        ]
        payload = format_anthropic_payload(
            prov,
            chat,
            msgs,
            skills_markdown="### Skill: Code Architect\nWrite clean code.",
        )
        # System skill block should have ephemeral cache_control
        self.assertEqual(
            payload["system"][-1].get("cache_control"),
            {"type": "ephemeral"},
        )
        # Penultimate user message should have ephemeral cache_control
        first_user_block = payload["messages"][0]["content"][-1]
        self.assertEqual(first_user_block.get("cache_control"), {"type": "ephemeral"})

    def test_llamacpp_kv_cache_and_idle_model_auto_unload(self) -> None:
        llamacpp_prov = ProviderConfig(
            id="local-llama",
            name="llama.cpp",
            provider_type="llamacpp",
            base_url="http://localhost:8080/v1",
            default_model="qwen2.5",
            enable_kv_cache=True,
            idle_unload_minutes=5,
        )
        chat = ChatSession(
            id="chat-kv",
            title="KV Test",
            provider_id="local-llama",
            model="qwen2.5",
            system_prompt="System",
        )
        msgs = [
            MessageNode(id="1", chat_id="chat-kv", parent_id=None, selected_child_id=None, role="user", content="Hi")
        ]
        payload = format_openai_payload(llamacpp_prov, chat, msgs)
        self.assertTrue(payload.get("cache_prompt"))
        self.assertIn("id_slot", payload)

        unloaded_Log: list[tuple[str, str]] = []

        def _mock_unload(prov: ProviderConfig, model: str) -> bool:
            unloaded_Log.append((prov.id, model))
            return True

        monitor = IdleModelMonitor(unload_callback=_mock_unload)
        monitor.record_activity(llamacpp_prov, "qwen2.5")

        # Not expired yet at +60s
        expired = monitor.check_and_unload_expired(now=time.time() + 60)
        self.assertEqual(expired, [])

        # Expired after 6 minutes (> 5m)
        expired = monitor.check_and_unload_expired(now=time.time() + 360)
        self.assertEqual(expired, ["local-llama"])
        self.assertEqual(unloaded_Log, [("local-llama", "qwen2.5")])


if __name__ == "__main__":
    unittest.main()

