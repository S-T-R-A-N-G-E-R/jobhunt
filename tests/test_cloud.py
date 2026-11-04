import gzip
import stat

import pytest

from jobhunt import cloud, config, db


class FakeGh:
    """Records gh calls. `artifact` / `seed`: DB bytes the fake GitHub holds (None = none)."""

    def __init__(self, artifact=None, seed=None):
        self.calls: list[tuple[tuple, bytes | None]] = []
        self.artifact, self.seed = artifact, seed

    def __call__(self, *args, input=None):
        self.calls.append((args, input))
        if args[0] == "api":
            return "123\n" if self.artifact else ""
        if args[:2] == ("run", "download"):
            dest = args[args.index("-D") + 1]
            (cloud.Path(dest) / "jobhunt.db").write_bytes(self.artifact)
        if args[:2] == ("release", "download"):
            if self.seed is None:
                raise cloud.CloudError("release not found")
            dest = args[args.index("-D") + 1]
            (cloud.Path(dest) / cloud.SEED_ASSET).write_bytes(gzip.compress(self.seed))
        return ""

    def names(self):
        return [a[:2] for a, _ in self.calls]


def db_bytes(tmp_path, marker: str) -> bytes:
    """A valid jobhunt DB whose single run has run_date=marker."""
    path = tmp_path / f"{marker}.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO runs (run_date, started_at) VALUES (?, 'x')", (marker,))
    conn.commit()
    conn.close()
    return path.read_bytes()


def run_date(path) -> str:
    return db.connect(path).execute("SELECT run_date FROM runs").fetchone()[0]


@pytest.fixture
def gh(home, monkeypatch):
    monkeypatch.setenv("JOBHUNT_RUNNER_REPO", "me/runner")
    fake = FakeGh()
    monkeypatch.setattr(cloud, "gh", fake)
    return fake


def test_repo_must_be_configured(home, monkeypatch):
    monkeypatch.delenv("JOBHUNT_RUNNER_REPO", raising=False)
    with pytest.raises(cloud.CloudError, match="JOBHUNT_RUNNER_REPO"):
        cloud.push_secrets()


def test_push_secrets_sends_values_on_stdin_only(gh, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "ok-key")
    names = cloud.push_secrets()
    assert names == [
        "PROFILE_YAML", "RESUME_MD", "COMPANIES_YAML",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OLLAMA_API_KEY",
    ]  # fmt: skip
    sent = {args[2]: stdin for args, stdin in gh.calls}
    assert sent["PROFILE_YAML"] == config.PROFILE_PATH.read_bytes()
    assert sent["TELEGRAM_BOT_TOKEN"] == b"123:SECRET"
    for args, _ in gh.calls:
        assert args[:2] == ("secret", "set") and args[3:] == ("--repo", "me/runner")
        assert not any("SECRET" in a or "Backend" in a for a in args)  # never in argv


def test_push_secrets_requires_telegram(gh, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    with pytest.raises(cloud.CloudError, match="TELEGRAM_CHAT_ID"):
        cloud.push_secrets()


def test_write_files_from_secrets(home, monkeypatch):
    for p in (config.PROFILE_PATH, config.RESUME_PATH, config.COMPANIES_PATH):
        p.unlink()
    monkeypatch.setenv("PROFILE_YAML", "target_titles: [X]\n")
    monkeypatch.setenv("RESUME_MD", "resume text")
    monkeypatch.setenv("COMPANIES_YAML", "companies: []\n")
    assert cloud.write_files() == ["PROFILE_YAML", "RESUME_MD", "COMPANIES_YAML"]
    assert config.RESUME_PATH.read_text() == "resume text\n"
    assert config.load_profile().target_titles == ["X"]
    assert stat.S_IMODE(config.PROFILE_PATH.stat().st_mode) == 0o600


def test_write_files_keeps_profile_hash_despite_trimmed_secret(home, monkeypatch):
    laptop_hash = config.profile_hash()
    monkeypatch.setenv("PROFILE_YAML", config.PROFILE_PATH.read_text().rstrip("\n"))  # as GitHub
    monkeypatch.setenv("RESUME_MD", "r")
    monkeypatch.setenv("COMPANIES_YAML", "companies: []")
    cloud.write_files()
    assert config.profile_hash() == laptop_hash


def test_write_files_names_missing_secrets(home, monkeypatch, capsys):
    monkeypatch.setenv("PROFILE_YAML", "very private")
    with pytest.raises(cloud.CloudError, match="RESUME_MD, COMPANIES_YAML") as e:
        cloud.write_files()
    assert "very private" not in str(e.value)


def test_restore_prefers_seed_over_cache(gh, tmp_path):
    config.DB_PATH.write_bytes(db_bytes(tmp_path, "cache"))
    gh.seed, gh.artifact = db_bytes(tmp_path, "seed"), db_bytes(tmp_path, "artifact")
    assert cloud.restore_db(seed=True) == "seed"
    assert run_date(config.DB_PATH) == "seed"


def test_restore_uses_cache_before_artifact(gh, tmp_path):
    config.DB_PATH.write_bytes(db_bytes(tmp_path, "cache"))
    gh.artifact = db_bytes(tmp_path, "artifact")
    assert cloud.restore_db() == "cache"
    assert run_date(config.DB_PATH) == "cache"
    assert ("run", "download") not in gh.names()


def test_restore_falls_back_to_newest_artifact(gh, tmp_path):
    gh.artifact = db_bytes(tmp_path, "artifact")
    assert cloud.restore_db() == "artifact"
    assert run_date(config.DB_PATH) == "artifact"
    assert gh.calls[-1][0][:3] == ("run", "download", "123")


def test_restore_refuses_to_start_fresh_unless_allowed(gh):
    with pytest.raises(cloud.CloudError, match="seed-db"):
        cloud.restore_db()
    assert cloud.restore_db(allow_fresh=True) == "fresh"
    assert not config.DB_PATH.exists()


def test_restore_rejects_corrupt_db(gh):
    gh.artifact = b"not a database"
    with pytest.raises(cloud.CloudError, match="valid jobhunt DB"):
        cloud.restore_db()


def test_pull_db_backs_up_then_replaces(gh, tmp_path):
    config.DB_PATH.write_bytes(db_bytes(tmp_path, "local"))
    gh.artifact = db_bytes(tmp_path, "cloud")
    backup = cloud.pull_db()
    assert run_date(config.DB_PATH) == "cloud" and run_date(backup) == "local"
    assert backup.name.startswith("jobhunt.db-bak-")  # matches the data/*.db-* ignore rule


def test_pull_db_without_artifact(gh):
    with pytest.raises(cloud.CloudError, match="No jobhunt-db artifact"):
        cloud.pull_db()


def test_seed_db_uploads_snapshot_and_starts_forced_seeded_run(gh, tmp_path, monkeypatch):
    config.DB_PATH.write_bytes(db_bytes(tmp_path, "local"))
    uploaded = {}

    def record(*args, input=None):
        if args[:2] == ("release", "create"):
            uploaded["gz"] = cloud.Path(args[3]).read_bytes()
        return FakeGh.__call__(gh, *args, input=input)

    monkeypatch.setattr(cloud, "gh", record)
    cloud.seed_db()
    assert [n for n in gh.names()] == [
        ("release", "delete"), ("release", "create"), ("workflow", "run"),
    ]  # fmt: skip
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(uploaded["gz"]))
    assert run_date(restored) == "local"
    assert gh.calls[-1][0][-4:] == ("-f", "force=true", "-f", "seed=true")


def test_trigger(gh):
    cloud.trigger()
    assert gh.calls == [
        (("workflow", "run", "daily.yml", "--repo", "me/runner",
          "-f", "force=false", "-f", "seed=false"), None),
    ]  # fmt: skip
