# Setup

From nothing to a daily Telegram digest, in about 20 minutes. Run every command from the project folder. If its path contains spaces, keep it in quotes.

This page gets a first run working on your machine. Then choose where the daily run lives (step 8), and set up applying with [APPLY.md](APPLY.md) when you're ready.

## 1. Install uv

```bash
brew install uv                                     # macOS with Homebrew
curl -LsSf https://astral.sh/uv/install.sh | sh     # anywhere else
```

## 2. Install Ollama and pull the model

1. Install Ollama from <https://ollama.com/download> and start it.
2. If the daily run will be local, make sure it starts automatically:
   - **macOS:** if the app has no "Launch at login" option, add it as a login item from the terminal:
     ```bash
     osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/Ollama.app", hidden:true}'
     ```
     You can remove it later under **System Settings → General → Login Items**.
   - **Linux:** the installer sets up an `ollama` systemd service.
3. Pull the model (about 4 GB):
   ```bash
   ollama pull qwen3.5:4b
   ```

The model name lives in `config/profile.yaml` under `llm.local.model`, so you can switch to another Ollama model later.

## 3. Get the code

```bash
git clone https://github.com/S-T-R-A-N-G-E-R/jobhunt.git
cd jobhunt
uv sync
```

## 4. Create your private files

All of these are git-ignored and never leave your machine:

```bash
cp .env.example .env
cp config/profile.example.yaml config/profile.yaml
cp config/companies.example.yaml config/companies.yaml
```

### Your profile
Edit `config/profile.yaml`. The comments explain each field.
- **Titles:** use specific `target_titles` ("Senior Backend Engineer", not "Engineer"), because titles match as phrases. See [DECISIONS.md](DECISIONS.md#titles-match-as-phrases-not-word-overlap).
- **Excluded words:** `title_exclude_keywords` match whole words, so list "intern" and "internship" separately.
- **Scoring inputs:** `what_i_want`, `must_haves` and `dealbreakers` drive the scoring. Write them honestly.
- **Leave alone at first:** the `llm`, `scoring` and `digest` sections can stay as they are.

### Your resume
Turn your PDF into text, then open `data/resume.md` and clean up anything the extraction mangled. It's also worth removing contact details, since scoring doesn't need them.

```bash
uv run jobhunt resume import path/to/resume.pdf
```

### Guard your commits (if you'll commit to the repo)
```bash
uv run jobhunt dev install-hooks
```
This installs a git pre-commit hook that reads your git-ignored personal files at commit time (`config/answers.yaml`, `config/profile.yaml`, `config/companies.yaml`, `.env`, `data/resume.md`) and blocks any commit whose added lines contain one of their values: your name, email, phone, CTC and salary numbers, Telegram chat ID, API keys, runner repo name, or a tracked company that isn't in `companies.example.yaml`.
- **Ignored as too generic:** values under 4 characters, whole numbers under 100, and company names that already appear in the committed code (a company called "Writer" vs the `Writer` class).
- **Output:** the file, line and which setting matched, never the value itself.
- **Bypass once:** `git commit --no-verify`.

## 5. Add companies

Paste a careers URL. The app detects the job-board system (Greenhouse, Lever or Ashby), checks that the board returns jobs, and adds it to `config/companies.yaml`:

```bash
uv run jobhunt company add https://job-boards.greenhouse.io/<company>
uv run jobhunt company add https://jobs.lever.co/<company>
uv run jobhunt company add https://jobs.ashbyhq.com/<company> --priority high
```

- **High priority:** `--priority high` gives a company +5 points and scores its jobs first.
- **Other systems:** a URL that isn't on one of the three supported systems goes on the `manual_check` list, so you remember to check it by hand.
- **No `companies.yaml` yet:** `company add` creates it for you.
- **Check new boards:** two organisations can share a slug, so look at the job titles on a new board before relying on it.

## 6. Create the Telegram bot

1. In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts. It gives you a token like `123456:ABC…`.
2. Open `.env` in an editor and paste the token after `TELEGRAM_BOT_TOKEN=`. Pasting it in an editor keeps the token out of your shell history.
3. Send any message to your new bot.
4. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id":…}`. Put that number in `.env` as `TELEGRAM_CHAT_ID`.

If the token is ever exposed, revoke it in @BotFather (`/revoke`) and paste the new one.

## 7. Check everything

```bash
uv run jobhunt llm test          # Ollama answering? (the paid fallback is optional)
uv run jobhunt fetch             # pull jobs from every board
uv run jobhunt score --dry-run   # how many jobs pass the filters, without calling the model
uv run jobhunt run --preview     # full run, digest printed instead of sent
uv run jobhunt run               # full run, digest sent to Telegram
```

**Tune the filter before the first real run:**
- If `--dry-run` shows hundreds of jobs passing, tighten `target_titles` or add excluded words.
- If it shows close to none, loosen them.

**Scoring speed:** about 10–25 seconds per job on a laptop. Each run scores at most `scoring.max_llm_calls_per_run` jobs (default 60). Anything left over is scored on later runs, or right away with `uv run jobhunt score`.

## 8. Run it every day

### In the cloud (recommended)
Run the daily job on GitHub Actions from a private repo, so the digest arrives even when your computer is off: [CLOUD.md](CLOUD.md). Skip the local schedule below if you do.

### macOS
```bash
uv run jobhunt schedule install
uv run jobhunt schedule status
```

This installs a launchd agent (`local.jobhunt.daily`). It runs at 08:00, again at load, and every hour after that. The `--if-not-run-today` guard makes it do real work only until one run succeeds that day, so a Mac that was asleep at 08:00 catches up within an hour of waking. Logs go to `logs/`. Remove it with `uv run jobhunt schedule uninstall`.

### Linux and Windows
`jobhunt schedule` is macOS-only. Use your OS scheduler to run this hourly from the project folder:

```bash
uv run jobhunt run --if-not-run-today --quiet
```

For example, as a crontab entry: `0 * * * * cd /path/to/jobhunt && uv run jobhunt run --if-not-run-today --quiet`.

Next: [USAGE.md](USAGE.md) for day-to-day commands, and [APPLY.md](APPLY.md) to set up assisted applying.
