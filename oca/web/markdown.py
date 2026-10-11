"""A deliberately small Markdown-to-HTML renderer for OCA's own reports (headings, lists, tables,
fenced code, paragraphs, bold, inline code, links). Everything is HTML-escaped first; no raw HTML passes."""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)")


def _inline(text: str) -> str:
    text = str(escape(text))
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return text


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if len(cells) >= 2 and all(re.fullmatch(r":?-+:?", c) for c in cells[1] if c):
        head, body = cells[0], cells[2:]
    else:
        head, body = None, cells
    out = ['<table class="compact">']
    if head:
        out.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead>")
    out.append("<tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table>")
    return "".join(out)


def to_html(markdown: str) -> Markup:
    html: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            html.append(f'<pre class="{escape(lang)}">{escape(chr(10).join(block))}</pre>')
            continue
        if not stripped:
            i += 1
            continue
        if stripped == "---":
            html.append("<hr>")
            i += 1
            continue
        m = re.match(r"(#{1,4})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            html.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            html.append(_table(rows))
            continue
        if stripped.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            html.append("<ul>" + "".join(items) + "</ul>")
            continue
        if stripped.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote.append(_inline(lines[i].strip()[2:]))
                i += 1
            html.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,4}\s|```|\||- |> |---$)", lines[i].strip()):
            para.append(_inline(lines[i].strip()))
            i += 1
        html.append("<p>" + "<br>".join(para) + "</p>")
    return Markup("\n".join(html))
