import sqlite3

from jobhunt import db, decisions

from .conftest import FakeTelegram
from .test_pipeline import run


def press(update_id, data, chat=42, message_id=7):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cq{update_id}",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}},
        },
    }


def conn():
    c = sqlite3.connect(":memory:")
    c.execute(decisions.SCHEMA)
    return c


def stored(c):
    return {r[0]: r[1] for r in c.execute("SELECT job_id, action FROM decisions")}


def test_presses_are_stored_answered_marked_and_confirmed(home):
    c = conn()
    tg = FakeTelegram(updates=[press(10, "a:5"), press(11, "s:6"), {"update_id": 12}])
    assert decisions.poll(c, tg.transport()) == 2
    assert stored(c) == {5: "approve", 6: "skip"}
    methods = [m for m, _ in tg.calls]
    assert methods.count("answerCallbackQuery") == 2  # stops the spinner
    edits = [p for m, p in tg.calls if m == "editMessageReplyMarkup"]
    assert edits[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Apply ✓"
    assert edits[1]["reply_markup"]["inline_keyboard"][0][1]["text"] == "⏭ Skip ✓"
    assert tg.calls[-1] == ("getUpdates", {"offset": 13, "timeout": 0, "limit": 1})
    assert tg.updates == []  # confirmed: never read again
    assert decisions.poll(c, tg.transport()) == 0


def test_latest_press_wins_and_replays_change_nothing(home):
    c = conn()
    decisions.poll(c, FakeTelegram(updates=[press(10, "a:5"), press(11, "s:5")]).transport())
    assert stored(c) == {5: "skip"}
    # The same (older) press seen again, e.g. the cloud's copy merged on the laptop.
    assert not decisions.record(c, 5, "approve", 10, "t")
    assert not decisions.record(c, 5, "approve", 11, "t")
    assert stored(c) == {5: "skip"}
    decisions.record_terminal(c, 5, "approve")  # changed my mind in `apply pick`
    assert stored(c) == {5: "approve"}
    assert not decisions.record(c, 5, "skip", 11, "t")  # the old press doesn't undo it
    decisions.record(c, 5, "skip", 12, "t")  # a newer press does
    assert stored(c) == {5: "skip"}


def test_other_chats_and_junk_are_ignored(home):
    c = conn()
    updates = [press(1, "a:5", chat=999), press(2, "x:5"), press(3, "a:abc"), press(4, "a:")]
    tg = FakeTelegram(updates=updates)
    assert decisions.poll(c, tg.transport()) == 0
    assert stored(c) == {}
    assert tg.updates == []  # still confirmed, so junk isn't re-read forever


def test_every_run_reads_presses_even_when_it_skips(home):
    run()
    tg = FakeTelegram(updates=[press(20, "a:1")])
    r, _ = run(tg=tg, if_not_run_today=True)
    assert r is None  # nothing else to do today...
    assert stored(db.connect()) == {1: "approve"}  # ...but the press was saved


def test_telegram_down_does_not_break_the_run(home):
    r, _ = run(tg=FakeTelegram(down=True))
    assert r["status"] == "partial"  # only because the digest couldn't be sent
