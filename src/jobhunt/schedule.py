"""macOS launchd agent. Local mode: daily 08:00 plus hourly catch-up (guarded by
--if-not-run-today). Cloud mode: start the cloud workflow at 09:30 and 14:00, a backup for
GitHub's delayed or dropped crons (the workflow's own guard makes it a no-op once done)."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from . import config

LABEL = "local.jobhunt.daily"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = AGENTS_DIR / f"{LABEL}.plist"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def domain() -> str:
    return f"gui/{os.getuid()}"


CLOUD_TIMES = [(9, 30), (14, 0)]


def plist(cloud: bool = False) -> dict:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    root = str(config.ROOT)
    base = [uv, "run", "--directory", root, "jobhunt"]
    # A missed time (laptop asleep) runs once on wake.
    when = (
        {"StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in CLOUD_TIMES]}
        if cloud
        else {
            "StartCalendarInterval": {"Hour": 8, "Minute": 0},
            "StartInterval": 3600,
            "RunAtLoad": True,
        }
    )
    return {
        "Label": LABEL,
        "ProgramArguments": base
        + (["cloud", "run"] if cloud else ["run", "--if-not-run-today", "--quiet"]),
        "WorkingDirectory": root,
        "EnvironmentVariables": {"PATH": PATH},
        **when,
        "StandardOutPath": str(config.LOG_DIR / "launchd.out.log"),
        "StandardErrorPath": str(config.LOG_DIR / "launchd.err.log"),
    }


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def legacy_agents() -> list[Path]:
    """jobhunt agents installed under an older label (they run `jobhunt run`)."""
    found = []
    for path in AGENTS_DIR.glob("*jobhunt*.plist"):
        try:
            data = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        args = data.get("ProgramArguments", [])
        if data.get("Label") != LABEL and "jobhunt" in args and "run" in args:
            found.append(path)
    return found


def remove_legacy() -> list[str]:
    """Unload and delete agents installed under an older label. Returns their labels."""
    removed = []
    for path in legacy_agents():
        label = plistlib.loads(path.read_bytes())["Label"]
        launchctl("bootout", f"{domain()}/{label}")  # ignore "not loaded"
        path.unlink()
        removed.append(label)
    return removed


def install(cloud: bool = False) -> str:
    removed = remove_legacy()
    config.LOG_DIR.mkdir(exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(plistlib.dumps(plist(cloud)))
    launchctl("bootout", f"{domain()}/{LABEL}")  # ignore "not loaded"
    r = launchctl("bootstrap", domain(), str(PLIST_PATH))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    note = f" (removed old agent: {', '.join(removed)})" if removed else ""
    return f"Installed and loaded {PLIST_PATH}{note}"


def uninstall() -> str:
    removed = remove_legacy()
    launchctl("bootout", f"{domain()}/{LABEL}")
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        return f"Unloaded and removed {PLIST_PATH}"
    return f"Removed old agent: {', '.join(removed)}" if removed else "Not installed"


def status() -> str:
    legacy = [p.stem for p in legacy_agents()]
    warning = (
        f"\nOld agent still installed: {', '.join(legacy)} (run `jobhunt schedule install`)"
        if legacy
        else ""
    )
    return _status() + warning


def _status() -> str:
    r = launchctl("print", f"{domain()}/{LABEL}")
    if r.returncode != 0:
        return "Not loaded" + (" (plist exists but isn't loaded)" if PLIST_PATH.exists() else "")
    keep = ("state =", "last exit code", "runs =", "path =")
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith(keep)]
    return "Loaded\n" + "\n".join(dict.fromkeys(lines))
