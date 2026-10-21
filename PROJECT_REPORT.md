# Job Autopilot: Project Report

**A local, AI-powered pipeline that finds, screens, and prepares job applications on its own,
including overnight with the laptop lid closed, so the candidate's time goes only to the decisions that need a person.**

| | |
|---|---|
| **Type** | Personal tool, built end to end (backend, LLM integration, PDF rendering, browser extension, scheduling) |
| **Stack** | Python, Flask, SQLite, Claude (Claude Code CLI or Anthropic API, with web search), reportlab, Leaflet maps, Chrome extension (MV3), Playwright, macOS launchd/pmset · online dashboard on GitHub Pages + Supabase (Postgres, Auth, row-level security, realtime) |
| **Size** | About 12,600 lines across the app, extension, website, and SQL · 102 automated tests |
| **Platforms** | The app runs on a laptop or desktop: macOS (primary), Windows, Linux. A hosted online dashboard covers phone, tablet, and TV · one-command setup by Claude Code ([CLAUDE_SETUP.md](CLAUDE_SETUP.md)) · one-click in-app updates |
| **Status** | In daily use since October 2026 |

---

## 1. The problem

An international new-grad job search involves a lot of repetitive, draining screening work:

- **Volume.** Thousands of new postings appear every day across hundreds of company career sites, and the
  best ones get hundreds of applicants within a day. Being early matters.
- **Every posting needs the same checks.** Is it entry level? Is it in the US? Does it require citizenship or
  a clearance? Does the company sponsor H-1B visas? Does the pay clear a floor? Is the required stack a real
  match? Answering this for a 600-word description takes several minutes. Doing it dozens of times a day is
  exhausting, and most of the answers are "no".
- **Tailoring is slow.** A tailored one-page resume and cover letter that stay honest to the candidate's
  record take 45 to 60 minutes to write well by hand.
- **Decision fatigue.** After an hour of reading job descriptions, judgment gets worse, and the
  applications that matter get less attention.

**Goal:** move all repeatable reading, filtering, and drafting to software that runs unattended, and leave
only the human decisions (submit, ask for a referral, answer a fact only the candidate knows) in one short
inbox.

## 2. What it does while you sleep

The pipeline runs on a schedule, by default 8:00 AM and 3:00 PM Eastern, with times you can change. It runs
**even when the Mac is asleep with the lid closed**:

1. **The Mac wakes itself.** A small root LaunchDaemon books a `pmset` wake event two minutes before each run.
   It re-books after every run and whenever the schedule changes, and it keeps the wake time in sync with the
   Settings page.
2. **The run starts headless.** A launchd agent starts the pipeline under `caffeinate`, so the Mac stays awake
   until the run finishes. If the always-on dashboard is running, the run happens inside it with live progress.
   If there is no network, the run fails loudly instead of "finishing" in one second, and a connection that
   drops mid-run pauses the work and retries every 5 minutes. On **Windows and Linux** the same schedule is
   driven by a clock inside the dashboard, which is autostarted at login by Task Scheduler or systemd and
   optionally woken by a wake timer. A run missed while the machine slept (up to 90 minutes) still starts on wake.
3. **About 4,000 company job boards are swept.** That's roughly 160,000 postings, checked in about 4 minutes.
4. **Rules remove most postings before any AI call.** The rules drop stale postings, senior titles, roles
   outside the US, 4+ years of experience required, citizenship or clearance language, explicit "no
   sponsorship" wording, and listed pay below $100K.
5. **Every company's sponsorship is checked.** First against 42,984 employers from the USCIS H-1B Employer Data
   Hub, then against sponsors the app has learned. Otherwise Claude does a short web lookup, once per company,
   and the answer is cached for 90 days.
6. **Claude screens the survivors** against the candidate's own master prompt. Each job gets a verdict (APPLY /
   APPLY WITH RISK / DON'T APPLY), a harsh match score, eligibility gates, and a recruiter's six-second read.
7. **Claude writes the documents for strong matches** (7/10 and up, configurable): a tailored resume and cover
   letter rendered straight to PDF. The app measures every line and checks page fit, word limits, and style
   rules. Failing lines go back to Claude as targeted patches.
8. **The app drafts referral texts** for people the candidate knows at each company, plus LinkedIn notes.
9. **If Claude's usage limit is hit mid-run**, all work pauses and resumes on its own at the reset time. Nothing
   is lost.

**When the candidate wakes up**, the dashboard has today's postings already screened and scored, documents
ready for the best matches, and a **Needs you** inbox with only these items:

- Ask a specific person for a referral (the text is already written)
- Decide on a risky-fit role (build documents or skip)
- Answer a fact only you know (asked once, remembered forever)
- Review and submit (the Chrome extension fills the form; you press Submit)

The dashboard opens with eight tiles in a 4 × 2 grid: new roles today, ready to submit, waiting on your call,
**applied today**, **applied this week** (Sunday to Saturday), applied all time, interviewing, and assessments to
finish. Below them are **two maps**: one pins every job that's ready to submit, and the other pins submitted applications. The second map
switches between **Today** (applied, moved forward, or rejected today) and **All time**, and four toggle chips,
**Applied**, **Interviewing / offer**, **Assessments**, and **Rejected**, show any mix, such as only rejections or
only pending assessments. Jobs that can't be pinned (remote, country-wide) are listed under the map, never dropped. One
glance shows where the search is concentrated, where it's working, and where the open opportunities are.

![Application maps](docs/screenshots/maps.png)

### Replies and deadlines, handled

Employers answer by email, so the app reads the candidate's inboxes (Gmail, Outlook / Microsoft 365, or Apple
Mail), **read-only**. A rule filter drops alerts and newsletters, so only application emails reach Claude. Claude
sorts each one into received, rejected, assessment, interview, or offer. The matching job then updates itself, with
an undo for every change.

**Online assessments get special care, because they have deadlines.** The due time is read from the email
("by 6:00am PT on October 26", "at 8:41 PM EDT") and converted to the candidate's time zone. Each pending
assessment then shows up:

- as a dashboard tile and an **Assessments to finish** card
- in the welcome line ("2 assessments to finish: …, soonest first")
- on **Needs you**
- as an amber pin on the map
- on the phone dashboard

This includes assessments from companies the candidate applied to outside the app, which have no job to match.
A desktop reminder fires every morning, and again 3 hours before each deadline. An assessment closes itself when
the company later invites the candidate to interview, makes an offer, or declines.

![Assessments to finish](docs/screenshots/assessments.png)

### Away from the laptop

The app itself lives on the laptop (`127.0.0.1`). Its layout adapts from a narrow split-screen window to a 32-inch
monitor, but it isn't meant to be opened on a phone. For that there's an optional **online dashboard** that works
on a phone, tablet, or TV. It shows the same progress, jobs, email updates,
and assessments. It can also start a run or an email check on the candidate's computer. Behind it:

- **Free hosting:** GitHub Pages and Supabase.
- **Sign-in:** email and password, confirmed with emailed 6-digit codes. The account is created during setup,
  from the candidate's own computer, which connects it in the same step. On the site they only sign in, and the
  link lives on the GitHub repo page.
- **Isolation:** row-level security, so each account sees only its own rows.
- **Outbound-only computer:** it fetches requests every 30 seconds; nothing on the internet can reach in.
- **Sleeping Mac:** it can wake every 30 minutes to pick up requests from the phone.

New users get a **guided tour** of the dashboard on first launch. When the maintainer pushes a release, every
install shows **Update available** with the release notes and updates itself in one click.

## 3. Time and mental energy saved

These figures come from the app's own database and timing logs over its first five days of use
(October 11–15, 2026).

| Stage | Volume | What it would take by hand | What it takes now |
|---|---|---|---|
| Postings swept per run | ~160,000 across ~4,100 boards | Not feasible manually | ~4 minutes, unattended |
| Rule-filtered jobs stored for review | 645 | ~1–2 min each to skim and reject | 0 minutes |
| Jobs screened in depth by Claude | 457 | ~5 min each to read ~600 words and check fit and visa (~38 hours) | ~22 s each, unattended |
| Rejected with a written reason | 300 of 457 (66%) | The same reading, then the same "no" | 0 minutes; the reason is one click away |
| Tailored resume and cover-letter packages | 92 | ~45–60 min each (~69–92 hours) | ~4–5 min each, unattended, plus a few minutes of review |
| Jobs dropped for listed pay under $100K | 28 | Finding the pay line in each description | 0 minutes |
| Companies with unknown sponsorship checked online | 47 | ~5–10 min each searching H-1B disclosure sites | ~17 s each, cached for 90 days |
| Applications submitted | 75 | Retyping the same answers into every portal | One autofill click, then Submit |

*Manual times are conservative assumptions, not measurements. The automated times are measured medians from the app's
timing table (screen 21.8 s; document draft 252 s plus 40 s per revision round; PDF render 2.1 s; sponsor lookup about 17 s).*

**Estimated savings in the first five days: over 100 hours of screening and drafting.**

The bigger gain is **mental energy**. About two out of three screened jobs are rejected, and the candidate never
reads them. Every rejection still has its reason on record ("requires 5+ years", "JD says it will not sponsor",
"pay below $100K"), so trusting the filter takes one glance, not a re-read. What reaches a person is a short list
of strong, eligible roles with documents already done. Decision fatigue goes down, and each real application
gets full attention.

**Speed matters as well.** The morning run finishes before the candidate's day starts, so same-day postings can be
applied to within hours of going live, while applicant counts are still low.

## 4. Architecture

```
            ┌────────────────────────────────── launchd (8:00 / 15:00) + pmset wake, lid closed ────┐
            ▼                                                                                       │
  Sources (public APIs)          Prefilter (no LLM)      Sponsor check            Claude            │
  Greenhouse · Lever · Ashby ──▶ age · title · US ──▶  USCIS 42,984 ──▶   screen (verdict, ──┐      │
  Workday · SmartRecruiters      years · visa words    learned sponsors      score, gates)   │      │
  Jobright · Simplify · HN       pay floor ($100K)     web lookup (cached)                   ▼      │
                                                                            tailor JSON ─▶  PDF ─▶ checks
                                                                                ▲       (reportlab) │
                                                                                └── patch revisions ┘
                                                                                         │
                     SQLite (WAL) ◀──────────────────────────────────────────────────────┘
                         │
          Flask dashboard · Needs you inbox · People/referrals ──▶ Chrome extension autofill (you submit)
```

| Component | Responsibility |
|---|---|
| `app/sources/` | Public ATS APIs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters) and aggregator feeds. A **board directory** collects about 4,000 company boards from community new-grad lists every day. |
| `app/prefilter.py`, `app/salary.py` | Rule gates, plus pay-range parsing from ATS fields or description text, with hourly and monthly pay annualized |
| `app/h1b.py` | USCIS data import (UTF-16/TSV safe, spacing-insensitive name matching), learned sponsors, and the cached web lookup via Claude with web search |
| `app/geo.py` | Splits messy location strings ("US-CA-Menlo Park", "SF • Seattle (United States)") into places, geocodes each once (Nominatim, 1 request per second, cached), and groups jobs into map pins |
| `app/llm.py` | One function for structured JSON calls through the Claude Code CLI (a Team or Enterprise seat, no API bill) or the Anthropic API with prompt caching. Detects usage limits and pauses work. |
| `app/pipeline.py` | Fetch → prefilter → sponsor check → screen → tailor → render → check → revise → queue human steps |
| `app/render/` | reportlab resume and cover-letter builders with justified text, live links, and per-line fill measurement |
| `app/worker.py` | A thread pool with an atomic job claim, de-duplication, and a pause/resume across usage limits |
| `app/web.py` + templates | Dashboard (KPIs, maps, charts), Jobs, Needs you, People, Background, Settings, and a profile/prompt editor with version history, all reached from one sidebar that lists every page and sub-page (collapses to an icon rail; a drawer in narrow windows) |
| `extension/` | Chrome MV3 side panel: matches the open page to a job, uploads the tailored PDFs, answers questions from the profile, learns from edits, never submits |
| `app/mailsync.py` | Read-only inbox readers (Gmail IMAP, Microsoft Graph, Apple Mail), the application-email filter, Claude classification, job matching, and undo |
| `app/assessments.py` | Due times parsed from assessment emails (time and zone), one pending list (matched or not), Needs you items, and daily and 3-hours-before reminders |
| `app/cloud.py`, `docs/`, `supabase/` | The online dashboard: an outbound-only connector that pushes summaries and runs whitelisted requests; a static site (strict CSP, PKCE sessions); and the Postgres schema with row-level security, rate limits, and operator-only stats |
| `app/updater.py` | In-app updates: checks GitHub every 6 hours, shows CHANGELOG notes, and pulls with `--ff-only --autostash`, keeping the user's settings on conflicts, then restarts in place |
| `scripts/` | launchd schedule installer and pmset wake helper (macOS), and a demo-data seeder |
| `CLAUDE_SETUP.md` | Setup instructions written for Claude Code: OS detection, per-OS prerequisites, profile interview, verification, autostart, wake timers, troubleshooting |

## 5. Engineering decisions worth noting

- **Cheap checks first, expensive checks last.** Titles and dates are checked on all ~160K postings; descriptions
  are fetched only for the survivors; Claude sees only jobs that pass every rule. Sweeping 4,000 boards adds
  minutes, not hours, and no extra AI usage.
- **Structured outputs everywhere.** Every Claude call returns schema-validated JSON (verdicts, documents,
  patches, form answers), so the pipeline never has to parse prose.
- **Patch-style revisions.** When a bullet wraps or a page overflows, only the failing lines go back to Claude.
  The rest of the document stays as it was. Revisions run at low effort and finish in about 40 seconds.
- **The candidate's master prompt is a file, never code.** It's read fresh on every call, edited in the
  dashboard, and versioned. The repository ships only a placeholder template, so personal data never reaches git.
- **Honest by construction.** Claude may use only facts from the profile. Unknown facts become a single question
  in the inbox, and the answer is saved and reused on every future run.
- **Thread-local SQLite connections with WAL** keep the dashboard responsive while a dozen fetch threads write.
  An earlier one-connection-per-query design starved the UI during runs.
- **Stop before the limit, not at it.** Before background Claude calls, the app reads the seat's real usage. It
  asks Claude Code for `/usage` in non-interactive mode and parses the structured `usage_report.rate_limits`, a
  local command that uses no tokens, cached for 90 seconds. At 95% of the 5-hour or weekly limit, everything
  pauses until that window resets, so the candidate's own Claude sessions never hit a wall. Screening batches
  chain on their own until the backlog is empty.
- **Usage-limit resilience.** Limit errors are detected from Claude's response. All workers pause until the
  reset time, and the work resumes where it left off. A manual click resumes early if the limit was reset.
- **Cheap online verification.** Sponsor lookups use a small task-specific system prompt (not the 180 KB
  master prompt) plus web search. Each company is looked up once, positive and negative answers are cached
  for 90 days, and a per-run budget caps usage.
- **Maps without API keys.** Leaflet with Esri's gray canvas tiles (light and dark versions swap with the
  theme) and OpenStreetMap geocoding within its usage policy. Rejected applications get their own pin, so
  they never hide active ones.
- **Setup as instructions for an AI agent, not a script.** Instead of three fragile install scripts, one
  Markdown file tells the new user's own Claude Code what to do on each OS. It also lists what needs a human
  (passwords, license agreements, Chrome clicks) and how to verify every step. The agent adapts to the machine,
  such as a missing Python, a different package manager, or a missing font, which a shell script can't.
- **Portable by design.** OS-specific pieces sit behind small switches: launchd on macOS and a built-in clock
  elsewhere, font discovery across macOS, Windows, and Linux folders (Liberation Serif as the metric-compatible
  fallback), OS-neutral date formatting, and guarded Unix-only signals. The Linux path was verified end to end
  in a clean Debian container: all tests pass, PDFs render, and the dashboard serves every page.
- **Self-healing documents.** A bullet up to 3% too long stays on its line with about 0.03pt tighter letter
  spacing. Pages use the smaller sizes the prompt allows (down to 10pt) before overflowing. A **Fix it** button
  re-fits flagged documents with no model call. In practice that alone fixed every flagged resume so far.
- **Security for the online dashboard:**
  - **Login:** confirmed with emailed codes, and errors never reveal whether an email has an account.
  - **Database:** row-level security on every table, a 30-requests-an-hour limit, and triggers so nobody can
    write rows for a computer they don't own.
  - **Each computer:** connects only outward, keeps a refresh token in the OS keychain (never the password),
    and acts on four whitelisted requests only.
  - **Verification:** the rules were tested in a real Postgres with a second, hostile account before going live.
- **Privacy by default for the operator view.** The site owner sees people's application **counts** and the city
  each person chose to share. Which companies people apply to never leaves their computer. The privacy note
  says exactly what's visible.
- **Sync that can't skip.** Changes sync by last-modified time, with a ">=" cursor plus idempotent upserts.
  Deletes leave tombstones (SQLite triggers), so an undo, ignore, or reassignment on the computer reaches the
  phone, not only new rows.
- **Updates tested against real git repos.** The updater's tests build a throwaway remote and clone. One case
  has the release and the user's settings change the same file. That test caught git reporting success while
  leaving conflict markers in a settings file; the updater now always resolves in the user's favor.
- **Clear boundaries.** Submitting applications and sending LinkedIn invites stay manual. Forms carry legal
  attestations and CAPTCHAs, and LinkedIn prohibits automation. Sites behind a login or bot protection
  (Indeed, Handshake, LinkedIn) are not scraped. Only public APIs and public data are used.

## 6. Results so far (first ten days, October 11–21, 2026)

- **2,429** postings tracked: **1,462** removed by rules with no AI cost, and **967** screened in depth by Claude
- **149** tailored resume and cover-letter packages, and **149** applications submitted
- The first application **moved forward to an interview**. **2** online assessments are tracked, each with its
  exact deadline and daily reminders.
- **66** application emails read from two inboxes, and **57** matched to their job automatically
  (rejections, confirmations, assessments, interview invites)
- **~4,100** company boards and **~160,000** postings swept per run in about 4 minutes
- **42,984** USCIS sponsor records loaded, plus sponsors learned from feeds and verified online
- **97** job locations geocoded and pinned on the dashboard maps
- **102** automated tests, including:
  - Chromium-driven form-fill tests
  - row-level security checked against a real Postgres
  - the updater checked against real git repositories
  - regression tests for real failures: sponsorship-refusal wording, pay parsing, PDF word-spacing leaks,
    same-second sync cursors, an Email-search crash, and due times in PT and EDT
- Runs on **macOS, Windows, and Linux**, with a phone dashboard. Anyone with a Claude seat can set it up with
  **one command**.

## 7. Lessons learned

- **The top of the funnel was the bottleneck, not the model.** Tightening the screen didn't help when only
  about 16 new postings a day reached it. Widening the sources from 89 boards to about 4,000 and adding real
  sponsor data did.
- **A mention isn't a signal.** "Visa sponsorship" in a job description is often the opposite ("unable to offer
  visa sponsorship"). The fix was to count only wording that offers sponsorship, and to treat refusals as
  hard blockers before any AI call.
- **Long-running local automation needs honest status.** Several fixes were about the UI telling the truth:
  "not started" versus "waiting for a worker", a failed offline run versus "done", and a paused queue with a
  one-click resume.
- **Write setup for the agent that will run it.** The person replicating the project already has a capable
  agent. Giving it clear goals, guardrails, and checks per OS was simpler and more robust than maintaining
  install scripts, and it turned a long README into a single command.
- **Real data beats more rules.** Loading the USCIS export released 164 held-back jobs in one step. Matching
  employer names without spaces ("JPMorganChase" vs "JPMORGAN CHASE & CO") recovered more. Web lookups covered
  the rest.

- **Read the deadline, not just the label.** "You have an assessment" was useful; "due Sunday 8:41 PM" is
  actionable. Keeping the time and time zone from the email turned a status into a reminder system.
- **Count what the user actually has, not what the database happened to match.** An assessment from a company
  applied to outside the app was invisible until the list was built from the emails themselves.
- **Shipping updates is a feature too.** Once other people run the app, a safe, one-click update path (and a test
  for the conflict case) matters as much as any new screen.

## 8. Possible next steps

- Track applicant counts and reply rates per source, and use them to rank boards
- Run the Windows setup path on a real Windows machine and fold any fixes back into CLAUDE_SETUP.md
- Color map pins by match score, and add a heat layer for the cities with the most open roles
- An email follow-up draft (Outlook) with the tailored PDFs attached, for contacts the candidate already has
- A weekly digest of rejection reasons, to show which profile gaps cost the most roles
- Push notifications on the phone for assessment deadlines (today they're desktop notifications and a phone card)
- Add assessment links to a calendar with one click

---

*Screenshots and setup instructions are in the [README](README.md). The screenshots use the built-in demo data with a fictional candidate.*
