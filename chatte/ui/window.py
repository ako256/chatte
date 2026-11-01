"""Main ApplicationWindow for Chatte (earth.dlc.Chatte) built with GTK 4.22 & Libadwaita 1.9."""

from __future__ import annotations

import dataclasses
from pathlib import Path
import json
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from chatte.api.client import DirectAIClient
from chatte.core.context_manager import (
    ContextBreakdown,
    IdleModelMonitor,
    build_sliding_window,
)
from chatte.core.skills import SkillsManager
from chatte.core.tools import BUILTIN_TOOLS_SCHEMA, execute_tool_call
from chatte.core.web_search import (
    Citation,
    format_citations_for_prompt,
    search_web,
)
from chatte.storage.database import (
    ChatDatabase,
    ChatSession,
    MessageNode,
    ProviderConfig,
)
from chatte.ui.dialogs import PreferencesDialog, SkillsDialog
from chatte.ui.message_row import MessageWidget


class ChatteWindow(Adw.ApplicationWindow):
    """Pure native GNOME 50 AI Assistant Window."""

    def __init__(
        self,
        application: Adw.Application,
        db: ChatDatabase | None = None,
    ) -> None:
        super().__init__(application=application)
        self.set_title("Chatte")
        self.set_default_size(1140, 760)
        self.set_size_request(360, 480)

        self.db = db or ChatDatabase()
        self.skills_mgr = SkillsManager()
        self.ai_client = DirectAIClient()
        self.idle_monitor = IdleModelMonitor(
            unload_callback=self.ai_client.unload_local_model,
            on_unloaded_ui=self.show_toast,
        )

        self._streaming_stop_event: threading.Event | None = None
        self._is_streaming = False
        self._sidebar_collapsed = False

        chats = self.db.list_chats()
        if chats:
            self.current_chat: ChatSession = chats[0]
        else:
            self.current_chat = self.db.create_chat(title="New Chat")

        self._build_ui()
        self._refresh_sidebar()
        self._load_chat_into_view(self.current_chat.id)

        GLib.timeout_add_seconds(15, self._on_idle_timer_tick)

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()

        root_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        self._sidebar_panel = self._build_sidebar()
        root_hbox.append(self._sidebar_panel)

        content_area = self._build_content_area()
        content_area.set_hexpand(True)
        root_hbox.append(content_area)

        self._toast_overlay.set_child(root_hbox)
        self.set_content(self._toast_overlay)

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar_box.add_css_class("sidebar-panel")
        sidebar_box.set_size_request(250, -1)

        # Compact draggable header row without Adw.HeaderBar's ~90px minimum width floor
        win_handle = Gtk.WindowHandle()
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_row.add_css_class("sidebar-header")

        self._sidebar_title_lbl = Gtk.Label(label="Chatte", xalign=0.5)
        self._sidebar_title_lbl.add_css_class("heading")
        self._sidebar_title_lbl.set_hexpand(True)
        self._sidebar_title_lbl.set_margin_start(28)
        header_row.append(self._sidebar_title_lbl)

        self._collapse_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self._collapse_btn.add_css_class("flat")
        self._collapse_btn.add_css_class("composer-icon-btn")
        self._collapse_btn.set_valign(Gtk.Align.CENTER)
        self._collapse_btn.set_halign(Gtk.Align.CENTER)
        self._collapse_btn.set_tooltip_text("Collapse sidebar")
        self._collapse_btn.connect("clicked", self._on_toggle_sidebar_collapsed)
        header_row.append(self._collapse_btn)

        win_handle.set_child(header_row)
        sidebar_box.append(win_handle)

        # Middle section (Search + Chat list) — hidden when collapsed
        self._sidebar_middle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._sidebar_middle_box.set_vexpand(True)

        self._search_entry = Gtk.SearchEntry(placeholder_text="Search chats…")
        self._search_entry.set_margin_start(10)
        self._search_entry.set_margin_end(10)
        self._search_entry.set_margin_top(2)
        self._search_entry.connect("search-changed", lambda *_: self._refresh_sidebar())
        self._sidebar_middle_box.append(self._search_entry)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._chat_list_box = Gtk.ListBox()
        self._chat_list_box.add_css_class("navigation-sidebar")
        self._chat_list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._chat_list_box.connect("row-selected", self._on_sidebar_row_selected)
        scroller.set_child(self._chat_list_box)
        self._sidebar_middle_box.append(scroller)
        sidebar_box.append(self._sidebar_middle_box)

        # Spacer visible only when collapsed to push the vertical buttons to the bottom-left
        self._collapsed_spacer = Gtk.Box()
        self._collapsed_spacer.set_vexpand(True)
        self._collapsed_spacer.set_visible(False)
        sidebar_box.append(self._collapsed_spacer)

        # Bottom Bar: [Server Config]  [New Chat (+)]  [Preferences]  [Skills (★)]
        # Horizontal when expanded, Vertically stacked when collapsed
        self._bottom_actions_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
        )
        self._bottom_actions_box.set_homogeneous(True)
        self._bottom_actions_box.set_margin_start(6)
        self._bottom_actions_box.set_margin_end(6)
        self._bottom_actions_box.set_margin_top(4)
        self._bottom_actions_box.set_margin_bottom(8)

        server_btn = Gtk.Button(icon_name="network-server-symbolic")
        server_btn.add_css_class("flat")
        server_btn.add_css_class("composer-icon-btn")
        server_btn.set_tooltip_text("Server Configuration")
        server_btn.connect(
            "clicked", lambda *_: self.open_preferences_dialog(initial_page="providers")
        )
        self._bottom_actions_box.append(server_btn)

        new_chat_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_chat_btn.add_css_class("flat")
        new_chat_btn.add_css_class("composer-icon-btn")
        new_chat_btn.set_tooltip_text("New Chat (Ctrl+N)")
        new_chat_btn.connect("clicked", lambda *_: self.action_new_chat())
        self._bottom_actions_box.append(new_chat_btn)

        prefs_btn = Gtk.Button(icon_name="preferences-system-symbolic")
        prefs_btn.add_css_class("flat")
        prefs_btn.add_css_class("composer-icon-btn")
        prefs_btn.set_tooltip_text("Preferences")
        prefs_btn.connect(
            "clicked", lambda *_: self.open_preferences_dialog(initial_page="general")
        )
        self._bottom_actions_box.append(prefs_btn)

        skills_btn = Gtk.Button(icon_name="starred-symbolic")
        skills_btn.add_css_class("flat")
        skills_btn.add_css_class("composer-icon-btn")
        skills_btn.set_tooltip_text("Skills")
        skills_btn.connect("clicked", lambda *_: self.open_skills_dialog())
        self._bottom_actions_box.append(skills_btn)

        sidebar_box.append(self._bottom_actions_box)
        return sidebar_box

    def _on_toggle_sidebar_collapsed(self, *_args: object) -> None:
        self._sidebar_collapsed = not self._sidebar_collapsed
        if self._sidebar_collapsed:
            self._sidebar_panel.set_size_request(38, -1)
            self._sidebar_title_lbl.set_visible(False)
            self._sidebar_middle_box.set_visible(False)
            self._collapsed_spacer.set_visible(True)
            self._collapse_btn.set_hexpand(True)
            self._collapse_btn.set_icon_name("go-next-symbolic")
            self._collapse_btn.set_tooltip_text("Expand sidebar")
            self._bottom_actions_box.set_orientation(Gtk.Orientation.VERTICAL)
            self._bottom_actions_box.set_homogeneous(False)
            self._bottom_actions_box.set_margin_start(2)
            self._bottom_actions_box.set_margin_end(2)
        else:
            self._sidebar_panel.set_size_request(250, -1)
            self._sidebar_title_lbl.set_visible(True)
            self._sidebar_middle_box.set_visible(True)
            self._collapsed_spacer.set_visible(False)
            self._collapse_btn.set_hexpand(False)
            self._collapse_btn.set_icon_name("go-previous-symbolic")
            self._collapse_btn.set_tooltip_text("Collapse sidebar")
            self._bottom_actions_box.set_orientation(Gtk.Orientation.HORIZONTAL)
            self._bottom_actions_box.set_homogeneous(True)
            self._bottom_actions_box.set_margin_start(6)
            self._bottom_actions_box.set_margin_end(6)

    def _build_content_area(self) -> Gtk.Widget:
        toolbar_view = Adw.ToolbarView()

        # Content HeaderBar: Only Model Dropdown in center, optional small ProgressBar on right
        header = Adw.HeaderBar()

        self._model_menu_btn = Gtk.MenuButton()
        self._model_menu_btn.add_css_class("flat")
        self._model_menu_btn.add_css_class("model-selector-btn")
        self._model_popover = self._build_model_selector_popover()
        self._model_menu_btn.set_popover(self._model_popover)
        header.set_title_widget(self._model_menu_btn)

        # Optional small progress bar in place of the old Tokens text badge
        self._token_menu_btn = Gtk.MenuButton()
        self._token_menu_btn.add_css_class("flat")
        self._header_context_bar = Gtk.ProgressBar()
        self._header_context_bar.add_css_class("header-context-bar")
        self._header_context_bar.set_valign(Gtk.Align.CENTER)
        self._token_menu_btn.set_child(self._header_context_bar)
        self._token_popover = self._build_context_meter_popover()
        self._token_menu_btn.set_popover(self._token_popover)
        header.pack_end(self._token_menu_btn)

        toolbar_view.add_top_bar(header)

        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Clean scrollable message area (shows nothing when empty)
        self._msg_scroller = Gtk.ScrolledWindow()
        self._msg_scroller.add_css_class("chat-scroll")
        self._msg_scroller.set_vexpand(True)
        self._msg_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(780)
        clamp.set_tightening_threshold(580)

        self._messages_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._messages_vbox.set_margin_top(18)
        self._messages_vbox.set_margin_bottom(18)
        self._messages_vbox.set_margin_start(18)
        self._messages_vbox.set_margin_end(18)
        clamp.set_child(self._messages_vbox)
        self._msg_scroller.set_child(clamp)
        main_vbox.append(self._msg_scroller)

        # Bottom Composer Area (must never vexpand so _msg_scroller gets full window height)
        composer_clamp = Adw.Clamp()
        composer_clamp.set_maximum_size(780)
        composer_clamp.set_vexpand(False)
        composer_clamp.set_valign(Gtk.Align.END)
        composer_clamp.set_child(self._build_composer_bar())
        main_vbox.append(composer_clamp)

        toolbar_view.set_content(main_vbox)
        return toolbar_view

    def _build_model_selector_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        self._profile_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._profile_list_box.set_margin_top(6)
        self._profile_list_box.set_margin_bottom(6)
        self._profile_list_box.set_margin_start(6)
        self._profile_list_box.set_margin_end(6)
        self._profile_list_box.set_size_request(210, -1)
        pop.set_child(self._profile_list_box)
        pop.connect("show", lambda *_: self._refresh_profile_selector_popover())
        return pop

    def _refresh_profile_selector_popover(self) -> None:
        child = self._profile_list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._profile_list_box.remove(child)
            child = nxt

        providers = self.db.list_providers()
        active_prov_id = self.current_chat.provider_id if self.current_chat else ""

        for p in providers:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("plus-menu-item")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            vbox.set_hexpand(True)
            name_lbl = Gtk.Label(label=p.name, xalign=0.0)
            mod_lbl = Gtk.Label(label=p.default_model, xalign=0.0)
            mod_lbl.add_css_class("caption")
            mod_lbl.add_css_class("dim-label")
            vbox.append(name_lbl)
            vbox.append(mod_lbl)
            row.append(vbox)
            if p.id == active_prov_id:
                row.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            btn.set_child(row)
            btn.connect("clicked", self._on_select_profile_from_popover, p.id)
            self._profile_list_box.append(btn)

    def _on_select_profile_from_popover(self, _btn: Gtk.Button, provider_id: str) -> None:
        prov = self.db.get_provider(provider_id) or self.db.get_active_provider()
        updated = self.db.update_chat_settings(
            self.current_chat.id,
            provider_id=prov.id,
            model=prov.default_model,
        )
        if updated:
            self.current_chat = updated
        self._model_menu_btn.set_label(prov.default_model)
        self._model_popover.popdown()
        self._refresh_sidebar()
        self._refresh_token_meter()

    def _build_context_meter_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(280, -1)

        self._ctx_breakdown_lbl = Gtk.Label(xalign=0.0)
        self._ctx_breakdown_lbl.add_css_class("caption")
        self._ctx_breakdown_lbl.set_wrap(True)
        box.append(self._ctx_breakdown_lbl)

        tok_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tok_lbl = Gtk.Label(label="Max Tokens", xalign=0.0)
        tok_lbl.set_hexpand(True)
        tok_row.append(tok_lbl)
        self._quick_win_tokens = Gtk.SpinButton.new_with_range(1024, 256000, 1024)
        self._quick_win_tokens.connect("value-changed", self._on_quick_window_changed)
        tok_row.append(self._quick_win_tokens)
        box.append(tok_row)

        turns_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        turns_lbl = Gtk.Label(label="Max Turns", xalign=0.0)
        turns_lbl.set_hexpand(True)
        turns_row.append(turns_lbl)
        self._quick_win_turns = Gtk.SpinButton.new_with_range(2, 200, 2)
        self._quick_win_turns.connect("value-changed", self._on_quick_window_changed)
        turns_row.append(self._quick_win_turns)
        box.append(turns_row)

        self._unload_btn = Gtk.Button(label="Unload Local Model")
        self._unload_btn.add_css_class("flat")
        self._unload_btn.connect("clicked", self._on_unload_local_model_now)
        box.append(self._unload_btn)

        pop.set_child(box)
        return pop

    def _build_composer_bar(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_vexpand(False)
        outer.set_valign(Gtk.Align.END)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_margin_top(2)
        outer.set_margin_bottom(12)

        # Single sleek borderless pill composer bar
        frame_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        frame_box.add_css_class("composer-frame")
        frame_box.set_vexpand(False)
        frame_box.set_valign(Gtk.Align.CENTER)

        # '+' Button on the left
        self._plus_btn = Gtk.MenuButton(icon_name="list-add-symbolic")
        self._plus_btn.add_css_class("circular")
        self._plus_btn.add_css_class("flat")
        self._plus_btn.add_css_class("composer-icon-btn")
        self._plus_btn.set_valign(Gtk.Align.CENTER)
        self._plus_popover = self._build_plus_menu_popover()
        self._plus_btn.set_popover(self._plus_popover)
        frame_box.append(self._plus_btn)

        # Inline Chips Box right beside the '+' button (for Web Search & active Skills)
        self._inline_chips_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._inline_chips_box.set_valign(Gtk.Align.CENTER)
        frame_box.append(self._inline_chips_box)

        # Prompt TextView with Overlay Placeholder ("Ask anything")
        self._prompt_buffer = Gtk.TextBuffer()
        self._prompt_buffer.connect("changed", self._on_prompt_buffer_changed)
        self._prompt_view = Gtk.TextView.new_with_buffer(self._prompt_buffer)
        self._prompt_view.add_css_class("composer-textview")
        self._prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._prompt_view.set_hexpand(True)
        self._prompt_view.set_vexpand(False)
        self._prompt_view.set_valign(Gtk.Align.FILL)
        self._prompt_view.set_top_margin(3)
        self._prompt_view.set_bottom_margin(3)
        self._prompt_view.set_left_margin(4)
        self._prompt_view.set_right_margin(4)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_composer_key_pressed)
        self._prompt_view.add_controller(key_ctrl)

        prompt_scroll = Gtk.ScrolledWindow()
        prompt_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        prompt_scroll.set_min_content_height(24)
        prompt_scroll.set_max_content_height(130)
        prompt_scroll.set_propagate_natural_height(True)
        prompt_scroll.set_hexpand(True)
        prompt_scroll.set_valign(Gtk.Align.CENTER)
        prompt_scroll.set_child(self._prompt_view)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_valign(Gtk.Align.CENTER)
        overlay.set_child(prompt_scroll)

        self._placeholder_lbl = Gtk.Label(label="Ask anything", xalign=0.0)
        self._placeholder_lbl.add_css_class("composer-placeholder")
        self._placeholder_lbl.set_margin_start(4)
        self._placeholder_lbl.set_valign(Gtk.Align.CENTER)
        self._placeholder_lbl.set_can_target(False)
        overlay.add_overlay(self._placeholder_lbl)

        frame_box.append(overlay)

        # Send / Stop Button (using crisp native `go-up-symbolic`)
        self._send_btn = Gtk.Button(icon_name="go-up-symbolic")
        self._send_btn.add_css_class("circular")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.add_css_class("composer-icon-btn")
        self._send_btn.set_valign(Gtk.Align.CENTER)
        self._send_btn.set_tooltip_text("Send")
        self._send_btn.connect("clicked", self._on_send_or_stop_clicked)
        frame_box.append(self._send_btn)

        outer.append(frame_box)
        return outer

    def _build_plus_menu_popover(self) -> Gtk.Popover:
        """Minimal '+' button popup with hover-activated Skills submenu."""
        pop = Gtk.Popover()
        pop.set_has_arrow(False)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hbox.set_margin_top(6)
        hbox.set_margin_bottom(6)
        hbox.set_margin_start(6)
        hbox.set_margin_end(6)

        # Primary Minimal Column: Attach File, Web Search, Skills ▸
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left_col.set_size_request(170, -1)

        # 1. Attach File
        attach_btn = Gtk.Button()
        attach_btn.add_css_class("flat")
        attach_btn.add_css_class("plus-menu-item")
        a_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        a_box.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
        a_box.append(Gtk.Label(label="Attach file", xalign=0.0))
        attach_btn.set_child(a_box)
        attach_btn.connect(
            "clicked",
            lambda *_: (pop.popdown(), self._on_attach_file_clicked()),
        )
        left_col.append(attach_btn)

        # 2. Web Search toggle
        search_btn = Gtk.Button()
        search_btn.add_css_class("flat")
        search_btn.add_css_class("plus-menu-item")
        s_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        s_box.append(Gtk.Image.new_from_icon_name("system-search-symbolic"))
        self._plus_search_lbl = Gtk.Label(label="Web search", xalign=0.0)
        self._plus_search_lbl.set_hexpand(True)
        s_box.append(self._plus_search_lbl)
        self._plus_search_check_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        s_box.append(self._plus_search_check_icon)
        search_btn.set_child(s_box)
        search_btn.connect("clicked", self._on_toggle_web_search_from_plus)
        left_col.append(search_btn)

        # 3. Skills ▸ (Reveals all skills on hover or click)
        skills_trigger = Gtk.Button()
        skills_trigger.add_css_class("flat")
        skills_trigger.add_css_class("plus-menu-item")
        sk_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sk_row.append(Gtk.Image.new_from_icon_name("starred-symbolic"))
        sk_lbl = Gtk.Label(label="Skills", xalign=0.0)
        sk_lbl.set_hexpand(True)
        sk_row.append(sk_lbl)
        sk_row.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        skills_trigger.set_child(sk_row)

        # Hover controller on "Skills ▸" opens the skills submenu column
        hover_ctrl = Gtk.EventControllerMotion()
        hover_ctrl.connect("enter", lambda *_: self._skills_revealer.set_reveal_child(True))
        skills_trigger.add_controller(hover_ctrl)
        skills_trigger.connect(
            "clicked",
            lambda *_: self._skills_revealer.set_reveal_child(
                not self._skills_revealer.get_reveal_child()
            ),
        )
        left_col.append(skills_trigger)
        hbox.append(left_col)

        # Hover-revealed Skills Submenu Column
        self._skills_revealer = Gtk.Revealer()
        self._skills_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self._skills_revealer.set_reveal_child(False)

        right_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right_wrap.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._plus_skills_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._plus_skills_box.set_size_request(190, -1)
        right_wrap.append(self._plus_skills_box)
        self._skills_revealer.set_child(right_wrap)
        hbox.append(self._skills_revealer)

        pop.set_child(hbox)
        pop.connect(
            "show",
            lambda *_: (
                self._skills_revealer.set_reveal_child(False),
                self._refresh_plus_popover_skills(),
            ),
        )
        return pop

    # -------------------------------------------------------------------------
    # Sidebar & Chat Session Management
    # -------------------------------------------------------------------------

    def _refresh_sidebar(self) -> None:
        query = self._search_entry.get_text().strip()
        chats = self.db.list_chats(search_query=query)

        child = self._chat_list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._chat_list_box.remove(child)
            child = nxt

        selected_row: Gtk.ListBoxRow | None = None

        for chat in chats:
            row = Gtk.ListBoxRow()
            row._chat_id = chat.id  # type: ignore[attr-defined]
            row.add_css_class("sidebar-chat-row")

            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

            title_prefix = "📌 " if chat.pinned else ""
            t_lbl = Gtk.Label(label=f"{title_prefix}{chat.title}", xalign=0.0)
            t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            t_lbl.set_hexpand(True)
            hbox.append(t_lbl)

            ren_btn = Gtk.Button(icon_name="document-edit-symbolic")
            ren_btn.add_css_class("flat")
            ren_btn.add_css_class("message-action-btn")
            ren_btn.set_valign(Gtk.Align.CENTER)
            ren_btn.set_tooltip_text("Rename")
            ren_btn.connect("clicked", self._on_rename_chat_clicked, chat.id)
            hbox.append(ren_btn)

            pin_btn = Gtk.Button(icon_name="view-pin-symbolic")
            pin_btn.add_css_class("flat")
            pin_btn.add_css_class("message-action-btn")
            pin_btn.set_valign(Gtk.Align.CENTER)
            pin_btn.set_tooltip_text("Pin")
            pin_btn.connect(
                "clicked",
                lambda _b, cid=chat.id, p=chat.pinned: self._on_toggle_pin(cid, not p),
            )
            hbox.append(pin_btn)

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("message-action-btn")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.set_tooltip_text("Delete")
            del_btn.connect("clicked", self._on_delete_chat_clicked, chat.id)
            hbox.append(del_btn)

            row.set_child(hbox)
            self._chat_list_box.append(row)

            if self.current_chat and chat.id == self.current_chat.id:
                selected_row = row

        if selected_row:
            self._chat_list_box.select_row(selected_row)

    def _on_sidebar_row_selected(
        self,
        _box: Gtk.ListBox,
        row: Gtk.ListBoxRow | None,
    ) -> None:
        if row is None:
            return
        chat_id = getattr(row, "_chat_id", None)
        if chat_id and (not self.current_chat or chat_id != self.current_chat.id):
            self._load_chat_into_view(chat_id)

    def action_new_chat(self) -> None:
        prov = self.db.get_active_provider()
        new_chat = self.db.create_chat(
            title="New Chat",
            provider_id=prov.id,
            model=prov.default_model,
        )
        self.current_chat = new_chat
        self._refresh_sidebar()
        self._load_chat_into_view(new_chat.id)
        self._prompt_view.grab_focus()

    def _on_rename_chat_clicked(self, _btn: Gtk.Button, chat_id: str) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        dialog = Adw.AlertDialog.new("Rename Chat", None)
        entry = Gtk.Entry(text=chat.title)
        entry.set_hexpand(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def _on_resp(dlg: Adw.AlertDialog, res: Gio.AsyncResult) -> None:
            try:
                choice = dlg.choose_finish(res)
            except Exception:
                return
            if choice == "rename":
                new_title = entry.get_text().strip() or "Untitled Chat"
                self.db.rename_chat(chat_id, new_title)
                if self.current_chat and self.current_chat.id == chat_id:
                    self.current_chat.title = new_title
                self._refresh_sidebar()

        dialog.choose(self, None, _on_resp)

    def _on_toggle_pin(self, chat_id: str, pinned: bool) -> None:
        self.db.set_chat_pinned(chat_id, pinned)
        self._refresh_sidebar()

    def _on_delete_chat_clicked(self, _btn: Gtk.Button, chat_id: str) -> None:
        self.db.delete_chat(chat_id)
        remaining = self.db.list_chats()
        if not remaining:
            self.current_chat = self.db.create_chat(title="New Chat")
        elif self.current_chat and self.current_chat.id == chat_id:
            self.current_chat = remaining[0]
        self._refresh_sidebar()
        self._load_chat_into_view(self.current_chat.id)

    # -------------------------------------------------------------------------
    # Loading Conversation Tree & Branch Controls
    # -------------------------------------------------------------------------

    def _load_chat_into_view(self, chat_id: str) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        self.current_chat = chat

        # Header shows the profile's model name
        prov = self.db.get_provider(chat.provider_id) or self.db.get_active_provider()
        self._model_menu_btn.set_label(prov.default_model)

        self._quick_win_tokens.set_value(chat.sliding_window_tokens)
        self._quick_win_turns.set_value(chat.sliding_window_turns)

        # Context usage bar optional visibility
        show_ctx_bar = bool(self.db.get_setting("show_context_bar", True))
        self._token_menu_btn.set_visible(show_ctx_bar)

        self._refresh_plus_popover_skills()
        self._refresh_inline_chips()
        self._rebuild_message_widgets()
        self._refresh_token_meter()
        if not self._search_entry.has_focus():
            GLib.idle_add(lambda: (self._prompt_view.grab_focus(), GLib.SOURCE_REMOVE)[1])

    def _rebuild_message_widgets(self) -> None:
        child = self._messages_vbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._messages_vbox.remove(child)
            child = nxt

        thread = self.db.get_active_thread(self.current_chat.id)
        raw_visible = [m for m in thread if m.role in ("user", "assistant")]

        # Coalesce intermediate tool-calling assistant nodes into the final assistant node of each turn
        visible_messages: list[MessageNode] = []
        pending_reasoning: list[str] = []
        pending_tools: list[dict] = []
        pending_citations: list[dict] = []

        for idx, msg in enumerate(raw_visible):
            if msg.role == "user":
                pending_reasoning.clear()
                pending_tools.clear()
                pending_citations.clear()
                visible_messages.append(msg)
                continue

            has_next_assistant_in_turn = (
                idx + 1 < len(raw_visible) and raw_visible[idx + 1].role == "assistant"
            )
            is_intermediate_tool_step = has_next_assistant_in_turn and (
                bool(msg.tool_calls) or msg.content.strip() in ("", "…", "*(Used tools)*")
            )
            if is_intermediate_tool_step:
                if msg.reasoning_content.strip():
                    pending_reasoning.append(msg.reasoning_content.strip())
                if msg.tool_calls:
                    pending_tools.extend(msg.tool_calls)
                if msg.citations:
                    pending_citations.extend(msg.citations)
                continue

            if pending_reasoning or pending_tools or pending_citations:
                r_parts: list[str] = []
                for rp in pending_reasoning:
                    if rp and rp not in r_parts:
                        r_parts.append(rp)
                cur_r = msg.reasoning_content.strip()
                if cur_r and cur_r not in r_parts:
                    # Avoid duplicating if cur_r already starts with the joined r_parts
                    joined_prev = "\n\n".join(r_parts)
                    if joined_prev and cur_r.startswith(joined_prev):
                        r_parts = [cur_r]
                    else:
                        r_parts.append(cur_r)
                combined_reasoning = "\n\n".join(r_parts)

                # Merge tool calls without duplicating identical call IDs
                combined_tools: list[dict] = []
                seen_tc_ids: set[str] = set()
                for tc in pending_tools + (msg.tool_calls or []):
                    tc_id = str(tc.get("id") or json.dumps(tc, sort_keys=True))
                    if tc_id not in seen_tc_ids:
                        seen_tc_ids.add(tc_id)
                        combined_tools.append(tc)

                # Merge citations without duplicating URLs
                combined_citations: list[dict] = []
                seen_urls: set[str] = set()
                for c in pending_citations + (msg.citations or []):
                    u = str(c.get("url") or "").strip().rstrip("/").lower()
                    key = u or f"{c.get('domain')}:{c.get('title')}"
                    if key not in seen_urls:
                        seen_urls.add(key)
                        combined_citations.append(c)

                cleaned_content = (
                    "" if msg.content.strip() == "*(Used tools)*" else msg.content
                )
                msg = dataclasses.replace(
                    msg,
                    content=cleaned_content,
                    reasoning_content=combined_reasoning,
                    tool_calls=combined_tools,
                    citations=combined_citations,
                )
                pending_reasoning.clear()
                pending_tools.clear()
                pending_citations.clear()

            visible_messages.append(msg)

        for msg in visible_messages:
            siblings = self.db.get_siblings(msg.id)
            row_widget = MessageWidget(
                message=msg,
                siblings=siblings,
                on_switch_branch=self._on_switch_branch,
                on_edit_and_branch=self._on_edit_message_and_branch,
                on_fork_from_here=self._on_fork_chat_from_message,
                on_regenerate_branch=self._on_regenerate_as_branch,
                on_toast=self.show_toast,
            )
            self._messages_vbox.append(row_widget)

        self._scroll_to_bottom()

    def _on_switch_branch(self, target_message_id: str) -> None:
        self.db.switch_branch(self.current_chat.id, target_message_id)
        self.current_chat = self.db.get_chat(self.current_chat.id) or self.current_chat
        self._rebuild_message_widgets()
        self._refresh_token_meter()

    def _on_edit_message_and_branch(
        self,
        original_message_id: str,
        new_content: str,
    ) -> None:
        if self._is_streaming:
            return

        branched_msg = self.db.edit_and_branch(
            chat_id=self.current_chat.id,
            original_message_id=original_message_id,
            new_content=new_content,
            skills_used=self.current_chat.active_skills,
        )
        self.current_chat = self.db.get_chat(self.current_chat.id) or self.current_chat
        self._rebuild_message_widgets()
        self._refresh_token_meter()

        if branched_msg.role == "user":
            self._start_assistant_generation()

    def _on_fork_chat_from_message(self, message_id: str) -> None:
        forked = self.db.fork_chat_from_message(
            chat_id=self.current_chat.id,
            up_to_message_id=message_id,
        )
        self.current_chat = forked
        self._refresh_sidebar()
        self._load_chat_into_view(forked.id)

    def _on_regenerate_as_branch(self, assistant_message_id: str) -> None:
        if self._is_streaming:
            return
        msg = self.db.get_message(assistant_message_id)
        if not msg or not msg.parent_id:
            return
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE chats SET active_leaf_id = ? WHERE id = ?",
                (msg.parent_id, self.current_chat.id),
            )
        self.current_chat = self.db.get_chat(self.current_chat.id) or self.current_chat
        self._start_assistant_generation()

    # -------------------------------------------------------------------------
    # Minimal '+' Menu & Inline Chips Beside '+' Button
    # -------------------------------------------------------------------------

    def _refresh_plus_popover_skills(self) -> None:
        self._plus_search_check_icon.set_visible(self.current_chat.web_search_enabled)

        child = self._plus_skills_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._plus_skills_box.remove(child)
            child = nxt

        active_set = set(self.current_chat.active_skills or [])
        for sk in self.skills_mgr.list_skills():
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("plus-menu-item")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=sk.name, xalign=0.0)
            lbl.set_hexpand(True)
            row.append(lbl)
            if sk.slug in active_set:
                row.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            btn.set_child(row)
            btn.connect("clicked", self._on_toggle_skill_from_plus, sk.slug)
            self._plus_skills_box.append(btn)

    def _on_toggle_skill_from_plus(self, _btn: Gtk.Button, slug: str) -> None:
        active = list(self.current_chat.active_skills or [])
        if slug in active:
            active = [s for s in active if s != slug]
        else:
            active.append(slug)
        updated = self.db.update_chat_settings(
            self.current_chat.id, active_skills=active
        )
        if updated:
            self.current_chat = updated
        self._refresh_plus_popover_skills()
        self._refresh_inline_chips()
        self._refresh_token_meter()

    def _on_toggle_web_search_from_plus(self, _btn: Gtk.Button) -> None:
        new_state = not self.current_chat.web_search_enabled
        updated = self.db.update_chat_settings(
            self.current_chat.id, web_search_enabled=new_state
        )
        if updated:
            self.current_chat = updated
        self._refresh_plus_popover_skills()
        self._refresh_inline_chips()
        self._plus_popover.popdown()

    def _refresh_inline_chips(self) -> None:
        """Render active Web Search & Skill chips right beside the '+' button inside the chatbox."""
        child = self._inline_chips_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._inline_chips_box.remove(child)
            child = nxt

        if self.current_chat.web_search_enabled:
            ws_btn = Gtk.Button(label="🌐 Search ✕")
            ws_btn.add_css_class("inline-chip")
            ws_btn.add_css_class("flat")
            ws_btn.set_tooltip_text("Disable web search")
            ws_btn.connect("clicked", self._on_disable_web_search_chip)
            self._inline_chips_box.append(ws_btn)

        for slug in self.current_chat.active_skills or []:
            sk = self.skills_mgr.get_skill(slug)
            title = sk.name if sk else slug
            btn = Gtk.Button(label=f"{title} ✕")
            btn.add_css_class("inline-chip")
            btn.add_css_class("flat")
            btn.set_tooltip_text("Remove skill")
            btn.connect("clicked", self._on_remove_skill_chip, slug)
            self._inline_chips_box.append(btn)

    def _on_disable_web_search_chip(self, _btn: Gtk.Button) -> None:
        updated = self.db.update_chat_settings(
            self.current_chat.id, web_search_enabled=False
        )
        if updated:
            self.current_chat = updated
        self._refresh_inline_chips()

    def _on_remove_skill_chip(self, _btn: Gtk.Button, slug: str) -> None:
        active = [s for s in (self.current_chat.active_skills or []) if s != slug]
        updated = self.db.update_chat_settings(
            self.current_chat.id, active_skills=active
        )
        if updated:
            self.current_chat = updated
        self._refresh_inline_chips()
        self._refresh_token_meter()

    def _on_attach_file_clicked(self) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Attach File")

        def _on_opened(dlg: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
            try:
                gfile = dlg.open_finish(res)
            except Exception:
                return
            if not gfile:
                return
            path_str = gfile.get_path()
            if not path_str:
                return
            p = Path(path_str)
            try:
                content = p.read_text(encoding="utf-8", errors="replace")[:24000]
            except Exception as exc:
                self.show_toast(f"Could not read file: {exc}")
                return
            ext = p.suffix.lstrip(".") or "text"
            start, end = self._prompt_buffer.get_bounds()
            existing = self._prompt_buffer.get_text(start, end, True)
            attached = f"{existing}\n\nFile `{p.name}`:\n```{ext}\n{content}\n```\n".lstrip()
            self._prompt_buffer.set_text(attached)
            self._prompt_view.grab_focus()

        dialog.open(self, None, _on_opened)

    # -------------------------------------------------------------------------
    # Real-Time Context Progress Bar & Token Meter
    # -------------------------------------------------------------------------

    def _on_prompt_buffer_changed(self, buf: Gtk.TextBuffer) -> None:
        start, end = buf.get_bounds()
        txt = buf.get_text(start, end, True)
        self._placeholder_lbl.set_visible(len(txt) == 0)
        self._refresh_token_meter()

    def _compute_current_breakdown(self) -> ContextBreakdown:
        thread = self.db.get_active_thread(self.current_chat.id)
        skills_md = self.skills_mgr.load_active_skills_markdown(
            self.current_chat.active_skills
        )
        memories = self.db.list_memories(only_enabled=True)
        mem_txt = "\n".join(f"- {m['fact']}" for m in memories)
        start, end = self._prompt_buffer.get_bounds()
        draft = self._prompt_buffer.get_text(start, end, True)
        tools_schema = BUILTIN_TOOLS_SCHEMA if self.current_chat.tools_enabled else None

        return build_sliding_window(
            chat=self.current_chat,
            thread=thread,
            skills_markdown=skills_md,
            memories_text=mem_txt,
            draft_text=draft,
            tools_schema=tools_schema,
        )

    def _refresh_token_meter(self) -> None:
        bd = self._compute_current_breakdown()
        self._header_context_bar.set_fraction(bd.usage_fraction)
        self._token_menu_btn.set_tooltip_text(
            f"Context: {bd.total_tokens:,} / {bd.max_tokens:,} tokens"
        )
        self._ctx_breakdown_lbl.set_label(
            f"Context: {bd.total_tokens:,} / {bd.max_tokens:,} tokens "
            f"({bd.included_messages} messages in window)"
        )

        prov = self.db.get_provider(self.current_chat.provider_id) or self.db.get_active_provider()
        self._unload_btn.set_visible(prov.provider_type in ("ollama", "llamacpp"))

    def _on_quick_window_changed(self, _spin: Gtk.SpinButton) -> None:
        tok_val = int(self._quick_win_tokens.get_value())
        turns_val = int(self._quick_win_turns.get_value())
        updated = self.db.update_chat_settings(
            self.current_chat.id,
            sliding_window_tokens=tok_val,
            sliding_window_turns=turns_val,
        )
        if updated:
            self.current_chat = updated
        self._refresh_token_meter()

    def _on_unload_local_model_now(self, _btn: Gtk.Button) -> None:
        prov = self.db.get_provider(self.current_chat.provider_id) or self.db.get_active_provider()
        model = self.current_chat.model

        def _worker() -> None:
            self.ai_client.unload_local_model(prov, model)
            self.idle_monitor.mark_unloaded(prov.id)
            GLib.idle_add(self.show_toast, f"Unloaded {model}")
            GLib.idle_add(self._refresh_token_meter)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_idle_timer_tick(self) -> bool:
        self.idle_monitor.check_and_unload_expired()
        self._refresh_token_meter()
        return GLib.SOURCE_CONTINUE

    # -------------------------------------------------------------------------
    # Sending Messages, Web Search Grounding, Tool Calling & Streaming
    # -------------------------------------------------------------------------

    def _on_composer_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if not (state & Gdk.ModifierType.SHIFT_MASK):
                self._on_send_or_stop_clicked()
                return True
        return False

    def _on_send_or_stop_clicked(self, *_args: object) -> None:
        if self._is_streaming:
            if self._streaming_stop_event:
                self._streaming_stop_event.set()
            return

        start, end = self._prompt_buffer.get_bounds()
        user_text = self._prompt_buffer.get_text(start, end, True).strip()
        if not user_text:
            return

        self._prompt_buffer.set_text("")

        thread = self.db.get_active_thread(self.current_chat.id)
        if not thread and self.current_chat.title in ("New Chat", "Welcome Chat"):
            short_title = user_text.splitlines()[0][:44].strip()
            if len(user_text) > 44:
                short_title += "…"
            self.db.rename_chat(self.current_chat.id, short_title)
            self.current_chat.title = short_title

        self.db.add_message(
            chat_id=self.current_chat.id,
            role="user",
            content=user_text,
            skills_used=list(self.current_chat.active_skills or []),
        )
        self.current_chat = self.db.get_chat(self.current_chat.id) or self.current_chat
        self._refresh_sidebar()
        self._rebuild_message_widgets()
        self._start_assistant_generation(user_query_for_search=user_text)

    def _start_assistant_generation(self, user_query_for_search: str = "") -> None:
        if self._is_streaming:
            return

        self._is_streaming = True
        self._streaming_stop_event = threading.Event()
        self._send_btn.set_icon_name("media-playback-stop-symbolic")
        self._send_btn.remove_css_class("suggested-action")
        self._send_btn.add_css_class("destructive-action")
        self._send_btn.set_tooltip_text("Stop")

        prov = self.db.get_provider(self.current_chat.provider_id) or self.db.get_active_provider()
        self.idle_monitor.record_activity(prov, self.current_chat.model)

        placeholder_node = self.db.add_message(
            chat_id=self.current_chat.id,
            role="assistant",
            content="…",
            skills_used=list(self.current_chat.active_skills or []),
        )
        self.current_chat = self.db.get_chat(self.current_chat.id) or self.current_chat

        siblings = self.db.get_siblings(placeholder_node.id)
        assistant_widget = MessageWidget(
            message=placeholder_node,
            siblings=siblings,
            on_switch_branch=self._on_switch_branch,
            on_edit_and_branch=self._on_edit_message_and_branch,
            on_fork_from_here=self._on_fork_chat_from_message,
            on_regenerate_branch=self._on_regenerate_as_branch,
            on_toast=self.show_toast,
        )
        self._messages_vbox.append(assistant_widget)
        self._scroll_to_bottom()

        chat_snapshot = self.current_chat
        stop_ev = self._streaming_stop_event

        def _worker() -> None:
            collected_citations: list[Citation] = []
            web_search_context = ""

            if chat_snapshot.web_search_enabled:
                search_q = user_query_for_search
                if not search_q:
                    th = self.db.get_active_thread(chat_snapshot.id)
                    for m in reversed(th):
                        if m.role == "user":
                            search_q = m.content
                            break
                if search_q:
                    found = search_web(search_q, max_results=5, start_index=1)
                    if found:
                        collected_citations.extend(found)
                        web_search_context = format_citations_for_prompt(found)

            skills_md = self.skills_mgr.load_active_skills_markdown(
                chat_snapshot.active_skills
            )
            memories = self.db.list_memories(only_enabled=True)
            mem_txt = "\n".join(f"- {m['fact']}" for m in memories)
            tools_schema = (
                BUILTIN_TOOLS_SCHEMA if chat_snapshot.tools_enabled else None
            )

            current_assistant_node = placeholder_node
            accumulated_tool_calls: list[dict] = []
            accumulated_reasoning: list[str] = []
            max_tool_rounds = 6

            for _round in range(max_tool_rounds + 1):
                full_thread = self.db.get_active_thread(chat_snapshot.id)
                history_before_assistant = [
                    m for m in full_thread if m.id != current_assistant_node.id
                ]
                # On the final synthesis round after max_tool_rounds, pass tools_schema=None
                # so the model is guaranteed to synthesize a final visible text response.
                round_tools_schema = (
                    tools_schema if _round < max_tool_rounds else None
                )
                bd = build_sliding_window(
                    chat=chat_snapshot,
                    thread=history_before_assistant,
                    skills_markdown=skills_md,
                    memories_text=mem_txt,
                    tools_schema=round_tools_schema,
                )

                def _on_delta(visible_txt: str, reasoning_txt: str) -> None:
                    r_parts = list(accumulated_reasoning)
                    if reasoning_txt.strip():
                        r_parts.append(reasoning_txt.strip())
                    c_dicts = [c.to_dict() for c in collected_citations]
                    GLib.idle_add(
                        lambda vt=visible_txt, rt="\n\n".join(r_parts), cd=c_dicts: (
                            assistant_widget.update_streaming_content(
                                vt,
                                rt,
                                citations=cd,
                                is_streaming=True,
                            ),
                            GLib.SOURCE_REMOVE,
                        )[1]
                    )

                stream_res = self.ai_client.stream_chat(
                    provider=prov,
                    chat=chat_snapshot,
                    window_messages=bd.window_messages,
                    skills_markdown=skills_md,
                    memories_text=mem_txt,
                    web_search_context=web_search_context,
                    tools_schema=round_tools_schema,
                    on_delta=_on_delta,
                    stop_event=stop_ev,
                )

                if stream_res.error:
                    err_md = f"**Connection Error:** `{stream_res.error}`"
                    updated_node = self.db.update_message(
                        current_assistant_node.id,
                        content=err_md,
                        reasoning_content=stream_res.reasoning_content,
                    )
                    if updated_node:
                        GLib.idle_add(assistant_widget.finalize_message, updated_node)
                    break

                has_more_rounds = (
                    bool(stream_res.tool_calls)
                    and not stream_res.stopped
                    and _round < max_tool_rounds
                )

                if has_more_rounds:
                    if stream_res.reasoning_content.strip():
                        accumulated_reasoning.append(stream_res.reasoning_content.strip())
                    accumulated_tool_calls.extend(stream_res.tool_calls)

                    # Save only this round's own reasoning & tool_calls on its DB node
                    self.db.update_message(
                        current_assistant_node.id,
                        content=stream_res.content or "",
                        reasoning_content=stream_res.reasoning_content,
                        tool_calls=stream_res.tool_calls,
                        prompt_tokens=stream_res.prompt_tokens,
                        completion_tokens=stream_res.completion_tokens,
                        cached_tokens=stream_res.cached_tokens,
                    )

                    # Keep the single live widget updated with tool calls + animated waiting dots
                    GLib.idle_add(
                        assistant_widget.update_streaming_tools,
                        list(accumulated_tool_calls),
                    )
                    GLib.idle_add(
                        assistant_widget.update_streaming_content,
                        "",
                        "\n\n".join(accumulated_reasoning),
                    )

                    for tc in stream_res.tool_calls:
                        if stop_ev and stop_ev.is_set():
                            break
                        tc_id = tc.get("id") or "call_0"
                        fn = tc.get("function") or {}
                        fn_name = fn.get("name") or ""
                        fn_args = fn.get("arguments") or "{}"
                        tool_out, new_cits = execute_tool_call(
                            fn_name,
                            fn_args,
                            existing_citations=collected_citations,
                        )
                        if new_cits:
                            collected_citations.extend(new_cits)
                        self.db.add_message(
                            chat_id=chat_snapshot.id,
                            role="tool",
                            content=tool_out,
                            tool_call_id=tc_id,
                        )

                    current_assistant_node = self.db.add_message(
                        chat_id=chat_snapshot.id,
                        role="assistant",
                        content="…",
                        skills_used=list(chat_snapshot.active_skills or []),
                    )
                    continue

                # Final round: store this round's own reasoning & tool calls on the final DB node
                # (_rebuild_message_widgets coalesces all rounds in the turn into one MessageWidget)
                citation_dicts = [c.to_dict() for c in collected_citations]
                updated_node = self.db.update_message(
                    current_assistant_node.id,
                    content=stream_res.content,
                    reasoning_content=stream_res.reasoning_content,
                    tool_calls=stream_res.tool_calls,
                    citations=citation_dicts,
                    prompt_tokens=stream_res.prompt_tokens,
                    completion_tokens=stream_res.completion_tokens,
                    cached_tokens=stream_res.cached_tokens,
                )
                break

            GLib.idle_add(self._finish_streaming_ui)

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_streaming_ui(self) -> bool:
        self._is_streaming = False
        self._streaming_stop_event = None
        self._send_btn.set_icon_name("go-up-symbolic")
        self._send_btn.remove_css_class("destructive-action")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.set_tooltip_text("Send")
        self.current_chat = self.db.get_chat(self.current_chat.id) or self.current_chat
        self._rebuild_message_widgets()
        self._refresh_token_meter()
        return GLib.SOURCE_REMOVE

    def _scroll_to_bottom(self) -> None:
        def _do_scroll() -> bool:
            vadj = self._msg_scroller.get_vadjustment()
            if vadj:
                vadj.set_value(vadj.get_upper() - vadj.get_page_size())
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_do_scroll)

    # -------------------------------------------------------------------------
    # Dialogs & Toasts
    # -------------------------------------------------------------------------

    def open_preferences_dialog(self, initial_page: str = "providers") -> None:
        dlg = PreferencesDialog(
            db=self.db,
            ai_client=self.ai_client,
            current_chat=self.current_chat,
            on_changed=lambda: self._load_chat_into_view(self.current_chat.id),
            initial_page=initial_page,
        )
        dlg.present(self)

    def open_skills_dialog(self) -> None:
        dlg = SkillsDialog(
            skills_mgr=self.skills_mgr,
            on_changed=lambda: (
                self._refresh_plus_popover_skills(),
                self._refresh_inline_chips(),
                self._refresh_token_meter(),
            ),
        )
        dlg.present(self)

    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(2)
        self._toast_overlay.add_toast(toast)
