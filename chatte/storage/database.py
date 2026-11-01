"""Zero-tracking local SQLite database with tree-based message branching."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatte.storage import keyring


def get_data_dir() -> Path:
    """Return the local data directory for Chatte, creating it with 0700 permissions."""
    override = os.environ.get("CHATTE_DATA_DIR")
    if override:
        base = Path(override)
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            base = Path(xdg_data) / "chatte"
        else:
            base = Path.home() / ".local" / "share" / "chatte"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


@dataclass
class ProviderConfig:
    id: str
    name: str
    provider_type: str  # "openai" | "anthropic" | "ollama" | "llamacpp"
    base_url: str
    default_model: str
    custom_headers: dict[str, str] = field(default_factory=dict)
    enable_prompt_cache: bool = True
    enable_kv_cache: bool = True
    idle_unload_minutes: int = 15
    is_active: bool = False

    @property
    def api_key(self) -> str:
        """Fetch API key exclusively from GNOME Keyring."""
        return keyring.lookup_api_key(self.id)

    def set_api_key(self, key: str) -> bool:
        """Save API key exclusively to GNOME Keyring."""
        return keyring.store_api_key(self.id, self.name, key)


@dataclass
class ChatSession:
    id: str
    title: str
    provider_id: str
    model: str
    system_prompt: str
    sliding_window_tokens: int = 16384
    sliding_window_turns: int = 40
    active_leaf_id: str | None = None
    pinned: bool = False
    web_search_enabled: bool = False
    tools_enabled: bool = True
    active_skills: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class MessageNode:
    id: str
    chat_id: str
    parent_id: str | None
    selected_child_id: str | None
    role: str  # "user" | "assistant" | "tool"
    content: str
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    created_at: float = 0.0


class ChatDatabase:
    """Local SQLite storage engine supporting DAG/tree message branching."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            self.db_path = get_data_dir() / "chatte.db"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        self._init_schema()
        self._seed_defaults()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL DEFAULT 'openai',
                base_url TEXT NOT NULL,
                default_model TEXT NOT NULL,
                custom_headers_json TEXT NOT NULL DEFAULT '{}',
                enable_prompt_cache INTEGER NOT NULL DEFAULT 1,
                enable_kv_cache INTEGER NOT NULL DEFAULT 1,
                idle_unload_minutes INTEGER NOT NULL DEFAULT 15,
                is_active INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model TEXT NOT NULL,
                system_prompt TEXT NOT NULL DEFAULT 'You are Chatte, a helpful, accurate, and thoughtful native GNOME AI assistant. Use Markdown and LaTeX ($...$ for inline, $$...$$ for display math) when appropriate.',
                sliding_window_tokens INTEGER NOT NULL DEFAULT 16384,
                sliding_window_turns INTEGER NOT NULL DEFAULT 40,
                active_leaf_id TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                web_search_enabled INTEGER NOT NULL DEFAULT 0,
                tools_enabled INTEGER NOT NULL DEFAULT 1,
                active_skills_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                parent_id TEXT,
                selected_child_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                reasoning_content TEXT NOT NULL DEFAULT '',
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                tool_call_id TEXT,
                citations_json TEXT NOT NULL DEFAULT '[]',
                skills_used_json TEXT NOT NULL DEFAULT '[]',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_messages_parent_id ON messages(chat_id, parent_id);

            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                fact TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def _seed_defaults(self) -> None:
        cur = self.conn.cursor()
        row = cur.execute("SELECT COUNT(*) AS cnt FROM providers").fetchone()
        if row and row["cnt"] > 0:
            return

        default_providers = [
            (
                "openai-default",
                "OpenAI / Compatible API",
                "openai",
                "https://api.openai.com/v1",
                "gpt-4o",
                "{}",
                1,
                0,
                0,
                1,
            ),
            (
                "anthropic-default",
                "Anthropic Claude API",
                "anthropic",
                "https://api.anthropic.com/v1",
                "claude-3-7-sonnet-latest",
                "{}",
                1,
                0,
                0,
                0,
            ),
            (
                "ollama-local",
                "Ollama (Local Server)",
                "ollama",
                "http://localhost:11434/v1",
                "llama3.2",
                "{}",
                1,
                1,
                15,
                0,
            ),
            (
                "llamacpp-local",
                "llama.cpp (Local Server)",
                "llamacpp",
                "http://localhost:8080/v1",
                "local-model",
                "{}",
                1,
                1,
                15,
                0,
            ),
        ]
        cur.executemany(
            """
            INSERT INTO providers (
                id, name, provider_type, base_url, default_model,
                custom_headers_json, enable_prompt_cache, enable_kv_cache,
                idle_unload_minutes, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            default_providers,
        )
        self.conn.commit()

    # -------------------------------------------------------------------------
    # Providers
    # -------------------------------------------------------------------------

    def list_providers(self) -> list[ProviderConfig]:
        rows = self.conn.execute(
            "SELECT * FROM providers ORDER BY is_active DESC, name ASC"
        ).fetchall()
        return [self._row_to_provider(r) for r in rows]

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        row = self.conn.execute(
            "SELECT * FROM providers WHERE id = ?", (provider_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_provider(row)

    def get_active_provider(self) -> ProviderConfig:
        row = self.conn.execute(
            "SELECT * FROM providers WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        if not row:
            row = self.conn.execute("SELECT * FROM providers LIMIT 1").fetchone()
        return self._row_to_provider(row)

    def set_active_provider(self, provider_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE providers SET is_active = 0")
            self.conn.execute(
                "UPDATE providers SET is_active = 1 WHERE id = ?", (provider_id,)
            )

    def save_provider(
        self,
        provider: ProviderConfig,
        api_key: str | None = None,
    ) -> ProviderConfig:
        with self.conn:
            if provider.is_active:
                self.conn.execute("UPDATE providers SET is_active = 0")
            self.conn.execute(
                """
                INSERT INTO providers (
                    id, name, provider_type, base_url, default_model,
                    custom_headers_json, enable_prompt_cache, enable_kv_cache,
                    idle_unload_minutes, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    provider_type = excluded.provider_type,
                    base_url = excluded.base_url,
                    default_model = excluded.default_model,
                    custom_headers_json = excluded.custom_headers_json,
                    enable_prompt_cache = excluded.enable_prompt_cache,
                    enable_kv_cache = excluded.enable_kv_cache,
                    idle_unload_minutes = excluded.idle_unload_minutes,
                    is_active = excluded.is_active
                """,
                (
                    provider.id,
                    provider.name,
                    provider.provider_type,
                    provider.base_url.rstrip("/"),
                    provider.default_model.strip() or "gpt-4o",
                    json.dumps(provider.custom_headers or {}),
                    int(provider.enable_prompt_cache),
                    int(provider.enable_kv_cache),
                    int(provider.idle_unload_minutes),
                    int(provider.is_active),
                ),
            )
        if api_key is not None:
            provider.set_api_key(api_key)
        return provider

    def delete_provider(self, provider_id: str) -> None:
        keyring.clear_api_key(provider_id)
        with self.conn:
            self.conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            remaining = self.conn.execute(
                "SELECT id FROM providers WHERE is_active = 1"
            ).fetchone()
            if not remaining:
                first = self.conn.execute("SELECT id FROM providers LIMIT 1").fetchone()
                if first:
                    self.conn.execute(
                        "UPDATE providers SET is_active = 1 WHERE id = ?",
                        (first["id"],),
                    )

    @staticmethod
    def _row_to_provider(row: sqlite3.Row) -> ProviderConfig:
        try:
            headers = json.loads(row["custom_headers_json"] or "{}")
        except json.JSONDecodeError:
            headers = {}
        return ProviderConfig(
            id=row["id"],
            name=row["name"],
            provider_type=row["provider_type"],
            base_url=row["base_url"],
            default_model=row["default_model"],
            custom_headers=headers,
            enable_prompt_cache=bool(row["enable_prompt_cache"]),
            enable_kv_cache=bool(row["enable_kv_cache"]),
            idle_unload_minutes=int(row["idle_unload_minutes"]),
            is_active=bool(row["is_active"]),
        )

    # -------------------------------------------------------------------------
    # Chats
    # -------------------------------------------------------------------------

    def create_chat(
        self,
        title: str = "New Chat",
        provider_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        sliding_window_tokens: int = 16384,
        sliding_window_turns: int = 40,
    ) -> ChatSession:
        active_prov = (
            self.get_provider(provider_id) if provider_id else None
        ) or self.get_active_provider()
        now = time.time()
        chat_id = str(uuid.uuid4())
        default_sys = (
            system_prompt
            if system_prompt is not None
            else (
                "You are Chatte, a helpful, accurate, and thoughtful native GNOME AI assistant. "
                "Format code in fenced code blocks with language identifiers and format mathematical "
                "expressions using LaTeX ($...$ for inline math and $$...$$ for display math)."
            )
        )
        chat = ChatSession(
            id=chat_id,
            title=title,
            provider_id=active_prov.id,
            model=model or active_prov.default_model,
            system_prompt=default_sys,
            sliding_window_tokens=sliding_window_tokens,
            sliding_window_turns=sliding_window_turns,
            active_leaf_id=None,
            pinned=False,
            web_search_enabled=False,
            tools_enabled=True,
            active_skills=[],
            created_at=now,
            updated_at=now,
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO chats (
                    id, title, provider_id, model, system_prompt,
                    sliding_window_tokens, sliding_window_turns, active_leaf_id,
                    pinned, web_search_enabled, tools_enabled, active_skills_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat.id,
                    chat.title,
                    chat.provider_id,
                    chat.model,
                    chat.system_prompt,
                    chat.sliding_window_tokens,
                    chat.sliding_window_turns,
                    chat.active_leaf_id,
                    int(chat.pinned),
                    int(chat.web_search_enabled),
                    int(chat.tools_enabled),
                    json.dumps(chat.active_skills),
                    chat.created_at,
                    chat.updated_at,
                ),
            )
        return chat

    def list_chats(self, search_query: str = "") -> list[ChatSession]:
        q = (search_query or "").strip()
        if q:
            like = f"%{q}%"
            rows = self.conn.execute(
                """
                SELECT DISTINCT c.* FROM chats c
                LEFT JOIN messages m ON m.chat_id = c.id
                WHERE c.title LIKE ? OR m.content LIKE ?
                ORDER BY c.pinned DESC, c.updated_at DESC
                """,
                (like, like),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM chats ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
        return [self._row_to_chat(r) for r in rows]

    def get_chat(self, chat_id: str) -> ChatSession | None:
        row = self.conn.execute(
            "SELECT * FROM chats WHERE id = ?", (chat_id,)
        ).fetchone()
        return self._row_to_chat(row) if row else None

    def rename_chat(self, chat_id: str, new_title: str) -> None:
        cleaned = (new_title or "").strip() or "Untitled Chat"
        with self.conn:
            self.conn.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
                (cleaned, time.time(), chat_id),
            )

    def set_chat_pinned(self, chat_id: str, pinned: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE chats SET pinned = ? WHERE id = ?",
                (int(pinned), chat_id),
            )

    def update_chat_settings(
        self,
        chat_id: str,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        sliding_window_tokens: int | None = None,
        sliding_window_turns: int | None = None,
        web_search_enabled: bool | None = None,
        tools_enabled: bool | None = None,
        active_skills: list[str] | None = None,
    ) -> ChatSession | None:
        chat = self.get_chat(chat_id)
        if not chat:
            return None
        if provider_id is not None:
            chat.provider_id = provider_id
        if model is not None:
            chat.model = model
        if system_prompt is not None:
            chat.system_prompt = system_prompt
        if sliding_window_tokens is not None:
            chat.sliding_window_tokens = max(512, int(sliding_window_tokens))
        if sliding_window_turns is not None:
            chat.sliding_window_turns = max(2, int(sliding_window_turns))
        if web_search_enabled is not None:
            chat.web_search_enabled = bool(web_search_enabled)
        if tools_enabled is not None:
            chat.tools_enabled = bool(tools_enabled)
        if active_skills is not None:
            chat.active_skills = list(active_skills)

        with self.conn:
            self.conn.execute(
                """
                UPDATE chats SET
                    provider_id = ?,
                    model = ?,
                    system_prompt = ?,
                    sliding_window_tokens = ?,
                    sliding_window_turns = ?,
                    web_search_enabled = ?,
                    tools_enabled = ?,
                    active_skills_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    chat.provider_id,
                    chat.model,
                    chat.system_prompt,
                    chat.sliding_window_tokens,
                    chat.sliding_window_turns,
                    int(chat.web_search_enabled),
                    int(chat.tools_enabled),
                    json.dumps(chat.active_skills),
                    time.time(),
                    chat.id,
                ),
            )
        return self.get_chat(chat_id)

    def delete_chat(self, chat_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self.conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    @staticmethod
    def _row_to_chat(row: sqlite3.Row) -> ChatSession:
        try:
            skills = json.loads(row["active_skills_json"] or "[]")
        except json.JSONDecodeError:
            skills = []
        return ChatSession(
            id=row["id"],
            title=row["title"],
            provider_id=row["provider_id"],
            model=row["model"],
            system_prompt=row["system_prompt"],
            sliding_window_tokens=int(row["sliding_window_tokens"]),
            sliding_window_turns=int(row["sliding_window_turns"]),
            active_leaf_id=row["active_leaf_id"],
            pinned=bool(row["pinned"]),
            web_search_enabled=bool(row["web_search_enabled"]),
            tools_enabled=bool(row["tools_enabled"]),
            active_skills=skills,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    # -------------------------------------------------------------------------
    # Tree-Structured Messages & Branching
    # -------------------------------------------------------------------------

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        parent_id: str | None = "__USE_ACTIVE_LEAF__",
        reasoning_content: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        citations: list[dict[str, Any]] | None = None,
        skills_used: list[str] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> MessageNode:
        chat = self.get_chat(chat_id)
        if not chat:
            raise ValueError(f"Chat {chat_id} not found")

        if parent_id == "__USE_ACTIVE_LEAF__":
            resolved_parent_id = chat.active_leaf_id
        else:
            resolved_parent_id = parent_id

        now = time.time()
        msg_id = str(uuid.uuid4())
        node = MessageNode(
            id=msg_id,
            chat_id=chat_id,
            parent_id=resolved_parent_id,
            selected_child_id=None,
            role=role,
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls or [],
            tool_call_id=tool_call_id,
            citations=citations or [],
            skills_used=skills_used or [],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            created_at=now,
        )

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO messages (
                    id, chat_id, parent_id, selected_child_id, role, content,
                    reasoning_content, tool_calls_json, tool_call_id,
                    citations_json, skills_used_json, prompt_tokens,
                    completion_tokens, cached_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.id,
                    node.chat_id,
                    node.parent_id,
                    node.selected_child_id,
                    node.role,
                    node.content,
                    node.reasoning_content,
                    json.dumps(node.tool_calls),
                    node.tool_call_id,
                    json.dumps(node.citations),
                    json.dumps(node.skills_used),
                    node.prompt_tokens,
                    node.completion_tokens,
                    node.cached_tokens,
                    node.created_at,
                ),
            )
            if resolved_parent_id:
                self.conn.execute(
                    "UPDATE messages SET selected_child_id = ? WHERE id = ?",
                    (node.id, resolved_parent_id),
                )
            self.conn.execute(
                "UPDATE chats SET active_leaf_id = ?, updated_at = ? WHERE id = ?",
                (node.id, now, chat_id),
            )
        return node

    def update_message(
        self,
        message_id: str,
        *,
        content: str | None = None,
        reasoning_content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cached_tokens: int | None = None,
    ) -> MessageNode | None:
        node = self.get_message(message_id)
        if not node:
            return None
        if content is not None:
            node.content = content
        if reasoning_content is not None:
            node.reasoning_content = reasoning_content
        if tool_calls is not None:
            node.tool_calls = tool_calls
        if citations is not None:
            node.citations = citations
        if prompt_tokens is not None:
            node.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            node.completion_tokens = completion_tokens
        if cached_tokens is not None:
            node.cached_tokens = cached_tokens

        with self.conn:
            self.conn.execute(
                """
                UPDATE messages SET
                    content = ?,
                    reasoning_content = ?,
                    tool_calls_json = ?,
                    citations_json = ?,
                    prompt_tokens = ?,
                    completion_tokens = ?,
                    cached_tokens = ?
                WHERE id = ?
                """,
                (
                    node.content,
                    node.reasoning_content,
                    json.dumps(node.tool_calls),
                    json.dumps(node.citations),
                    node.prompt_tokens,
                    node.completion_tokens,
                    node.cached_tokens,
                    node.id,
                ),
            )
            self.conn.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (time.time(), node.chat_id),
            )
        return node

    def get_message(self, message_id: str) -> MessageNode | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return self._row_to_message(row) if row else None

    def get_active_thread(self, chat_id: str) -> list[MessageNode]:
        """Return the ordered list of messages along the active branch of `chat_id`."""
        chat = self.get_chat(chat_id)
        if not chat or not chat.active_leaf_id:
            return []

        chain: list[MessageNode] = []
        visited: set[str] = set()
        curr_id: str | None = chat.active_leaf_id
        while curr_id and curr_id not in visited:
            visited.add(curr_id)
            msg = self.get_message(curr_id)
            if not msg:
                break
            chain.append(msg)
            curr_id = msg.parent_id

        chain.reverse()
        return chain

    def get_ancestors_up_to(self, message_id: str) -> list[MessageNode]:
        """Return the ordered ancestor path from root up to and including `message_id`."""
        chain: list[MessageNode] = []
        visited: set[str] = set()
        curr_id: str | None = message_id
        while curr_id and curr_id not in visited:
            visited.add(curr_id)
            msg = self.get_message(curr_id)
            if not msg:
                break
            chain.append(msg)
            curr_id = msg.parent_id
        chain.reverse()
        return chain

    def get_siblings(self, message_id: str) -> list[MessageNode]:
        """Return all sibling messages sharing the same parent_id in chronological order."""
        msg = self.get_message(message_id)
        if not msg:
            return []
        if msg.parent_id is None:
            rows = self.conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ? AND parent_id IS NULL
                ORDER BY created_at ASC, rowid ASC
                """,
                (msg.chat_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ? AND parent_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (msg.chat_id, msg.parent_id),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def switch_branch(self, chat_id: str, target_message_id: str) -> list[MessageNode]:
        """Switch the active branch to pass through `target_message_id` down to its leaf."""
        target = self.get_message(target_message_id)
        if not target or target.chat_id != chat_id:
            return self.get_active_thread(chat_id)

        with self.conn:
            # Update parent pointers up the tree so `selected_child_id` points along this path
            curr = target
            while curr.parent_id:
                self.conn.execute(
                    "UPDATE messages SET selected_child_id = ? WHERE id = ?",
                    (curr.id, curr.parent_id),
                )
                parent = self.get_message(curr.parent_id)
                if not parent:
                    break
                curr = parent

            # Walk down from `target` following `selected_child_id` (or latest child) to find the leaf
            leaf = target
            visited: set[str] = {leaf.id}
            while True:
                next_node: MessageNode | None = None
                if leaf.selected_child_id:
                    cand = self.get_message(leaf.selected_child_id)
                    if cand and cand.parent_id == leaf.id:
                        next_node = cand
                if next_node is None:
                    child_row = self.conn.execute(
                        """
                        SELECT * FROM messages
                        WHERE chat_id = ? AND parent_id = ?
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT 1
                        """,
                        (chat_id, leaf.id),
                    ).fetchone()
                    if child_row:
                        next_node = self._row_to_message(child_row)
                if not next_node or next_node.id in visited:
                    break
                visited.add(next_node.id)
                leaf = next_node

            self.conn.execute(
                "UPDATE chats SET active_leaf_id = ?, updated_at = ? WHERE id = ?",
                (leaf.id, time.time(), chat_id),
            )
        return self.get_active_thread(chat_id)

    def edit_and_branch(
        self,
        chat_id: str,
        original_message_id: str,
        new_content: str,
        skills_used: list[str] | None = None,
    ) -> MessageNode:
        """Edit a message by creating a new sibling branch from its parent onward."""
        orig = self.get_message(original_message_id)
        if not orig or orig.chat_id != chat_id:
            raise ValueError(f"Message {original_message_id} not found in chat {chat_id}")

        return self.add_message(
            chat_id=chat_id,
            role=orig.role,
            content=new_content,
            parent_id=orig.parent_id,
            skills_used=skills_used if skills_used is not None else orig.skills_used,
        )

    def fork_chat_from_message(
        self,
        chat_id: str,
        up_to_message_id: str,
        new_title: str | None = None,
    ) -> ChatSession:
        """Clone the conversation branch up to `up_to_message_id` into a new independent chat."""
        source_chat = self.get_chat(chat_id)
        if not source_chat:
            raise ValueError(f"Chat {chat_id} not found")

        ancestors = self.get_ancestors_up_to(up_to_message_id)
        fork_title = new_title or f"{source_chat.title} (Branch)"
        new_chat = self.create_chat(
            title=fork_title,
            provider_id=source_chat.provider_id,
            model=source_chat.model,
            system_prompt=source_chat.system_prompt,
            sliding_window_tokens=source_chat.sliding_window_tokens,
            sliding_window_turns=source_chat.sliding_window_turns,
        )
        self.update_chat_settings(
            new_chat.id,
            web_search_enabled=source_chat.web_search_enabled,
            tools_enabled=source_chat.tools_enabled,
            active_skills=source_chat.active_skills,
        )

        prev_new_id: str | None = None
        for msg in ancestors:
            cloned = self.add_message(
                chat_id=new_chat.id,
                role=msg.role,
                content=msg.content,
                parent_id=prev_new_id,
                reasoning_content=msg.reasoning_content,
                tool_calls=msg.tool_calls,
                tool_call_id=msg.tool_call_id,
                citations=msg.citations,
                skills_used=msg.skills_used,
                prompt_tokens=msg.prompt_tokens,
                completion_tokens=msg.completion_tokens,
                cached_tokens=msg.cached_tokens,
            )
            prev_new_id = cloned.id

        return self.get_chat(new_chat.id)  # type: ignore[return-value]

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageNode:
        try:
            tool_calls = json.loads(row["tool_calls_json"] or "[]")
        except json.JSONDecodeError:
            tool_calls = []
        try:
            citations = json.loads(row["citations_json"] or "[]")
        except json.JSONDecodeError:
            citations = []
        try:
            skills_used = json.loads(row["skills_used_json"] or "[]")
        except json.JSONDecodeError:
            skills_used = []

        return MessageNode(
            id=row["id"],
            chat_id=row["chat_id"],
            parent_id=row["parent_id"],
            selected_child_id=row["selected_child_id"],
            role=row["role"],
            content=row["content"],
            reasoning_content=row["reasoning_content"] or "",
            tool_calls=tool_calls,
            tool_call_id=row["tool_call_id"],
            citations=citations,
            skills_used=skills_used,
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            cached_tokens=int(row["cached_tokens"] or 0),
            created_at=float(row["created_at"]),
        )

    # -------------------------------------------------------------------------
    # Local Memories
    # -------------------------------------------------------------------------

    def list_memories(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        if only_enabled:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE enabled = 1 ORDER BY created_at ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "fact": r["fact"],
                "enabled": bool(r["enabled"]),
                "created_at": float(r["created_at"]),
            }
            for r in rows
        ]

    def add_memory(self, fact: str) -> dict[str, Any]:
        mid = str(uuid.uuid4())
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO memories (id, fact, enabled, created_at) VALUES (?, ?, 1, ?)",
                (mid, fact.strip(), now),
            )
        return {"id": mid, "fact": fact.strip(), "enabled": True, "created_at": now}

    def delete_memory(self, memory_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

    # -------------------------------------------------------------------------
    # App Settings
    # -------------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO settings (key, value_json)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                (key, json.dumps(value)),
            )

