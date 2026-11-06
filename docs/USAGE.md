# Usage

Prefix every command with `uv run` (for example `uv run jobhunt top`), and run it from the project folder.

## The daily loop

1. The daily run (in the cloud, [CLOUD.md](CLOUD.md), or your local schedule) fetches, filters, scores and sends the digest each morning.
2. Read the Telegram digest. Each job shows company, title, location, score, a one-line pitch, the top reason, a red flag if any, pay if stated, and a link.
3. Press ✅ Apply or ⏭ Skip under each job (or decide later with `jobhunt apply pick`).
4. Run `jobhunt apply` on your laptop: it fills each approved job's form, and you review and click Submit. jobhunt never submits for you. See [APPLY.md](APPLY.md).

Jobs are never sent twice. On a day with no new matches you still get one short status line, so you know it ran.

If the cloud runs your daily job, don't run `jobhunt run` on the laptop: its DB doesn't know what the cloud sent. Use `jobhunt cloud run`, and `jobhunt cloud pull-db` before `top` or `status`.

## Commands

| Command | What it does |
|:--|:--|
| `jobhunt run` | fetch, then score, then digest. Add `--preview` to print the digest instead of sending it, `--quiet` to hide the progress bar, and `--if-not-run-today` to do nothing if today already has a successful run (what the scheduler uses). |
| `jobhunt fetch` | Fetch every board in `companies.yaml` and store the jobs. |
| `jobhunt score` | Hard-filter and LLM-score new or changed jobs, up to the per-run cap. |
| `jobhunt score --dry-run` | Show which jobs would be scored and the estimated number of LLM calls. Calls no model. |
| `jobhunt score --rescore` | Also rescore jobs scored under an older profile or prompt. Stale ones go first. |
| `jobhunt digest` | Send unsent matches above the threshold. `--preview` prints instead. |
| `jobhunt top --n 20` | Best current matches as a table, including ones already sent. |
| `jobhunt status` | Last run summary, unscored backlog, stale scores. |
| `jobhunt company add <url>` | Detect the ATS and slug from a careers URL, verify the board, and add it. `--priority high` and `--name` are optional. |
| `jobhunt resume import <pdf>` | Extract your resume PDF into `data/resume.md`. |
| `jobhunt llm test` | Check the local model (and the paid one, if configured) with a tiny prompt. |
| `jobhunt calibrate --n 15` | Score the same jobs with the local and paid models side by side. Needs `ANTHROPIC_API_KEY`. |
| `jobhunt schedule install` / `uninstall` / `status` | Manage the macOS launchd agent. `install --cloud` instead starts the cloud workflow at 09:30 and 14:00, a backup for GitHub's dropped crons. |
| `jobhunt cloud push-secrets` / `seed-db` / `pull-db` / `run` | Run the daily job on GitHub Actions instead of your machine. See [CLOUD.md](CLOUD.md). |
| `jobhunt apply` (`--limit`, `--job <id>`, `--no-pull`) | Assisted applying on your laptop: fills each approved job's form; you press Submit. See [APPLY.md](APPLY.md). |
| `jobhunt apply pick` / `queue` / `status` / `prepare <id> [--letter]` | Decide in the terminal, list approved jobs, see applications, or draft without filling. |
| `jobhunt dev install-hooks` | Install the pre-commit hook that blocks commits containing your personal values ([SETUP.md](SETUP.md#guard-your-commits-if-youll-commit-to-the-repo)). |
| `jobhunt dev save-fixture <ats> <slug> [--keep 3]` | Save a trimmed real API response to `output/fixtures/` (git-ignored), to inspect a board's structure. |

## Tuning

### After you change `profile.yaml`
- **Hard filters** (title and location) re-run automatically on the next `score` or `run`. They're cheap.
- **LLM scores** are not redone automatically, because that could mean hundreds of slow local calls. `jobhunt status` shows how many are stale. Run `jobhunt score --rescore` to redo them. It still respects the per-run cap.

### Tweaking the scoring prompt
Edit `src/jobhunt/prompts/score_job.md`. It is filled with your profile, your resume and the job (description truncated to about 6,000 characters).
- Keep it short and explicit. A 4B model follows a few clear rules better than many subtle ones.
- Changing the prompt marks existing scores as stale, the same as a profile change, so run `jobhunt score --rescore` afterwards.
- Some rules are enforced in code rather than the prompt: the seniority cap, verdict-from-score and the priority boost. See [DECISIONS.md](DECISIONS.md#code-side-guardrails-on-top-of-the-llm).

### Useful settings in `profile.yaml`

| Setting | Default | Effect |
|:--|:--|:--|
| `scoring.min_score_for_digest` | 70 | Lowest score that gets sent |
| `scoring.max_llm_calls_per_run` | 60 | Jobs scored per run; the rest wait as backlog |
| `digest.max_jobs` | 10 | Jobs per digest |
| `digest.stretch_max` | 2 | Stretch roles after the main matches (see below); 0 turns the section off |
| `llm.local.model` | `qwen3.5:4b` | Any model you've pulled in Ollama |
| `llm.local.num_ctx` | 16384 | Context window; it must fit the profile, resume and job |

Applying has its own settings (`settings.max_per_session`, `settings.writing`) in `answers.yaml`: see [APPLY.md](APPLY.md#the-writing-model).

### Stretch roles

A role the model marks as needing more seniority than you have is capped at 40, so it never reaches the main list. If such a role would have scored at least `min_score_for_digest` without the cap, it can appear in a **Stretch roles** section after the main matches. The section shows up to `digest.stretch_max` of them, best first, each with its score before the cap and the main reason it's above your level. Like any digest job, a stretch role is sent only once.

Scores from before this feature only kept the capped value, so older capped jobs don't appear as stretch roles; newly scored ones do.

## The paid fallback (optional)

If the local model returns invalid output for a job twice, jobhunt can send that one job to Claude instead. It's off by default, and everything works without it.

1. Add `ANTHROPIC_API_KEY` to `.env`.
2. Set `llm.fallback.enabled: true` in `profile.yaml`.
3. Check it with `jobhunt llm test`.

**Limits:**
- Paid calls are capped per run (`llm.fallback.max_calls_per_run`, default 10), logged, and counted in the digest.
- When Ollama is down, jobhunt never falls back to the paid model. It skips scoring and tells you how to fix Ollama.
- With a key in place, `jobhunt calibrate --n 15` shows how closely the local scores agree with the paid model's, so you can decide whether the local model is good enough.
