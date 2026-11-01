"""Unit tests for local SQLite DAG storage, GNOME Keyring API key isolation,
chat renaming, automatic message edit branching, and chat forking.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatte.storage import keyring
from chatte.storage.database import ChatDatabase, ProviderConfig


class TestStorageAndBranching(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_chatte.db"
        self.db = ChatDatabase(self.db_path)

    def tearDown(self) -> None:
        self.db.conn.close()
        self.tmp_dir.cleanup()

    def test_api_key_stored_in_gnome_keyring_not_sqlite(self) -> None:
        """Ensure API keys live strictly in GNOME Keyring (earth.dlc.Chatte.ApiKey) and never in SQLite."""
        cols = [
            r["name"]
            for r in self.db.conn.execute("PRAGMA table_info(providers)").fetchall()
        ]
        self.assertNotIn("api_key", cols)

        prov = ProviderConfig(
            id="test-secret-prov",
            name="Test Secret Provider",
            provider_type="openai",
            base_url="https://api.example.com/v1",
            default_model="gpt-4o",
        )
        self.db.save_provider(prov, api_key="sk-super-secret-keyring-token")

        # Verify SQLite file bytes do NOT contain the secret key
        raw_bytes = self.db_path.read_bytes()
        self.assertNotIn(b"sk-super-secret-keyring-token", raw_bytes)

        # Verify keyring lookup returns the key
        loaded = self.db.get_provider("test-secret-prov")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.api_key, "sk-super-secret-keyring-token")

        # Clean up secret
        keyring.clear_api_key("test-secret-prov")
        self.assertEqual(loaded.api_key, "")

    def test_chat_rename_and_pin(self) -> None:
        chat = self.db.create_chat(title="Initial Title")
        self.db.rename_chat(chat.id, "Renamed Native GNOME Chat")
        self.db.set_chat_pinned(chat.id, True)

        updated = self.db.get_chat(chat.id)
        assert updated is not None
        self.assertEqual(updated.title, "Renamed Native GNOME Chat")
        self.assertTrue(updated.pinned)

    def test_edit_message_auto_branches_and_switches_branches(self) -> None:
        """Editing a message must create a new sibling branch and preserve the old branch."""
        chat = self.db.create_chat(title="Branching Test")

        m1_user = self.db.add_message(chat.id, "user", "What is 2 + 2?")
        m1_asst = self.db.add_message(chat.id, "assistant", "2 + 2 is 4.")
        m2_user = self.db.add_message(chat.id, "user", "Multiply that by 10.")
        m2_asst = self.db.add_message(chat.id, "assistant", "4 * 10 is 40.")

        # Active thread has 4 messages
        thread_orig = self.db.get_active_thread(chat.id)
        self.assertEqual([m.id for m in thread_orig], [m1_user.id, m1_asst.id, m2_user.id, m2_asst.id])

        # Now edit m2_user -> automatically branches from m1_asst onward!
        m2_user_branch2 = self.db.edit_and_branch(
            chat.id,
            original_message_id=m2_user.id,
            new_content="Instead, cube that number.",
        )
        m2_asst_branch2 = self.db.add_message(
            chat.id, "assistant", "4 cubed is 64."
        )

        # Verify m2_user and m2_user_branch2 are siblings sharing parent m1_asst
        siblings = self.db.get_siblings(m2_user.id)
        self.assertEqual([s.id for s in siblings], [m2_user.id, m2_user_branch2.id])

        # Active thread should now follow Branch 2
        thread_b2 = self.db.get_active_thread(chat.id)
        self.assertEqual(
            [m.id for m in thread_b2],
            [m1_user.id, m1_asst.id, m2_user_branch2.id, m2_asst_branch2.id],
        )

        # Switch back to Branch 1 (m2_user) -> should restore downstream m2_asst
        switched = self.db.switch_branch(chat.id, m2_user.id)
        self.assertEqual(
            [m.id for m in switched],
            [m1_user.id, m1_asst.id, m2_user.id, m2_asst.id],
        )

    def test_fork_chat_from_specific_message(self) -> None:
        chat = self.db.create_chat(title="Main Thread")
        m1 = self.db.add_message(chat.id, "user", "First question")
        m2 = self.db.add_message(chat.id, "assistant", "First answer")
        _m3 = self.db.add_message(chat.id, "user", "Second question")

        # Fork chat from m2 onward
        forked = self.db.fork_chat_from_message(chat.id, m2.id, new_title="Forked Thread")
        self.assertEqual(forked.title, "Forked Thread")

        forked_thread = self.db.get_active_thread(forked.id)
        self.assertEqual(len(forked_thread), 2)
        self.assertEqual(forked_thread[0].content, m1.content)
        self.assertEqual(forked_thread[1].content, m2.content)


if __name__ == "__main__":
    unittest.main()

