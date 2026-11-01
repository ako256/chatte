# Chatte (`earth.dlc.Chatte`) — Native GNOME 50 AI Assistant

Chatte is a 100% native GNOME 50 desktop application built with **Python 3**, **GTK 4.22**, **Libadwaita 1.9**, **GtkSourceView 5**, **PangoCairo**, and **Libsecret 1** (`GNOME Keyring`) with **zero WebView usage**.

## Features

- **Direct Provider Connections**: Connects directly to OpenAI, Anthropic, Ollama, and `llama.cpp` API endpoints via streaming SSE (`httpx`) without intermediary proxy servers.
- **GNOME Keyring API Key Security**: API keys are stored strictly in GNOME Keyring under the `earth.dlc.Chatte.ApiKey` schema (`libsecret`) and are never written to the SQLite database.
- **Context & Token Management**:
  - Real-time token counting in the header bar and composer.
  - Customizable sliding context windows (by max tokens and max conversation turns).
  - Prompt caching support (`cache_control: {"type": "ephemeral"}` for Anthropic and deterministic prefix ordering for OpenAI/DeepSeek).
  - Local model KV cache slot reuse (`cache_prompt` + `id_slot` for `llama.cpp`, `keep_alive` for Ollama) and automatic idle model unloading from VRAM/RAM.
- **On-Demand Custom Skills & Tools (`+` Button in Chatbox)**:
  - Reusable Markdown Skills (`~/.local/share/chatte/skills/*.md`) loaded on-demand via the `+` button in the chatbox.
  - Built-in Function Calling / Tool Calls (`web_search`, `fetch_webpage`, `calculate_math`, `get_system_info`).
  - One-click Structured Prompt templates and local text/code file attachment.
- **Built-in Web Search & Inline Citations**:
  - Zero-config **DuckDuckGo + Wikipedia** search integration with clickable inline citation badges (`[1]`, `[2]`) and a source citations footer bar.
- **100% Native Formatting**:
  - Native GTK 4 Markdown AST renderer (headings, lists, blockquotes, `Gtk.Grid` tables, collapsible reasoning blocks).
  - `GtkSourceView 5` syntax-highlighted code blocks (`Adwaita` / `Adwaita-dark`) with language badges and Copy buttons.
  - Native Pango + PangoCairo LaTeX math engine supporting inline math (`$...$`) and display math (`$$...$$`, `\frac`, `\sqrt`, `\sum`, `\int`, matrices).
- **Zero Tracking / Local Storage & Tree Branching**:
  - Strictly local SQLite DAG storage (`~/.local/share/chatte/chatte.db`).
  - Renamable chats, automatic conversation branching when any message is edited (`< Branch 1 / N >`), and one-click forking from any message onward into a new chat.

## Running Chatte

```bash
python3 run.py
```

## Running Automated Tests

```bash
python3 -m unittest discover -s tests -v
```

