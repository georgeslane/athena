"""Convert the model's Markdown into the small HTML subset Telegram accepts, or plain text for Siri."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
_CHUNK_TARGET = 3500  # leave room for HTML tags added during conversion

_CODE_BLOCK = re.compile(r"```([\w+-]*)[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def markdown_to_telegram_html(text: str) -> str:
    placeholders: list[str] = []

    def stash(fragment: str) -> str:
        placeholders.append(fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    def code_block(m: re.Match[str]) -> str:
        lang, body = m.group(1), html.escape(m.group(2).rstrip("\n"), quote=False)
        attr = f' class="language-{lang}"' if lang else ""
        return stash(f"<pre><code{attr}>{body}</code></pre>")

    text = _CODE_BLOCK.sub(code_block, text)
    text = _INLINE_CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _LINK.sub(
        lambda m: stash(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1), quote=False)}</a>'), text
    )

    text = html.escape(text, quote=False)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)

    return re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)


def markdown_to_speech(text: str) -> str:
    """Plain text for Siri to read out: no Markdown symbols, and links reduced to their text."""
    text = _CODE_BLOCK.sub(lambda m: m.group(2), text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _HEADING.sub(r"\1", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC.sub(r"\1", text)
    text = _STRIKE.sub(r"\1", text)
    text = _BULLET.sub(r"\1", text)
    return text.strip()


def split_message(text: str, limit: int = _CHUNK_TARGET) -> list[str]:
    """Split long replies on paragraph or line boundaries, keeping code blocks intact where possible."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    in_code = False
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current and not in_code:
            chunks.append(current)
            current = ""
        while len(line) > limit:  # a single enormous line
            chunks.append(line[:limit])
            line = line[limit:]
        if line.lstrip().startswith("```"):
            in_code = not in_code
        current += line
        if len(current) > limit * 1.15:  # code block too big to keep whole
            chunks.append(current)
            current = ""
    if current.strip():
        chunks.append(current)
    return [c.strip("\n") for c in chunks if c.strip()]
