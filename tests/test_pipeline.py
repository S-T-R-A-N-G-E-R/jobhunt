import json

from jobhunt import config, db, pipeline, scoring

from .conftest import GOOD_SCORE, FakeBoards, FakeLLM, FakeTelegram, board_payloads


def run(boards=None, llm=None, tg=None, **kw):
    boards, llm, tg = boards or FakeBoards(), llm or FakeLLM(), tg or FakeTelegram()
    run_id = pipeline.run(
        show_progress=False,
        fetch_transport=boards.transport(),
        llm_transport=llm.transport(),
        telegram_transport=tg.transport(),
        **kw,
    )
    conn = db.connect()
    return (db.get_run(conn, run_id) if run_id else None), conn


def test_full_run_then_rerun_is_idempotent(home):
    llm, tg = FakeLLM(), FakeTelegram()
    r, conn = run(llm=llm, tg=tg)
    assert r["status"] == "ok"
    assert (r["companies"], r["fetched"], r["new_jobs"]) == (3, 4, 4)
    # Account Executive (title) and London (location) are filtered; 2 jobs go to the LLM.
    assert r["filtered_out"] == 2 and r["scored_local"] == 2 and llm.chat_calls == 2
    assert r["sent"] == 2 and len(tg.messages) == 3  # header + one message per job
    assert tg.messages[0].startswith("2 new matches today · 4 jobs checked across 3 companies")
    assert "Backend Engineer &lt;Payments&gt;" in tg.messages[2]  # escaped
    # One ping per digest: only the header notifies. Each job has its own Apply / Skip buttons.
    assert [m["disable_notification"] for m in tg.sent] == [False, True, True]
    job_ids = [r[0] for r in conn.execute("SELECT job_id FROM digests ORDER BY id")]
    buttons = [m["reply_markup"]["inline_keyboard"][0] for m in tg.sent[1:]]
    assert [[b["callback_data"] for b in row] for row in buttons] == [
        [f"a:{i}", f"s:{i}"] for i in job_ids
    ]
    assert "reply_markup" not in tg.sent[0]
    assert (home / "output").glob("digest-*.md")

    llm2, tg2 = FakeLLM(), FakeTelegram()
    r2, conn = run(llm=llm2, tg=tg2)
    assert r2["status"] == "ok" and r2["new_jobs"] == 0
    assert llm2.chat_calls == 0 and r2["filtered_out"] == 0
    assert tg2.messages == []  # nothing new, status line already sent today
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4


def test_if_not_run_today_skips_after_ok_run(home):
    run()
    r, _ = run(if_not_run_today=True)
    assert r is None


def test_company_error_is_reported_but_run_is_ok(home):
    tg = FakeTelegram()
    r, _ = run(boards=FakeBoards(fail={"lev"}), tg=tg)
    assert r["status"] == "ok"
    assert set(json.loads(r["errors"])) == {"Lev"}
    assert "Couldn't fetch: Lev" in tg.messages[0]
    assert run(if_not_run_today=True)[0] is None  # not retried hourly


def test_every_fetch_failing_is_retryable(home):
    r, _ = run(boards=FakeBoards(fail={"acme", "lev", "ash"}))
    assert r["status"] == "partial"
    r2, _ = run(if_not_run_today=True)  # catch-up run is not skipped
    assert r2["status"] == "ok" and r2["fetched"] == 4


def test_telegram_down_is_retryable_and_resends(home):
    r, conn = run(tg=FakeTelegram(down=True))
    assert r["status"] == "partial" and "digest" in json.loads(r["errors"])
    assert conn.execute("SELECT count(*) FROM digests").fetchone()[0] == 0
    tg = FakeTelegram()
    r2, _ = run(tg=tg, if_not_run_today=True)
    assert r2["status"] == "ok" and r2["sent"] == 2 and len(tg.messages) == 3


def test_telegram_failure_fails_the_command(home, monkeypatch):
    """A failed send must turn the Actions run red, not leave it green."""
    from typer.testing import CliRunner

    from jobhunt import cli

    real = pipeline.run
    monkeypatch.setattr(
        pipeline, "run",
        lambda **kw: real(
            **kw, fetch_transport=FakeBoards().transport(), llm_transport=FakeLLM().transport(),
            telegram_transport=FakeTelegram(down=True).transport(),
        ),
    )  # fmt: skip
    result = CliRunner().invoke(cli.app, ["run", "--quiet"])
    assert result.exit_code == 1 and "Telegram send failed" in result.output
    assert "SECRET" not in result.output


def test_ollama_down_skips_scoring_and_is_retryable(home):
    tg = FakeTelegram()
    r, conn = run(llm=FakeLLM(ollama_up=False), tg=tg)
    assert r["status"] == "partial"
    assert r["scored_local"] == 0 and r["filtered_out"] == 2  # filters still run
    assert "open the Ollama app" in tg.messages[0]
    assert tg.messages[0].startswith("⚠️ Scoring skipped")

    llm, tg2 = FakeLLM(), FakeTelegram()
    r2, _ = run(llm=llm, tg=tg2, if_not_run_today=True)  # catch-up run is not skipped
    assert r2["status"] == "ok" and llm.chat_calls == 2 and r2["sent"] == 2
    assert run(if_not_run_today=True)[0] is None


def test_ollama_down_sends_no_paid_calls(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = FakeLLM(ollama_up=False)
    run(llm=llm)
    assert llm.paid_calls == 0


def test_invalid_local_output_escalates_in_run(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = FakeLLM(['{"bad": true}'])
    r, conn = run(llm=llm)
    assert r["escalated"] == 2 and llm.paid_calls == 2
    rows = conn.execute("SELECT provider, escalated FROM scores WHERE status='scored'").fetchall()
    assert {tuple(x) for x in rows} == {("anthropic", 1)}


def test_ollama_cloud_quota_is_reported_not_escalated(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # fallback enabled + key: still unused
    llm, tg = FakeLLM(quota_after=1), FakeTelegram()
    r, conn = run(llm=llm, tg=tg)
    assert r["scored_local"] == 1 and r["quota_skipped"] == 1 and r["score_failed"] == 0
    assert llm.paid_calls == 0
    assert tg.messages[0].startswith(
        "⚠️ Ollama Cloud free credits exhausted: 1 jobs not scored. Options: wait for reset, "
        "set vars.JOBHUNT_LLM=runner, or enable the paid fallback."
    )
    # The skipped job stays in the backlog (no score_failed row), so the next run scores it.
    llm2 = FakeLLM()
    run(llm=llm2)
    assert llm2.chat_calls == 1


def test_score_failed_without_fallback_does_not_crash(home):
    llm = FakeLLM(['{"bad": true}'])
    r, conn = run(llm=llm)
    assert r["status"] == "ok" and r["score_failed"] == 2
    err = conn.execute("SELECT error FROM scores WHERE status='score_failed'").fetchone()[0]
    assert "ANTHROPIC_API_KEY not set" in err
    llm2 = FakeLLM()
    run(llm=llm2)
    assert llm2.chat_calls == 0  # failures retry on a later day, not same day


def test_profile_change_refilters_but_does_not_auto_rescore(home):
    run()
    profile_path = config.PROFILE_PATH
    # Now also accept London and change the target title wording.
    profile_path.write_text(
        profile_path.read_text().replace(
            '["Bengaluru", "Remote - India"]', '["Bengaluru", "Remote - India", "London"]'
        )
    )
    llm = FakeLLM()
    r, conn = run(llm=llm)
    # The London job's filter row was stale -> re-evaluated -> passes -> new job to score.
    assert llm.chat_calls == 1
    profile = config.load_profile()
    p = scoring.plan(conn, profile, config.load_companies(), write=False)
    assert p.stale == 2 and p.backlog == 0

    p = scoring.plan(conn, profile, config.load_companies(), rescore=True, write=False)
    assert len(p.to_score) == 2
    ph = config.profile_hash()
    latest = db.latest_scores(conn).values()
    assert {s["profile_hash"] for s in latest if s["status"] == "filtered"} == {ph}


def test_rescore_respects_cap(home):
    run()
    config.PROFILE_PATH.write_text(
        config.PROFILE_PATH.read_text().replace(
            "max_llm_calls_per_run: 60", "max_llm_calls_per_run: 1"
        )
    )
    conn = db.connect()
    llm = FakeLLM()
    from jobhunt.llm import LLM

    profile = config.load_profile()
    pipeline.score(
        conn,
        profile,
        config.load_companies(),
        LLM(profile.llm, llm.transport()),
        rescore=True,
        show_progress=False,
    )
    assert llm.chat_calls == 1


def test_backlog_beyond_cap_is_scored_later(home):
    config.PROFILE_PATH.write_text(
        config.PROFILE_PATH.read_text().replace(
            "max_llm_calls_per_run: 60", "max_llm_calls_per_run: 1"
        )
    )
    r, conn = run()
    assert r["scored_local"] == 1
    p = scoring.plan(conn, config.load_profile(), config.load_companies(), write=False)
    assert p.backlog == 1
    llm = FakeLLM()
    run(llm=llm)
    assert llm.chat_calls == 1
    p = scoring.plan(conn, config.load_profile(), config.load_companies(), write=False)
    assert p.backlog == 0


def test_changed_job_is_rescored_and_vanished_job_deactivated(home):
    run()
    payloads = board_payloads()
    payloads["acme"]["jobs"][0]["content"] = "Totally new description"
    payloads["lev"] = []
    llm = FakeLLM()
    _, conn = run(boards=FakeBoards(payloads), llm=llm)
    assert llm.chat_calls == 1
    inactive = conn.execute("SELECT company FROM jobs WHERE is_active=0").fetchall()
    assert [x[0] for x in inactive] == ["Lev"]


def test_removed_company_jobs_are_deactivated(home):
    run()
    config.COMPANIES_PATH.write_text(
        "companies:\n  - {name: Acme, ats: greenhouse, slug: acme}\n"
        "  - {name: Ash, ats: ashby, slug: ash, priority: high}\n"
    )
    _, conn = run()
    rows = conn.execute("SELECT company, is_active FROM jobs").fetchall()
    assert {c for c, active in rows if not active} == {"Lev"}
    assert all(active for c, active in rows if c != "Lev")


def test_old_inactive_descriptions_are_dropped_without_rescoring(home):
    run()
    payloads = board_payloads()
    gone = payloads.pop("lev")
    run(boards=FakeBoards(payloads | {"lev": []}))
    conn = db.connect()
    conn.execute("UPDATE jobs SET last_seen_at='2020-01-01T00:00:00+00:00' WHERE is_active=0")
    conn.commit()
    assert db.prune_descriptions(conn) == 1
    row = conn.execute("SELECT description_text FROM jobs WHERE company='Lev'").fetchone()
    assert row[0] is None
    assert db.latest_scores(conn)  # scores kept
    # The job comes back unchanged: description refilled, same hash, no new LLM call.
    llm = FakeLLM()
    run(boards=FakeBoards(payloads | {"lev": gone}), llm=llm)
    row = conn.execute(
        "SELECT description_text, is_active FROM jobs WHERE company='Lev'"
    ).fetchone()
    assert row["description_text"] == "Python services" and row["is_active"] == 1
    assert llm.chat_calls == 0


def test_crash_sends_failure_notice(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pipeline, "fetch", boom)
    tg = FakeTelegram()
    try:
        run(tg=tg)
    except RuntimeError:
        pass
    conn = db.connect()
    assert db.get_run(conn)["status"] == "failed"
    assert "run failed" in tg.messages[0] and "jobhunt.log" in tg.messages[0]


def test_token_never_logged(home):
    run(boards=FakeBoards(fail={"lev"}))
    assert "SECRET" not in (home / "logs" / "jobhunt.log").read_text()


def test_timezone_override(monkeypatch):
    from jobhunt import config

    monkeypatch.setenv("JOBHUNT_TIMEZONE", "Pacific/Kiritimati")  # UTC+14
    assert db.local_date("2026-01-01T12:00:00+00:00") == "2026-01-02"
    monkeypatch.delenv("JOBHUNT_TIMEZONE")
    assert config.local_tz() is not None


def over_score(score, flags):
    return json.dumps(GOOD_SCORE | {"score": score, "seniority_match": "over", "red_flags": flags})


def test_stretch_section_shows_capped_roles_once(home):
    llm = FakeLLM([over_score(90, ["compensation unknown", "Needs 5+ years of experience"]),
                   over_score(50, [])])  # fmt: skip
    tg = FakeTelegram()
    r, conn = run(llm=llm, tg=tg)
    header, section, job = tg.messages
    assert header.startswith("0 new matches today")  # both capped at 40, below the threshold
    assert section == "<b>🧗 Stretch roles</b> (1, above your level)"  # the 50 isn't a near-miss
    assert "⭐ 90 before the cap" in job
    assert "<b>Stretch</b>, above your level: Needs 5+ years of experience" in job
    assert "reply_markup" in tg.sent[2] and tg.sent[1]["disable_notification"]
    assert r["sent"] == 1
    rows = conn.execute("SELECT score, uncapped_score FROM scores WHERE status='scored'")
    assert sorted(tuple(x) for x in rows) == [(40, 50), (40, 90)]

    tg2 = FakeTelegram()
    run(llm=FakeLLM(), tg=tg2)
    assert tg2.messages == []  # marked sent: never repeats


def test_stretch_section_can_be_turned_off(home):
    config.PROFILE_PATH.write_text(
        config.PROFILE_PATH.read_text() + "\ndigest:\n  stretch_max: 0\n"
    )
    tg = FakeTelegram()
    r, _ = run(llm=FakeLLM([over_score(95, [])]), tg=tg)
    assert "Stretch" not in "".join(tg.messages) and r["sent"] == 0


def test_migration_backfills_uncapped_score_where_known(home):
    conn = db.connect()
    conn.execute(
        "INSERT INTO jobs (source, company, external_id, title, url, content_hash,"
        " first_seen_at, last_seen_at) VALUES ('gh','A','1','t','u','h','x','x')"
    )
    for score, seniority in [(80, "match"), (30, "over"), (40, "over")]:
        db.insert_score(conn, job_id=1, content_hash="h", profile_hash="p",
                        hard_filter_result="pass", status="scored", score=score,
                        seniority_match=seniority)  # fmt: skip
    with conn:  # back to schema version 2
        conn.execute("ALTER TABLE scores DROP COLUMN uncapped_score")
        conn.execute("DROP TABLE decisions")
        conn.execute("DELETE FROM schema_version WHERE version >= 3")
    conn.close()
    rows = db.connect().execute("SELECT score, uncapped_score FROM scores ORDER BY id")
    assert [tuple(x) for x in rows] == [(80, 80), (30, 30), (40, None)]  # the cap hid 40's


def scored(score, reason="Python services", gap="On-call rota"):
    return json.dumps(GOOD_SCORE | {"score": score, "reasons": [reason], "red_flags": [gap]})


def test_near_misses_fill_a_short_list_once(home, monkeypatch):
    llm = FakeLLM([scored(62), scored(50)])
    tg = FakeTelegram()
    r, conn = run(llm=llm, tg=tg)
    header, section, job = tg.messages
    assert header.startswith("0 new matches today")
    assert section == "<b>🔎 Near misses (1, scored 55–69)</b>"  # the 50 is out of range
    assert "⭐ 62" in job and "✅ Python services" in job and "🚩 On-call rota" in job
    assert "reply_markup" in tg.sent[2] and r["sent"] == 1
    tg2 = FakeTelegram()
    run(tg=tg2)
    assert tg2.messages == []  # marked sent: never repeated

    monkeypatch.setenv("JOBHUNT_NEAR_MISS", "45-55")
    from jobhunt import digest

    assert digest.near_miss_range() == (45, 55)
    monkeypatch.setenv("JOBHUNT_NEAR_MISS", "off")
    assert digest.near_miss_range() is None


def test_no_near_misses_when_the_main_list_has_two(home):
    from jobhunt import digest

    _, conn = run(llm=FakeLLM([scored(62), scored(50)]), tg=FakeTelegram(down=True))  # unsent
    assert len(digest.near_miss_jobs(conn, [], set())) == 1
    assert digest.near_miss_jobs(conn, ["a", "b"], set()) == []
