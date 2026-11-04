# Design decisions

Why jobhunt works the way it does. Each entry gives the decision, the problem it solved, and where it lives in the code. Add an entry whenever a design decision is made or a known limit is found.

## Filtering

### Titles match as phrases, not word overlap
A target title (minus seniority words such as "senior" or "staff") must appear in the job title as a contiguous phrase, in order.
- **Why:** loose word overlap let "Software Engineer, Enterprise AI" match the target "AI Engineer". On a real set of boards, switching to phrase matching cut the jobs passing the filters by more than half, and the ones removed were almost all false positives.
- **Where:** `scoring.title_matches`

### Reversed titles are reordered too
Before matching, a second form of the title is tried. Trailing qualifiers like "- Remote" or "(India)" are stripped, and if exactly one comma is left, "A, B" becomes "B A". A job passes if either form matches.
- **Why:** some boards write "Engineer, Machine Learning" instead of "Machine Learning Engineer".
- **Where:** `scoring.reorder_title`

### `relocation_ok: true` lets every location through the filter
When relocation is allowed, the location filter is skipped entirely. The LLM judges city and visa fit using `relocation_cities` and `relocation_notes` from the profile.
- **Why:** "willing to relocate to some cities, or abroad with visa sponsorship" can't be expressed as a simple location list.
- **Trade-off:** more jobs reach the (slower) LLM stage.

### Excluded keywords match whole words
`title_exclude_keywords` uses word boundaries, so "intern" does not exclude "Internship" or "International". List each form you want to exclude.

## Scoring

### `seniority_match` is defined from the role's side
In the prompt, `under` means the role is more junior than the candidate, `match` means about right, and `over` means the role needs more seniority or years than the candidate has.
- **Why:** with an undefined field, the 4B model read `over` as "candidate overqualified" and gave senior roles to a junior candidate a score of 97.

### Code-side guardrails on top of the LLM
- **Seniority cap:** a role marked `over` is capped at 40, whatever the model scored it (`OVER_SENIORITY_CAP`).
- **Verdict from score:** the verdict is derived from the final score (apply ≥ 75, maybe ≥ 50, otherwise skip), so the score and verdict can never contradict each other.
- **Priority boost:** high-priority companies get +5, clamped to 100, applied before the cap.
- **Why:** small models are inconsistent across fields of the same answer. Deterministic rules in code are cheap and testable.
- **Where:** `scoring.final_score`

### Stretch roles use the score before the cap
The seniority cap hides roles that fit well apart from the level. The digest can show a few of them in a separate, clearly labelled section (`digest.stretch_max`, default 2, 0 turns it off).
- **Stored, not recomputed:** `scores.uncapped_score` keeps the score before the cap, so the section needs no rescoring.
- **Older rows:** the migration copies `score` where the cap couldn't have changed it (not `over`, or under 40). Rows capped at exactly 40 stay NULL and never show as stretch, because their real score is unknown.
- **Which jobs:** marked `over`, and at least `min_score_for_digest` before the cap. A capped role that wouldn't have made the digest anyway isn't a near miss.
- **The reason shown:** the first red flag mentioning seniority, years or experience, otherwise a generic line. Adding a dedicated field to the LLM schema would change the prompt, and with it every job's `profile_hash`.
- **Sent once:** stretch roles are recorded in `digests` like any other job.

### Prompt rules for a small model
The prompt (`src/jobhunt/prompts/score_job.md`) is short and explicit. It tells the model to:
- penalise dealbreakers heavily, and not reward keyword stuffing;
- report compensation as unknown rather than guessing it;
- treat pay above the candidate's minimum as never a red flag;
- describe the candidate only with facts from the profile and resume.

### Rescoring is opt-in, and stale scores go first
Changing `profile.yaml` or the prompt changes a `profile_hash` stored on every score row.
- **Hard filters** (cheap) re-run automatically for rows with a stale hash.
- **LLM scores** are only redone with `jobhunt score --rescore`, because a profile tweak could otherwise trigger hundreds of slow local calls.
- `--rescore` puts stale scores ahead of the never-scored backlog, so a prompt fix takes effect in the next batch.

### Failed scores retry on a later day
A job whose LLM output fails validation (after the local retry and the optional paid fallback) is stored as `score_failed`. It is retried on a later day, not in the same day's re-runs, so repeated runs on one day make zero new LLM calls.

## Runs and reliability

### Which failures make a run retryable
A run is `partial`, so the next scheduler tick tries again the same day, when any of these happen:
- Ollama is unavailable;
- **every** company fetch failed, which usually means no network;
- the Telegram digest couldn't be sent.

A single failing company is recorded and shown in the digest, but the run still counts as `ok`.
- **Why `ok` for one failing company:** otherwise one broken board would re-fetch every board every hour all day.
- **Why `partial` for no network:** a laptop that woke up offline used to record an `ok` run with nothing fetched and nothing sent, and then skipped the rest of the day. Jobs are only marked as sent after Telegram accepts the message, so the retry sends them.

### Removing a company deactivates its jobs
A job only goes inactive when its company is fetched and the job is gone, and a removed company is never fetched again. Without a fix, its jobs would stay active and keep being scored and sent. So each fetch first marks jobs inactive whose `(ats, name)` is no longer in `companies.yaml` (`db.deactivate_unlisted`). Their scores stay, and their descriptions are dropped after 30 days like any inactive job.

### The scheduler label is generic
The launchd agent is `local.jobhunt.daily`. `schedule install`, `uninstall` and `status` detect agents installed under an older label (any `*jobhunt*.plist` that runs `jobhunt run`) and remove them on install or uninstall, so you never end up with two agents.

### Resume import uses pypdf's layout mode
The default text extraction split some PDFs into one word per line. Layout mode keeps lines and columns intact.

## Running in the cloud

See [CLOUD.md](CLOUD.md) for the setup.

### The scheduled run lives in a separate private repo
The public code repo never runs the daily job. Actions logs, caches and artifacts of a public repo are visible to anyone, and they would reveal which companies and roles are tracked. A private runner repo holds only the workflow, checks out the public code at `main`, and gets the personal files from Actions secrets. The runner repo's name comes from `JOBHUNT_RUNNER_REPO`, so the public code doesn't name it.

### The DB is kept in the Actions cache, with artifacts as a backup
The DB changes daily and is tens of MB, so committing it would bloat git history.
- **Cache:** the main state. Each run saves under a new key, and the next one restores the newest via a key prefix.
- **Artifact:** a backup uploaded every run, kept 7 days, newest 3 only, to stay well inside the 500 MB free artifact storage.
- **No state at all:** the run refuses to start empty unless told to (`allow_fresh`), because an empty DB would send a flood of old jobs.

### Seeding goes through a temporary release asset
`jobhunt cloud seed-db` uploads a gzipped `VACUUM INTO` snapshot as an asset of a `seed` release in the private runner repo, then starts a run with `seed=true`.
- **Why not a workflow input:** dispatch inputs can't carry a file this size.
- **Why not an artifact:** artifacts can only be uploaded from inside a workflow.

The run deletes the release after saving the DB.

### Ollama Cloud is the default cloud LLM, with the runner as an option
- **Ollama Cloud:** uses the same `/api/chat` code path, with bearer auth (sent only over https). It's fast (about 2 s per job with `gpt-oss:20b`), but the resume, profile and job text go to Ollama's servers.
- **Ollama on the runner:** keeps all data on the runner, but is CPU-only and slow.

Model and think level come from env overrides (`JOBHUNT_LLM_MODEL`, `JOBHUNT_LLM_THINK`), so one `profile.yaml` serves both the laptop and the cloud, and its `profile_hash` doesn't change.

### Quota errors are not scoring failures
When Ollama Cloud answers 402/429 (or a 4xx naming a usage limit, quota or credits), scoring stops for the run. The remaining jobs stay unscored, with no `score_failed` row, so a later run picks them up. The count is stored in `runs.quota_skipped` and shown at the top of the digest. These jobs are never escalated to the paid model automatically, because a quota problem isn't a sign the job needs a stronger model.

### Old descriptions are dropped, scores are kept
Descriptions of jobs inactive for 30+ days are set to NULL at the end of each run. `content_hash` and the scores stay, so a job that reappears gets its description back on the next fetch and isn't rescored.

### Run output stays out of the Actions log
The workflow redirects `jobhunt run` output to a file on the runner. Errors still reach the user through Telegram.

### Personal files keep their final newline
GitHub trims the final newline of a secret. `write-files` adds it back, because `profile_hash` hashes the exact bytes of `profile.yaml`. Without it, every laptop score looked stale in the cloud and every filter row was re-evaluated.

## Applying (Phases 3 and 4)

See [APPLY.md](APPLY.md) for the flow.

### jobhunt fills, you submit
Code never clicks a submit button, never presses Enter in a field (implicit submission), and never calls `submit()`. Choices are made with `fill`, `check`, `select_option`, `set_input_files`, clicks on combobox options, and Ashby's Yes/No buttons. As a second line of defence, a capture-phase listener on `window` cancels every `submit` event, and every click on a submit-like button, while jobhunt is filling. It's switched off before the page is handed to you. The offline tests fail if any submit event, Submit click or non-GET request happens.
- **Where:** `apply/form.py` (`GUARD_ON_JS`, `GUARD_OFF_JS`), `tests/test_apply_forms.py`

### Application state lives in its own laptop DB
`cloud pull-db` replaces `data/jobhunt.db` wholesale, so anything the laptop wrote there would be lost. Decisions and applications go in `data/applications.db`. Job ids refer to `jobhunt.db`.
- **Where:** `apply/store.py`

### Button presses: read at the start of every run, newest wins
Nothing listens to Telegram live. Telegram keeps unread updates for 24 hours, so presses are read with `getUpdates` at the start of every cloud run (before the "already ran today" guard, so the 12:17 catch-up also reads them) and at the start of `jobhunt apply`. Whoever reads an update stores it and confirms it with a higher offset, so it isn't read again.
- **Storage:** keyed by job; a decision with a higher `update_id` replaces the old one. A replay of the same press changes nothing, so the cloud's copy (it comes down with `pull-db` and is merged on the laptop) can't undo a newer terminal pick.
- **Feedback:** `answerCallbackQuery` is tried but usually fails, because the query is too old by then. The button spins for a few seconds on the phone. The message's keyboard is then edited to put a ✓ on the chosen button.
- **Only your chat:** presses from any other chat id are ignored.
- **Where:** `decisions.py`

### One Telegram message per job
Inline keyboards attach to a message, so each job gets its own message with its own buttons. The header is the only message that notifies; the rest use `disable_notification`, so a digest makes one ping. Each job is marked sent as soon as Telegram accepts its message, so a failure halfway resends only the rest.

### Apply settings live in answers.yaml, not profile.yaml
`profile_hash` hashes `profile.yaml`, and changing it marks every score stale. `max_per_session` and the writing model have nothing to do with scoring, so they live in the laptop-only `answers.yaml`, with defaults in code.

### Answers depend on where the job is
"Are you authorized to work here?", "Will you need sponsorship?" and the expected-salary currency depend on the job's country. The job's location is classed as India (any Indian city), abroad (any other named place), or unclear ("Remote", empty). Unclear means the field is left for you, never guessed.
- **Where:** `answers.job_country`

### Choice fields need a clear match
An option is picked only if its words equal the answer's, or it's the only option containing all the answer's words, or (for a Yes/No answer) the only option starting with Yes/No. "No, I don't have a disability" finds "No, I do not have a disability and have not had one in the past". "1" is never mapped to a range like "Below 2 yrs".
- **Where:** `answers.pick_option`

### Long labels are matched on their last sentence
Lever often puts a paragraph before the question ("…information about race, disability or veteran status is never used… Please select your gender:"). Matching the whole text would hit the wrong rule, so labels over 120 characters are matched on their last sentence.

### The cover letter is checked, retried once, then flagged
A second LLM pass lists claims in the letter that the resume and facts don't support. If it finds any (or the length is off), the letter is regenerated once with those claims named. If claims remain, the letter is flagged: `jobhunt apply` opens it in your editor before upload, and your edited version is treated as reviewed (a hash of the letter in `meta.json` detects the edit) and re-rendered to PDF.
- **Why not more retries:** with `gpt-oss:20b` at `think: low`, a second retry rarely removes the last claim, and higher thinking levels took over 3 minutes per letter.
- **Schema:** the letter is one string with blank lines between paragraphs. `gpt-oss` returned a single item for a list-of-paragraphs schema every time.
- **Where:** `materials.write_letter`, `prompts/cover_letter.md`, `prompts/check_claims.md`

### Sensitive questions skip the LLM entirely
A question matching the sensitive patterns (pay, phone, email, authorization, visa, sponsorship, EEO and demographics, notice period) is never drafted, even when `answers.yaml` lacks the value. The model never sees those values, so it could only return an empty answer or guess one. The field stays red with the answers.yaml key to add, and the session lists the missing keys once at the end. Older drafts for such questions are ignored too.
- **Where:** `answers.SENSITIVE`, `answers.sensitive_key`, `apply/form.to_draft`, `apply/form.missing_keys`

### The writing model sees no contact, pay, authorization or EEO data
Those answers are only typed into forms. The writing model gets the resume, the `facts` list, current role, years, education and work preferences.
- **Where:** `answers.llm_context`

### Forms are found by their ATS's own URL first
Greenhouse URLs in the jobs DB often point at the company's own careers site. jobhunt first opens the ATS's hosted form (Greenhouse also has an embed URL that works when the hosted page redirects), then the job's URL, looking in every frame for the ATS's form. If none is found, the page still opens with everything ready to copy.
- **Where:** `apply/session.open_form`, `apply/greenhouse.py`

### One scanner, small adapters
Each adapter gives only its question-container and label selectors (plus Ashby's Yes/No selector) and its form URLs. A single scanner finds each container, its kind and options, and tags it, so filling never depends on positions.

### The automated browser isn't disguised
The browser identifies itself as automated. Hiding that (removing the automation flag) would be evading bot detection, which the CAPTCHA rule forbids. The cost: CAPTCHA challenges may appear more often, and you solve them.

## Repository hygiene

### A pre-commit hook checks staged lines against your personal files
Personal values live only in git-ignored files, so the hook reads them at commit time instead of keeping a list anywhere. It checks only added lines, reports the matching setting rather than the value, and skips values too generic to mean anything (under 4 characters, whole numbers under 100). A tracked company name is allowed if it's in the example file or already in the committed tree, because names like "Writer" collide with code.
- **Ceiling:** a value committed once with `--no-verify` counts as "in the tree" afterwards (for company names only).
- **Where:** `hooks.py`, `jobhunt dev install-hooks`

## Known limits

- **Small-model scoring is rough.** `qwen3.5:4b` on a laptop is fast enough and free, but some scores are still too high or too low. Calibrate it against the paid model with `jobhunt calibrate --n 15`.
- **Big companies often have no public board.** Many large employers run their own careers sites or Workday. These go on the `manual_check` list until Phase 2 adds email-alert parsing.
- **A board's slug doesn't prove whose board it is.** Common names can belong to an unrelated organisation with the same name. `jobhunt company add` reports the job count; check a few titles before relying on a new board.
- **"Software Engineer, Machine Learning" still doesn't match "Machine Learning Engineer".** It reorders to "Machine Learning Software Engineer". Ignoring "software" would fix that, but would also let "Software Engineer, Enterprise AI" match "AI Engineer", so it was left out. Add the exact title to `target_titles` if you need it.
- **Ollama Cloud's free allowance isn't documented precisely.** When it runs out, jobs wait for a later run (see "Quota errors are not scoring failures").
- **The runner LLM option is untested.** The workflow steps exist, but the timing on a CPU runner hasn't been measured.
- **Excluded keywords are literal.** See "Excluded keywords match whole words" above.
- **Assisted applying covers Greenhouse, Lever and Ashby only.** Other forms open with everything ready to copy. See the gaps in [APPLY.md](APPLY.md#limits-and-known-gaps).
