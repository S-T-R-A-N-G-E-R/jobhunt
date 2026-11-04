import plistlib

from jobhunt import schedule


def test_legacy_agents_detected_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "AGENTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(schedule, "launchctl", lambda *a: calls.append(a))
    old = tmp_path / "com.someone.jobhunt.plist"
    old.write_bytes(
        plistlib.dumps(
            {"Label": "com.someone.jobhunt", "ProgramArguments": ["uv", "run", "jobhunt", "run"]}
        )
    )
    current = tmp_path / f"{schedule.LABEL}.plist"
    current.write_bytes(plistlib.dumps(schedule.plist()))
    unrelated = tmp_path / "com.other.jobhunter-notes.plist"
    unrelated.write_bytes(plistlib.dumps({"Label": "x", "ProgramArguments": ["notes"]}))

    assert schedule.legacy_agents() == [old]
    assert schedule.remove_legacy() == ["com.someone.jobhunt"]
    assert not old.exists() and current.exists() and unrelated.exists()
    assert calls[0][0] == "bootout" and calls[0][1].endswith("/com.someone.jobhunt")


def test_label_is_generic():
    assert schedule.LABEL == "local.jobhunt.daily"
    assert schedule.plist()["Label"] == schedule.LABEL
