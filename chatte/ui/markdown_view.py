"""100% Native GTK 4 Markdown, GtkSourceView 5 Code Block, and LaTeX Block Renderer."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gdk, GLib, Gtk, GtkSource, Pango

from chatte.ui.latex_widget import LatexBlockWidget, latex_inline_to_pango

GtkSource.init()

_LANG_ALIASES: dict[str, str] = {
    "py": "python3",
    "python": "python3",
    "js": "js",
    "javascript": "js",
    "ts": "typescript",
    "typescript": "typescript",
    "sh": "sh",
    "bash": "sh",
    "zsh": "sh",
    "shell": "sh",
    "c++": "cpp",
    "cpp": "cpp",
    "cxx": "cpp",
    "c#": "c-sharp",
    "cs": "c-sharp",
    "rs": "rust",
    "yml": "yaml",
    "md": "markdown",
    "tex": "latex",
    "dockerfile": "dockerfile",
}


@dataclass
class MdBlock:
    kind: str  # "paragraph" | "heading" | "code" | "latex" | "blockquote" | "list" | "table" | "hr"
    text: str = ""
    level: int = 1
    lang: str = ""
    ordered: bool = False
    items: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


def inline_markdown_to_pango(
    text: str,
    citation_map: dict[int, dict[str, Any]] | None = None,
) -> str:
    """Convert inline Markdown + inline LaTeX ($...$, \\(...\\)) + [n] citations to Pango markup."""
    if not text:
        return ""

    placeholders: list[str] = []

    def _stash(pango_fragment: str) -> str:
        idx = len(placeholders)
        placeholders.append(pango_fragment)
        return f"\x00PH{idx}PH\x00"

    # 1. Inline code `...`
    def _repl_code(m: re.Match[str]) -> str:
        code_txt = html.escape(m.group(1))
        return _stash(f"<tt><b>{code_txt}</b></tt>")

    s = re.sub(r"`([^`\n]+)`", _repl_code, text)

    # 2. Inline LaTeX \(...\) and $...$
    def _repl_latex_paren(m: re.Match[str]) -> str:
        return _stash(latex_inline_to_pango(m.group(1)))

    s = re.sub(r"\\\((.+?)\\\)", _repl_latex_paren, s)

    def _repl_latex_dollar(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if not inner:
            return m.group(0)
        return _stash(latex_inline_to_pango(inner))

    s = re.sub(r"(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)", _repl_latex_dollar, s)

    # 3. Normalize [[n]](url) -> [n] if n is in citation_map, else regular link
    s = re.sub(r"\[\[(\d{1,2})\]\]\((https?://[^\s\)]+)\)", r"[\1](\2)", s)

    def _repl_link(m: re.Match[str]) -> str:
        label = m.group(1).strip()
        raw_url = m.group(2).strip()
        if label.isdigit() and citation_map and int(label) in citation_map:
            return f"[{label}]"
        esc_label = html.escape(label)
        esc_url = html.escape(raw_url, quote=True)
        return _stash(f'<a href="{esc_url}">{esc_label}</a>')

    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", _repl_link, s)

    # 4. Inline citation badges [1], [2], or [1-domain] — deduplicate consecutive same-domain citations
    def _repl_citation_group(m: re.Match[str]) -> str:
        group_str = m.group(0)
        nums = [int(x) for x in re.findall(r"\[(\d{1,2})(?:[·\-][^\]]+)?\]", group_str)]
        seen_nums: set[int] = set()
        seen_domains: set[str] = set()
        rendered_badges: list[str] = []
        for num in nums:
            if num in seen_nums:
                continue
            seen_nums.add(num)
            if citation_map and num in citation_map:
                c = citation_map[num]
                url = html.escape(str(c.get("url") or "#"), quote=True)
                domain = str(c.get("domain") or num)
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
                esc_domain = html.escape(domain)
                rendered_badges.append(f'<a href="{url}"><b>[{num}·{esc_domain}]</b></a>')
            else:
                rendered_badges.append(f"<b>[{num}]</b>")
        return _stash(" ".join(rendered_badges))

    s = re.sub(
        r"(?:\[\d{1,2}(?:[·\-][a-zA-Z0-9\.\-_]+)?\](?!\()\s*)+",
        _repl_citation_group,
        s,
    )

    # Escape remaining plain text
    s = html.escape(s)

    # 5. Bold + Italic, Bold, Italic, Strikethrough
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)

    # Restore stashed fragments
    for idx, frag in enumerate(placeholders):
        s = s.replace(f"\x00PH{idx}PH\x00", frag)

    return s


def extract_paragraph_citations(
    text: str,
    citation_map: dict[int, dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract inline citations ([1], [1-domain], [[1]](url)) from a paragraph or item,
    returning (cleaned_text_without_inline_citations, deduplicated_citations_for_end_of_paragraph).
    """
    if not text:
        return "", []

    # Temporarily mask inline code `...` so brackets inside code are never touched
    code_stash: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        idx = len(code_stash)
        code_stash.append(m.group(0))
        return f"\x00CD{idx}CD\x00"

    s = re.sub(r"`[^`\n]+`", _stash_code, text)

    # Normalize [[n]](url) -> [n] and [n](url) -> [n] when n is a 1-2 digit citation index
    s = re.sub(r"\[\[(\d{1,2})\]\]\((https?://[^\s\)]+)\)", r"[\1]", s)

    def _norm_num_link(m: re.Match[str]) -> str:
        lbl = m.group(1).strip()
        if lbl.isdigit():
            return f"[{lbl}]"
        return m.group(0)

    s = re.sub(r"\[(\d{1,2})\]\((https?://[^\s\)]+)\)", _norm_num_link, s)

    extracted: list[dict[str, Any]] = []
    seen_nums: set[int] = set()
    seen_domains: set[str] = set()

    cit_pattern = re.compile(r"\[(\d{1,2})(?:[·\-]([a-zA-Z0-9\.\-_]+))?\](?!\()")

    for m in cit_pattern.finditer(s):
        num = int(m.group(1))
        inline_domain = m.group(2) or ""
        if num in seen_nums:
            continue
        seen_nums.add(num)
        if citation_map and num in citation_map:
            c = citation_map[num]
            domain = str(c.get("domain") or inline_domain or f"Source {num}")
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            extracted.append(c)
        elif inline_domain:
            if inline_domain in seen_domains:
                continue
            seen_domains.add(inline_domain)
            extracted.append(
                {
                    "index": num,
                    "domain": inline_domain,
                    "title": inline_domain,
                    "url": f"https://{inline_domain}",
                    "snippet": "",
                }
            )
        else:
            extracted.append(
                {
                    "index": num,
                    "domain": f"Source {num}",
                    "title": f"Source {num}",
                    "url": "",
                    "snippet": "",
                }
            )

    # Strip inline citations from mid-sentence / paragraph prose
    cleaned = cit_pattern.sub("", s)
    # Clean up extra whitespace left before punctuation (e.g. "word [1]." -> "word.")
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)

    for idx, orig_code in enumerate(code_stash):
        cleaned = cleaned.replace(f"\x00CD{idx}CD\x00", orig_code)

    return cleaned.strip(), extracted


def prepare_streaming_markdown(markdown_text: str) -> str:
    """Sanitize and auto-close in-progress Markdown constructs during streaming so
    formatting happens on the fly before showing to the user (never flashing raw
    ###, **, ```, or incomplete table/citation syntax).
    """
    if not markdown_text:
        return ""

    text = markdown_text.replace("\r\n", "\n")
    lines = text.split("\n")

    # Check if we are currently inside an unclosed fenced code block ```
    in_code_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence

    if in_code_fence:
        if lines and re.match(r"^\s*`{1,2}\s*$", lines[-1]):
            lines.pop()
        return "\n".join(lines)

    # Outside code fences: inspect trailing line(s) for incomplete block markers
    while lines:
        last = lines[-1]
        last_s = last.strip()
        if re.match(r"^(#{1,6}|[-*+]|\d+[.)]|>|`{1,3}|\${1,2}|\\\[)\s*$", last_s):
            lines.pop()
            continue
        if (
            last_s.startswith("|")
            and last_s.count("|") >= 2
            and (len(lines) == 1 or "|" not in lines[-2])
        ):
            lines.pop()
            continue
        break

    if not lines:
        return ""

    active = lines[-1]
    # 1. Strip trailing incomplete link or citation at the very end of the active line
    active = re.sub(r"\s*\[[^\]]*$", "", active)
    active = re.sub(r"\s*\[[^\]]+\]\([^\)]*$", "", active)

    # 2. Strip trailing bare emphasis/code openers with no characters after them yet
    active = re.sub(r"(\*{1,3}|_{1,2}|~{1,2}|`+)$", "", active)

    # 3. Auto-close unclosed inline code `...` on the active line
    if active.count("`") % 2 == 1:
        active += "`"

    # 4. Auto-close unclosed **bold** on the active line
    bold_count = len(re.findall(r"\*\*", active))
    if bold_count % 2 == 1:
        active += "**"

    # 5. Auto-close unclosed *italic* (single asterisks not part of **)
    single_star_count = len(re.findall(r"(?<!\*)\*(?!\*)", active))
    if single_star_count % 2 == 1:
        active += "*"

    # 6. Auto-close unclosed ~~strikethrough~~
    tilde_count = len(re.findall(r"~~", active))
    if tilde_count % 2 == 1:
        active += "~~"

    lines[-1] = active
    return "\n".join(lines)


def parse_markdown_blocks(markdown_text: str) -> list[MdBlock]:
    """Parse full Markdown text into a list of structural `MdBlock` items."""
    text = (markdown_text or "").replace("\r\n", "\n")
    lines = text.split("\n")
    blocks: list[MdBlock] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 1. Fenced code block ```lang ... ```
        if stripped.startswith("```"):
            lang = stripped[3:].strip().split()[0] if len(stripped) > 3 else ""
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            blocks.append(MdBlock(kind="code", lang=lang, text="\n".join(code_lines)))
            continue

        # 2. Display LaTeX block $$...$$ or \[...\] or \begin{...}
        if stripped.startswith("$$") or stripped.startswith(r"\["):
            delim_end = "$$" if stripped.startswith("$$") else r"\]"
            first_rest = stripped[2:].strip()
            if first_rest.endswith(delim_end) and len(first_rest) >= len(delim_end):
                inner_tex = first_rest[: -len(delim_end)].strip()
                blocks.append(MdBlock(kind="latex", text=inner_tex))
                i += 1
                continue
            tex_lines: list[str] = [first_rest] if first_rest else []
            i += 1
            while i < n:
                cur_s = lines[i].strip()
                if cur_s.endswith(delim_end):
                    before_end = cur_s[: -len(delim_end)].strip()
                    if before_end:
                        tex_lines.append(before_end)
                    i += 1
                    break
                tex_lines.append(lines[i])
                i += 1
            blocks.append(MdBlock(kind="latex", text="\n".join(tex_lines).strip()))
            continue

        # 3. Horizontal rule
        if re.match(r"^(\*{3,}|-{3,}|_{3,})$", stripped):
            blocks.append(MdBlock(kind="hr"))
            i += 1
            continue

        # 4. Heading # .. ######
        hm = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if hm:
            blocks.append(
                MdBlock(kind="heading", level=len(hm.group(1)), text=hm.group(2).strip())
            )
            i += 1
            continue

        # 5. Table | col1 | col2 |
        if (
            "|" in stripped
            and i + 1 < n
            and re.match(r"^\|?[\s:\-\|]+\|?$", lines[i + 1].strip())
            and "-" in lines[i + 1]
        ):
            headers = [
                c.strip() for c in stripped.strip("|").split("|")
            ]
            i += 2  # skip header and separator line
            rows: list[list[str]] = []
            while i < n and "|" in lines[i].strip() and lines[i].strip():
                row_cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(row_cells)
                i += 1
            blocks.append(MdBlock(kind="table", headers=headers, rows=rows))
            continue

        # 6. Blockquote > ...
        if stripped.startswith(">"):
            q_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                q_lines.append( re.sub(r"^>\s?", "", lines[i].strip()) )
                i += 1
            blocks.append(MdBlock(kind="blockquote", text="\n".join(q_lines)))
            continue

        # 7. Unordered or Ordered List
        ul_m = re.match(r"^(\s*)[-\*\+]\s+(.+)$", line)
        ol_m = re.match(r"^(\s*)\d+[\.\)]\s+(.+)$", line)
        if ul_m or ol_m:
            ordered = bool(ol_m)
            items: list[str] = []
            while i < n:
                cur_l = lines[i]
                m_u = re.match(r"^(\s*)[-\*\+]\s+(.+)$", cur_l)
                m_o = re.match(r"^(\s*)\d+[\.\)]\s+(.+)$", cur_l)
                if m_u:
                    items.append(m_u.group(2).strip())
                    i += 1
                elif m_o:
                    items.append(m_o.group(2).strip())
                    i += 1
                else:
                    break
            blocks.append(MdBlock(kind="list", ordered=ordered, items=items))
            continue

        # 8. Regular Paragraph (accumulate until blank line or block starter)
        para_lines: list[str] = []
        while i < n:
            cur_s = lines[i].strip()
            if (
                not cur_s
                or cur_s.startswith("```")
                or cur_s.startswith("$$")
                or cur_s.startswith(r"\[")
                or cur_s.startswith("#")
                or cur_s.startswith(">")
                or re.match(r"^(\s*)[-\*\+]\s+", lines[i])
                or re.match(r"^(\s*)\d+[\.\)]\s+", lines[i])
            ):
                break
            para_lines.append(lines[i])
            i += 1

        joined_para = "\n".join(para_lines).strip()
        if joined_para:
            blocks.append(MdBlock(kind="paragraph", text=joined_para))

    return blocks


class NativeCodeBlock(Gtk.Box):
    """Native GtkSourceView 5 syntax-highlighted code block with Copy button."""

    def __init__(
        self,
        code: str,
        language_id: str = "",
        on_copied: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.code = code
        self.language_id = (language_id or "").strip().lower()
        self.add_css_class("code-block-frame")
        self.set_overflow(Gtk.Overflow.HIDDEN)

        # Header Bar
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("code-block-header")

        lang_label = Gtk.Label(
            label=self.language_id or "code",
            xalign=0.0,
        )
        lang_label.add_css_class("code-lang-label")
        lang_label.set_hexpand(True)
        header.append(lang_label)

        copy_btn = Gtk.Button()
        copy_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._copy_icon = Gtk.Image.new_from_icon_name("edit-copy-symbolic")
        self._copy_lbl = Gtk.Label(label="Copy")
        copy_box.append(self._copy_icon)
        copy_box.append(self._copy_lbl)
        copy_btn.set_child(copy_box)
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text("Copy code to clipboard")
        copy_btn.connect("clicked", self._on_copy_clicked, on_copied)
        header.append(copy_btn)
        self.append(header)

        # GtkSource.Buffer & GtkSource.View
        self.buffer = GtkSource.Buffer()
        self.buffer.set_text(code)
        self._apply_language()
        self._apply_style_scheme()

        style_mgr = Adw.StyleManager.get_default()
        style_mgr.connect("notify::dark", lambda *_: self._apply_style_scheme())

        self.source_view = GtkSource.View.new_with_buffer(self.buffer)
        self.source_view.set_editable(False)
        self.source_view.set_cursor_visible(False)
        self.source_view.set_monospace(True)
        self.source_view.set_show_line_numbers(len(code.splitlines()) > 1)
        self.source_view.set_top_margin(10)
        self.source_view.set_bottom_margin(10)
        self.source_view.set_left_margin(12)
        self.source_view.set_right_margin(12)
        self.source_view.set_wrap_mode(Gtk.WrapMode.NONE)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(self.source_view)
        self.append(scroller)

    def _apply_language(self) -> None:
        if not self.language_id:
            return
        lm = GtkSource.LanguageManager.get_default()
        lookup_id = _LANG_ALIASES.get(self.language_id, self.language_id)
        lang = lm.get_language(lookup_id) or lm.get_language(self.language_id)
        if lang:
            self.buffer.set_language(lang)
            self.buffer.set_highlight_syntax(True)

    def _apply_style_scheme(self) -> None:
        sm = GtkSource.StyleSchemeManager.get_default()
        is_dark = Adw.StyleManager.get_default().get_dark()
        scheme_name = "Adwaita-dark" if is_dark else "Adwaita"
        scheme = sm.get_scheme(scheme_name) or sm.get_scheme(
            "classic-dark" if is_dark else "classic"
        )
        if scheme:
            self.buffer.set_style_scheme(scheme)

    def _on_copy_clicked(
        self,
        _btn: Gtk.Button,
        on_copied: Callable[[str], None] | None,
    ) -> None:
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(self.code)
        self._copy_icon.set_from_icon_name("object-select-symbolic")
        self._copy_lbl.set_label("Copied")
        if on_copied:
            on_copied("Copied code block to clipboard")

        def _reset() -> bool:
            self._copy_icon.set_from_icon_name("edit-copy-symbolic")
            self._copy_lbl.set_label("Copy")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(1800, _reset)


class MarkdownView(Gtk.Box):
    """Native GTK 4 composite widget that renders full Markdown, Code, Tables, and LaTeX."""

    def __init__(
        self,
        markdown_text: str = "",
        citations: list[dict[str, Any]] | None = None,
        on_toast: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_hexpand(True)
        self._on_toast = on_toast
        self._markdown_text = ""
        self._citations: list[dict[str, Any]] = citations or []
        self._rendered_blocks: list[MdBlock] = []
        self._rendered_widgets: list[Gtk.Widget] = []
        if markdown_text:
            self.set_markdown(markdown_text, citations=citations, is_streaming=False)

    def set_markdown(
        self,
        markdown_text: str,
        citations: list[dict[str, Any]] | None = None,
        *,
        is_streaming: bool = False,
    ) -> None:
        self._markdown_text = markdown_text or ""
        if citations is not None:
            self._citations = citations

        citation_map: dict[int, dict[str, Any]] = {}
        for idx, c in enumerate(self._citations, start=1):
            c_idx = int(c.get("index") or idx)
            citation_map[c_idx] = c

        effective_md = (
            prepare_streaming_markdown(self._markdown_text)
            if is_streaming
            else self._markdown_text
        )
        blocks = parse_markdown_blocks(effective_md)
        if not blocks and effective_md.strip():
            blocks = [MdBlock(kind="paragraph", text=effective_md.strip())]

        # Incremental block diffing: preserve completed block widgets above the active block
        common_len = 0
        max_common = min(len(blocks), len(self._rendered_blocks))
        while common_len < max_common and blocks[common_len] == self._rendered_blocks[common_len]:
            common_len += 1

        # Remove trailing widgets that changed
        while len(self._rendered_widgets) > common_len:
            w = self._rendered_widgets.pop()
            self._rendered_blocks.pop()
            self.remove(w)

        # Build and append new/updated blocks
        for blk in blocks[common_len:]:
            widget = self._build_block_widget(blk, citation_map)
            self._rendered_blocks.append(blk)
            self._rendered_widgets.append(widget)
            self.append(widget)

    def _build_block_widget(
        self,
        blk: MdBlock,
        citation_map: dict[int, dict[str, Any]],
    ) -> Gtk.Widget:
        if blk.kind == "heading":
            css_map = {
                1: "title-1",
                2: "title-2",
                3: "title-3",
                4: "title-4",
                5: "heading",
                6: "heading",
            }
            return self._make_text_with_citation_chips(
                blk.text,
                citation_map,
                css_class=css_map.get(blk.level, "heading"),
            )

        if blk.kind == "paragraph":
            return self._make_text_with_citation_chips(blk.text, citation_map)

        if blk.kind == "code":
            return NativeCodeBlock(
                code=blk.text,
                language_id=blk.lang,
                on_copied=self._on_toast,
            )

        if blk.kind == "latex":
            return LatexBlockWidget(
                latex_source=blk.text,
                on_copied=self._on_toast,
            )

        if blk.kind == "blockquote":
            q_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            q_box.add_css_class("blockquote-box")
            q_box.append(self._make_text_with_citation_chips(blk.text, citation_map))
            return q_box

        if blk.kind == "list":
            list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for idx, item_txt in enumerate(blk.items, start=1):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.set_margin_start(8)
                bullet = f"{idx}." if blk.ordered else "•"
                b_lbl = Gtk.Label(label=bullet, xalign=0.0, yalign=0.0)
                b_lbl.add_css_class("heading")
                row.append(b_lbl)
                item_widget = self._make_text_with_citation_chips(item_txt, citation_map)
                row.append(item_widget)
                list_box.append(row)
            return list_box

        if blk.kind == "table":
            return self._build_table_widget(blk, citation_map)

        if blk.kind == "hr":
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(6)
            sep.set_margin_bottom(6)
            return sep

        return self._make_text_with_citation_chips(blk.text, citation_map)

    def _make_text_with_citation_chips(
        self,
        text: str,
        citation_map: dict[int, dict[str, Any]],
        *,
        css_class: str | None = None,
    ) -> Gtk.Widget:
        """Render rich paragraph/item text with inline citations stripped and placed
        as sleek pill chips at the end of the paragraph.
        """
        cleaned_text, para_citations = extract_paragraph_citations(text, citation_map)
        lbl = self._make_rich_label(cleaned_text, citation_map)
        if css_class:
            lbl.add_css_class(css_class)

        if not para_citations:
            return lbl

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_hexpand(True)
        if cleaned_text:
            box.append(lbl)

        chips_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chips_row.set_halign(Gtk.Align.START)

        for c in para_citations:
            domain = str(c.get("domain") or f"Source {c.get('index', '')}")
            title = str(c.get("title") or domain)
            url = str(c.get("url") or "")
            snippet = str(c.get("snippet") or "")

            chip_btn = Gtk.Button(label=domain)
            chip_btn.add_css_class("para-citation-chip")
            chip_btn.set_can_focus(False)
            tooltip_parts = [p for p in [title, url, snippet] if p]
            if tooltip_parts:
                chip_btn.set_tooltip_text("\n".join(tooltip_parts))
            if url.startswith(("http://", "https://")):
                chip_btn.connect(
                    "clicked",
                    lambda _b, u=url: Gtk.UriLauncher.new(u).launch(None, None, None),
                )
            chips_row.append(chip_btn)

        box.append(chips_row)
        return box

    def _make_rich_label(
        self,
        text: str,
        citation_map: dict[int, dict[str, Any]],
    ) -> Gtk.Label:
        lbl = Gtk.Label(xalign=0.0, yalign=0.0)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_selectable(False)
        lbl.set_can_focus(False)
        lbl.set_hexpand(True)
        markup = inline_markdown_to_pango(text, citation_map)
        try:
            lbl.set_markup(markup)
        except Exception:
            lbl.set_text(text)
        lbl.connect("activate-link", self._on_activate_link)
        return lbl

    def _build_table_widget(
        self,
        blk: MdBlock,
        citation_map: dict[int, dict[str, Any]],
    ) -> Gtk.Widget:
        grid = Gtk.Grid()
        grid.add_css_class("md-table-grid")
        grid.set_overflow(Gtk.Overflow.HIDDEN)
        grid.set_hexpand(True)
        grid.set_column_spacing(0)
        grid.set_row_spacing(0)

        for col_idx, h_txt in enumerate(blk.headers):
            cell = self._make_text_with_citation_chips(h_txt, citation_map)
            cell.add_css_class("md-table-header")
            grid.attach(cell, col_idx, 0, 1, 1)

        for row_idx, row_cells in enumerate(blk.rows, start=1):
            for col_idx, c_txt in enumerate(row_cells):
                cell = self._make_text_with_citation_chips(c_txt, citation_map)
                cell.add_css_class("md-table-cell")
                grid.attach(cell, col_idx, row_idx, 1, 1)

        return grid

    @staticmethod
    def _on_activate_link(_label: Gtk.Label, uri: str) -> bool:
        if uri and uri.startswith(("http://", "https://")):
            launcher = Gtk.UriLauncher.new(uri)
            launcher.launch(None, None, None)
            return True
        return False

    def _clear_children(self) -> None:
        self._rendered_blocks.clear()
        self._rendered_widgets.clear()
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt

