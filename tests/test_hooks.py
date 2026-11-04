import subprocess

from jobhunt import hooks

ANSWERS = """identity:
  full_name: Alex Quillfeather
  email: alex@example.org
  phone: "+91 90000 11111"
work:
  current_company: Fictional Widgets
  current_ctc_lpa: 12.5
  expected_ctc_lpa: 20
  expected_usd: 60000
"""


def diff(*lines: str) -> str:
    return "\n".join(["+++ b/docs/x.md", "@@ -0,0 +1,9 @@", *(f"+{x}" for x in lines)])


def vals(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "answers.yaml").write_text(ANSWERS)
    (tmp_path / "config" / "companies.yaml").write_text(
        "companies:\n  - {name: Orbital Labs}\n  - {name: Acme}\n  - {name: Kestrel}\n"
    )
    (tmp_path / "config" / "companies.example.yaml").write_text("companies:\n  - {name: Acme}\n")
    env = "TELEGRAM_CHAT_ID=987654321\nJOBHUNT_RUNNER_REPO=me/secret-runner\n"
    (tmp_path / ".env").write_text(env)
    (tmp_path / "data" / "resume.md").write_text("Contact: alex.alt@example.net")
    return hooks.values(tmp_path)


def test_blocks_personal_values_and_names_them_by_label(tmp_path):
    v = vals(tmp_path)
    hits = hooks.find(diff(
        "Current CTC: 12.5 LPA", "call +91-90000-11111", "Alex Quillfeather applied",
        "chat 987654321", "repo secret-runner", "at Fictional Widgets", "USD 60,000",
        "works at Orbital Labs", "alex.alt@example.net",
    ), v, in_tree=lambda w: False)  # fmt: skip
    assert [h.split(" ", 1)[1] for h in hits] == [
        "work.current_ctc_lpa", "identity.phone", "identity.full_name", ".env TELEGRAM_CHAT_ID",
        ".env JOBHUNT_RUNNER_REPO", "work.current_company", "work.expected_usd",
        "companies.yaml name", "identity.full_name", "resume.md email",
    ]  # fmt: skip
    assert hits[0].startswith("docs/x.md:1 ")
    assert not any("12.5" in h or "Alex" in h for h in hits)


def test_ignores_short_generic_and_example_values(tmp_path):
    v = vals(tmp_path)
    ok = diff("expected 20 LPA", "112.5 and 12.55", "Acme is in the example",
              "Kestrel is already in the tree", "removed line stays")  # fmt: skip
    assert hooks.find(ok, v, in_tree=lambda w: w == "Kestrel") == []
    assert hooks.find("-Current CTC 12.5\n", v) == []  # removed lines are fine


def test_install_writes_an_executable_hook(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    hook = hooks.install(tmp_path)
    assert hook == (tmp_path / ".git" / "hooks" / "pre-commit").resolve()
    assert hook.stat().st_mode & 0o111 and "jobhunt.hooks" in hook.read_text()


def test_in_tree_works_on_an_orphan_branch(tmp_path, monkeypatch):
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                    cwd=tmp_path, check=True, capture_output=True)  # fmt: skip
    git("init", "-q")
    (tmp_path / "llm.py").write_text("class Writer: ...\n")
    git("add", ".")
    git("commit", "-qm", "init")
    git("checkout", "-q", "--orphan", "fresh")
    monkeypatch.chdir(tmp_path)
    assert hooks._in_tree("Writer") and not hooks._in_tree("Kestrel")
