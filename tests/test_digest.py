from jobhunt.digest import _html_block, split_messages


def test_split_messages_respects_limit():
    parts = ["x" * 1500 for _ in range(10)]
    messages = split_messages(parts)
    assert len(messages) > 1
    assert all(len(m) <= 4096 for m in messages)
    assert sum(m.count("x") for m in messages) == 15000


def test_html_escaping():
    block = _html_block(
        {
            "company": "A&B",
            "title": "Dev <Go>",
            "location": "Remote",
            "score": 90,
            "pitch": "",
            "reason": "",
            "flag": "",
            "comp": "",
            "url": 'https://x/?a=1&b="2"',
        }
    )
    assert "A&amp;B" in block and "Dev &lt;Go&gt;" in block
    assert 'href="https://x/?a=1&amp;b=&quot;2&quot;"' in block
