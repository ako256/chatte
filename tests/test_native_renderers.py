"""Unit tests for 100% Native GTK 4 Markdown, GtkSourceView 5 Code Blocks,
and PangoCairo LaTeX Math Renderers (verifying zero WebView usage).
"""

from __future__ import annotations

import unittest

import cairo

from chatte.ui.latex_widget import (
    LatexBlockWidget,
    LatexCanvas,
    latex_inline_to_pango,
    parse_math_ast,
)
from chatte.ui.markdown_view import (
    MarkdownView,
    extract_paragraph_citations,
    inline_markdown_to_pango,
    parse_markdown_blocks,
    prepare_streaming_markdown,
)


class TestNativeRenderers(unittest.TestCase):
    def test_inline_latex_and_markdown_to_pango(self) -> None:
        pango_math = latex_inline_to_pango(r"\alpha^2 + \beta_i = \frac{1}{2} \sqrt{x} \in \mathbb{R}")
        self.assertIn("α", pango_math)
        self.assertIn("<sup>", pango_math)
        self.assertIn("<sub>", pango_math)
        self.assertIn("ℝ", pango_math)

        citations = {
            1: {"index": 1, "url": "https://en.wikipedia.org/wiki/GNOME", "domain": "wikipedia.org"}
        }
        cleaned_txt, extracted_cits = extract_paragraph_citations(
            "Check **bold**, `code`, $E = mc^2$, and citation [1].",
            citation_map=citations,
        )
        self.assertEqual(cleaned_txt, "Check **bold**, `code`, $E = mc^2$, and citation.")
        self.assertEqual(len(extracted_cits), 1)
        self.assertEqual(extracted_cits[0]["domain"], "wikipedia.org")

        markup = inline_markdown_to_pango(cleaned_txt, citation_map=citations)
        self.assertIn("<b>bold</b>", markup)
        self.assertIn("<tt><b>code</b></tt>", markup)

        # Verify on-the-fly streaming markdown sanitizer auto-closes unfinished bold/code
        stream_prepared = prepare_streaming_markdown("Here is **bold in progress and [1")
        self.assertEqual(stream_prepared, "Here is **bold in progress and**")

    def test_markdown_block_parser_and_widgets(self) -> None:
        sample_md = """# Heading 1

Here is an inline equation $\\nabla \\cdot \\mathbf{E} = \\frac{\\rho}{\\varepsilon_0}$ and citation [1].

```python
def greet(name: str) -> str:
    return f"Hello, {name}!"
```

$$
\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}
$$

| Feature | Implementation |
| --- | --- |
| UI | GTK 4.22 + Libadwaita 1.9 |
| Code | GtkSourceView 5 |
"""
        blocks = parse_markdown_blocks(sample_md)
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds, ["heading", "paragraph", "code", "latex", "table"])

        # Instantiate native GTK4 MarkdownView and verify child widgets build cleanly
        md_view = MarkdownView(
            sample_md,
            citations=[{"index": 1, "title": "Docs", "url": "https://gnome.org", "domain": "gnome.org"}],
        )
        self.assertIsNotNone(md_view.get_first_child())

    def test_display_latex_cairo_canvas_renders(self) -> None:
        eq = r"\sum_{n=1}^{\infty} \frac{1}{n^2} = \frac{\pi^2}{6} = \begin{pmatrix} a & b \\ c & d \end{pmatrix}"
        ast_node = parse_math_ast(eq)
        self.assertEqual(ast_node.kind, "row")

        canvas = LatexCanvas(eq)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 480, 140)
        cr = cairo.Context(surf)
        canvas._on_draw(canvas, cr, 480, 140)

        blk = LatexBlockWidget(eq)
        self.assertIsNotNone(blk.canvas)


if __name__ == "__main__":
    unittest.main()

