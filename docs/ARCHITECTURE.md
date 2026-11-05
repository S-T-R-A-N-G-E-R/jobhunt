# Architecture

jobhunt is a small, synchronous-where-possible Python CLI with two halves:

- **Discovery** (`jobhunt run`): **fetch → filter → score → digest**. It runs daily on GitHub Actions from a private runner repo ([CLOUD.md](CLOUD.md)), or on your machine with launchd or cron.
- **Applying** (`jobhunt apply`): **decisions → materials → fill → you submit → record**. It runs only on your laptop, in a visible browser ([APPLY.md](APPLY.md)).

The decisions behind the details are in [DECISIONS.md](DECISIONS.md).

## Module map

```
src/jobhunt/
├── cli.py            Typer app: every command; thin wrappers over the modules below
├── config.py         Paths, pydantic models for profile.yaml / companies.yaml, .env, profile_hash()
├── models.py         Job (normalised posting), ScoreOutput (LLM schema), HTML-to-text helpers
├── net.py            Async httpx client for job boards: User-Agent, 1 req/s per host, retries
├── sources/
│   ├── __init__.py   detect_ats(url), fetch_all(companies): concurrent across companies
│   ├── greenhouse.py api_url() + parse() → list[Job]
│   ├── lever.py      (also api.eu.lever.co via region: eu)
│   └── ashby.py
├── db.py             SQLite connection, migrations, upserts, run bookkeeping
├── scoring.py        Hard filters, the scoring plan (what needs the LLM), scoring loop, guardrails
├── llm/
│   ├── __init__.py   LLM.complete_json(): retry → escalate; health_check()
│   ├── ollama.py     POST /api/chat with a JSON schema in "format"
│   └── anthropic.py  Messages API with forced tool use (plain httpx, no SDK)
├── prompts/
│   ├── score_job.md  Prompt template: {{profile}}, {{resume}}, {{job}}
│   └── cover_letter.md, check_claims.md, answer_question.md   Writing prompts (Phase 3)
├── digest.py         Build and send the digest (one message per job, with Apply/Skip buttons)
├── decisions.py      Read button presses (getUpdates), store them, mark the chosen button
├── answers.py        answers.yaml model, question → answer rules, strict option picking
├── materials.py      Cover letter (+ claims check, PDF) and drafted answers per job
├── apply/
│   ├── session.py    `jobhunt apply`: pull, queue, fill each job, record the outcome
│   ├── form.py       Generic scanner/filler, submit guard, highlights, outcome detection
│   ├── store.py      data/applications.db: decisions, applications
│   └── greenhouse.py, lever.py, ashby.py   Selectors and form URLs per ATS
├── pipeline.py       run(): one full run, crash handling, logging setup
├── schedule.py       macOS launchd agent: install / uninstall / status
└── cloud.py          GitHub Actions runner: secrets, DB seed / restore / pull, trigger (via gh)
```

## Two databases

| | `data/jobhunt.db` | `data/applications.db` |
|:--|:--|:--|
| **Lives** | In the cloud (Actions cache + artifact backups) when you use the runner; on your machine otherwise | Only on your laptop, never uploaded |
| **Written by** | `jobhunt run` (cloud or local) | `jobhunt apply` and its subcommands |
| **Holds** | Jobs, scores, digests, runs, and the Apply/Skip presses cloud runs read | Your decisions (merged from both sources) and applications |
| **On the laptop** | A copy, replaced wholesale by `cloud pull-db` (which `jobhunt apply` runs first) | The source of truth |

Job ids in `applications.db` refer to `jobhunt.db`. Because `pull-db` replaces the laptop's `jobhunt.db`, nothing the laptop writes is kept there.

## Data model

SQLite at `data/jobhunt.db`. Migrations are plain SQL strings in `db.MIGRATIONS`, applied in order and tracked in `schema_version`.

| Table | Key columns | Notes |
|:--|:--|:--|
| `jobs` | `source`, `company`, `external_id` (unique together), `title`, `location`, `is_remote`, `url`, `description_text`, `compensation_text`, `posted_at`, `content_hash`, `first_seen_at`, `last_seen_at`, `is_active` | One row per posting. After a company fetches successfully, its postings that disappeared get `is_active = 0`. A company whose fetch failed is left untouched. |
| `scores` | `job_id`, `content_hash`, `profile_hash`, `hard_filter_result`, `score`, `verdict`, `reasons`, `red_flags`, `seniority_match`, `extracted_comp`, `one_line_pitch`, `uncapped_score`, `provider`, `model`, `escalated`, `latency_ms`, `status`, `error`, `scored_at` | Append-only history. The latest row per job is the current state. `status` is `filtered`, `scored` or `score_failed`. |
| `digests` | `job_id` (unique), `run_id`, `score`, `sent_at` | A job is sent at most once, ever. |
| `decisions` | `job_id` (unique), `action` (`approve` / `skip`), `update_id`, `decided_at` | Button presses read by cloud runs. The laptop merges them into `applications.db`. |
| `runs` | `run_date` (local date; `JOBHUNT_TIMEZONE` or the machine's zone), `status` (`running` / `ok` / `partial` / `failed`), counts (`companies`, `fetched`, `new_jobs`, `filtered_out`, `scored_local`, `escalated`, `score_failed`, `quota_skipped`, `sent`), `digest_sent`, `ollama_status`, `errors` (JSON) | One row per `jobhunt run`. |

**Hashes:**
- `content_hash` is a hash of title, location and description. When it changes, the job is re-filtered and re-scored.
- `profile_hash` is a hash of `profile.yaml` and the prompt template. When it changes, filter rows are re-evaluated automatically, and LLM scores become *stale* until `--rescore`.

`data/applications.db` holds `decisions` (same shape) and `applications` (`job_id`, `company`, `title`, `url`, `status` `applied` / `skipped` / `failed`, `reason`, `cover_letter`, `screenshot`, `updated_at`).

## The run pipeline (`pipeline.run`)

0. **Button presses.** Read pending Apply / Skip presses from Telegram into `decisions` (always, even when the guard below exits).
1. **Guard.** With `--if-not-run-today`, exit if a run with `status = ok` already exists for today's local date.
2. **Start** a `runs` row, then load `profile.yaml` and `companies.yaml`. A missing file exits with a "copy the example" message.
3. **Fetch.** `sources.fetch_all` fetches every company concurrently through one `net.Http` client:
   - descriptive User-Agent and 20 s timeout;
   - at most one request per second per host;
   - up to 3 retries with exponential backoff (1, 2, 4 s) on 429, 5xx and connection errors.

   Each response is parsed into `Job` models (HTML stripped, whitespace normalised) and upserted. A failing company is logged, recorded in `runs.errors` and skipped.
4. **Score** (`scoring.plan`, then `scoring.score_jobs`):
   - **Health check:** `GET /api/tags` with a 5 s timeout, and the configured model must be listed. If it fails, only the hard filters run, the problem and its fix are stored in `runs.ollama_status`, and the run ends as `partial`.
   - **Plan:** for each active job, skip it if its latest score row matches the current `content_hash` and `profile_hash`. Otherwise run the hard filters (title phrase match, excluded keywords, location, remote, relocation). Rejections are stored as `filtered` rows. Jobs that pass and have no current score form the queue, ordered high-priority companies first, then newest. `--rescore` adds stale scored jobs ahead of the queue.
   - **Score:** jobs are scored one at a time (a laptop model shouldn't get parallel requests), up to `max_llm_calls_per_run`, with a Rich progress bar. Code guardrails are applied to each result (priority boost, seniority cap, verdict from score) before it's stored.
5. **Digest** (`digest.send`):
   - **Select:** unsent, active, scored jobs with score ≥ `min_score_for_digest`, best first, at most `digest.max_jobs`. Then up to `digest.stretch_max` stretch roles: unsent jobs marked `over` whose `uncapped_score` (the score before the seniority cap) is at least the threshold.
   - **Format:** the header gives counts plus notes about Ollama, quota, fetch errors, escalations and failures (split under Telegram's 4,096-character limit). Each job is its own HTML-escaped card with **✅ Apply** / **⏭ Skip** buttons; stretch roles follow under a "🧗 Stretch roles" line.
   - **Send:** the header goes out via `sendMessage` and is the only message that notifies; job cards are sent with `disable_notification`. Each job is recorded in `digests` as soon as Telegram accepts its card, and the text is appended to `output/digest-YYYY-MM-DD.md`.
   - **Quiet days:** if there's nothing new and today's status line was already sent, nothing is sent.
6. **Finish.** The run is `partial` if Ollama was unavailable, every company fetch failed, or the digest couldn't be sent. Otherwise it's `ok`. On any crash it is marked `failed`, the traceback is logged, and a short Telegram message gives the error and the log path.

## Telegram decisions

Nothing listens to Telegram live; presses wait on Telegram's side until someone reads them.

1. A button press creates a `callback_query` update carrying `a:<job id>` or `s:<job id>`.
2. `decisions.poll` reads updates with `getUpdates` at the start of every `jobhunt run` (before the "already ran today" guard) and every `jobhunt apply`. Updates from other chats are ignored.
3. Each press is stored in the reader's `decisions` table (`jobhunt.db` for runs, `applications.db` for `jobhunt apply`; newest `update_id` wins), the button gets a ✓ via `editMessageReplyMarkup`, and the update is confirmed by asking for a higher offset.
4. On the laptop, `apply/store.merge_cloud` copies the cloud's decisions (from the pulled `jobhunt.db`) into `applications.db`. Terminal picks (`apply pick`) are written there directly.

**The 24-hour limit:** Telegram drops unread updates after 24 hours. With the cloud's five daily slots (07:47 to 14:43 IST, each reading presses even when the day's run is already done), a press normally waits at most about 17 hours, overnight. If no run or `jobhunt apply` happens for a full day, older presses are lost; press the button again or use `apply pick`.

## The apply session (`apply/session.run`)

1. **Sync.** `cloud pull-db` (unless `--no-pull`), merge the cloud's decisions, then read new presses from Telegram.
2. **Queue.** Approved jobs with no application yet, up to `settings.max_per_session`.
3. **Per job:** open the ATS form in a persistent Chromium profile (`.browser-profile/`) → scan the fields (`apply/form.scan`, with the adapter's selectors) → write the cover letter only if the form takes one, and draft answers for non-sensitive free-text questions (`materials.prepare`) → fill with the submit guard on (`apply/form.fill`) → highlight what's left → print the checklist → wait for you.
4. **Record.** Look for a confirmation page (or ask), save a screenshot, write the `applications` row.
5. **Finish.** Step timings, missing answer-bank keys, and one Telegram line: "Session done: X applied · Y skipped · Z this week".

## The LLM layer (`llm/`)

`LLM.complete_json(prompt, schema, task)` returns an `LLMResult` (the parsed pydantic object, provider, model, latency, and `escalated`). Every stored score records which provider and model produced it.

**Ollama, the default provider.** It calls the native `POST {OLLAMA_HOST}/api/chat` with:
- `"stream": false` and `"think": false` (no reasoning tokens);
- `"format": <the pydantic model's JSON schema>`, so the output is schema-constrained;
- `"options": {"temperature": 0, "num_ctx": …}`;
- `"keep_alive"` from config, so the model stays loaded for the whole run.

The response is validated with pydantic.

**Retry, then escalate:**
1. If validation fails, retry once locally. The model's bad output and a short "your previous output was invalid because …" message are added to the conversation.
2. If that fails too, escalate to the paid provider only when the fallback is enabled, `ANTHROPIC_API_KEY` is set, and the per-run paid cap isn't reached. Escalations are logged and counted in the digest.
3. Otherwise raise `LLMError`. The job is stored as `score_failed` with the reason, and the run continues.

**Anthropic, the optional fallback.** It uses the Messages API over plain httpx, with a single `submit` tool whose `input_schema` is the same JSON schema, forced with `tool_choice`. The tool input is validated by the same pydantic model.

**Heavy tasks.** A task listed in `llm.fallback.heavy_tasks` goes straight to the paid model when it's available. No task is marked heavy by default.

**Never silently paid.** When Ollama is down, the paid model is never used as a substitute.

### Scoring vs writing

| | Scoring (`jobhunt run`) | Writing (cover letters, drafts) |
|:--|:--|:--|
| **Config** | `llm` in `profile.yaml` | `settings.writing` in `answers.yaml` |
| **Default** | local Ollama, `qwen3.5:4b`, `think: false`. The cloud workflow overrides model and think level with `JOBHUNT_LLM_MODEL` / `JOBHUNT_LLM_THINK` and points `OLLAMA_HOST` at Ollama Cloud (`gpt-oss:20b`, `low`) | Ollama Cloud `gpt-oss:20b`, `think: low` |
| **If it fails** | retry once, then the paid fallback if enabled | the local model from `profile.yaml`, then (through it) the paid fallback if enabled |
| **Sees** | profile, resume, job | resume, `facts`, current role, education, work preferences, job |

**Quota.** Ollama Cloud answers 402/429 (or a 4xx about usage limits) when credits run out, and the provider raises `QuotaError`. Scoring stops for the run, and the remaining jobs stay unscored for a later run (counted in `runs.quota_skipped`, shown in the digest). Writing moves on to the local model. A quota error is never escalated to the paid model.

**Sensitive questions never reach an LLM.** Contact details, pay, work authorization and EEO answers are excluded from the writing context (`answers.llm_context`). Form questions about pay, phone, email, authorization, visas, sponsorship, notice period, EEO or demographics (`answers.SENSITIVE`) are answered only from `answers.yaml`, never drafted; a missing value is left for you with the key to add.

## Idempotency

| Re-running on the same day… | …does this |
|:--|:--|
| Fetching | Upserts on `(source, company, external_id)`: no duplicate jobs |
| Unchanged jobs | Skipped by the plan: no new LLM calls |
| Failed scores | Retried only on a later day |
| Digest | Already-sent jobs are excluded by `digests`; the "alive" line is sent at most once a day |
| Scheduler | `--if-not-run-today` makes extra hourly ticks no-ops after a successful run |

## Scheduling

### In the cloud

`deploy/github-actions/daily.yml` is the workflow for a private runner repo. Five crons (07:47 IST plus catch-ups until 14:43 IST, because GitHub delays and drops scheduled runs) check out this repo and run `jobhunt cloud write-files`, `jobhunt cloud restore-db` and `jobhunt run --if-not-run-today`, then save the DB to the cache and an artifact unless it's unchanged. See [CLOUD.md](CLOUD.md).

### On macOS (local alternative)

`schedule.py` writes `~/Library/LaunchAgents/local.jobhunt.daily.plist` with `plistlib` and loads it with `launchctl bootstrap gui/<uid>`. The agent runs:

```
<uv> run --directory <project> jobhunt run --if-not-run-today --quiet
```

**Agent settings:**
- **When:** `StartCalendarInterval` 08:00, `RunAtLoad`, and `StartInterval` 3600, so a sleeping Mac catches up within an hour of waking.
- **Environment:** absolute paths, `WorkingDirectory` set to the project, and a `PATH` that includes Homebrew.
- **Logs:** stdout and stderr go to `logs/launchd.*.log`.

**Old labels:** agents installed under an older label are detected and removed on `install` and `uninstall`.

## Tests

`uv run pytest` is fully offline:
- A socket guard fails any test that tries to open a network connection.
- The job boards, Ollama, Anthropic and Telegram are all replaced with `httpx.MockTransport` fakes.
- Form filling runs headless Chromium against local HTML fixtures (`tests/fixtures/forms/`) served from `127.0.0.1`; the tests fail if anything submits. They need `uv run playwright install chromium` once.
- Parser fixtures in `tests/fixtures/` are synthetic: fictional companies, with the same JSON structure as each real API.
