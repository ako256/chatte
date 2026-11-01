"""Libadwaita 1.9 PreferencesDialog, Provider Configuration (with GNOME Keyring),
and On-Demand Markdown Skills Manager Dialog.
"""

from __future__ import annotations

import threading
import uuid
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, GLib, Gtk, GtkSource

from chatte.api.client import DirectAIClient
from chatte.core.skills import Skill, SkillsManager
from chatte.storage.database import ChatDatabase, ChatSession, ProviderConfig


PROVIDER_TYPES = [
    ("openai", "OpenAI Compatible"),
    ("anthropic", "Anthropic Claude"),
    ("ollama", "Ollama (Local)"),
    ("llamacpp", "llama.cpp (Local)"),
]


class PreferencesDialog(Adw.PreferencesDialog):
    """Native Libadwaita 1.9 PreferencesDialog for Providers, Keyring API keys, and General Settings."""

    def __init__(
        self,
        db: ChatDatabase,
        ai_client: DirectAIClient,
        current_chat: ChatSession | None = None,
        on_changed: Callable[[], None] | None = None,
        initial_page: str = "providers",
    ) -> None:
        super().__init__()
        self.set_title("Preferences")
        self.set_content_width(680)
        self.set_content_height(600)

        self.db = db
        self.ai_client = ai_client
        self.current_chat = current_chat
        self._on_changed = on_changed

        self._providers: list[ProviderConfig] = self.db.list_providers()
        self._selected_provider: ProviderConfig = (
            self._providers[0] if self._providers else self.db.get_active_provider()
        )
        self._updating_form = False

        self._prov_page = self._build_providers_page()
        self._gen_page = self._build_general_and_context_page()
        if initial_page == "general":
            self.set_visible_page(self._gen_page)

    def _build_providers_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="Servers",
            icon_name="network-server-symbolic",
        )

        # Group 1: Provider Profile Selector & Add/Remove
        sel_group = Adw.PreferencesGroup(title="Provider Profile")

        self._provider_combo = Adw.ComboRow(title="Profile")
        self._refresh_provider_combo_model()
        self._provider_combo.connect("notify::selected", self._on_provider_combo_changed)
        sel_group.add(self._provider_combo)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_margin_top(6)

        add_prov_btn = Gtk.Button(label="Add Server")
        add_prov_btn.set_icon_name("list-add-symbolic")
        add_prov_btn.connect("clicked", self._on_new_provider)
        btn_box.append(add_prov_btn)

        del_prov_btn = Gtk.Button(label="Delete")
        del_prov_btn.add_css_class("destructive-action")
        del_prov_btn.connect("clicked", self._on_delete_provider)
        btn_box.append(del_prov_btn)

        sel_group.add(btn_box)
        page.add(sel_group)

        # Group 2: Endpoint & GNOME Keyring Credentials
        conn_group = Adw.PreferencesGroup(title="Connection")

        self._name_row = Adw.EntryRow(title="Name")
        conn_group.add(self._name_row)

        self._type_combo = Adw.ComboRow(title="API Type")
        type_strings = Gtk.StringList.new([label for _, label in PROVIDER_TYPES])
        self._type_combo.set_model(type_strings)
        conn_group.add(self._type_combo)

        self._url_row = Adw.EntryRow(title="Endpoint URL")
        conn_group.add(self._url_row)

        self._api_key_row = Adw.PasswordEntryRow(title="API Key")
        conn_group.add(self._api_key_row)

        self._model_row = Adw.EntryRow(title="Model")
        fetch_btn = Gtk.Button(label="Fetch")
        fetch_btn.set_valign(Gtk.Align.CENTER)
        fetch_btn.add_css_class("flat")
        fetch_btn.connect("clicked", self._on_fetch_models_clicked)
        self._model_row.add_suffix(fetch_btn)
        conn_group.add(self._model_row)

        self._active_switch = Adw.SwitchRow(title="Default Provider")
        conn_group.add(self._active_switch)
        page.add(conn_group)

        # Group 3: Caching & Local Model Management
        cache_group = Adw.PreferencesGroup(title="Caching &amp; Local Models")

        self._prompt_cache_switch = Adw.SwitchRow(title="Prompt Caching")
        cache_group.add(self._prompt_cache_switch)

        self._kv_cache_switch = Adw.SwitchRow(title="Reuse Local KV Cache")
        cache_group.add(self._kv_cache_switch)

        self._idle_spin = Adw.SpinRow.new_with_range(0, 240, 1)
        self._idle_spin.set_title("Auto-Unload Idle Model (Minutes)")
        cache_group.add(self._idle_spin)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.set_margin_top(10)
        save_btn.connect("clicked", self._on_save_provider_clicked)
        cache_group.add(save_btn)

        page.add(cache_group)
        self.add(page)

        self._load_provider_into_form(self._selected_provider)
        return page

    def _build_general_and_context_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="General",
            icon_name="preferences-system-symbolic",
        )

        # App Behavior Group (Tools & Context Bar visibility)
        ui_group = Adw.PreferencesGroup(title="Interface &amp; Tools")

        show_ctx_bar = bool(self.db.get_setting("show_context_bar", True))
        self._show_ctx_bar_switch = Adw.SwitchRow(
            title="Show Context Usage Bar in Header",
        )
        self._show_ctx_bar_switch.set_active(show_ctx_bar)
        self._show_ctx_bar_switch.connect("notify::active", self._on_toggle_show_ctx_bar)
        ui_group.add(self._show_ctx_bar_switch)

        tools_on = (
            self.current_chat.tools_enabled
            if self.current_chat
            else bool(self.db.get_setting("tools_enabled", True))
        )
        self._tools_switch = Adw.SwitchRow(
            title="Enable Function Calling &amp; Tools",
        )
        self._tools_switch.set_active(tools_on)
        self._tools_switch.connect("notify::active", self._on_toggle_tools_enabled)
        ui_group.add(self._tools_switch)

        page.add(ui_group)

        if self.current_chat:
            ctx_group = Adw.PreferencesGroup(title="Context Window")

            self._win_tokens_spin = Adw.SpinRow.new_with_range(1024, 256000, 1024)
            self._win_tokens_spin.set_title("Max Context Tokens")
            self._win_tokens_spin.set_value(self.current_chat.sliding_window_tokens)
            ctx_group.add(self._win_tokens_spin)

            self._win_turns_spin = Adw.SpinRow.new_with_range(2, 200, 2)
            self._win_turns_spin.set_title("Max Conversation Turns")
            self._win_turns_spin.set_value(self.current_chat.sliding_window_turns)
            ctx_group.add(self._win_turns_spin)

            self._sys_prompt_row = Adw.EntryRow(title="System Prompt")
            self._sys_prompt_row.set_text(self.current_chat.system_prompt)
            ctx_group.add(self._sys_prompt_row)

            apply_ctx_btn = Gtk.Button(label="Apply")
            apply_ctx_btn.add_css_class("suggested-action")
            apply_ctx_btn.set_margin_top(8)
            apply_ctx_btn.connect("clicked", self._on_apply_chat_context)
            ctx_group.add(apply_ctx_btn)

            page.add(ctx_group)

        # Local Memories Group
        mem_group = Adw.PreferencesGroup(title="Memories")

        self._new_memory_row = Adw.EntryRow(title="Add memory…")
        add_mem_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_mem_btn.set_valign(Gtk.Align.CENTER)
        add_mem_btn.add_css_class("flat")
        add_mem_btn.connect("clicked", self._on_add_memory)
        self._new_memory_row.add_suffix(add_mem_btn)
        mem_group.add(self._new_memory_row)

        self._memories_list_box = Gtk.ListBox()
        self._memories_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._memories_list_box.add_css_class("boxed-list")
        self._memories_list_box.set_margin_top(8)
        mem_group.add(self._memories_list_box)
        self._refresh_memories_list()

        page.add(mem_group)
        self.add(page)
        return page

    def _on_toggle_show_ctx_bar(self, row: Adw.SwitchRow, _pspec: object) -> None:
        self.db.set_setting("show_context_bar", bool(row.get_active()))
        if self._on_changed:
            self._on_changed()

    def _on_toggle_tools_enabled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        active = bool(row.get_active())
        self.db.set_setting("tools_enabled", active)
        if self.current_chat:
            self.db.update_chat_settings(self.current_chat.id, tools_enabled=active)
        if self._on_changed:
            self._on_changed()

    def _refresh_provider_combo_model(self) -> None:
        self._providers = self.db.list_providers()
        names = [p.name for p in self._providers]
        self._provider_combo.set_model(Gtk.StringList.new(names))

    def _load_provider_into_form(self, prov: ProviderConfig) -> None:
        self._updating_form = True
        self._selected_provider = prov
        self._name_row.set_text(prov.name)
        type_idx = 0
        for idx, (ptype, _) in enumerate(PROVIDER_TYPES):
            if ptype == prov.provider_type:
                type_idx = idx
                break
        self._type_combo.set_selected(type_idx)
        self._url_row.set_text(prov.base_url)
        self._api_key_row.set_text(prov.api_key)
        self._model_row.set_text(prov.default_model)
        self._active_switch.set_active(prov.is_active)
        self._prompt_cache_switch.set_active(prov.enable_prompt_cache)
        self._kv_cache_switch.set_active(prov.enable_kv_cache)
        self._idle_spin.set_value(prov.idle_unload_minutes)
        self._updating_form = False

    def _on_provider_combo_changed(self, combo: Adw.ComboRow, _pspec: object) -> None:
        if self._updating_form:
            return
        idx = combo.get_selected()
        if 0 <= idx < len(self._providers):
            self._load_provider_into_form(self._providers[idx])

    def _on_new_provider(self, _btn: Gtk.Button) -> None:
        new_id = f"custom-{uuid.uuid4().hex[:8]}"
        new_prov = ProviderConfig(
            id=new_id,
            name="Custom Endpoint",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            default_model="gpt-4o-mini",
            enable_prompt_cache=True,
            enable_kv_cache=False,
            idle_unload_minutes=0,
            is_active=False,
        )
        self.db.save_provider(new_prov, api_key="")
        self._updating_form = True
        self._refresh_provider_combo_model()
        for idx, p in enumerate(self._providers):
            if p.id == new_id:
                self._provider_combo.set_selected(idx)
                break
        self._updating_form = False
        self._load_provider_into_form(new_prov)

    def _on_delete_provider(self, _btn: Gtk.Button) -> None:
        if len(self._providers) <= 1:
            return
        del_id = self._selected_provider.id
        self.db.delete_provider(del_id)
        self._updating_form = True
        self._refresh_provider_combo_model()
        self._provider_combo.set_selected(0)
        self._updating_form = False
        self._load_provider_into_form(self._providers[0])
        if self._on_changed:
            self._on_changed()

    def _on_save_provider_clicked(self, _btn: Gtk.Button) -> None:
        type_idx = self._type_combo.get_selected()
        ptype = PROVIDER_TYPES[type_idx][0] if 0 <= type_idx < len(PROVIDER_TYPES) else "openai"

        updated = ProviderConfig(
            id=self._selected_provider.id,
            name=self._name_row.get_text().strip() or "Provider",
            provider_type=ptype,
            base_url=self._url_row.get_text().strip() or "https://api.openai.com/v1",
            default_model=self._model_row.get_text().strip() or "gpt-4o",
            enable_prompt_cache=self._prompt_cache_switch.get_active(),
            enable_kv_cache=self._kv_cache_switch.get_active(),
            idle_unload_minutes=int(self._idle_spin.get_value()),
            is_active=self._active_switch.get_active(),
        )
        api_key_val = self._api_key_row.get_text().strip()
        self.db.save_provider(updated, api_key=api_key_val)

        self._updating_form = True
        self._refresh_provider_combo_model()
        for idx, p in enumerate(self._providers):
            if p.id == updated.id:
                self._provider_combo.set_selected(idx)
                break
        self._updating_form = False

        self.add_toast(Adw.Toast.new("Saved"))
        if self._on_changed:
            self._on_changed()

    def _on_fetch_models_clicked(self, _btn: Gtk.Button) -> None:
        type_idx = self._type_combo.get_selected()
        ptype = PROVIDER_TYPES[type_idx][0] if 0 <= type_idx < len(PROVIDER_TYPES) else "openai"
        temp_prov = ProviderConfig(
            id=self._selected_provider.id,
            name=self._name_row.get_text().strip(),
            provider_type=ptype,
            base_url=self._url_row.get_text().strip(),
            default_model=self._model_row.get_text().strip(),
        )
        temp_prov.set_api_key(self._api_key_row.get_text().strip())

        def _worker() -> None:
            models = self.ai_client.list_models(temp_prov)

            def _finish() -> bool:
                if models:
                    self._model_row.set_text(models[0])
                    self.add_toast(Adw.Toast.new(f"Found {len(models)} models"))
                else:
                    self.add_toast(Adw.Toast.new("Could not fetch models"))
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_apply_chat_context(self, _btn: Gtk.Button) -> None:
        if not self.current_chat:
            return
        self.db.update_chat_settings(
            self.current_chat.id,
            system_prompt=self._sys_prompt_row.get_text().strip(),
            sliding_window_tokens=int(self._win_tokens_spin.get_value()),
            sliding_window_turns=int(self._win_turns_spin.get_value()),
        )
        self.add_toast(Adw.Toast.new("Saved"))
        if self._on_changed:
            self._on_changed()

    def _on_add_memory(self, _btn: Gtk.Button) -> None:
        txt = self._new_memory_row.get_text().strip()
        if not txt:
            return
        self.db.add_memory(txt)
        self._new_memory_row.set_text("")
        self._refresh_memories_list()
        if self._on_changed:
            self._on_changed()

    def _refresh_memories_list(self) -> None:
        child = self._memories_list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._memories_list_box.remove(child)
            child = nxt

        memories = self.db.list_memories()
        if not memories:
            empty_row = Adw.ActionRow(title="No memories saved")
            self._memories_list_box.append(empty_row)
            return

        for mem in memories:
            row = Adw.ActionRow(title=GLib.markup_escape_text(mem["fact"]))
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.set_valign(Gtk.Align.CENTER)
            mid = mem["id"]
            del_btn.connect("clicked", self._on_delete_memory, mid)
            row.add_suffix(del_btn)
            self._memories_list_box.append(row)

    def _on_delete_memory(self, _btn: Gtk.Button, memory_id: str) -> None:
        self.db.delete_memory(memory_id)
        self._refresh_memories_list()
        if self._on_changed:
            self._on_changed()


class SkillsDialog(Adw.Dialog):
    """Native Libadwaita 1.9 Dialog for creating and editing reusable Markdown Skills."""

    def __init__(
        self,
        skills_mgr: SkillsManager,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_title("Skills")
        self.set_content_width(720)
        self.set_content_height(520)

        self.skills_mgr = skills_mgr
        self._on_changed = on_changed
        self._current_slug: str | None = None

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        new_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_btn.set_tooltip_text("New Skill")
        new_btn.connect("clicked", self._on_new_skill)
        header.pack_start(new_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_skill)
        header.pack_end(save_btn)

        toolbar_view.add_top_bar(header)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(220)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_min_content_width(200)
        self._list_box = Gtk.ListBox()
        self._list_box.add_css_class("navigation-sidebar")
        self._list_box.connect("row-selected", self._on_row_selected)
        left_scroll.set_child(self._list_box)
        paned.set_start_child(left_scroll)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right_box.set_margin_top(12)
        right_box.set_margin_bottom(12)
        right_box.set_margin_start(14)
        right_box.set_margin_end(14)

        form_list = Gtk.ListBox()
        form_list.set_selection_mode(Gtk.SelectionMode.NONE)
        form_list.add_css_class("boxed-list")

        self._name_entry = Adw.EntryRow(title="Name")
        form_list.append(self._name_entry)

        self._desc_entry = Adw.EntryRow(title="Description")
        form_list.append(self._desc_entry)
        right_box.append(form_list)

        instr_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label="Instructions (Markdown)", xalign=0.0)
        lbl.add_css_class("heading")
        lbl.set_hexpand(True)
        instr_hdr.append(lbl)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.add_css_class("error")
        del_btn.set_tooltip_text("Delete Skill")
        del_btn.connect("clicked", self._on_delete_skill)
        instr_hdr.append(del_btn)
        right_box.append(instr_hdr)

        self._md_buffer = GtkSource.Buffer()
        lm = GtkSource.LanguageManager.get_default()
        md_lang = lm.get_language("markdown")
        if md_lang:
            self._md_buffer.set_language(md_lang)
            self._md_buffer.set_highlight_syntax(True)

        sm = GtkSource.StyleSchemeManager.get_default()
        is_dark = Adw.StyleManager.get_default().get_dark()
        scheme = sm.get_scheme("Adwaita-dark" if is_dark else "Adwaita")
        if scheme:
            self._md_buffer.set_style_scheme(scheme)

        self._md_view = GtkSource.View.new_with_buffer(self._md_buffer)
        self._md_view.set_monospace(True)
        self._md_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._md_view.set_top_margin(10)
        self._md_view.set_bottom_margin(10)
        self._md_view.set_left_margin(12)
        self._md_view.set_right_margin(12)

        ed_frame = Gtk.Frame()
        ed_scroll = Gtk.ScrolledWindow()
        ed_scroll.set_vexpand(True)
        ed_scroll.set_child(self._md_view)
        ed_frame.set_child(ed_scroll)
        right_box.append(ed_frame)

        paned.set_end_child(right_box)
        toolbar_view.set_content(paned)
        self.set_child(toolbar_view)

        self._populate_skills_list()

    def _populate_skills_list(self) -> None:
        child = self._list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list_box.remove(child)
            child = nxt

        self._skills = self.skills_mgr.list_skills()
        for sk in self._skills:
            row = Gtk.ListBoxRow()
            row._skill_slug = sk.slug  # type: ignore[attr-defined]
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(10)
            box.set_margin_end(10)
            t_lbl = Gtk.Label(label=sk.name, xalign=0.0)
            t_lbl.add_css_class("heading")
            box.append(t_lbl)
            row.set_child(box)
            self._list_box.append(row)

        first = self._list_box.get_row_at_index(0)
        if first:
            self._list_box.select_row(first)

    def _on_row_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        slug = getattr(row, "_skill_slug", None)
        if not slug:
            return
        sk = self.skills_mgr.get_skill(slug)
        if sk:
            self._load_skill(sk)

    def _load_skill(self, sk: Skill) -> None:
        self._current_slug = sk.slug
        self._name_entry.set_text(sk.name)
        self._desc_entry.set_text(sk.description)
        self._md_buffer.set_text(sk.instructions)

    def _on_new_skill(self, _btn: Gtk.Button) -> None:
        self._current_slug = f"skill-{uuid.uuid4().hex[:6]}"
        self._name_entry.set_text("New Skill")
        self._desc_entry.set_text("")
        self._md_buffer.set_text("")
        self._name_entry.grab_focus()

    def _on_save_skill(self, _btn: Gtk.Button) -> None:
        name = self._name_entry.get_text().strip() or "Custom Skill"
        desc = self._desc_entry.get_text().strip()
        start, end = self._md_buffer.get_bounds()
        instructions = self._md_buffer.get_text(start, end, True).strip()
        slug = self._current_slug or name.lower()
        saved = self.skills_mgr.save_skill(
            slug=slug,
            name=name,
            description=desc,
            instructions=instructions,
        )
        self._current_slug = saved.slug
        self._populate_skills_list()
        if self._on_changed:
            self._on_changed()

    def _on_delete_skill(self, _btn: Gtk.Button) -> None:
        if not self._current_slug:
            return
        self.skills_mgr.delete_skill(self._current_slug)
        self._current_slug = None
        self._populate_skills_list()
        if self._on_changed:
            self._on_changed()
