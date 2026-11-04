"""Run jobhunt on GitHub Actions from a private runner repo (see docs/CLOUD.md).

Laptop side: push secrets, seed / pull the DB, trigger a run. Runner side: write the personal
files from secrets and restore the DB. All GitHub access goes through the `gh` CLI. Secret
values are only ever passed on stdin, never as arguments, and never printed.
"""

import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import config
from .config import env

WORKFLOW = "daily.yml"
ARTIFACT = "jobhunt-db"
SEED_TAG = "seed"
SEED_ASSET = "jobhunt.db.gz"
KEEP_BACKUPS = 3

# secret name -> attribute of `config` holding the file's path
FILE_SECRETS = {
    "PROFILE_YAML": "PROFILE_PATH",
    "RESUME_MD": "RESUME_PATH",
    "COMPANIES_YAML": "COMPANIES_PATH",
}
ENV_SECRETS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
OPTIONAL_SECRETS = ["OLLAMA_API_KEY", "ANTHROPIC_API_KEY"]


class CloudError(Exception):
    pass


def gh(*args: str, input: bytes | None = None) -> str:
    r = subprocess.run(["gh", *args], input=input, capture_output=True)
    if r.returncode:
        # Only the subcommand goes in the message: arguments never hold secrets, but keep it short.
        raise CloudError(f"`gh {' '.join(args[:2])}` failed: {r.stderr.decode()[:300].strip()}")
    return r.stdout.decode()


def repo() -> str:
    name = env("JOBHUNT_RUNNER_REPO")
    if not name:
        raise CloudError("Set JOBHUNT_RUNNER_REPO=<owner>/<private-repo> in .env")
    return name


# ---- laptop side ------------------------------------------------------------------------------


def push_secrets() -> list[str]:
    """Upload the personal files and Telegram values as Actions secrets. Returns names set."""
    r, done = repo(), []
    for name, attr in FILE_SECRETS.items():
        path: Path = getattr(config, attr)
        if not path.exists():
            raise CloudError(f"Missing {path}")
        gh("secret", "set", name, "--repo", r, input=path.read_bytes())
        done.append(name)
    for name in ENV_SECRETS + OPTIONAL_SECRETS:
        value = env(name)
        if not value:
            if name in ENV_SECRETS:
                raise CloudError(f"{name} not set in .env")
            continue
        gh("secret", "set", name, "--repo", r, input=value.encode())
        done.append(name)
    return done


def trigger(force: bool = False, seed: bool = False) -> None:
    gh(
        "workflow", "run", WORKFLOW, "--repo", repo(),
        "-f", f"force={str(force).lower()}", "-f", f"seed={str(seed).lower()}",
    )  # fmt: skip


def seed_db() -> int:
    """Upload a snapshot of the local DB as the cloud's starting state, then start a run that
    uses it. Returns the compressed size in bytes."""
    if not config.DB_PATH.exists():
        raise CloudError(f"No local DB at {config.DB_PATH}")
    r = repo()
    with tempfile.TemporaryDirectory() as tmp:
        snap, gz = Path(tmp) / "jobhunt.db", Path(tmp) / SEED_ASSET
        src = sqlite3.connect(config.DB_PATH)
        src.execute("VACUUM INTO ?", (str(snap),))  # consistent, compact copy
        src.close()
        with snap.open("rb") as f, gzip.open(gz, "wb") as out:
            shutil.copyfileobj(f, out)
        try:
            gh("release", "delete", SEED_TAG, "--repo", r, "--yes", "--cleanup-tag")
        except CloudError:
            pass  # no earlier seed
        gh(
            "release", "create", SEED_TAG, str(gz), "--repo", r, "--title", "DB seed",
            "--notes", "Temporary: the next run restores it and deletes this release.",
        )  # fmt: skip
        size = gz.stat().st_size
    # force: the seeded DB may already have a successful run for today.
    trigger(force=True, seed=True)
    return size


def pull_db() -> Path | None:
    """Replace data/jobhunt.db with the newest cloud DB. Returns the backup path (if any)."""
    r = repo()
    with tempfile.TemporaryDirectory() as tmp:
        new = download_artifact(r, Path(tmp))
        if new is None:
            raise CloudError(f"No {ARTIFACT} artifact in {r} yet: has a cloud run succeeded?")
        check_db(new)
        backup = None
        if config.DB_PATH.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = config.DB_PATH.with_name(f"{config.DB_PATH.name}-bak-{stamp}")
            shutil.copy2(config.DB_PATH, backup)
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(new, config.DB_PATH)
    # `jobhunt apply` pulls every time: keep only the newest few backups.
    for old in sorted(config.DB_PATH.parent.glob(f"{config.DB_PATH.name}-bak-*"))[:-KEEP_BACKUPS]:
        old.unlink()
    return backup


# ---- runner side ------------------------------------------------------------------------------


def write_files() -> list[str]:
    """Write the personal files from env vars (Actions secrets), readable only by this user."""
    missing = [n for n in [*FILE_SECRETS, *ENV_SECRETS] if not os.environ.get(n, "").strip()]
    if missing:
        raise CloudError(f"Missing secrets in the runner repo: {', '.join(missing)}")
    for name, attr in FILE_SECRETS.items():
        path: Path = getattr(config, attr)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            # GitHub trims a secret's final newline; restore it so profile_hash() matches the
            # laptop's (otherwise every score looks stale in the cloud).
            f.write(os.environ[name].rstrip("\n") + "\n")
    return list(FILE_SECRETS)


def restore_db(seed: bool = False, allow_fresh: bool = False) -> str:
    """Make data/jobhunt.db the latest state. Order: seed release (only when asked) > the DB
    actions/cache already restored > newest artifact > fresh (only when allowed).
    Returns which one was used."""
    db_path = config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    r = repo()
    with tempfile.TemporaryDirectory() as tmp:
        if seed:
            gh("release", "download", SEED_TAG, "--repo", r, "-p", SEED_ASSET, "-D", tmp)
            snap = Path(tmp) / "jobhunt.db"
            with gzip.open(Path(tmp) / SEED_ASSET, "rb") as f, snap.open("wb") as out:
                shutil.copyfileobj(f, out)
            check_db(snap)
            shutil.move(snap, db_path)
            return "seed"
        if db_path.exists():
            check_db(db_path)
            return "cache"
        new = download_artifact(r, Path(tmp))
        if new is not None:
            check_db(new)
            shutil.move(new, db_path)
            return "artifact"
    if allow_fresh:
        return "fresh"
    raise CloudError(
        "No saved DB (no cache, no artifact). Run `jobhunt cloud seed-db` from your laptop, or "
        "start the workflow with allow_fresh=true to start empty (its first digest then sends "
        "the best of all current jobs)."
    )


# ---- shared -----------------------------------------------------------------------------------


def newest_artifact_run(r: str) -> str | None:
    """The workflow run id of the newest unexpired DB artifact, or None."""
    out = gh(
        "api", f"repos/{r}/actions/artifacts?name={ARTIFACT}&per_page=100", "--jq",
        "[.artifacts[] | select(.expired | not)] | sort_by(.created_at) | last"
        " | .workflow_run.id // empty",
    )  # fmt: skip
    return out.strip() or None


def download_artifact(r: str, dest: Path) -> Path | None:
    run_id = newest_artifact_run(r)
    if run_id is None:
        return None
    gh("run", "download", run_id, "--repo", r, "-n", ARTIFACT, "-D", str(dest))
    return dest / "jobhunt.db"


def check_db(path: Path) -> None:
    try:
        conn = sqlite3.connect(path)
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.execute("SELECT count(*) FROM runs").fetchone()
        conn.close()
    except sqlite3.DatabaseError as e:
        raise CloudError(f"{path.name} isn't a valid jobhunt DB: {e}") from None
    if not ok:
        raise CloudError(f"{path.name} failed the SQLite integrity check")
