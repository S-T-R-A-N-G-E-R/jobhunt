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


def test_late_note_only_for_a_late_first_run(monkeypatch):
    from jobhunt import db
    from jobhunt.digest import late_note

    monkeypatch.setenv("JOBHUNT_TIMEZONE", "Asia/Kolkata")
    conn = db.connect(":memory:")

    def run(started_utc: str):
        conn.execute("INSERT INTO runs (run_date, started_at) VALUES ('2026-11-05', ?)",
                     (started_utc,))  # fmt: skip
        return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    first = run("2026-11-05T05:00:00+00:00")  # 10:30 IST: 2h 43m after 07:47
    assert late_note(conn, first) is None  # not a scheduled run: no env
    monkeypatch.setenv("JOBHUNT_SCHEDULED_AT", "07:47")
    assert late_note(conn, first).startswith("⏰ Today's run started 2h 43m after 07:47")
    assert late_note(conn, run("2026-11-05T09:13:00+00:00")) is None  # a retry, not the first
    conn.execute("DELETE FROM runs")
    assert late_note(conn, run("2026-11-05T03:23:00+00:00")) is None  # 08:53 IST: 66 min
