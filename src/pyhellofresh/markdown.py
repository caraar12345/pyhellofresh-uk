"""Convert HelloFresh's recipe HTML to Markdown.

HelloFresh step/description HTML is a small subset: ``<p>``, ``<strong>``,
``<em>``, colour ``<span>``s, the odd ``<br>`` and HTML entities. This module
converts that subset with the standard library's ``html.parser`` only.

Safety properties:

* Allow-list: only paragraphs, line breaks, bold, italic, lists and links are
  emitted. Every other tag (``span``, ``div``, ``img``, ...) is dropped while
  its text is kept; ``script``/``style`` contents are dropped entirely.
* Text is escaped, so characters in the recipe text such as ``*`` or ``[``
  cannot turn into Markdown formatting or links.
* Links are only emitted for ``http``, ``https`` and ``mailto`` URLs; anything
  else (e.g. ``javascript:``) is reduced to its text.
* No HTML is passed through, so rendering the result with a Markdown renderer
  that allows raw HTML still cannot inject markup.

Callouts: HelloFresh flags warnings and tips inline with a bold label, e.g.
``<strong>IMPORTANT:</strong><em> Wash your hands...</em>``. With
``alerts=True`` (the default) the label and the text after it are split out of
the paragraph into a GitHub-flavoured Markdown alert::

    > [!IMPORTANT]
    > *Wash your hands...*

Renderers without GFM alert support show this as an ordinary blockquote with a
literal ``[!IMPORTANT]`` line. Pass ``alerts=False`` to keep plain CommonMark
(``**IMPORTANT:** *Wash your hands...*``).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

_BLOCK_TAGS = {"p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
_EMPHASIS = {"strong": "**", "b": "**", "em": "*", "i": "*"}
_SKIP_CONTENT = {"script", "style", "template"}
_SAFE_SCHEMES = {"http", "https", "mailto"}

# Characters with inline meaning in CommonMark.
_INLINE_ESCAPE = re.compile(r"([\\`*_\[\]<>|~])")
# Constructs that only mean something at the start of a line.
_LINE_START_ESCAPE = re.compile(r"^(\s*)(?:([#>+\-=])|(\d+)([.)]))(?=\s|$)", re.MULTILINE)
_WHITESPACE = re.compile(r"[ \t\r\n\f]+")

# Bold labels that become GFM alerts. Upper-case only, to match HelloFresh's
# house style and avoid catching ordinary bold words like "Tip".
_ALERT_LABEL = re.compile(r"^(IMPORTANT|TIP|NOTE|WARNING|CAUTION)\s*:?$")
# Placeholder for an alert label inside the inline buffer. NUL never survives
# from input text (stripped in handle_data), so it cannot be forged.
_ALERT_SENTINEL = re.compile("\x00([A-Z]+)\x00")


def html_to_markdown(html: str | None, *, alerts: bool = True) -> str:
    """Convert a HelloFresh HTML fragment to safe Markdown.

    With ``alerts=True`` (default), ``IMPORTANT:``/``TIP:``/``NOTE:``/
    ``WARNING:``/``CAUTION:`` callouts become GitHub-flavoured Markdown alerts;
    with ``alerts=False`` the output is plain CommonMark.
    Returns an empty string for empty input.
    """
    if not html:
        return ""
    parser = _MarkdownParser(alerts=alerts)
    parser.feed(html)
    parser.close()
    return parser.result()


class _MarkdownParser(HTMLParser):
    def __init__(self, *, alerts: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._alerts = alerts
        self._blocks: list[str] = []
        # Stack of inline buffers; emphasis/link tags push a new buffer.
        self._stack: list[tuple[str, list[str], dict[str, str]]] = [("", [], {})]
        self._skip_depth = 0
        self._lists: list[list[int | None]] = []  # [counter] per open list; None = bullet
        self._list_lines: list[str] = []

    # -- tag handling -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._append("\n")
        elif tag in ("ul", "ol"):
            self._flush_block()
            self._lists.append([1 if tag == "ol" else None])
        elif tag == "li":
            self._flush_block()
        elif tag in _BLOCK_TAGS:
            self._flush_block()
        elif tag in _EMPHASIS or tag == "a":
            self._stack.append((tag, [], {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("ul", "ol"):
            self._end_list()
        elif tag == "li":
            self._flush_block(list_item=True)
        elif tag in _BLOCK_TAGS:
            self._flush_block()
        elif tag in _EMPHASIS or tag == "a":
            self._close_inline(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _WHITESPACE.sub(" ", data.replace("\x00", ""))
        self._append(_INLINE_ESCAPE.sub(r"\\\1", text))

    # -- helpers ------------------------------------------------------

    def _append(self, text: str) -> None:
        self._stack[-1][1].append(text)

    def _close_inline(self, tag: str) -> None:
        # Close any unclosed tags above the matching one (tolerate bad nesting).
        if not any(t == tag for t, _, _ in self._stack[1:]):
            return
        while True:
            open_tag, parts, attrs = self._stack.pop()
            inner = "".join(parts)
            label = _ALERT_LABEL.match(inner.strip())
            if (
                label
                and self._alerts
                and open_tag in ("strong", "b")
                and len(self._stack) == 1  # directly in the paragraph, not nested
                and not self._lists
            ):
                self._append(f"\x00{label.group(1)}\x00")
            else:
                self._append(_render_inline(open_tag, inner, attrs))
            if open_tag == tag:
                return

    def _flush_block(self, *, list_item: bool = False) -> None:
        while len(self._stack) > 1:
            self._close_inline(self._stack[-1][0])
        parts = self._stack[0][1]
        raw = "".join(parts)
        parts.clear()
        if not self._lists:
            self._blocks.extend(_split_alerts(raw))
            return
        # Alerts can't live inside list items; fall back to bold labels.
        text = _format_block(_ALERT_SENTINEL.sub(r"**\1:**", raw))
        if not text:
            return
        indent = "  " * (len(self._lists) - 1)
        counter = self._lists[-1]
        if list_item and counter[0] is not None:
            marker = f"{counter[0]}."
            counter[0] += 1
        elif list_item:
            marker = "-"
        else:
            marker = " "  # stray text inside a list: continuation line
        line = indent + marker + " " + text.replace("\n", "\n" + indent + "   ")
        self._list_lines.append(line.rstrip() if marker == " " else line)

    def _end_list(self) -> None:
        self._flush_block()
        if self._lists:
            self._lists.pop()
        if not self._lists and self._list_lines:
            self._blocks.append("\n".join(self._list_lines))
            self._list_lines = []

    def result(self) -> str:
        while self._lists:
            self._end_list()
        self._flush_block()
        return "\n\n".join(self._blocks)


def _format_block(text: str) -> str:
    """Trim each line, join with hard breaks and escape line-start syntax."""
    lines = [line.strip() for line in text.split("\n")]
    text = "  \n".join(line for line in lines if line)
    return _LINE_START_ESCAPE.sub(_escape_line_start, text)


def _split_alerts(raw: str) -> list[str]:
    """Split a paragraph at alert labels into a paragraph plus GFM alert blocks."""
    pieces = _ALERT_SENTINEL.split(raw)
    # pieces = [before, LABEL, after, LABEL, after, ...]
    blocks = []
    before = _format_block(pieces[0])
    for i in range(1, len(pieces), 2):
        label = pieces[i]
        body = _format_block(pieces[i + 1].lstrip(" :"))
        if not body:
            # A bare label with nothing after it: keep it as bold text.
            before = f"{before} **{label}:**".strip()
            continue
        if before:
            blocks.append(before)
            before = ""
        quoted = "\n".join(f"> {line}" for line in body.split("\n"))
        blocks.append(f"> [!{label}]\n{quoted}")
    if before:
        blocks.append(before)
    return blocks


def _escape_line_start(m: re.Match[str]) -> str:
    # "# x" -> "\\# x"; "1. x" -> "1\\. x" (CommonMark escapes the delimiter, not the digit).
    if m.group(2):
        return f"{m.group(1)}\\{m.group(2)}"
    return f"{m.group(1)}{m.group(3)}\\{m.group(4)}"


def _render_inline(tag: str, inner: str, attrs: dict[str, str]) -> str:
    # Move surrounding whitespace outside the markers: "**garlic **" is not bold
    # in CommonMark, "**garlic** " is.
    core = inner.strip()
    lead = " " if inner[:1].isspace() else ""
    trail = " " if inner[-1:].isspace() else ""
    if not core:
        return lead or trail
    if tag == "a":
        href = attrs.get("href", "").strip()
        if urlparse(href).scheme.lower() in _SAFE_SCHEMES:
            safe_href = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            return f"{lead}[{core}]({safe_href}){trail}"
        return f"{lead}{core}{trail}"
    marker = _EMPHASIS[tag]
    # Already wrapped in the same marker (e.g. <strong><b>x</b></strong>).
    if core.startswith(marker) and core.endswith(marker) and len(core) > 2 * len(marker):
        return f"{lead}{core}{trail}"
    return f"{lead}{marker}{core}{marker}{trail}"
