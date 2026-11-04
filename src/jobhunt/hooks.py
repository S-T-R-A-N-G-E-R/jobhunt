"""Pre-commit guard: block commits whose added lines contain values from your personal files.

The values are read at commit time from the git-ignored files and never written anywhere. Hits
are reported by label and line, never by value. Bypass once with `git commit --no-verify`.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values

from . import config

HOOK = """#!/bin/sh
# Installed by `jobhunt dev install-hooks`: blocks commits that contain personal values.
exec uv run --quiet --directory "$(git rev-parse --show-toplevel)" python -m jobhunt.hooks
"""

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\+?\d[\d ()-]{8,}\d")
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OLLAMA_API_KEY", "ANTHROPIC_API_KEY")


def _yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _names(path: Path) -> set[str]:
    return {str(c.get("name", "")) for c in _yaml(path).get("companies") or [] if c}


def _number(label: str, x) -> list[tuple[str, str, str]]:
    """Plain integers under 100 are too common to block; decimals and big numbers aren't."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not x:
        return []
    if float(x).is_integer():
        n = int(x)
        return [] if n < 100 else [(label, "num", str(n)), (label, "num", f"{n:,}")]
    return [(label, "num", f"{x:g}")]


def values(root: Path | None = None) -> list[tuple[str, str, str]]:
    """(label, kind, value) for every sensitive value. kind: text | num | digits | company."""
    root = root or config.ROOT
    out: list[tuple[str, str, str]] = []
    a = _yaml(root / "config" / "answers.yaml")
    ident, work = a.get("identity") or {}, a.get("work") or {}
    for key in ("full_name", "preferred_name", "email", "linkedin"):
        out.append((f"identity.{key}", "text", str(ident.get(key) or "")))
    for part in str(ident.get("full_name") or "").split():
        out.append(("identity.full_name", "text", part))
    out.append(("identity.phone", "digits", str(ident.get("phone") or "")))
    out.append(("work.current_company", "text", str(work.get("current_company") or "")))
    for key in ("current_ctc_lpa", "expected_ctc_lpa", "expected_usd"):
        out += _number(f"work.{key}", work.get(key))
    for key, x in (_yaml(root / "config" / "profile.yaml").get("compensation") or {}).items():
        out += _number(f"profile.compensation.{key}", x)
    env = dotenv_values(root / ".env")
    for key in SECRET_ENV:
        out.append((f".env {key}", "text", env.get(key) or ""))
    repo = env.get("JOBHUNT_RUNNER_REPO") or ""
    out += [(".env JOBHUNT_RUNNER_REPO", "text", v) for v in (repo, repo.rpartition("/")[2])]
    example = _names(root / "config" / "companies.example.yaml")
    for name in _names(root / "config" / "companies.yaml") - example:
        out.append(("companies.yaml name", "company", name))
    try:
        resume = (root / "data" / "resume.md").read_text()
    except OSError:
        resume = ""
    out += [("resume.md email", "text", e) for e in EMAIL.findall(resume)]
    out += [("resume.md phone", "digits", p) for p in PHONE.findall(resume)]
    return [v for v in out if _worth_checking(*v)]


def _worth_checking(label: str, kind: str, value: str) -> bool:
    if kind == "digits":
        return len(re.sub(r"\D", "", value)) >= 8
    return len(value.strip()) >= 4


def _base() -> str:
    """HEAD, or on a branch with no commits yet (orphan), the newest commit on any branch."""
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    return git("rev-parse", "-q", "--verify", "HEAD") or git(
        "for-each-ref", "--count=1", "--sort=-committerdate", "--format=%(objectname)", "refs/heads"
    )


def _in_tree(word: str, base: str | None = None) -> bool:
    """Already in the committed tree, so not a new leak (e.g. a company that's also a class
    name). ponytail: a value that slipped in with --no-verify is then allowed too."""
    base = base or _base()
    if not base:
        return False
    r = subprocess.run(["git", "grep", "-q", "-w", "-i", "-F", word, base, "--"],
                       capture_output=True)  # fmt: skip
    return r.returncode == 0


def find(diff: str, vals: list[tuple[str, str, str]], in_tree=_in_tree) -> list[str]:
    """Hits in the added lines of a unified diff, as "file:line label" (never the value)."""
    hits, path, line_no = [], "", 0
    allowed: dict[str, bool] = {}
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif m := re.match(r"@@ -\S+ \+(\d+)", line):
            line_no = int(m.group(1)) - 1
        elif line.startswith("+"):
            line_no += 1
            text, digits = line[1:], re.sub(r"\D", "", line)
            for label, kind, value in vals:
                if kind == "digits":
                    hit = re.sub(r"\D", "", value)[-10:] in digits
                elif kind == "num":
                    hit = re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d,]|\.\d)", text)
                else:
                    hit = re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.I)
                    if hit and kind == "company":
                        hit = not allowed.setdefault(value, in_tree(value))
                if hit and f"{path}:{line_no} {label}" not in hits:
                    hits.append(f"{path}:{line_no} {label}")
        elif not line.startswith("-"):
            line_no += 1
    return hits


def install(root: Path | None = None) -> Path:
    root = root or config.ROOT
    git_dir = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    hook = (root / git_dir / "pre-commit").resolve()
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK)
    hook.chmod(0o755)
    return hook


def main() -> int:
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff"],
                          capture_output=True, text=True, check=True).stdout  # fmt: skip
    hits = find(diff, values())
    if not hits:
        return 0
    print("Commit blocked: staged changes contain personal values:", file=sys.stderr)
    for h in hits:
        print(f"  {h}", file=sys.stderr)
    print("Remove them (use placeholders), or bypass once with --no-verify.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
