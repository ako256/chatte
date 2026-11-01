"""Unit tests for On-Demand Markdown Skills, Function Calling Tools,
and Web Search Citation formatting.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chatte.api.client import split_think_tags
from chatte.core.skills import SkillsManager
from chatte.core.tools import execute_tool_call, safe_eval_math
from chatte.core.web_search import Citation, format_citations_for_prompt


class TestSkillsToolsAndCitations(unittest.TestCase):
    def test_skills_manager_seeds_and_loads_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SkillsManager(Path(tmp))
            skills = mgr.list_skills()
            self.assertGreaterEqual(len(skills), 4)

            custom = mgr.save_skill(
                slug="rust-safety",
                name="Rust Borrow Checker Guru",
                description="Zero-copy idioms and lifetimes",
                instructions="Always explain ownership and lifetimes clearly.",
            )
            self.assertEqual(custom.slug, "rust-safety")

            loaded_md = mgr.load_active_skills_markdown(["rust-safety", "code-architect"])
            self.assertIn("Rust Borrow Checker Guru", loaded_md)
            self.assertIn("Code Architect", loaded_md)

    def test_builtin_tools_calculator_and_system_info(self) -> None:
        self.assertAlmostEqual(safe_eval_math("sqrt(144) + 2**3"), 20.0)
        out_calc, cits = execute_tool_call(
            "calculate_math", {"expression": "factorial(5) + 5"}
        )
        self.assertEqual(cits, [])
        self.assertEqual(json.loads(out_calc)["result"], 125)

        out_sys, _ = execute_tool_call("get_system_info", {})
        sys_data = json.loads(out_sys)
        self.assertEqual(sys_data["app_id"], "earth.dlc.Chatte")

    def test_citation_formatting_and_think_tags(self) -> None:
        cits = [
            Citation(
                index=1,
                title="GNOME 50 Release Notes",
                url="https://release.gnome.org/50/",
                snippet="GNOME 50 introduces modern Libadwaita enhancements.",
                domain="release.gnome.org",
                source_engine="DuckDuckGo",
            )
        ]
        prompt_block = format_citations_for_prompt(cits)
        self.assertIn("[1] Title: GNOME 50 Release Notes", prompt_block)
        self.assertIn("https://release.gnome.org/50/", prompt_block)

        reasoning, visible = split_think_tags(
            "<think>First let us derive the integral.</think>The result is $\\sqrt{\\pi}$."
        )
        self.assertEqual(reasoning, "First let us derive the integral.")
        self.assertEqual(visible, "The result is $\\sqrt{\\pi}$.")

    def test_tool_execution_timeout_enforced(self) -> None:
        import time
        from unittest.mock import patch

        def slow_fetch(*_args, **_kwargs):
            time.sleep(0.5)
            return "Slow", "Content"

        with patch("chatte.core.tools.fetch_webpage_text", side_effect=slow_fetch):
            out, cits = execute_tool_call(
                "fetch_webpage",
                {"url": "https://example.com"},
                timeout=0.08,
            )
            self.assertIn("timed out", out)
            self.assertEqual(cits, [])


if __name__ == "__main__":
    unittest.main()

