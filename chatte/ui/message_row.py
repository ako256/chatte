"""Clean, minimal native GTK 4 message widgets matching modern chat proportions."""

from __future__ import annotations

import json
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk, Pango

from chatte.storage.database import MessageNode
from chatte.ui.markdown_view import MarkdownView


class MessageWidget(Gtk.Box):
    """Renders a user bubble (right-aligned) or assistant response (left-aligned, borderless)."""

    def __init__(
        self,
        message: MessageNode,
        siblings: list[MessageNode],
        *,
        on_switch_branch: Callable[[str], None] | None = None,
        on_edit_and_branch: Callable[[str, str], None] | None = None,
        on_fork_from_here: Callable[[str], None] | None = None,
        on_regenerate_branch: Callable[[str], None] | None = None,
        on_toast: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.message = message
        self.siblings = siblings
        self._on_switch_branch = on_switch_branch
        self._on_edit_and_branch = on_edit_and_branch
        self._on_fork_from_here = on_fork_from_here
        self._on_regenerate_branch = on_regenerate_branch
        self._on_toast = on_toast

        is_user = message.role == "user"
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        if is_user:
            self.set_halign(Gtk.Align.END)
        else:
            self.set_halign(Gtk.Align.FILL)
            self.set_hexpand(True)
            self.add_css_class("assistant-message")

        # 1. Collapsible Reasoning / Thought Process + Tool Usage (hidden inside Thought process)
        self._reasoning_expander = Gtk.Expander(label="Thought process")
        self._reasoning_expander.add_css_class("thought-expander")
        self._reasoning_expander.set_can_focus(False)

        self._thought_inner_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        self._thought_inner_box.add_css_class("thought-content")

        self._reasoning_label = Gtk.Label(
            label=message.reasoning_content,
            xalign=0.0,
            yalign=0.0,
        )
        self._reasoning_label.set_wrap(True)
        self._reasoning_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._reasoning_label.set_selectable(False)
        self._reasoning_label.set_can_focus(False)
        self._reasoning_label.set_visible(bool(message.reasoning_content.strip()))
        self._thought_inner_box.append(self._reasoning_label)

        # Collapsible Tool Calls nested inside the hidden Thought process expander
        self._tools_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._tools_box.set_visible(bool(message.tool_calls))
        self._populate_tool_calls(message.tool_calls)
        self._thought_inner_box.append(self._tools_box)

        self._reasoning_expander.set_child(self._thought_inner_box)
        self._reasoning_expander.set_visible(
            bool(message.reasoning_content.strip()) or bool(message.tool_calls)
        )
        if not is_user:
            self.append(self._reasoning_expander)

        # 2b. Animated Three-Dots Waiting Indicator (while waiting for assistant output)
        self._waiting_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._waiting_box.add_css_class("waiting-dots-box")
        self._waiting_dots: list[Gtk.Label] = []
        for idx in range(3):
            dot = Gtk.Label(label="●")
            dot.add_css_class("waiting-dot")
            if idx == 0:
                dot.add_css_class("waiting-dot-active")
            self._waiting_dots.append(dot)
            self._waiting_box.append(dot)
        self._waiting_step = 0
        self._waiting_timer_id: int | None = None
        self._stream_flush_timer_id: int | None = None
        self._pending_stream_content: str = ""

        is_waiting_initial = not is_user and message.content.strip() in ("", "…")
        self._waiting_box.set_visible(is_waiting_initial)
        if not is_user:
            self.append(self._waiting_box)
        if is_waiting_initial:
            self._start_waiting_animation()
        self.connect(
            "destroy",
            lambda *_: (
                self._stop_waiting_animation(),
                self._cancel_stream_flush_timer(),
            ),
        )

        # 3. Main Content Stack (View vs Inline Edit)
        self._content_stack = Gtk.Stack()
        self._content_stack.set_vhomogeneous(False)
        self._content_stack.set_hhomogeneous(False)
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        initial_md = "" if is_waiting_initial else message.content
        self.markdown_view = MarkdownView(
            markdown_text=initial_md,
            citations=message.citations,
            on_toast=on_toast,
        )

        if is_user:
            bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            bubble.add_css_class("user-bubble")
            bubble.set_halign(Gtk.Align.END)
            bubble.append(self.markdown_view)
            self._content_stack.add_named(bubble, "view")
        else:
            self._content_stack.add_named(self.markdown_view, "view")

        # Inline Editor for "Edit -> Auto-Branch"
        edit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        edit_box.add_css_class("user-bubble")
        self._edit_buffer = Gtk.TextBuffer()
        self._edit_buffer.set_text(message.content)
        self._edit_textview = Gtk.TextView.new_with_buffer(self._edit_buffer)
        self._edit_textview.add_css_class("composer-textview")
        self._edit_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        edit_scroll = Gtk.ScrolledWindow()
        edit_scroll.set_min_content_height(60)
        edit_scroll.set_max_content_height(220)
        edit_scroll.set_min_content_width(320)
        edit_scroll.set_propagate_natural_height(True)
        edit_scroll.set_child(self._edit_textview)
        edit_box.append(edit_scroll)

        edit_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        edit_actions.set_halign(Gtk.Align.END)

        cancel_edit_btn = Gtk.Button(label="Cancel")
        cancel_edit_btn.add_css_class("flat")
        cancel_edit_btn.add_css_class("pill")
        cancel_edit_btn.connect(
            "clicked", lambda *_: self._content_stack.set_visible_child_name("view")
        )
        edit_actions.append(cancel_edit_btn)

        save_branch_btn = Gtk.Button(label="Send")
        save_branch_btn.add_css_class("suggested-action")
        save_branch_btn.add_css_class("pill")
        save_branch_btn.connect("clicked", self._on_save_edit_branch)
        edit_actions.append(save_branch_btn)
        edit_box.append(edit_actions)

        self._content_stack.add_named(edit_box, "edit")
        self.append(self._content_stack)

        # 4. Subtle Action Row right under the message (Copy, Edit, Regenerate, Fork, Citations, Branch < 1/N >)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        footer.add_css_class("message-actions")
        footer.set_halign(Gtk.Align.END if is_user else Gtk.Align.START)
        footer.set_visible(not is_waiting_initial)
        self._footer = footer

        # Copy button
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.add_css_class("flat")
        copy_btn.add_css_class("message-action-btn")
        copy_btn.set_tooltip_text("Copy")
        copy_btn.connect("clicked", self._on_copy_message)
        footer.append(copy_btn)

        # Edit (Auto-Branch) button
        edit_btn = Gtk.Button(icon_name="document-edit-symbolic")
        edit_btn.add_css_class("flat")
        edit_btn.add_css_class("message-action-btn")
        edit_btn.set_tooltip_text("Edit message")
        edit_btn.connect("clicked", self._on_start_edit)
        footer.append(edit_btn)

        # Regenerate button (for assistant messages)
        if not is_user:
            regen_btn = Gtk.Button(icon_name="view-refresh-symbolic")
            regen_btn.add_css_class("flat")
            regen_btn.add_css_class("message-action-btn")
            regen_btn.set_tooltip_text("Regenerate")
            regen_btn.connect(
                "clicked",
                lambda *_: self._on_regenerate_branch
                and self._on_regenerate_branch(self.message.id),
            )
            footer.append(regen_btn)

        # Branch / Fork Chat From Here button
        fork_btn = Gtk.Button(icon_name="network-transmit-receive-symbolic")
        fork_btn.add_css_class("flat")
        fork_btn.add_css_class("message-action-btn")
        fork_btn.set_tooltip_text("Branch into new chat from here")
        fork_btn.connect(
            "clicked",
            lambda *_: self._on_fork_from_here
            and self._on_fork_from_here(self.message.id),
        )
        footer.append(fork_btn)

        # Citations toggle button beside the branch button (hidden unless citations exist)
        self._citations_btn = Gtk.ToggleButton(icon_name="insert-link-symbolic")
        self._citations_btn.add_css_class("flat")
        self._citations_btn.add_css_class("message-action-btn")
        self._citations_btn.set_tooltip_text("Sources")
        self._citations_btn.set_visible(not is_user and bool(message.citations))
        self._citations_btn.connect(
            "toggled",
            lambda btn: self._citations_revealer.set_reveal_child(btn.get_active()),
        )
        if not is_user:
            footer.append(self._citations_btn)

        # Branch Switcher (< 1 / N >) when multiple sibling branches exist
        if len(siblings) > 1:
            curr_idx = 0
            for idx, sib in enumerate(siblings):
                if sib.id == message.id:
                    curr_idx = idx
                    break

            branch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            branch_box.add_css_class("branch-switcher")

            prev_btn = Gtk.Button(icon_name="go-previous-symbolic")
            prev_btn.add_css_class("flat")
            prev_btn.add_css_class("message-action-btn")
            prev_btn.set_sensitive(curr_idx > 0)
            prev_btn.set_tooltip_text("Previous branch")
            if curr_idx > 0:
                prev_id = siblings[curr_idx - 1].id
                prev_btn.connect(
                    "clicked",
                    lambda _b, tid=prev_id: self._on_switch_branch
                    and self._on_switch_branch(tid),
                )
            branch_box.append(prev_btn)

            branch_lbl = Gtk.Label(label=f"{curr_idx + 1}/{len(siblings)}")
            branch_lbl.add_css_class("caption")
            branch_lbl.set_margin_start(2)
            branch_lbl.set_margin_end(2)
            branch_box.append(branch_lbl)

            next_btn = Gtk.Button(icon_name="go-next-symbolic")
            next_btn.add_css_class("flat")
            next_btn.add_css_class("message-action-btn")
            next_btn.set_sensitive(curr_idx + 1 < len(siblings))
            next_btn.set_tooltip_text("Next branch")
            if curr_idx + 1 < len(siblings):
                next_id = siblings[curr_idx + 1].id
                next_btn.connect(
                    "clicked",
                    lambda _b, tid=next_id: self._on_switch_branch
                    and self._on_switch_branch(tid),
                )
            branch_box.append(next_btn)
            footer.append(branch_box)

        self.append(footer)

        # 5. Hidden Citations Drawer (toggled by the Sources button beside the branch button)
        self._citations_revealer = Gtk.Revealer()
        self._citations_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._citations_revealer.set_reveal_child(False)
        self._citations_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._citations_box.set_margin_top(4)
        self._citations_revealer.set_child(self._citations_box)
        self._populate_citations(message.citations)
        if not is_user:
            self.append(self._citations_revealer)

    def _start_waiting_animation(self) -> None:
        self._waiting_box.set_visible(True)
        if hasattr(self, "_footer"):
            self._footer.set_visible(False)
        if self._waiting_timer_id is None:
            self._waiting_timer_id = GLib.timeout_add(260, self._tick_waiting_dots)

    def _stop_waiting_animation(self) -> None:
        if self._waiting_timer_id is not None:
            GLib.source_remove(self._waiting_timer_id)
            self._waiting_timer_id = None
        self._waiting_box.set_visible(False)
        if hasattr(self, "_footer"):
            self._footer.set_visible(True)

    def _tick_waiting_dots(self) -> bool:
        if not self._waiting_box.get_visible():
            self._waiting_timer_id = None
            return GLib.SOURCE_REMOVE
        self._waiting_step = (self._waiting_step + 1) % 3
        for idx, dot in enumerate(self._waiting_dots):
            if idx == self._waiting_step:
                dot.add_css_class("waiting-dot-active")
            else:
                dot.remove_css_class("waiting-dot-active")
        return GLib.SOURCE_CONTINUE

    def _update_thought_visibility(self) -> None:
        has_reasoning = bool(self.message.reasoning_content.strip())
        has_tools = bool(self.message.tool_calls)
        self._reasoning_label.set_visible(has_reasoning)
        self._tools_box.set_visible(has_tools)
        self._reasoning_expander.set_visible(has_reasoning or has_tools)

    def update_streaming_tools(self, tool_calls: list[dict[str, Any]]) -> None:
        self.message.tool_calls = tool_calls
        self._populate_tool_calls(tool_calls)
        self._update_thought_visibility()

    def _cancel_stream_flush_timer(self) -> None:
        if self._stream_flush_timer_id is not None:
            GLib.source_remove(self._stream_flush_timer_id)
            self._stream_flush_timer_id = None

    def _flush_buffered_stream(self) -> bool:
        self._stream_flush_timer_id = None
        content = self._pending_stream_content
        if content.strip() in ("", "…"):
            self._start_waiting_animation()
            self.markdown_view.set_markdown(
                "",
                citations=self.message.citations,
                is_streaming=True,
            )
        else:
            self._stop_waiting_animation()
            self.markdown_view.set_markdown(
                content,
                citations=self.message.citations,
                is_streaming=True,
            )
        return GLib.SOURCE_REMOVE

    def update_streaming_content(
        self,
        content: str,
        reasoning_content: str = "",
        *,
        citations: list[dict[str, Any]] | None = None,
        is_streaming: bool = True,
    ) -> None:
        self.message.content = content
        self.message.reasoning_content = reasoning_content
        if citations is not None:
            self.message.citations = citations
        if reasoning_content.strip():
            self._reasoning_label.set_text(reasoning_content)
        self._update_thought_visibility()

        if not is_streaming:
            self._cancel_stream_flush_timer()
            self._stop_waiting_animation()
            self.markdown_view.set_markdown(
                content,
                citations=self.message.citations,
                is_streaming=False,
            )
            return

        if content.strip() in ("", "…"):
            self._cancel_stream_flush_timer()
            self._start_waiting_animation()
            self.markdown_view.set_markdown(
                "",
                citations=self.message.citations,
                is_streaming=True,
            )
            return

        # Buffer incoming stream output briefly (65ms) so Markdown blocks & inline styles
        # are formatted cleanly before showing to the user.
        self._pending_stream_content = content
        if self._stream_flush_timer_id is None:
            self._stream_flush_timer_id = GLib.timeout_add(65, self._flush_buffered_stream)

    def finalize_message(self, updated: MessageNode) -> None:
        self._cancel_stream_flush_timer()
        self._stop_waiting_animation()
        self.message = updated
        if updated.reasoning_content.strip():
            self._reasoning_label.set_text(updated.reasoning_content)
        self._populate_tool_calls(updated.tool_calls)
        self._update_thought_visibility()
        self._populate_citations(updated.citations)
        self.markdown_view.set_markdown(
            updated.content,
            citations=updated.citations,
            is_streaming=False,
        )

    def _populate_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        child = self._tools_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._tools_box.remove(child)
            child = nxt

        if not tool_calls:
            self._tools_box.set_visible(False)
            return

        self._tools_box.set_visible(True)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or "tool"
            args = fn.get("arguments") or "{}"
            exp = Gtk.Expander(label=f"Used tool: {name}")
            exp.add_css_class("tool-call-box")
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
                pretty = json.dumps(parsed, indent=2)
            except Exception:
                pretty = str(args)
            lbl = Gtk.Label(label=pretty, xalign=0.0)
            lbl.set_selectable(True)
            lbl.set_wrap(True)
            lbl.add_css_class("monospace")
            lbl.set_margin_start(8)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            exp.set_child(lbl)
            self._tools_box.append(exp)

    def _populate_citations(self, citations: list[dict[str, Any]]) -> None:
        child = self._citations_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._citations_box.remove(child)
            child = nxt

        # Deduplicate citations by normalized URL while preserving first index
        unique_citations: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for c in citations or []:
            norm_url = str(c.get("url") or "").strip().rstrip("/").lower()
            key = norm_url or f"{c.get('domain')}:{c.get('title')}"
            if key in seen_urls:
                continue
            seen_urls.add(key)
            unique_citations.append(c)

        has_citations = self.message.role != "user" and bool(unique_citations)
        if hasattr(self, "_citations_btn"):
            self._citations_btn.set_visible(has_citations)
            self._citations_btn.set_tooltip_text(
                f"Sources ({len(unique_citations)})" if has_citations else "Sources"
            )

        if not unique_citations:
            self._citations_box.set_visible(False)
            return

        self._citations_box.set_visible(True)
        flow = Gtk.FlowBox()
        flow.add_css_class("citations-flowbox")
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(4)
        flow.set_column_spacing(6)
        flow.set_row_spacing(4)

        for idx, c in enumerate(unique_citations, start=1):
            c_idx = c.get("index") or idx
            title = str(c.get("title") or c.get("domain") or "Source")
            domain = str(c.get("domain") or "web")
            url = str(c.get("url") or "")
            snippet = str(c.get("snippet") or "")

            short_title = title if len(title) <= 28 else title[:25] + "…"
            btn = Gtk.Button(label=f"[{c_idx}] {domain} · {short_title}")
            btn.add_css_class("citation-chip")
            btn.set_can_focus(False)
            btn.set_tooltip_text(f"{title}\n{url}\n\n{snippet}")
            if url.startswith(("http://", "https://")):
                btn.connect(
                    "clicked",
                    lambda _b, u=url: Gtk.UriLauncher.new(u).launch(None, None, None),
                )
            child_wrap = Gtk.FlowBoxChild()
            child_wrap.set_can_focus(False)
            child_wrap.set_child(btn)
            flow.append(child_wrap)

        self._citations_box.append(flow)

    def _on_start_edit(self, _btn: Gtk.Button) -> None:
        self._edit_buffer.set_text(self.message.content)
        self._content_stack.set_visible_child_name("edit")
        self._edit_textview.grab_focus()

    def _on_save_edit_branch(self, _btn: Gtk.Button) -> None:
        start, end = self._edit_buffer.get_bounds()
        new_text = self._edit_buffer.get_text(start, end, True).strip()
        self._content_stack.set_visible_child_name("view")
        if new_text and self._on_edit_and_branch:
            self._on_edit_and_branch(self.message.id, new_text)

    def _on_copy_message(self, _btn: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(self.message.content)
        if self._on_toast:
            self._on_toast("Copied")
