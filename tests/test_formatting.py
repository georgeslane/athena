from pi_assistant.formatting import markdown_to_telegram_html, split_message


def test_basic_markdown():
    out = markdown_to_telegram_html("# Plan\n**Bold** and *italic* and ~~gone~~\n- one\n- two")
    assert out == "<b>Plan</b>\n<b>Bold</b> and <i>italic</i> and <s>gone</s>\n• one\n• two"


def test_html_is_escaped_outside_and_inside_code():
    out = markdown_to_telegram_html("a < b & c > d `x<y>` \n```python\nif a < b:\n    pass\n```")
    assert "a &lt; b &amp; c &gt; d" in out
    assert "<code>x&lt;y&gt;</code>" in out
    assert '<pre><code class="language-python">if a &lt; b:\n    pass</code></pre>' in out


def test_code_is_not_formatted():
    out = markdown_to_telegram_html("`**not bold**` and snake_case_name")
    assert "<code>**not bold**</code>" in out
    assert "snake_case_name" in out


def test_links():
    out = markdown_to_telegram_html("See [the docs](https://example.com/a?b=1&c=2).")
    assert '<a href="https://example.com/a?b=1&amp;c=2">the docs</a>' in out


def test_lone_asterisks_left_alone():
    assert markdown_to_telegram_html("2 * 3 * 4") == "2 * 3 * 4"


def test_split_short_message_untouched():
    assert split_message("hello") == ["hello"]


def test_split_long_message_on_lines():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1150 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
