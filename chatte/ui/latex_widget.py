"""100% Native LaTeX Math Expression Parser & PangoCairo Canvas Renderer.

Supports both:
1. Inline LaTeX math ($...$, \\(...\\)) converted to Pango markup + Unicode math symbols.
2. Display LaTeX math ($$...$$, \\[...\\], \\begin{...}) rendered on a native GTK4
   `Gtk.DrawingArea` using Cairo + PangoCairo with true vertical fractions (\\frac),
   radicals with overbars (\\sqrt), stacked summation/integral limits (\\sum, \\int),
   superscripts/subscripts, and scalable matrix delimiters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import math
from typing import Callable

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo


LATEX_SYMBOLS: dict[str, str] = {
    # Lowercase Greek
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\epsilon": "ϵ",
    r"\varepsilon": "ε",
    r"\zeta": "ζ",
    r"\eta": "η",
    r"\theta": "θ",
    r"\vartheta": "ϑ",
    r"\iota": "ι",
    r"\kappa": "κ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\nu": "ν",
    r"\xi": "ξ",
    r"\pi": "π",
    r"\rho": "ρ",
    r"\varrho": "ϱ",
    r"\sigma": "σ",
    r"\varsigma": "ς",
    r"\tau": "τ",
    r"\upsilon": "υ",
    r"\phi": "ϕ",
    r"\varphi": "φ",
    r"\chi": "χ",
    r"\psi": "ψ",
    r"\omega": "ω",
    # Uppercase Greek
    r"\Gamma": "Γ",
    r"\Delta": "Δ",
    r"\Theta": "Θ",
    r"\Lambda": "Λ",
    r"\Xi": "Ξ",
    r"\Pi": "Π",
    r"\Sigma": "Σ",
    r"\Upsilon": "Υ",
    r"\Phi": "Φ",
    r"\Psi": "Ψ",
    r"\Omega": "Ω",
    # Calculus & Big Operators
    r"\sum": "∑",
    r"\prod": "∏",
    r"\coprod": "∐",
    r"\int": "∫",
    r"\iint": "∬",
    r"\iiint": "∭",
    r"\oint": "∮",
    r"\bigcup": "⋃",
    r"\bigcap": "⋂",
    r"\bigoplus": "⨁",
    r"\bigotimes": "⨂",
    r"\lim": "lim",
    r"\sup": "sup",
    r"\inf": "inf",
    r"\max": "max",
    r"\min": "min",
    r"\det": "det",
    r"\log": "log",
    r"\ln": "ln",
    r"\exp": "exp",
    r"\sin": "sin",
    r"\cos": "cos",
    r"\tan": "tan",
    r"\cot": "cot",
    r"\sec": "sec",
    r"\csc": "csc",
    r"\sinh": "sinh",
    r"\cosh": "cosh",
    r"\tanh": "tanh",
    r"\arcsin": "arcsin",
    r"\arccos": "arccos",
    r"\arctan": "arctan",
    # Operators & Relations
    r"\partial": "∂",
    r"\nabla": "∇",
    r"\infty": "∞",
    r"\hbar": "ℏ",
    r"\ell": "ℓ",
    r"\Re": "ℜ",
    r"\Im": "ℑ",
    r"\wp": "℘",
    r"\emptyset": "∅",
    r"\varnothing": "∅",
    r"\forall": "∀",
    r"\exists": "∃",
    r"\nexists": "∄",
    r"\in": "∈",
    r"\notin": "∉",
    r"\ni": "∋",
    r"\subset": "⊂",
    r"\supset": "⊃",
    r"\subseteq": "⊆",
    r"\supseteq": "⊇",
    r"\cup": "∪",
    r"\cap": "∩",
    r"\setminus": "∖",
    r"\pm": "±",
    r"\mp": "∓",
    r"\times": "×",
    r"\div": "÷",
    r"\cdot": "·",
    r"\cdots": "⋯",
    r"\ldots": "…",
    r"\vdots": "⋮",
    r"\ddots": "⋱",
    r"\ast": "∗",
    r"\star": "⋆",
    r"\circ": "∘",
    r"\bullet": "∙",
    r"\oplus": "⊕",
    r"\ominus": "⊖",
    r"\otimes": "⊗",
    r"\odot": "⊙",
    r"\leq": "≤",
    r"\le": "≤",
    r"\geq": "≥",
    r"\ge": "≥",
    r"\neq": "≠",
    r"\ne": "≠",
    r"\approx": "≈",
    r"\equiv": "≡",
    r"\cong": "≅",
    r"\sim": "∼",
    r"\simeq": "≃",
    r"\propto": "∝",
    r"\perp": "⊥",
    r"\parallel": "∥",
    r"\mid": "∣",
    r"\to": "→",
    r"\rightarrow": "→",
    r"\leftarrow": "←",
    r"\leftrightarrow": "↔",
    r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔",
    r"\iff": "⟺",
    r"\implies": "⟹",
    r"\mapsto": "↦",
    r"\uparrow": "↑",
    r"\downarrow": "↓",
    r"\langle": "⟨",
    r"\rangle": "⟩",
    r"\lceil": "⌈",
    r"\rceil": "⌉",
    r"\lfloor": "⌊",
    r"\rfloor": "⌋",
    r"\quad": "  ",
    r"\qquad": "    ",
    r"\,": " ",
    r"\;": " ",
    r"\:": " ",
    r"\!": "",
    r"\{": "{",
    r"\}": "}",
    r"\%": "%",
}

BLACKBOARD_BOLD: dict[str, str] = {
    "R": "ℝ",
    "N": "ℕ",
    "Z": "ℤ",
    "Q": "ℚ",
    "C": "ℂ",
    "E": "𝔼",
    "P": "ℙ",
    "F": "𝔽",
    "H": "ℍ",
}

BIG_OPS = {"∑", "∏", "∐", "∫", "∬", "∭", "∮", "⋃", "⋂", "⨁", "⨂", "lim", "sup", "inf", "max", "min"}


def _read_braced_group(src: str, pos: int) -> tuple[str, int]:
    """Read `{...}` or a single token starting at `pos`."""
    n = len(src)
    while pos < n and src[pos].isspace():
        pos += 1
    if pos >= n:
        return "", pos
    if src[pos] == "{":
        depth = 0
        start = pos + 1
        i = pos
        while i < n:
            if src[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[start:i], i + 1
            i += 1
        return src[start:], n
    if src[pos] == "\\":
        j = pos + 1
        if j < n and not src[j].isalpha():
            return src[pos : j + 1], j + 1
        while j < n and src[j].isalpha():
            j += 1
        return src[pos:j], j
    return src[pos], pos + 1


def latex_inline_to_pango(latex_src: str) -> str:
    """Convert inline LaTeX math ($...$) into rich Pango markup with Unicode math glyphs."""
    s = latex_src.strip()
    s = s.replace(r"\left", "").replace(r"\right", "")
    out: list[str] = []
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]
        if ch == "\\":
            j = i + 1
            if j < n and not s[j].isalpha():
                cmd = s[i : j + 1]
                i = j + 1
            else:
                while j < n and s[j].isalpha():
                    j += 1
                cmd = s[i:j]
                i = j

            if cmd == r"\frac":
                num, i = _read_braced_group(s, i)
                den, i = _read_braced_group(s, i)
                num_m = latex_inline_to_pango(num)
                den_m = latex_inline_to_pango(den)
                out.append(f"({num_m})/({den_m})")
            elif cmd == r"\sqrt":
                while i < n and s[i].isspace():
                    i += 1
                root_idx = ""
                if i < n and s[i] == "[":
                    end_b = s.find("]", i + 1)
                    if end_b != -1:
                        root_idx = s[i + 1 : end_b]
                        i = end_b + 1
                rad, i = _read_braced_group(s, i)
                rad_m = latex_inline_to_pango(rad)
                if root_idx:
                    idx_m = latex_inline_to_pango(root_idx)
                    out.append(f"<sup>{idx_m}</sup>√({rad_m})")
                else:
                    out.append(f"√({rad_m})")
            elif cmd == r"\mathbb":
                grp, i = _read_braced_group(s, i)
                mapped = "".join(BLACKBOARD_BOLD.get(c, c) for c in grp.strip())
                out.append(html.escape(mapped))
            elif cmd in (r"\text", r"\mathrm", r"\operatorname"):
                grp, i = _read_braced_group(s, i)
                out.append(html.escape(grp))
            elif cmd in (r"\mathbf", r"\boldsymbol"):
                grp, i = _read_braced_group(s, i)
                out.append(f"<b>{latex_inline_to_pango(grp)}</b>")
            elif cmd in (r"\mathit", r"\emph"):
                grp, i = _read_braced_group(s, i)
                out.append(f"<i>{latex_inline_to_pango(grp)}</i>")
            elif cmd == r"\hat":
                grp, i = _read_braced_group(s, i)
                out.append(f"{latex_inline_to_pango(grp)}\u0302")
            elif cmd == r"\bar" or cmd == r"\overline":
                grp, i = _read_braced_group(s, i)
                out.append(f"{latex_inline_to_pango(grp)}\u0304")
            elif cmd == r"\vec":
                grp, i = _read_braced_group(s, i)
                out.append(f"{latex_inline_to_pango(grp)}\u20d7")
            elif cmd in LATEX_SYMBOLS:
                out.append(html.escape(LATEX_SYMBOLS[cmd]))
            else:
                out.append(html.escape(cmd.lstrip("\\")))
        elif ch == "^":
            grp, i = _read_braced_group(s, i + 1)
            out.append(f"<sup>{latex_inline_to_pango(grp)}</sup>")
        elif ch == "_":
            grp, i = _read_braced_group(s, i + 1)
            out.append(f"<sub>{latex_inline_to_pango(grp)}</sub>")
        elif ch in "{}":
            i += 1
        elif ch.isalpha():
            out.append(f"<i>{html.escape(ch)}</i>")
            i += 1
        elif ch == "-":
            out.append("−")
            i += 1
        else:
            out.append(html.escape(ch))
            i += 1

    return "".join(out)


# =============================================================================
# Display Math AST & PangoCairo Layout Engine
# =============================================================================


@dataclass
class MathNode:
    kind: str  # "row" | "text" | "frac" | "sqrt" | "scripts" | "matrix" | "accent"
    text: str = ""
    italic: bool = False
    bold: bool = False
    children: list["MathNode"] = field(default_factory=list)
    num: "MathNode | None" = None
    den: "MathNode | None" = None
    base: "MathNode | None" = None
    sub: "MathNode | None" = None
    sup: "MathNode | None" = None
    index: "MathNode | None" = None
    rows: list[list["MathNode"]] = field(default_factory=list)
    left_delim: str = ""
    right_delim: str = ""
    accent: str = ""
    # Computed layout metrics (in pixels)
    width: float = 0.0
    ascent: float = 0.0
    descent: float = 0.0

    @property
    def height(self) -> float:
        return self.ascent + self.descent


def parse_math_ast(src: str) -> MathNode:
    """Parse a LaTeX math string into a structured `MathNode` expression tree."""
    s = src.strip()
    s = s.replace(r"\left", "").replace(r"\right", "")

    # Check if top-level has multi-line `\\` outside `\begin{...}`
    if r"\\" in s and r"\begin{" not in s:
        lines = [line.strip() for line in s.split(r"\\") if line.strip()]
        if len(lines) > 1:
            matrix_rows = [[parse_math_ast(line.replace("&", " "))] for line in lines]
            return MathNode(kind="matrix", rows=matrix_rows, left_delim="", right_delim="")

    items: list[MathNode] = []
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]
        if ch.isspace():
            i += 1
            continue

        if ch == "{":
            grp, i = _read_braced_group(s, i)
            items.append(parse_math_ast(grp))
            continue

        if ch == "\\":
            j = i + 1
            if j < n and not s[j].isalpha():
                cmd = s[i : j + 1]
                i = j + 1
            else:
                while j < n and s[j].isalpha():
                    j += 1
                cmd = s[i:j]
                i = j

            if cmd == r"\begin":
                env_name, i = _read_braced_group(s, i)
                end_tag = f"\\end{{{env_name}}}"
                end_idx = s.find(end_tag, i)
                if end_idx != -1:
                    body = s[i:end_idx]
                    i = end_idx + len(end_tag)
                else:
                    body = s[i:]
                    i = n

                raw_rows = [r.strip() for r in body.split(r"\\") if r.strip()]
                parsed_rows: list[list[MathNode]] = []
                for r_str in raw_rows:
                    cols = [parse_math_ast(c.strip()) for c in r_str.split("&")]
                    parsed_rows.append(cols)

                ld, rd = "", ""
                if env_name in ("pmatrix",):
                    ld, rd = "(", ")"
                elif env_name in ("bmatrix",):
                    ld, rd = "[", "]"
                elif env_name in ("Bmatrix",):
                    ld, rd = "{", "}"
                elif env_name in ("vmatrix",):
                    ld, rd = "|", "|"
                elif env_name in ("cases",):
                    ld, rd = "{", ""
                items.append(
                    MathNode(kind="matrix", rows=parsed_rows, left_delim=ld, right_delim=rd)
                )
                continue

            if cmd == r"\frac" or cmd == r"\dfrac" or cmd == r"\tfrac":
                num_str, i = _read_braced_group(s, i)
                den_str, i = _read_braced_group(s, i)
                items.append(
                    MathNode(
                        kind="frac",
                        num=parse_math_ast(num_str),
                        den=parse_math_ast(den_str),
                    )
                )
                continue

            if cmd == r"\sqrt":
                while i < n and s[i].isspace():
                    i += 1
                idx_node: MathNode | None = None
                if i < n and s[i] == "[":
                    end_b = s.find("]", i + 1)
                    if end_b != -1:
                        idx_node = parse_math_ast(s[i + 1 : end_b])
                        i = end_b + 1
                rad_str, i = _read_braced_group(s, i)
                items.append(
                    MathNode(
                        kind="sqrt",
                        base=parse_math_ast(rad_str),
                        index=idx_node,
                    )
                )
                continue

            if cmd == r"\mathbb":
                grp, i = _read_braced_group(s, i)
                mapped = "".join(BLACKBOARD_BOLD.get(c, c) for c in grp.strip())
                items.append(MathNode(kind="text", text=mapped, bold=True))
                continue

            if cmd in (r"\text", r"\mathrm", r"\operatorname"):
                grp, i = _read_braced_group(s, i)
                items.append(MathNode(kind="text", text=grp, italic=False))
                continue

            if cmd in (r"\mathbf", r"\boldsymbol"):
                grp, i = _read_braced_group(s, i)
                node = parse_math_ast(grp)
                node.bold = True
                items.append(node)
                continue

            if cmd in (r"\hat", r"\vec", r"\bar", r"\overline", r"\tilde", r"\dot"):
                grp, i = _read_braced_group(s, i)
                items.append(
                    MathNode(
                        kind="accent",
                        base=parse_math_ast(grp),
                        accent=cmd.lstrip("\\"),
                    )
                )
                continue

            if cmd in LATEX_SYMBOLS:
                sym = LATEX_SYMBOLS[cmd]
                items.append(MathNode(kind="text", text=sym, italic=False))
                continue

            items.append(MathNode(kind="text", text=cmd.lstrip("\\"), italic=False))
            continue

        if ch in ("^", "_"):
            op = ch
            grp, i = _read_braced_group(s, i + 1)
            script_node = parse_math_ast(grp)
            if items and items[-1].kind == "scripts":
                prev = items[-1]
                if op == "^":
                    prev.sup = script_node
                else:
                    prev.sub = script_node
            else:
                base_node = items.pop() if items else MathNode(kind="text", text="")
                items.append(
                    MathNode(
                        kind="scripts",
                        base=base_node,
                        sup=script_node if op == "^" else None,
                        sub=script_node if op == "_" else None,
                    )
                )
            continue

        if ch in "+-=*/<>(),[];:|!":
            sym = "−" if ch == "-" else ch
            pad = " " if ch in "+=<>−" else ""
            items.append(MathNode(kind="text", text=f"{pad}{sym}{pad}", italic=False))
            i += 1
            continue

        if ch.isdigit() or ch == ".":
            j = i
            while j < n and (s[j].isdigit() or s[j] == "."):
                j += 1
            items.append(MathNode(kind="text", text=s[i:j], italic=False))
            i = j
            continue

        if ch.isalpha():
            items.append(MathNode(kind="text", text=ch, italic=True))
            i += 1
            continue

        items.append(MathNode(kind="text", text=ch, italic=False))
        i += 1

    if len(items) == 1:
        return items[0]
    return MathNode(kind="row", children=items)


class LatexCanvas(Gtk.DrawingArea):
    """Native GTK 4 Cairo + PangoCairo canvas that measures and draws display LaTeX equations."""

    def __init__(self, latex_source: str, base_font_size: float = 13.5) -> None:
        super().__init__()
        self.latex_source = latex_source.strip()
        self.base_font_size = base_font_size
        self.ast_root = parse_math_ast(self.latex_source)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.START)

        # Perform initial measurement using a scratch image surface
        self._remeasure()
        self.set_draw_func(self._on_draw)

        style_mgr = Adw.StyleManager.get_default()
        style_mgr.connect("notify::dark", lambda *_: self.queue_draw())

    def _remeasure(self) -> None:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
        cr = cairo.Context(surf)
        self._measure_node(cr, self.ast_root, self.base_font_size)
        w = max(80, int(math.ceil(self.ast_root.width + 24)))
        h = max(36, int(math.ceil(self.ast_root.height + 20)))
        self.set_content_width(w)
        self.set_content_height(h)

    def _make_pango_layout(
        self,
        cr: cairo.Context,
        text: str,
        font_pt: float,
        italic: bool = False,
        bold: bool = False,
    ) -> Pango.Layout:
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription()
        desc.set_family("serif, Cantarell, Noto Serif, DejaVu Serif")
        desc.set_size(int(max(7.0, font_pt) * Pango.SCALE))
        if italic:
            desc.set_style(Pango.Style.ITALIC)
        if bold:
            desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        return layout

    def _measure_node(self, cr: cairo.Context, node: MathNode, font_pt: float) -> None:
        if node.kind == "text":
            is_big = node.text.strip() in BIG_OPS and len(node.text.strip()) == 1
            scale = 1.45 if is_big else 1.0
            layout = self._make_pango_layout(
                cr, node.text, font_pt * scale, italic=node.italic, bold=node.bold
            )
            _, logical = layout.get_pixel_extents()
            w = float(logical.width)
            h = float(logical.height)
            node.width = max(2.0, w)
            node.ascent = h * 0.62
            node.descent = h * 0.38
            return

        if node.kind == "row":
            w = 0.0
            asc = font_pt * 0.6
            desc = font_pt * 0.35
            for child in node.children:
                self._measure_node(cr, child, font_pt)
                w += child.width + 1.5
                asc = max(asc, child.ascent)
                desc = max(desc, child.descent)
            node.width = w
            node.ascent = asc
            node.descent = desc
            return

        if node.kind == "frac":
            assert node.num is not None and node.den is not None
            child_pt = max(8.5, font_pt * 0.88)
            self._measure_node(cr, node.num, child_pt)
            self._measure_node(cr, node.den, child_pt)
            node.width = max(node.num.width, node.den.width) + 12.0
            node.ascent = node.num.height + 6.0
            node.descent = node.den.height + 6.0
            return

        if node.kind == "sqrt":
            assert node.base is not None
            self._measure_node(cr, node.base, font_pt)
            idx_w = 0.0
            if node.index is not None:
                self._measure_node(cr, node.index, max(7.5, font_pt * 0.65))
                idx_w = max(0.0, node.index.width - 4.0)
            node.width = idx_w + 14.0 + node.base.width + 5.0
            node.ascent = node.base.ascent + 6.0
            node.descent = node.base.descent + 2.0
            return

        if node.kind == "accent":
            assert node.base is not None
            self._measure_node(cr, node.base, font_pt)
            node.width = node.base.width + 2.0
            node.ascent = node.base.ascent + 6.0
            node.descent = node.base.descent
            return

        if node.kind == "scripts":
            assert node.base is not None
            self._measure_node(cr, node.base, font_pt)
            sub_pt = max(7.5, font_pt * 0.72)
            sup_w = sub_w = 0.0
            sup_h = sub_h = 0.0
            if node.sup is not None:
                self._measure_node(cr, node.sup, sub_pt)
                sup_w = node.sup.width
                sup_h = node.sup.height
            if node.sub is not None:
                self._measure_node(cr, node.sub, sub_pt)
                sub_w = node.sub.width
                sub_h = node.sub.height

            is_big_op = (
                node.base.kind == "text" and node.base.text.strip() in BIG_OPS
            )
            if is_big_op:
                node.width = max(node.base.width, sup_w, sub_w) + 6.0
                node.ascent = node.base.ascent + (sup_h + 2.0 if node.sup else 0.0)
                node.descent = node.base.descent + (sub_h + 2.0 if node.sub else 0.0)
            else:
                node.width = node.base.width + max(sup_w, sub_w) + 2.0
                node.ascent = max(node.base.ascent, node.base.ascent * 0.55 + sup_h)
                node.descent = max(node.base.descent, node.base.descent * 0.45 + sub_h)
            return

        if node.kind == "matrix":
            if not node.rows:
                node.width = 16.0
                node.ascent = 10.0
                node.descent = 10.0
                return
            num_cols = max(len(r) for r in node.rows)
            col_widths = [0.0] * num_cols
            row_heights: list[float] = []
            for r in node.rows:
                rh = 0.0
                for c_idx, cell in enumerate(r):
                    self._measure_node(cr, cell, font_pt * 0.92)
                    col_widths[c_idx] = max(col_widths[c_idx], cell.width)
                    rh = max(rh, cell.height)
                row_heights.append(rh)

            total_w = sum(col_widths) + max(0, num_cols - 1) * 14.0
            delim_pad = (10.0 if node.left_delim else 0.0) + (
                10.0 if node.right_delim else 0.0
            )
            total_h = sum(row_heights) + max(0, len(row_heights) - 1) * 8.0 + 8.0
            node.width = total_w + delim_pad + 8.0
            node.ascent = total_h * 0.52
            node.descent = total_h * 0.48

    def _draw_node(
        self,
        cr: cairo.Context,
        node: MathNode,
        x: float,
        baseline_y: float,
        font_pt: float,
    ) -> None:
        if node.kind == "text":
            is_big = node.text.strip() in BIG_OPS and len(node.text.strip()) == 1
            scale = 1.45 if is_big else 1.0
            layout = self._make_pango_layout(
                cr, node.text, font_pt * scale, italic=node.italic, bold=node.bold
            )
            cr.move_to(x, baseline_y - node.ascent)
            PangoCairo.show_layout(cr, layout)
            return

        if node.kind == "row":
            cx = x
            for child in node.children:
                self._draw_node(cr, child, cx, baseline_y, font_pt)
                cx += child.width + 1.5
            return

        if node.kind == "frac":
            assert node.num is not None and node.den is not None
            child_pt = max(8.5, font_pt * 0.88)
            bar_y = baseline_y - font_pt * 0.32
            # Numerator centered above bar
            num_x = x + (node.width - node.num.width) / 2.0
            num_base = bar_y - 4.0 - node.num.descent
            self._draw_node(cr, node.num, num_x, num_base, child_pt)

            # Horizontal fraction bar
            cr.set_line_width(1.35)
            cr.move_to(x + 3.0, bar_y)
            cr.line_to(x + node.width - 3.0, bar_y)
            cr.stroke()

            # Denominator centered below bar
            den_x = x + (node.width - node.den.width) / 2.0
            den_base = bar_y + 4.0 + node.den.ascent
            self._draw_node(cr, node.den, den_x, den_base, child_pt)
            return

        if node.kind == "sqrt":
            assert node.base is not None
            idx_w = 0.0
            if node.index is not None:
                sub_pt = max(7.5, font_pt * 0.65)
                self._draw_node(
                    cr,
                    node.index,
                    x + 1.0,
                    baseline_y - node.base.ascent * 0.55,
                    sub_pt,
                )
                idx_w = max(0.0, node.index.width - 4.0)

            rx = x + idx_w
            top_y = baseline_y - node.base.ascent - 4.0
            bot_y = baseline_y + node.base.descent
            mid_y = (top_y + bot_y) / 2.0

            cr.set_line_width(1.35)
            cr.move_to(rx + 1.0, mid_y + 2.0)
            cr.line_to(rx + 4.0, mid_y)
            cr.line_to(rx + 8.0, bot_y)
            cr.line_to(rx + 13.0, top_y)
            cr.line_to(rx + 14.0 + node.base.width + 3.0, top_y)
            cr.stroke()

            self._draw_node(cr, node.base, rx + 14.0, baseline_y, font_pt)
            return

        if node.kind == "accent":
            assert node.base is not None
            self._draw_node(cr, node.base, x + 1.0, baseline_y, font_pt)
            top_y = baseline_y - node.base.ascent - 2.0
            left_x = x + 2.0
            right_x = x + node.base.width
            mid_x = (left_x + right_x) / 2.0
            cr.set_line_width(1.25)
            if node.accent == "hat":
                cr.move_to(left_x, top_y + 2.0)
                cr.line_to(mid_x, top_y - 2.0)
                cr.line_to(right_x, top_y + 2.0)
                cr.stroke()
            elif node.accent == "vec":
                cr.move_to(left_x, top_y)
                cr.line_to(right_x, top_y)
                cr.line_to(right_x - 3.5, top_y - 2.5)
                cr.stroke()
            else:
                cr.move_to(left_x, top_y)
                cr.line_to(right_x, top_y)
                cr.stroke()
            return

        if node.kind == "scripts":
            assert node.base is not None
            sub_pt = max(7.5, font_pt * 0.72)
            is_big_op = (
                node.base.kind == "text" and node.base.text.strip() in BIG_OPS
            )
            if is_big_op:
                base_x = x + (node.width - node.base.width) / 2.0
                self._draw_node(cr, node.base, base_x, baseline_y, font_pt)
                if node.sup is not None:
                    sup_x = x + (node.width - node.sup.width) / 2.0
                    sup_base = baseline_y - node.base.ascent - 2.0 - node.sup.descent
                    self._draw_node(cr, node.sup, sup_x, sup_base, sub_pt)
                if node.sub is not None:
                    sub_x = x + (node.width - node.sub.width) / 2.0
                    sub_base = baseline_y + node.base.descent + 2.0 + node.sub.ascent
                    self._draw_node(cr, node.sub, sub_x, sub_base, sub_pt)
            else:
                self._draw_node(cr, node.base, x, baseline_y, font_pt)
                sx = x + node.base.width + 1.0
                if node.sup is not None:
                    sup_base = baseline_y - node.base.ascent * 0.55 - node.sup.descent * 0.5
                    self._draw_node(cr, node.sup, sx, sup_base, sub_pt)
                if node.sub is not None:
                    sub_base = baseline_y + node.base.descent * 0.45 + node.sub.ascent * 0.65
                    self._draw_node(cr, node.sub, sx, sub_base, sub_pt)
            return

        if node.kind == "matrix":
            if not node.rows:
                return
            num_cols = max(len(r) for r in node.rows)
            col_widths = [0.0] * num_cols
            row_ascs: list[float] = []
            row_descs: list[float] = []
            for r in node.rows:
                ra = rd = 6.0
                for c_idx, cell in enumerate(r):
                    col_widths[c_idx] = max(col_widths[c_idx], cell.width)
                    ra = max(ra, cell.ascent)
                    rd = max(rd, cell.descent)
                row_ascs.append(ra)
                row_descs.append(rd)

            top_y = baseline_y - node.ascent + 4.0
            bot_y = baseline_y + node.descent - 4.0

            # Draw left delimiter
            if node.left_delim:
                self._draw_delimiter(cr, node.left_delim, x + 2.0, top_y, bot_y, is_left=True)
            content_x = x + (12.0 if node.left_delim else 4.0)

            cy = top_y
            for r_idx, r in enumerate(node.rows):
                ra = row_ascs[r_idx]
                rd = row_descs[r_idx]
                row_base = cy + ra
                cx = content_x
                for c_idx, cell in enumerate(r):
                    cell_x = cx + (col_widths[c_idx] - cell.width) / 2.0
                    self._draw_node(cr, cell, cell_x, row_base, font_pt * 0.92)
                    cx += col_widths[c_idx] + 14.0
                cy += ra + rd + 8.0

            if node.right_delim:
                rx = x + node.width - 8.0
                self._draw_delimiter(cr, node.right_delim, rx, top_y, bot_y, is_left=False)

    @staticmethod
    def _draw_delimiter(
        cr: cairo.Context,
        delim: str,
        x: float,
        top_y: float,
        bot_y: float,
        is_left: bool,
    ) -> None:
        cr.set_line_width(1.4)
        w = 5.0 if is_left else -5.0
        mid_y = (top_y + bot_y) / 2.0
        if delim in ("(", ")"):
            cr.move_to(x + w, top_y)
            cr.curve_to(x - w * 0.3, top_y + (bot_y - top_y) * 0.25, x - w * 0.3, bot_y - (bot_y - top_y) * 0.25, x + w, bot_y)
            cr.stroke()
        elif delim in ("[", "]"):
            cr.move_to(x + w, top_y)
            cr.line_to(x, top_y)
            cr.line_to(x, bot_y)
            cr.line_to(x + w, bot_y)
            cr.stroke()
        elif delim in ("{", "}"):
            cr.move_to(x + w, top_y)
            cr.line_to(x + w * 0.4, top_y + 3.0)
            cr.line_to(x + w * 0.4, mid_y - 3.0)
            cr.line_to(x, mid_y)
            cr.line_to(x + w * 0.4, mid_y + 3.0)
            cr.line_to(x + w * 0.4, bot_y - 3.0)
            cr.line_to(x + w, bot_y)
            cr.stroke()
        elif delim == "|":
            cr.move_to(x + 2.0, top_y)
            cr.line_to(x + 2.0, bot_y)
            cr.stroke()

    def _on_draw(
        self,
        _area: Gtk.DrawingArea,
        cr: cairo.Context,
        width: int,
        height: int,
    ) -> None:
        is_dark = Adw.StyleManager.get_default().get_dark()
        if is_dark:
            cr.set_source_rgba(0.95, 0.95, 0.97, 1.0)
        else:
            cr.set_source_rgba(0.12, 0.13, 0.16, 1.0)

        self._measure_node(cr, self.ast_root, self.base_font_size)
        start_x = max(10.0, (width - self.ast_root.width) / 2.0)
        baseline_y = 10.0 + self.ast_root.ascent
        self._draw_node(cr, self.ast_root, start_x, baseline_y, self.base_font_size)


class LatexBlockWidget(Gtk.Box):
    """Native display LaTeX block card with horizontal scrolling and Copy LaTeX action."""

    def __init__(
        self,
        latex_source: str,
        on_copied: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.latex_source = latex_source.strip()
        self.add_css_class("latex-block-frame")

        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label="LaTeX Equation", xalign=0.0)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        lbl.set_hexpand(True)
        top_bar.append(lbl)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.add_css_class("flat")
        copy_btn.set_tooltip_text("Copy LaTeX source")
        copy_btn.connect("clicked", self._on_copy_clicked, on_copied)
        top_bar.append(copy_btn)
        self.append(top_bar)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_propagate_natural_height(True)
        scroller.set_propagate_natural_width(True)

        self.canvas = LatexCanvas(self.latex_source)
        scroller.set_child(self.canvas)
        self.append(scroller)

    def _on_copy_clicked(
        self,
        btn: Gtk.Button,
        on_copied: Callable[[str], None] | None,
    ) -> None:
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(self.latex_source)
        btn.set_icon_name("object-select-symbolic")
        if on_copied:
            on_copied("Copied LaTeX expression to clipboard")

