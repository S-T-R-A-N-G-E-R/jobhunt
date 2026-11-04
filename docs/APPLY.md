# Applying with jobhunt

jobhunt prepares and fills the application; **you press Submit**. It opens the real form in a visible browser on your laptop, fills what it can, attaches your resume and cover letter, highlights what it couldn't do, and hands the page to you. It never clicks Submit, never solves a CAPTCHA, and never makes up an answer about you.

Discovery (fetch, score, digest) keeps running in the cloud. Applying runs on your laptop, because it needs a visible browser and you at the keyboard.

## Setup (once)

1. Install the browser Playwright drives:
   ```bash
   uv run playwright install chromium
   ```
2. Copy the answer bank and fill it in:
   ```bash
   cp config/answers.example.yaml config/answers.yaml
   ```
   It holds your contact details, work details (notice period, CTC, authorization), preferences, education, the US-style EEO answers, the path of your resume PDF, and a list of `facts` you're happy to state. It's git-ignored.
3. Put your resume PDF where `files.resume` points (default `data/resume.pdf`, git-ignored). Keep `data/resume.md` in sync with it: that text is what the cover letter may draw on.
4. Optional: `OLLAMA_API_KEY` in `.env` for the writing model on Ollama Cloud. Without it, the local model writes.

## Daily use

| Command | What it does |
|:--|:--|
| `jobhunt apply` | The one you run. Pulls the cloud DB, reads your button presses, shows the queue, and then works through approved jobs one by one, up to `settings.max_per_session` (default 5). |
| `jobhunt apply --job <id>` | Only this job (naming it approves it). |
| `jobhunt apply pick` | Decide on digest jobs in the terminal: `a` apply, `s` skip, `n` later, `q` quit. |
| `jobhunt apply queue` | Approved jobs not applied to yet. |
| `jobhunt apply status` | Applications by date, plus this week's counts. |
| `jobhunt apply prepare <id>` | Draft answers without opening a visible browser, plus the cover letter if the form takes one. Add `--letter` to write the letter anyway. |

### Choosing jobs

Each job in the digest arrives as its own Telegram message with **✅ Apply** and **⏭ Skip** buttons. Only the first message of a digest makes a sound. Press either button whenever you like. Nothing is listening live, so the button spins for a few seconds and then stops. The press is saved at the next cloud run (twice a day) or the next `jobhunt apply`, whichever comes first, and the message then shows a ✓ on the button you chose. Press the other button later to change your mind: the newest press wins.

### One job, step by step

Every step prints a line as it goes: a spinner while it runs (pulling the DB, reading button presses, opening the browser and the form, writing the letter and drafts with the model named, filling), then ✓ and how long it took. At the end of the session, a table shows the time for each step, including your own review.


1. The form opens in a Chromium window that uses its own profile (`.browser-profile/`, git-ignored), so logins and cookies stay between sessions.
2. The cover letter is written if it doesn't exist yet and the form takes one: a cover-letter upload, a cover-letter text box, or Lever's "Additional information". Pages jobhunt doesn't recognise get one too, for you to paste. Letters go in `output/applications/<company>-<title>-<id>/`. It's checked for claims your resume doesn't support. If the check flags any, the letter opens in your text editor: fix it, save, and press Enter. The PDF is rebuilt from your version.
3. The form is filled. The terminal prints a checklist, and the page marks fields:
   - **red outline:** left empty (not in your answer bank, no option clearly matched, or a consent box that's yours to tick);
   - **orange outline:** filled from a low-confidence draft, so check it.
4. You review, change anything, solve a CAPTCHA if one appears, and click **Submit** yourself.
5. Back in the terminal, press **Enter** (or `s` to skip, `q` to stop the session). jobhunt looks for a confirmation page. If it can't tell, it asks "Did you submit? y/n". Applied jobs get a timestamp, the letter used, and a screenshot of the confirmation (`confirmation.png` next to the letter).
6. At the end, Telegram gets one line: "Session done: X applied · Y skipped · Z this week".

## What's filled, and from where

| Field | Source |
|:--|:--|
| Name, preferred name, email, phone, LinkedIn, GitHub, portfolio | `identity` |
| Location / city / "where are you based" | `identity.city`, `identity.country` |
| Resume upload | `files.resume`, or a `files.variants` entry whose keyword is in the job title |
| Cover letter upload | the generated PDF |
| Cover letter text box, or Lever's "Additional information" when there's no upload | the generated letter |
| Current company / title, notice period, start date, years of experience | `work` |
| Current CTC, expected CTC | `work.current_ctc_lpa`, `work.expected_ctc_lpa` for jobs in India, `work.expected_usd` for jobs abroad. If it's unclear where the job is (e.g. "Remote"), the field is left for you. |
| Authorized to work? Need sponsorship? | from `work`, **depending on where the job is**: India vs abroad. Unclear location = left for you. |
| Relocation, travel, in-office questions | `preferences` (Yes for choice fields, your text for text fields; marked medium confidence) |
| Education (school, degree, field, graduation year, GPA) | `education[0]` |
| "How did you hear about us" | `standard.how_heard` |
| Gender, Hispanic/Latino, race, veteran, disability, pronouns | `standard.eeo` |
| Questions you answer the same way every time | `custom` (exact or near match) |
| Other free-text questions ("Why us?") | an LLM draft grounded in your resume and facts, with a confidence level; low confidence = orange |

**Choice fields** (dropdowns, radios, Yes/No buttons) only get an option that clearly matches your answer: equal words, the only option containing all your answer's words, or the only option starting with Yes/No. Ranges like "Below 2 yrs / 3–7 yrs" aren't guessed.

**Never filled:** consent and acknowledgement checkboxes (privacy notices, arbitration agreements), URL fields you haven't set, anything not covered above.

**Sensitive questions never get a draft.** Questions about pay (CTC, salary, compensation), phone, email, work authorization, visas and sponsorship, EEO and demographics, or notice period are answered only from `answers.yaml`. If the value is missing, the field is left red with the note "add <key> to answers.yaml", and no LLM is called. At the end of the session, the missing keys are listed once, with the companies that asked for them. `apply prepare` lists them too.

**Never sent to an LLM:** your phone, email, CTC, work authorization and EEO answers. The writing model sees your resume, your `facts`, your current role, education and work preferences.

## The writing model

`settings.writing` in `answers.yaml` sets the model for cover letters and drafts: Ollama Cloud `gpt-oss:20b` with `think: low` by default. If it's unavailable (no key, out of credits, invalid output twice), the local model from `profile.yaml` writes instead, and the paid fallback is used only if `profile.yaml` enables it. `meta.json` next to each letter records which model wrote it and which checked it.

## Unrecognised pages

If a job's form isn't a Greenhouse, Lever or Ashby form jobhunt can read (for example a company's own careers site with no ATS iframe), the page still opens, nothing is filled, and the terminal prints your details and the cover letter's path to copy.

## Limits and known gaps

- **Per sitting:** at most `settings.max_per_session` forms (default 5).
- **CAPTCHAs:** the three ATSs load reCAPTCHA or hCaptcha. A challenge may appear when you submit, and solving it is up to you. The browser identifies itself as automated, and jobhunt doesn't hide that, so challenges may come up more often than in your everyday browser.
- **Location autocompletes:** when no suggestion matches exactly, the first suggestion starting with your city is picked, so check it.
- **Phone country pickers** aren't touched; the number is typed with its country code.
- **Multi-page and account-based forms** (Workday, iCIMS, Taleo) aren't supported.
- **Education and work-history repeaters** (add another school or job) are filled for the first entry only.
- **Ranges and numeric inputs:** answers like "1" vs "Below 2 yrs", or "12.5 LPA" in a number-only field, are left for you.
- **Labels jobhunt can't read** (no label, legend or aria-label) are reported as empty.
- **Ashby saves as you type.** Ashby stores field values and uploads the resume as soon as they're entered (an unsubmitted draft on their side), exactly as it does in any browser. Nothing is an application until you click Submit.
- **Uploads are renamed** to `<Your_Name>_Resume.pdf` and `<Your_Name>_Cover_Letter_<Company>.pdf`; your files on disk keep their names.
- **No letter for forms that don't take one:** it isn't written (no LLM calls), so nothing needs reviewing. `jobhunt apply prepare <id> --letter` writes it on demand.
- **Outcome detection** is a best guess (thank-you text or URL). When unsure, you're asked.
