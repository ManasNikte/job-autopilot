# Job Autopilot: Project Report

**A local, AI-powered pipeline that finds, screens, and prepares job applications on its own,
including overnight with the laptop lid closed, so the candidate's time goes only to the decisions that need a person.**

| | |
|---|---|
| **Type** | Personal tool, built end to end (backend, LLM integration, PDF rendering, browser extension, scheduling) |
| **Stack** | Python, Flask, SQLite, Claude (Claude Code CLI or Anthropic API, with web search), reportlab, Leaflet maps, Chrome extension (MV3), Playwright, macOS launchd/pmset |
| **Size** | About 8,000 lines across the app, extension, and templates · 57 automated tests |
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
   If there is no network, the run fails loudly instead of "finishing" in one second.
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

The dashboard opens with today's numbers (**applied today**, ready to submit, waiting on your call) and **two
maps**: one pins every job that's ready to submit, and the other pins submitted applications. The second map
switches between **Today** (applied, moved forward, or rejected today) and **All time**, and three toggle chips,
**Applied**, **Interviewing / offer**, and **Rejected**, show any mix, such as only rejections or only active
applications. Jobs that can't be pinned (remote, country-wide) are listed under the map, never dropped. One
glance shows where the search is concentrated, where it's working, and where the open opportunities are.

![Application maps](docs/screenshots/maps.png)

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
| `app/web.py` + templates | Dashboard (KPIs, maps, charts), Jobs, Needs you, People, Background, Settings, and a profile/prompt editor with version history |
| `extension/` | Chrome MV3 side panel: matches the open page to a job, uploads the tailored PDFs, answers questions from the profile, learns from edits, never submits |
| `scripts/` | launchd schedule installer, the pmset wake helper, and a demo-data seeder |

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
- **Usage-limit resilience.** Limit errors are detected from Claude's response. All workers pause until the
  reset time, and the work resumes where it left off. A manual click resumes early if the limit was reset.
- **Cheap online verification.** Sponsor lookups use a small task-specific system prompt (not the 180 KB
  master prompt) plus web search. Each company is looked up once, positive and negative answers are cached
  for 90 days, and a per-run budget caps usage.
- **Maps without API keys.** Leaflet with Esri's gray canvas tiles (light and dark versions swap with the
  theme) and OpenStreetMap geocoding within its usage policy. Rejected applications get their own pin, so
  they never hide active ones.
- **Clear boundaries.** Submitting applications and sending LinkedIn invites stay manual. Forms carry legal
  attestations and CAPTCHAs, and LinkedIn prohibits automation. Sites behind a login or bot protection
  (Indeed, Handshake, LinkedIn) are not scraped. Only public APIs and public data are used.

## 6. Results so far (first five days)

- **1,102** relevant postings tracked, **457** screened by Claude, **92** tailored document packages, **75**
  applications submitted, and the first application already **moved forward to interviews**
- **~4,100** company boards and **~160,000** postings swept per run in about 4 minutes
- **42,984** USCIS sponsor records loaded, **183** sponsors learned from feeds and screens, and **47** companies
  verified online
- **55** job locations geocoded and pinned on the dashboard maps
- **57** automated tests, including Chromium-driven form-fill tests and regression tests for real failures:
  sponsorship-refusal wording, pay parsing, and PDF word-spacing leaks

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
- **Real data beats more rules.** Loading the USCIS export released 164 held-back jobs in one step. Matching
  employer names without spaces ("JPMorganChase" vs "JPMORGAN CHASE & CO") recovered more. Web lookups covered
  the rest.

## 8. Possible next steps

- Track applicant counts and reply rates per source, and use them to rank boards
- Color map pins by match score, and add a heat layer for the cities with the most open roles
- An email follow-up draft (Outlook) with the tailored PDFs attached, for contacts the candidate already has
- A weekly digest of rejection reasons, to show which profile gaps cost the most roles

---

*Screenshots and setup instructions are in the [README](README.md). The screenshots use the built-in demo data with a fictional candidate.*
