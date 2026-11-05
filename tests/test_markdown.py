"""Tests for the HTML -> Markdown converter."""

import pytest

from pyhellofresh import html_to_markdown


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # Whitespace inside <strong> is moved outside so the bold still renders.
        (
            "<p>Add <strong>¼ tsp salt</strong> and the<strong> water for the rice</strong> (see pantry).</p>",
            "Add **¼ tsp salt** and the **water for the rice** (see pantry).",
        ),
        # Colour spans are dropped, entities decoded, emphasis kept.
        (
            '<p>10-15 mins. <span style="color: rgb(224, 62, 45)"><strong>IMPORTANT:</strong></span>'
            "<em> It&#39;s cooked when opaque</em>.</p>",
            "10-15 mins. **IMPORTANT:** *It's cooked when opaque*.",
        ),
        ("<p>One.</p>\n<p>Two. </p>", "One.\n\nTwo."),
        ("<p>a<br>b</p>", "a  \nb"),
        ("<ul><li>one</li><li><b>two</b></li></ul><p>after</p>", "- one\n- **two**\n\nafter"),
        ("<ol><li>a</li><li>b</li></ol>", "1. a\n2. b"),
        ("<p><strong></strong>empty</p>", "empty"),
        ("", ""),
        (None, ""),
    ],
)
def test_conversion(html: str | None, expected: str) -> None:
    assert html_to_markdown(html) == expected


def test_text_is_escaped() -> None:
    md = html_to_markdown("<p>5 * 2 [x](http://evil) _y_ <b>`code`</b></p>")
    assert md == r"5 \* 2 \[x\](http://evil) \_y\_ **\`code\`**"


def test_line_start_constructs_escaped() -> None:
    assert html_to_markdown("<p># not a heading</p><p>1. not a list</p><p>- nor this</p>") == (
        "\\# not a heading\n\n1\\. not a list\n\n\\- nor this"
    )


def test_unsafe_links_reduced_to_text() -> None:
    md = html_to_markdown(
        '<p><a href="javascript:alert(1)">bad</a> <a href="https://hellofresh.co.uk/a b">ok</a></p>'
    )
    assert md == "bad [ok](https://hellofresh.co.uk/a%20b)"


def test_script_style_and_raw_html_dropped() -> None:
    md = html_to_markdown("<p>hi<script>alert(1)</script><style>p{}</style><img src=x onerror=y></p>")
    assert md == "hi"
    assert "<" not in html_to_markdown("<p>&lt;script&gt;</p>").replace("\\<", "")


def test_bad_nesting_tolerated() -> None:
    assert html_to_markdown("<p><strong>unclosed <em>nest</p><p>next</p>") == (
        "**unclosed *nest***\n\nnext"
    )
