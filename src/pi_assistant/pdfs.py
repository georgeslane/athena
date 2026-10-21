"""Reading text out of PDFs, and making PDFs from Markdown: run in the code's sandbox (files.py).

This module stands alone, needing only its own packages, as its source is what's run there: pypdf to read,
markdown-it to turn Markdown into HTML, and fpdf2 to lay that out as a PDF. Nothing of Athena's is there.

A PDF made here uses DejaVu, if the system has it (the fonts-dejavu-core package), which has most of the
world's letters; otherwise the PDF standard's own fonts, which have only Western European ones, so others
are replaced.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_PAGES = 500  # read from a PDF; the rest are left out
DEJAVU = Path("/usr/share/fonts/truetype/dejavu")
FONTS = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf", "I": "DejaVuSans-Oblique.ttf",
         "BI": "DejaVuSans-BoldOblique.ttf"}  # fmt: skip
MONO = "DejaVuSansMono.ttf"
# Punctuation the PDF standard's fonts lack, as near as they have.
PLAIN = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "…": "...", "•": "*", " ": " ", "→": "->"})  # fmt: skip


def read_pdf(path: str, start: int, length: int) -> dict[str, Any]:
    """The text of a PDF, page by page, from character ``start``, ``length`` of them, and how much there is."""
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        return {"problem": "It's protected with a password, so it can't be read."}
    texts = [(page.extract_text() or "").strip() for page in reader.pages[:MAX_PAGES]]
    text = "\n\n".join(f"[Page {n}]\n{page}" for n, page in enumerate(texts, 1))
    return {
        "pages": len(reader.pages),
        "read": len(texts),
        "length": len(text),
        "text": text[start : start + length],
        "empty": not any(ch.isalnum() for page in texts for ch in page),  # a scan, say: pictures of text
    }


def make_pdf(markdown: str, path: str, title: str = "") -> dict[str, Any]:
    """Lay ``markdown`` out as an A4 PDF at ``path``. Raw HTML in it is shown as text, and pictures are left out:
    there's nothing to fetch them from."""
    from fpdf import FPDF
    from fpdf.enums import TextEmphasis
    from fpdf.html import DEFAULT_TAG_STYLES
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"]).disable("image")
    html = re.sub(r">\s+<", "><", md.render(markdown))  # the line breaks between tags would each take a line
    pdf = FPDF(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, 18)
    pdf.set_creator("Athena")
    if title:
        pdf.set_title(title)
    unicode = all((DEJAVU / name).is_file() for name in [*FONTS.values(), MONO])
    if unicode:
        for style, name in FONTS.items():
            pdf.add_font("body", style, str(DEJAVU / name))
        pdf.add_font("mono", "", str(DEJAVU / MONO))
        body, mono = "body", "mono"
    else:
        html = html.translate(PLAIN).encode("latin-1", "replace").decode("latin-1")
        body, mono = "helvetica", "courier"
    pdf.add_page()
    pdf.set_font(body, size=11)
    ink, quiet = "#1a1a1a", "#555555"  # rather than fpdf2's dark red
    styles = {f"h{n}": DEFAULT_TAG_STYLES[f"h{n}"].replace(color=ink) for n in range(1, 7)}
    styles["blockquote"] = DEFAULT_TAG_STYLES["blockquote"].replace(color=quiet, emphasis=TextEmphasis.I)
    styles["code"] = DEFAULT_TAG_STYLES["code"].replace(family=mono)
    styles["pre"] = DEFAULT_TAG_STYLES["pre"].replace(font_family=mono)
    pdf.write_html(html, font_family=body, table_line_separators=True, tag_styles=styles,
                   li_prefix_color=quiet, warn_on_tags_not_matching=False)  # fmt: skip
    pdf.output(path)
    return {"pages": pdf.page, "unicode": unicode}
