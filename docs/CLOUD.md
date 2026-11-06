# Running in the cloud (GitHub Actions)

The recommended way to run discovery every day: a GitHub Actions workflow in a private repo of yours fetches, scores and sends the digest every morning (07:47 IST, with catch-up slots until 14:43 IST), whether or not your laptop is on. The laptop is then for applying ([APPLY.md](APPLY.md)) and manual commands. Running it locally instead is covered in [SETUP.md](SETUP.md#8-run-it-every-day).

## How it fits together

```
your laptop                         private runner repo (GitHub Actions)
───────────                         ────────────────────────────────────
config/profile.yaml ─┐               secrets ──► written to disk at the start of each run
data/resume.md       ├─ push-secrets ┘
config/companies.yaml┘                checks out the public jobhunt code at main
.env (Telegram, keys)                 DB: actions/cache (main) + artifact (backup)
data/jobhunt.db ──── seed-db ──────►  daily cron ──► jobhunt run ──► Telegram
data/jobhunt.db ◄─── pull-db ───────  newest DB artifact
```

**Why a separate, private repo:** in a public repo, Actions logs, caches and artifacts are visible to anyone. They would show which companies and roles you track, and the DB artifact would contain your scores. The runner repo holds only the workflow and a README. The code stays in the public repo, and each run checks it out.

## Setup

1. **Create the runner repo** (private!) and add the workflow:
   ```bash
   gh repo create <you>/<private-runner-repo> --private
   git clone https://github.com/<you>/<private-runner-repo>.git && cd <private-runner-repo>
   mkdir -p .github/workflows
   cp ../jobhunt/deploy/github-actions/daily.yml .github/workflows/
   # edit `repository:` in the checkout step if you use a fork
   git add -A && git commit -m "feat: add daily workflow" && git push
   ```
2. **Point jobhunt at it.** In `.env` on the laptop:
   ```
   JOBHUNT_RUNNER_REPO=<you>/<private-runner-repo>
   OLLAMA_API_KEY=...          # only for Ollama Cloud (the default), see below
   ```
3. **Upload your files and keys** as Actions secrets:
   ```bash
   uv run jobhunt cloud push-secrets
   ```
   - **What it uploads:** `PROFILE_YAML`, `RESUME_MD` and `COMPANIES_YAML` from the files. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `.env`, plus `OLLAMA_API_KEY` and `ANTHROPIC_API_KEY` if they're set.
   - **How:** values go to `gh secret set` on stdin, so they never appear in arguments or output.
4. **Give the cloud your current state, so the first run doesn't flood Telegram:**
   ```bash
   uv run jobhunt cloud seed-db
   ```
   1. Your DB is snapshotted, gzipped and uploaded as an asset of a temporary `seed` release in the runner repo. A workflow input can't carry a file this size, and release assets don't count against artifact storage.
   2. A run starts with `seed=true` and `force=true`. It restores that snapshot, then deletes the release once it has saved the DB.
   3. Jobs you were already sent stay sent.
5. **Stop the laptop schedule** (if you installed one) once two cloud runs in a row have succeeded, so you don't get two digests from two databases:
   ```bash
   uv run jobhunt schedule uninstall
   ```

## Day to day

| Task | Command |
|:--|:--|
| Changed profile, resume, companies or a key | edit the file, then `uv run jobhunt cloud push-secrets` |
| Run now (also from your phone: Actions tab → daily → Run workflow) | `uv run jobhunt cloud run` (`--force` to rerun today) |
| See the cloud's data on the laptop (`top`, `status`) | `uv run jobhunt cloud pull-db` (backs up the local DB as `data/jobhunt.db-bak-<time>` first, keeping the newest 3). `jobhunt apply` does this for you. |
| Replace the cloud DB with the laptop's | `uv run jobhunt cloud seed-db` |

**Don't run `jobhunt run` on the laptop while the cloud is the source of truth.** Its DB doesn't know what the cloud sent, so you'd get duplicates. Use `cloud run` instead.

## Where the LLM runs

Set the repository variable `JOBHUNT_LLM` (Settings → Secrets and variables → Actions → Variables) to choose:

| | `cloud` (default): Ollama Cloud | `runner`: Ollama on the runner |
|:--|:--|:--|
| **Model** | `gpt-oss:20b` (override with `JOBHUNT_CLOUD_MODEL`) | `llm.local.model`, e.g. `qwen3.5:4b` (set `JOBHUNT_RUNNER_MODEL` to the same) |
| **Speed** | about 2 s per job | CPU only: much slower than a laptop with a GPU, plus about 4 GB of model download (cached) |
| **Privacy** | Your resume, profile and the job text go to Ollama's servers. Ollama says it doesn't train on them. | No job or resume data goes to any LLM provider. |
| **Cost** | Ollama Cloud account usage credits. When they run out, the affected jobs stay unscored, the digest says so, and they're scored on a later run. They're never sent to the paid fallback automatically. | Only Actions minutes |
| **Status** | Tested | Written but **not yet tested**: try `cloud run` and check the timing before relying on it |

- **Thinking:** `gpt-oss` can't turn thinking off, so the cloud run sets `JOBHUNT_LLM_THINK=low`.
- **Same profile on both:** the model and think level are env overrides, so your laptop keeps its local model with the same `profile.yaml`.
- **Paid fallback:** the Anthropic fallback works exactly as it does locally. It's optional, capped, and off unless enabled in `profile.yaml`.
- **Scores shift between models.** After switching, compare a few jobs and check that `scoring.min_score_for_digest` still makes sense.

## The database between runs

1. **Main state:** `actions/cache`, with a new key per run and a `jobhunt-db-` restore prefix.
2. **Backup:** each run uploads the DB as the `jobhunt-db` artifact, with 7-day retention. Only the newest 3 are kept.
3. **Restore order:** seed (only when asked) → cache → newest artifact.
4. **No state at all:** the run stops and asks you to `seed-db`, rather than starting empty and sending a flood. Start the workflow with `allow_fresh=true` if you really want to start empty.

Caches unused for 7 days are evicted, and so are artifacts after 7 days. If the workflow doesn't run for a week, `seed-db` again from a `pull-db`'d copy.

**Size:** the DB is mostly job descriptions. Descriptions of jobs inactive for 30+ days are dropped (scores are kept), and the DB is vacuumed before saving. A DB of about 50 MB compresses to about 9 MB, so three artifacts use about 30 MB of the 500 MB free artifact storage.

## The schedule, and GitHub's delays

GitHub starts scheduled workflows late at busy times (hours late has been seen) and sometimes skips them entirely. So the workflow has five slots, all at odd minutes:

| UTC | IST | Role |
|:--|:--|:--|
| 02:17 | 07:47 | the intended run |
| 03:23 | 08:53 | catch-up |
| 04:41 | 10:11 | catch-up |
| 06:47 | 12:17 | catch-up |
| 09:13 | 14:43 | catch-up |

- **One real run a day:** every slot runs `jobhunt run --if-not-run-today`. The first one that fires does the work; later ones find today's run done and exit. A `partial` run (no network, Ollama Cloud down) is retried by the next slot.
- **Every slot reads button presses** before the guard, so no press waits more than a few hours, well inside Telegram's 24-hour limit.
- **Skipped slots save nothing:** when the DB is byte-for-byte unchanged (today's run already done and no new presses), compacting, caching and the artifact upload are skipped. The job takes about 20 seconds.
- **Late-run note:** if nothing has reached Telegram yet today and a run starts more than 2 hours after 07:47 IST (`JOBHUNT_SCHEDULED_AT` in the workflow), its digest opens with "⏰ Nothing reached you before this…". That covers a day GitHub delayed or dropped, and a retry after a failed send.
- **A failed send is a red run:** if Telegram rejects the digest, `jobhunt run` exits 1, so the Actions run fails (GitHub emails you about failed runs by default) and the next slot retries.
- **Changing the time:** edit the crons and `JOBHUNT_SCHEDULED_AT` together.

### Laptop backup trigger

On some days GitHub fires none of the five slots for hours (on 2026-11-06, none of the four morning slots had started by 12:30 IST, during GitHub Actions incidents). Nothing inside GitHub can notice that, so the laptop can start the workflow itself:

```bash
uv run jobhunt schedule install --cloud
```

This launchd agent runs `jobhunt cloud run` (not forced) at 09:30 and 14:00 laptop time. If the day's run is already done, the workflow exits in seconds; if not, it does the run, and after 09:47 the digest carries the ⏰ line. If the laptop is asleep at those times, the agent runs once when it wakes. It replaces the local-run agent (`schedule install` without `--cloud`); `schedule uninstall` removes it.

## Minutes

On GitHub Free, private repos get $12 a month of Actions compute, about 2,000 Linux minutes. GitHub rounds each job up to a whole minute.
- **The day's real run:** usually 1–2 billed minutes: about 30 s of setup, about 30 s of fetching from around 40 boards, and a few new jobs scored at about 2 s each.
- **Each skipped slot:** about 20 s, billed as 1 minute. Four a day, plus two from the laptop backup trigger if installed.
- **Total:** about 5–8 minutes a day, so **about 150–250 minutes a month**, under 15% of the allowance. Runner mode is slower per job.

## Privacy in the logs

The workflow is written so that nothing personal reaches the Actions log:
- Secrets are written to files by `jobhunt cloud write-files` without being printed. There's no `cat`, no `set -x` and no debug output.
- `jobhunt run`'s output goes to a file on the runner, not the log. Errors still reach you: a crash sends its own Telegram message, and a failed workflow sends "jobhunt cloud run failed" with a link to the run.
- The log shows step names, timings, "DB restored from: cache" and the DB size.

Each run also reads your Apply / Skip presses from Telegram and stores them in the DB, so `jobhunt apply` on the laptop picks them up after its `pull-db` ([ARCHITECTURE.md](ARCHITECTURE.md#telegram-decisions)).

## Troubleshooting

| Symptom | Fix |
|:--|:--|
| `Missing secrets in the runner repo: …` | `uv run jobhunt cloud push-secrets` |
| `No saved DB (no cache, no artifact)` | `uv run jobhunt cloud seed-db` |
| Digest says "Ollama Cloud free credits exhausted" | Wait for the reset, set `JOBHUNT_LLM=runner`, or enable the paid fallback |
| `Set JOBHUNT_RUNNER_REPO=…` | Add it to `.env` on the laptop |
| A scheduled run is late, or the digest starts with ⏰ | GitHub delayed or dropped the morning slots, or an earlier send failed; a later slot did the run. If this happens often, install the [laptop backup trigger](#laptop-backup-trigger). Right now: `uv run jobhunt cloud run`. |
| A red run with "Telegram send failed" in the job | The bot token or chat ID in the runner's secrets is wrong (or Telegram was down). Check `.env`, then `uv run jobhunt cloud push-secrets` and `uv run jobhunt cloud run`. |
