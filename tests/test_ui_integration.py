"""End-to-end UI & Streaming Integration Tests for Chatte (earth.dlc.Chatte)."""

from __future__ import annotations

import http.server
import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw

from chatte.api.client import DirectAIClient
from chatte.app import ChatteApplication
from chatte.storage.database import ChatDatabase, ProviderConfig
from chatte.ui.dialogs import PreferencesDialog, SkillsDialog
from chatte.ui.window import ChatteWindow


class _MockOpenAIHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.endswith("/v1/models"):
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-4o-native-test", "object": "model"},
                        {"id": "llama-3.3-70b", "object": "model"},
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path.endswith("/v1/chat/completions"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "Deriving Gaussian integral via polar coordinates."
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "Here is the Gaussian integral [1]:\n\n$$\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}$$\n\n```python\nimport math\nprint(math.sqrt(math.pi))\n```"
                            }
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 128,
                        "completion_tokens": 64,
                        "prompt_tokens_details": {"cached_tokens": 96},
                    },
                },
            ]
            for ch in chunks:
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self.send_response(404)
        self.end_headers()


class TestUIIntegration(unittest.TestCase):
    def setUp(self) -> None:
        Adw.init()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp_dir.name) / "ui_test.db")
        self.server = socketserver.TCPServer(("127.0.0.1", 0), _MockOpenAIHandler)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.db.conn.close()
        self.tmp_dir.cleanup()

    def test_direct_streaming_and_window_widgets(self) -> None:
        prov = ProviderConfig(
            id="mock-local",
            name="Mock OpenAI Endpoint",
            provider_type="openai",
            base_url=f"http://127.0.0.1:{self.port}/v1",
            default_model="gpt-4o-native-test",
            enable_prompt_cache=True,
            is_active=True,
        )
        self.db.save_provider(prov, api_key="sk-test-keyring-only")

        client = DirectAIClient()
        models = client.list_models(prov)
        self.assertIn("gpt-4o-native-test", models)

        chat = self.db.create_chat(
            title="Integration Chat",
            provider_id=prov.id,
            model="gpt-4o-native-test",
        )
        u_msg = self.db.add_message(chat.id, "user", "Explain the Gaussian integral.")

        res = client.stream_chat(prov, chat, [u_msg])
        self.assertIsNone(res.error)
        self.assertIn("\\sqrt{\\pi}", res.content)
        self.assertEqual(res.prompt_tokens, 128)
        self.assertEqual(res.cached_tokens, 96)

        # Save response with citations and verify ChatteWindow builds all native widgets
        self.db.add_message(
            chat.id,
            "assistant",
            res.content,
            reasoning_content=res.reasoning_content,
            citations=[
                {
                    "index": 1,
                    "title": "Gaussian Integral — Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Gaussian_integral",
                    "snippet": "The Gaussian integral is the integral of the Gaussian function.",
                    "domain": "en.wikipedia.org",
                }
            ],
            skills_used=["math-latex-expert"],
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            cached_tokens=res.cached_tokens,
        )

        # Edit user message to create a second branch, then switch back to Branch 1
        _b2 = self.db.edit_and_branch(chat.id, u_msg.id, "What is Euler's identity?")
        self.db.add_message(chat.id, "assistant", "$$e^{i\\pi} + 1 = 0$$")
        self.db.switch_branch(chat.id, u_msg.id)

        app = ChatteApplication()
        app.register(None)
        app.do_startup()
        win = ChatteWindow(application=app, db=self.db)
        win._load_chat_into_view(chat.id)

        # Verify PreferencesDialog & SkillsDialog instantiate cleanly
        prefs = PreferencesDialog(db=self.db, ai_client=client, current_chat=win.current_chat)
        self.assertIsNotNone(prefs)
        skills_dlg = SkillsDialog(skills_mgr=win.skills_mgr)
        self.assertIsNotNone(skills_dlg)
        win.destroy()


if __name__ == "__main__":
    unittest.main()
