# jobhunt — personal job search automation

## What this project is
A personal job-search pipeline: it finds jobs, scores them against the user's profile, sends a Telegram digest with Apply/Skip buttons, and fills approved applications in a visible browser for the user to review and submit. The goal is a ~10 minute daily review of a ready-made queue, so reliability and low friction matter more than features.

Personal context (who the user is, local paths, machine) lives in the git-ignored `CLAUDE.local.md`, which Claude Code loads automatically alongside this file.

## Phases (build only the next phase when asked; next: Phase 2 email parsing)
1. Discovery + scoring + daily Telegram digest  ← done
2. Inbox parsing of job-board email alerts (LinkedIn/Naukri/Indeed), Approve/Skip buttons in Telegram, tracking statuses  ← buttons and tracking done (with 3/4); email parsing not started
3. Tailored cover letter generation + answer bank (`config/answers.yaml`) for common form questions  ← done
4. Playwright assisted filling for Greenhouse / Lever / Ashby; the user reviews and clicks Submit  ← done

## Hard rules
- **Git:** never add Claude, Claude Code, or any AI tool as author, co-author, or contributor. No `Co-Authored-By` trailers, no "Generated with" lines, no AI mentions in commit messages, PR descriptions, or code comments. Write commit messages as plain conventional commits (`feat: add lever fetcher`).
- **Never scrape LinkedIn, Naukri, Indeed or Glassdoor directly** and never automate logins to them. Their terms forbid it and the user's account is their professional identity. Job-board data comes only from email alerts (Phase 2).
- **Only use official public job-board APIs** for career pages (Greenhouse, Lever, Ashby). Max 1 request/second per host, identify with a descriptive User-Agent, retry with backoff.
- **Never invent facts about the user.** The LLM may summarize, score, and draft, but must only use information from `config/profile.yaml`, `config/answers.yaml` (minus contact details, pay, work authorization and EEO answers, which never go to an LLM) and `data/resume.md`.
- **Never submit an application.** jobhunt fills the form and stops; the user reviews it and clicks Submit themselves. Code must never click a submit button, press Enter in a form field, or call `form.submit()` / `requestSubmit()`. While filling, a guard cancels any submit (`apply/form.py`). Changing this needs the user's explicit decision per ATS, recorded in `docs/DECISIONS.md`.
- **Never solve or bypass a CAPTCHA**, and never use a CAPTCHA-solving service or tricks to hide the automated browser. If a CAPTCHA appears, the user solves it.
- **Never invent an answer about the user.** Form answers come from `config/answers.yaml`, `data/resume.md`, or an LLM draft grounded only in them. Anything uncertain is left empty and highlighted for the user. Choice fields get an option only when it clearly matches the answer.
- **Sensitive questions never go to an LLM.** Questions about pay, phone, email, work authorization, visas/sponsorship, notice period, EEO or demographics (`answers.SENSITIVE`) are answered only from `answers.yaml`, never drafted. A missing value is left empty and flagged with the answers.yaml key to add.
- **At most `settings.max_per_session` applications (default 5) per `jobhunt apply` sitting.**
- **Secrets** live in `.env`. Never commit them, never print them in logs.
- Personal data (`config/profile.yaml`, `config/companies.yaml`, `config/answers.yaml`, `data/resume.md`, resume PDFs, the SQLite DBs, `.browser-profile/`, `output/` (digests, cover letters, screenshots), `.env`, logs, `CLAUDE.local.md`, `PROGRESS.local.md`) is **git-ignored and never committed**. Only `*.example.*` versions are committed. Never put personal paths, names, companies or numbers in committed files, tests or fixtures.

## Tech conventions
- Python 3.11+, managed with `uv`. Package code in `src/jobhunt/`.
- `httpx` for HTTP, `pydantic` v2 for models, `sqlite3` (stdlib) for storage, `typer` for the CLI, `pytest` for tests.
- **LLMs are local-first in code, Ollama Cloud in the cloud run.** All LLM calls go through one small provider interface (`src/jobhunt/llm/`). The code default is a local Ollama server (`http://localhost:11434`, model `qwen3.5:4b`); the cloud workflow points `OLLAMA_HOST` at Ollama Cloud and overrides the model and think level by env (`gpt-oss:20b`, `low`) without touching `profile.yaml`. Either way it's called through its native `/api/chat` endpoint with `"think": false` (or `"low"` for gpt-oss) and a JSON schema in `"format"` so outputs are schema-constrained. A paid provider (Anthropic API) is used **only** as a fallback when the local result fails validation after a retry, or for tasks explicitly marked heavy in config. Every LLM result records which provider and model produced it. Writing tasks (cover letters, answer drafts) use `settings.writing` in `answers.yaml`: Ollama Cloud `gpt-oss:20b` by default, then the local model, then the paid fallback if enabled.
- Never send a job to the paid fallback silently: log it, count it in the run summary, and respect a per-run cap on paid calls.
- LLM model names and provider choices come from config, never hardcoded in logic.
- **Discovery runs in the cloud** (a private GitHub Actions runner repo, docs/CLOUD.md) with a catch-up run later the same day; the launchd schedule remains as a local option. **Applying runs only on the laptop** (visible browser, user at the keyboard), with its state in the git-ignored `data/applications.db`, never in the cloud's `jobhunt.db`.
- Tests never hit the network: use synthetic JSON fixtures in `tests/fixtures/` (fictional companies, same structure as each real API).
- Keep modules small and boring. Prefer clear code over clever abstractions.
- Every run must be idempotent: re-running the same day must not duplicate jobs, re-score unchanged jobs, or re-send the same digest.

## Docs to keep current
- When a design decision is made or a known limit is found, record it in `docs/DECISIONS.md` (project-level, no personal details).
- Personal progress and setup notes go in the git-ignored `PROGRESS.local.md`.
