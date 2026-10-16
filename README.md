# Job Autopilot

**A local, AI-powered job-application pipeline.** Twice a day it finds new US roles at companies that
sponsor H-1B visas. Claude screens each posting against your own "master prompt" (your background plus
your rules). For the good matches it writes a tailored one-page resume and cover letter, rendered straight
to PDF and checked automatically. A Chrome extension fills the application form, and you press Submit.
Anything that needs your judgment lands in one **Needs you** inbox.

Everything runs on your own machine at `http://127.0.0.1:5055`. Nothing is hosted.

![Dashboard](docs/screenshots/dashboard.png)

> 📄 **[Project report](PROJECT_REPORT.md)**: the problem, how it works overnight, time saved, architecture, and lessons learned.
>
> Screenshots use the built-in demo data (`scripts/demo_seed.py`). The candidate, "Jordan Lee", is fictional.

---

## Highlights

- **Multi-source job discovery, about 4,000 company boards.** Public Greenhouse, Lever, Ashby, Workday, and
  SmartRecruiters APIs for your curated sponsors, plus a *board directory* built daily from every company
  board linked in the public new-grad and internship lists (SimplifyJobs, speedyapply, vanshb03). Titles
  are checked first, and full descriptions are fetched only for new matches, so a full sweep of about 160K
  postings takes about 4 minutes. Jobright and SimplifyJobs feeds and Hacker News "Who is hiring?" posts
  are included too. Duplicates across boards are collapsed.
- **H-1B signal for every company.** First from your USCIS H-1B Employer Data Hub import (tens of thousands
  of employers, with names matched even when they're spelled differently). Then from sponsors the app learns
  from Jobright, Simplify, HN, and Claude's own screens. For anything still unknown, Claude runs a **short web
  lookup** on public H-1B disclosure data, once per company, cached for 90 days and capped per run.
  Employers that say they don't sponsor are filtered out. Wording like "unable to offer visa sponsorship"
  counts as a refusal, never as a signal.
- **Rule prefilter before any LLM call.** Drops stale postings, senior titles, roles outside the US,
  4+ years asked, explicit "no sponsorship / citizens only / clearance" wording, and **listed pay below
  your floor** (default $100K a year; ranges are parsed from ATS fields or the description, and hourly pay
  is annualized).
- **Claude screening with structured outputs.** Every job gets a verdict (APPLY / APPLY WITH RISK /
  DON'T APPLY), a harsh match score, eligibility gates, and a recruiter's six-second read, all as
  schema-validated JSON.
- **Tailored documents with automatic quality checks.** The resume and cover letter are drawn directly
  to PDF with reportlab. Each bullet's line fill is measured, and the page count, word count, and dash
  rules are checked. Failures go back to Claude as *patch-style* revisions, which change only the lines
  that failed. Documents are built only above your match threshold (default 7/10).
- **Chrome extension (Manifest V3) for autofill**, similar to Jobright. It works on Greenhouse, Lever,
  Ashby, Workday, iCIMS, and other portals. It uploads the tailored PDFs and answers every question from
  your profile, including EEO presets. It **learns from the answers you edit**, so it asks less over time,
  and it marks the job applied when the confirmation page appears. It never clicks Submit.
- **Referral engine.** Import your networking spreadsheet (`.xlsx`), or add, edit, and delete people. When a
  job at their company is ready, the app drafts a personal referral text for that exact role, with a
  one-click "Text them" link.
- **Runs while you sleep.** macOS launchd agents run the pipeline at the times you choose. An optional
  root helper schedules `pmset` wake-ups so runs happen even when the lid is closed. If Claude's usage
  limit is hit, work pauses and resumes at the reset time, or right away with **Resume now** if you reset
  your limit early.
- **Dashboard.** KPIs (including **applied today**), **maps of where you're applying**, a 14-day
  applications chart, a pipeline funnel, live per-job progress bars with an honest "not started / running /
  paused" status, server-side pagination and search everywhere, and dark, light, and system themes.
- **Application maps.** One map pins every job ready to submit. A second map pins submitted applications,
  with a **Today / All time** switch (today = applied, moved forward, or rejected today) and three toggle chips,
  **Applied**, **Interviewing / offer**, and **Rejected**, to show any mix (double-click a chip for only that
  status). Locations are geocoded once through OpenStreetMap and cached. Building or street names fall back to
  the company's location, and remote or country-wide jobs are listed under the map. Clicking a pin lists its jobs.

## How it works

```
 ┌──────────────┐   ┌────────────┐   ┌──────────────┐   ┌────────────────────┐   ┌─────────────┐
 │ Job sources  │──▶│ Prefilter  │──▶│ Claude screen│──▶│ Claude tailor      │──▶│ Needs you   │
 │ ATS APIs +   │   │ rules, no  │   │ verdict +    │   │ resume + cover JSON│   │ review, ask │
 │ feeds (2×/d) │   │ LLM cost   │   │ match score  │   │ → PDF → checks →   │   │ referral,   │
 └──────────────┘   └────────────┘   └──────────────┘   │ patch revisions    │   │ submit      │
                                                        └────────────────────┘   └──────┬──────┘
                                                                                        ▼
                                              Chrome extension autofills the form; you click Submit
```

| Layer | Tech |
|---|---|
| Backend | Python 3.11+, Flask, SQLite (WAL, thread-local connections), background worker thread pool |
| LLM | Claude via the headless Claude Code CLI (works with a Claude Team or Enterprise seat) or the Anthropic API with prompt caching, plus JSON-schema structured outputs |
| Documents | reportlab (direct PDF, justified text, live links), pypdf for verification |
| Browser | Chrome extension (MV3, shadow-DOM side panel), Playwright for the optional in-app autofill and tests |
| Scheduling | macOS launchd (user agents plus a root LaunchDaemon for `pmset` wake) |
| Maps | Leaflet with Esri gray canvas tiles (light and dark), OpenStreetMap Nominatim geocoding (cached) |
| Tests | pytest (57 tests, including Chromium-driven form-fill tests) |

## Screenshots

**Maps**: jobs ready to submit, and submitted applications (here: all time, with Applied, Interviewing / offer,
and Rejected all switched on; rejected pins are red).

![Application maps](docs/screenshots/maps.png)

**Needs you**: the only page you have to work through. Referral asks come first, then risky-fit
decisions, facts only you know, and applications ready to submit.

![Needs you](docs/screenshots/needs-you.png)

**Jobs**: status tabs, search, source, match, and sort filters, and the documents for each job.

![Jobs](docs/screenshots/jobs.png)

**Job detail**: the fit verdict, eligibility gates, recruiter teardown, interview prep, and the referral
for this role.

![Job detail](docs/screenshots/job-detail.png)

| Generated resume (one page, measured line fill) | People and referrals |
|---|---|
| ![Resume sample](docs/screenshots/resume-sample.png) | ![People](docs/screenshots/people.png) |

| Background runs and schedule | Settings (schedule, lid-closed wake, pay floor, sponsor lookups) |
|---|---|
| ![Background](docs/screenshots/background.png) | ![Settings](docs/screenshots/settings.png) |

| Sources (feeds, ~4,000-board directory, USCIS import) | Outreach notes |
|---|---|
| ![Sources](docs/screenshots/sources.png) | ![Outreach](docs/screenshots/outreach.png) |

| Profile and master-prompt editor | Light theme |
|---|---|
| ![Profile](docs/screenshots/profile.png) | ![Light theme](docs/screenshots/dashboard-light.png) |

---

## Quick start

> 🤖 **Let Claude Code set it up for you (macOS, Windows, or Linux).** With Claude Code installed and signed in, run
> this one command and follow along:
>
> ```bash
> claude "Set up Job Autopilot on this computer. Read https://raw.githubusercontent.com/ManasNikte/job-autopilot/main/CLAUDE_SETUP.md and follow the 'Instructions for Claude Code' section step by step."
> ```
>
> Details: [CLAUDE_SETUP.md](CLAUDE_SETUP.md). The manual steps below are the macOS path.

Requires Python 3.11+ and Google Chrome for the extension. Built on macOS; Windows and Linux are supported too (they use the dashboard's built-in scheduler — see [CLAUDE_SETUP.md](CLAUDE_SETUP.md)).

```bash
git clone https://github.com/ManasNikte/job-autopilot.git ~/job-autopilot
cd ~/job-autopilot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Try the UI with demo data (no Claude needed)

```bash
python scripts/demo_seed.py
JOB_AUTOPILOT_DATA=demo/data JOB_AUTOPILOT_PROFILE=demo/profile python -m app.cli serve
```

Open http://127.0.0.1:5055. The demo lives in `demo/` (gitignored) and never touches your real data.
Don't click **Run now** in demo mode unless Claude is set up.

### 1. Add your profile

On first run, the app copies the placeholder templates from `profile/examples/` into `profile/`. Your
real files stay **gitignored**, so they never reach the repository:

| File | What to put there |
|---|---|
| `profile/system_prompt.md` | **Your master prompt.** It starts from a complete, battle-tested template: every rule for fit verdicts, harsh scoring, ATS checks, one-page resume layout, cover letters, and the recruiter teardown is included. You fill in your profile (Section I), your project library (Section I-B), and the bracketed placeholders (search for `[`). Claude reads it fresh on every call. |
| `profile/applicant.json` | Contact details and fixed form answers (work authorization, start date, EEO) for autofill |
| `profile/answers.md` | Facts you confirm on **Needs you** are appended here automatically |
| `profile/company_aliases.json` | Optional company-name fixes for your networking spreadsheet |

You can also edit all of these on the dashboard's **Profile** page. It keeps a backup of every version.

### 2. Connect Claude (pick one)

**Option A: Claude Code CLI (uses a Claude Pro, Max, Team, or Enterprise seat; no API bill)**

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude        # type /login, sign in, then /exit
```

Keep `LLM_BACKEND=auto` in `.env`. The app runs `claude -p` headlessly with your prompt as a system
prompt file.

**Option B: Anthropic API key.** Set `ANTHROPIC_API_KEY=...` in `.env`. The master prompt is
prompt-cached, so after the first call each request pays about 10% for it.

Check the setup:

```bash
python -m app.cli check
```

### 3. Run it

```bash
python -m app.cli serve
```

Open http://127.0.0.1:5055, click **Run now**, then work through **Needs you** from top to bottom.

### 4. Install the Chrome extension

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and choose the `extension/` folder.
3. Open an application from a job's **Open application** button, then click **Autofill** in the side panel.

The extension only talks to your local app (`127.0.0.1:5055`).

### 5. Schedule daily runs (macOS, optional)

```bash
./scripts/install_schedule.sh          # runs at the times in config/schedule.json (default 8:00 and 15:00)
sudo ./scripts/install_wake.sh         # optional: wake the Mac for each run, even with the lid closed
```

Change the run times on the **Settings** page. The launchd agents and wake-ups follow automatically.

## Configuration

| Where | What |
|---|---|
| **Settings** page / `config/settings.json` | Run times, match threshold for building documents, minimum pay, online sponsor lookups per run, screening unknown-sponsor jobs, per-run caps, parallelism, effort levels, posting age |
| `config/companies.json` | Your curated company boards (`greenhouse`, `lever`, or `ashby` plus slug) |
| `config/search.json` | Role families, title exclusions, visa-blocker phrases, location rules |
| `config/feeds.json` | Aggregator feeds, the board directory's source lists, and the "require an H-1B signal" switch |
| `.env` | Claude backend, models, port (Settings overrides `.env`) |

**Import the USCIS H-1B data (recommended).** Download the yearly CSVs from the
[USCIS H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub) in your
browser and upload them on the **Sources** page. Several files can be uploaded at once. Directory jobs are
queued only for known sponsors, so this data covers most companies.

## What stays manual, and why

- **Submitting applications.** Forms include legal attestations (work authorization, EEO) and often
  CAPTCHAs, and an auto-submitted wrong answer can't be taken back. The app does everything up to the
  final click.
- **LinkedIn invites and messages.** LinkedIn's User Agreement prohibits automation, and accounts that
  automate get restricted. The app drafts the notes and opens the people search. You send them.
- **No scraping of logged-in or bot-protected sites** (LinkedIn, Indeed, Handshake). Jobs come from public
  APIs and public feeds only, and the H-1B data comes from your own USCIS download plus public web search.

## Project layout

```
app/
  sources/      job-board and feed fetchers (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, feeds,
                board directory)
  salary.py     pay-range parsing and the minimum-pay filter
  h1b.py        USCIS import, learned sponsors, cached online sponsor lookup
  geo.py        location → place splitting and cached geocoding for the maps
  prefilter.py  rule gate before any Claude call
  llm.py        Claude via Claude Code CLI or the Anthropic API; structured JSON; usage-limit pause
  pipeline.py   fetch → screen → tailor → render → check → revise → queue human steps
  render/       reportlab resume and cover-letter builders, plus PDF checks
  form_fill.py  form extraction, Claude answers, fill, learn-from-edits (shared by the extension)
  contacts.py   networking-sheet import, job matching, referral texts
  worker.py     background thread pool with pause and resume
  web.py        Flask dashboard and the extension API
extension/      Chrome extension (side panel, autofill, popup)
profile/examples/  placeholder profile templates (your real profile is gitignored)
prompts/        app adapter: how the app asks Claude for JSON
scripts/        launchd schedule, wake helper, demo seeder
tests/          pytest suite (fixture uses a fictional candidate)
```

## Tests

```bash
python -m playwright install chromium   # once, for the form-fill tests
python -m pytest
```

## Privacy

The database, generated documents, logs, and your profile live in `data/` and `profile/`. Both are
gitignored. Claude receives your master prompt and the job description; sponsor lookups send only a company
name. The map geocoder sends only place names such as "Austin, TX". The browser extension talks only to your
local server.
