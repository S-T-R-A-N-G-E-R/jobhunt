# Troubleshooting

Start with `uv run jobhunt status` (after `uv run jobhunt cloud pull-db` if the cloud runs your daily job). It shows the last run, its errors, the unscored backlog and any stale scores. Full logs are in `logs/jobhunt.log` (rotated), plus `logs/launchd.out.log` and `logs/launchd.err.log` for scheduled runs on macOS. Every digest is also saved to `output/digest-YYYY-MM-DD.md`.

Cloud runs (GitHub Actions) have their own table in [CLOUD.md](CLOUD.md#troubleshooting).

## Ollama and scoring

| Message | Cause | Fix |
|:--|:--|:--|
| `Ollama isn't running at http://localhost:11434: open the Ollama app (or run ollama serve)` | Ollama isn't started. Scoring was skipped; fetching still ran. | Start Ollama. On macOS the scheduler retries hourly, so the run catches up by itself. |
| `Ollama model qwen3.5:4b isn't pulled: run ollama pull qwen3.5:4b` | The configured model is missing. | Run that command, or change `llm.local.model`. |
| `local output invalid after retry (…); not escalating: fallback disabled in profile.yaml` | The model returned invalid JSON twice for one job. | Nothing urgent: the job is retried on a later day. If it happens a lot, try a larger model or enable the [paid fallback](USAGE.md#the-paid-fallback-optional). |
| `… not escalating: ANTHROPIC_API_KEY not set` / `paid call cap reached` | The fallback is enabled but has no key, or this run's paid cap is used up. | Add the key, or raise `llm.fallback.max_calls_per_run`. |
| `⚠️ Ollama Cloud free credits exhausted: N jobs not scored…` | The Ollama Cloud account is out of credits or rate-limited. Those jobs stay unscored and are retried on later runs. | Wait for the reset, switch the runner to `JOBHUNT_LLM=runner`, or enable the paid fallback ([CLOUD.md](CLOUD.md#where-the-llm-runs)). |
| `mlx: Unable to load kernel …` / `Unable to reach MTLCompilerService` (macOS) | An orphaned `ollama serve` left over from an earlier session (its app isn't running) has lost its connection to the Metal compiler. | Quit the Ollama app, stop any leftover `ollama serve` process (Activity Monitor), then open the app again. A reboot also fixes it. |
| `ollama --version` reports a much older client than the server, or commands behave oddly | An old Homebrew `ollama` earlier on your `PATH` shadows the CLI bundled with the app. | `which -a ollama` lists every copy. Remove the stale one (`brew uninstall ollama`) or put the app's CLI first. |
| Scoring is very slow | Model running on CPU, or a large `num_ctx` | Each job takes about 10–25 s on a laptop. Lower `scoring.max_llm_calls_per_run` to keep runs short; the backlog carries over. |
| Scores look wrong | Small-model limits | See [Tuning](USAGE.md#tuning) and [Known limits](DECISIONS.md#known-limits). Run `jobhunt calibrate` if you have an API key. |

## Boards and fetching

| Message | Cause | Fix |
|:--|:--|:--|
| `✗ <Company>: HTTPStatusError: Client error '404 Not Found' …` in `status`, or "Couldn't fetch: …" in the digest | The board's slug changed or the company moved to another system. The other companies are unaffected. | Find the new careers URL and run `jobhunt company add <url>`, then remove the old entry from `companies.yaml`. |
| `Couldn't read the <ats> board '<slug>'` | `company add` couldn't fetch the board. | Check the URL in a browser. Some companies use a different slug from their name. |
| `The <ats> board '<slug>' has 0 open jobs right now.` | The board exists but is empty. | It's added anyway. Check that it's the right company. |
| `<url> isn't a Greenhouse, Lever or Ashby board.` | The company uses Workday or its own careers site. | It was added to `manual_check`. Check it by hand. |
| `Missing config/companies.yaml.` | First run, or the file was deleted. | `cp config/companies.example.yaml config/companies.yaml`, or just run `jobhunt company add <url>`. |
| `Missing config/profile.yaml.` | First run. | `cp config/profile.example.yaml config/profile.yaml` and fill it in. |
| Far too many or too few jobs pass | Filters too loose or too tight | Run `jobhunt score --dry-run`, then adjust `target_titles` and `title_exclude_keywords`. Titles match as phrases. |

## Telegram

| Message | Cause | Fix |
|:--|:--|:--|
| `TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env` | `.env` is missing values. The digest was still written to `output/`. | Fill in `.env` ([SETUP.md](SETUP.md#6-create-the-telegram-bot)). |
| `Telegram send failed: {"ok":false,"error_code":401,"description":"Unauthorized"}` | Wrong or revoked token | Get a new token from @BotFather and update `.env`. |
| `Telegram send failed: {… "description":"Bad Request: chat not found"}` | Wrong chat ID, or you never messaged the bot | Send the bot a message, then fetch `getUpdates` again for the ID. |
| No message today | Nothing new, and today's status line was already sent | Normal. Check `jobhunt status` or `jobhunt top`. |

| An Apply/Skip press didn't register (no ✓ on the button) | Presses are read only at the next run or `jobhunt apply`, and Telegram drops unread presses after 24 hours | Wait for the next run, or decide with `jobhunt apply pick`. |

The bot token is never written to logs. httpx's request logging is turned down for exactly this reason.

## Scheduling (macOS)

| Symptom | Fix |
|:--|:--|
| `schedule status` says `Not loaded` | Run `jobhunt schedule install`. |
| `Old agent still installed: …` | Run `jobhunt schedule install`. It removes agents installed under an older label. |
| `launchctl bootstrap failed: …` | Run `jobhunt schedule uninstall`, then `install` again. |
| Ran at 08:00 but nothing was scored | Ollama wasn't up yet. The run is marked `partial` and the hourly tick retries it. |
| Ran with no network (every fetch failed, or Telegram unreachable) | Nothing to do: the run is marked `partial`, and the next tick (or the cloud's next slot) retries it. Jobs are marked sent only after Telegram accepts them. |
| `Already ran successfully today.` | The `--if-not-run-today` guard is working. Use `jobhunt run` without the flag to force a run. |
| A run crashed | You get a Telegram message saying `❌ jobhunt run failed: …` with the log path. Check `logs/jobhunt.log`. |

## Applying

| Symptom | Cause | Fix |
|:--|:--|:--|
| `Executable doesn't exist at …/chromium…` | Playwright's browser isn't installed. | `uv run playwright install chromium` |
| A CAPTCHA appears when you click Submit | The ATS (reCAPTCHA or hCaptcha) challenges the automated browser, which jobhunt doesn't disguise. | Solve it yourself, then submit. jobhunt never solves or bypasses CAPTCHAs. |
| A field is red with "add <key> to answers.yaml" | A sensitive question (pay, contact, authorization, EEO…) has no value in your answer bank, so it was left for you. | Fill it in on the form, then add the key to `config/answers.yaml` so it's filled next time. The session lists all missing keys at the end. |
| "answer it yourself (unclear for this job)" | Authorization, sponsorship or salary depends on the job's country, and the location ("Remote") doesn't say. | Answer it on the form. |
| `writing model gpt-oss:20b … failed` in the log, letter written by the local model | Ollama Cloud is out of credits, unreachable, or `OLLAMA_API_KEY` isn't set. | Nothing urgent: the local model takes over. `meta.json` next to the letter shows which model wrote it. |
| The form opens but nothing is filled | The page isn't a Greenhouse, Lever or Ashby form jobhunt can read (e.g. the company's own site). | Copy the details the terminal prints. See [APPLY.md](APPLY.md#unrecognised-pages). |
| "Did you submit? y/n" | No confirmation page was recognised. | Answer honestly; it decides whether the job is recorded as applied. |
| `Couldn't pull the cloud DB (…); using the local one.` | `gh` isn't logged in, `JOBHUNT_RUNNER_REPO` isn't set, or there's no network. | Fix `gh auth status`, or run with `--no-pull` to use the local copy. |
