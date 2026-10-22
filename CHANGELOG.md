# Changelog

Your dashboard shows the newest section here as "What's new" when an update is available.
Format: `## <version> (<date>)` followed by short, user-facing bullets.

## 3.13 (2026-10-22)
- **Help chat.** A Help button in the corner of every page opens a small chat. Ask anything about the app or describe a problem, like "why weren't my emails scanned?". It answers from the latest guides and release notes, and it checks your app's current state (last run, email checks, recent errors, pauses) to tell you what's going on and how to fix it. It runs on your own Claude seat, and nothing is saved.

## 3.12 (2026-10-22)
- **Active applications by role family:** a new line chart, one line per family, showing applications you've sent that haven't been rejected yet, day by day. Hover or tap to see every family's count that day. It sits next to the pipeline, and Posted today has its own full row again.
- **Updates install together, then show what's new.** When several updates are waiting, the banner says so and one click installs them all. After the restart, a short walkthrough visits each new feature on its page, and the banner lists the notes for every version you skipped.

## 3.11.4 (2026-10-22)
- **Fix type instead of Undo and Ignore.** On the Email page, each email has a Fix type menu. If Claude got it wrong (say, an online screening marked as an interview), pick what it really is: received, rejected, assessment, interview, offer, or not about a job. The job's status and Needs you follow. Assessments also get a Done button.
- **Three role-family donuts in one row:** applied, rejected, and assessments, with each family in the same color on all three. Posted today now sits beside the pipeline.
- **Apple Mail inboxes are read in full.** A busy university inbox gets more than 600 messages in two weeks, and the first read stopped there, missing older job emails. The limit is now 5,000, and one message Mail can't open no longer stops the rest.
- **Screening questionnaires are recognized** ("You are invited to our screening process").

## 3.11.3 (2026-10-22)
- **Forward job emails to your connected inbox.** An email you forward ("Fwd:") from your own address is now read, for example an assessment that landed in a school or work inbox the app can't reach. Claude reads the original message inside it.
- **Assessment game invitations are recognized,** such as "invitation to complete" or game-based tests.
- **More accurate due dates.** Claude now reads more of each email, because long tracking links no longer use up the space. A forwarded email's "Date:" line is no longer mistaken for a deadline.

## 3.11.2 (2026-10-22)
- **An expired Claude sign-in no longer fails your jobs.** When Claude Code's sign-in expires ("OAuth session expired"), the app now pauses, keeps the jobs waiting, and puts one item in Needs you that says how to sign in again. Before, every job screened during that time was marked as an error. Sign in again with `claude` → `/login`, then click Run now.

## 3.11.1 (2026-10-22)
- **The intro is now a complete walkthrough.** It visits every page in 25 short steps (the dashboard, Needs you, jobs, applied, assessments, email, people, your profile and resume template, sources, each settings page, search, and How it works), moving from page to page on its own. Back, Next, Skip, and the arrow keys work throughout. Already using the app? Replay it from **Settings → Getting started → Replay the tour**.

## 3.11 (2026-10-22)
- **Resumes that look right for every profile, automatically.** Empty sections are left off (no bare "Achievements" heading when you have none), and long rows wrap instead of overlapping: the contact line, coursework, skills, and titles or tech stacks that ran into their dates. The checks also ask Claude to shorten any row that doesn't fit on one line.
- **Pick an ATS-friendly resume template** in Profile → Resume template: Classic (new grads), Projects first, Experience first (2+ years), or Compact (a lot to say). All are one column with standard headings. "Preview with my resume" shows each one with your own content.
- **Optional prompt upgrade.** The same page can add the new resume rules to your master prompt (leave empty sections off, pick the most relevant material when there's too much, keep rows on one line). If your prompt already works for you, skip it. Your current prompt is saved first, and Profile → Earlier versions brings it back anytime.
- **How it works:** a new page (Overview → How it works) explains the six steps, what the app does for you, and what only you do.
- **The online dashboard gets the new charts** (the week day by day with a week picker, Time saved, the role-family donut, the pipeline) and pages for jobs, email updates, and requests.
- Saving a profile file twice in the same second no longer overwrites the earlier backup. Setup now asks new users to pick a template from previews of their own resume.

## 3.10.3 (2026-10-22)
- **The first run of the day catches up.** It looks back a day further than usual (48 hours with the default 24), and at least to the last successful run, so jobs posted after yesterday's last run, overnight, or while your computer was off aren't missed (up to 7 days). Later runs that day use your normal window. The activity log says when it's catching up. Turn it off in Settings → Pipeline and limits.

## 3.10.2 (2026-10-21)
- The activity log now says what happens after each screen, not just Claude's verdict: "APPLY WITH RISK (6/10), held: below your 7/10 threshold (no documents)", "APPLY (8/10), building documents", or "DON'T APPLY (3/10), skipped". A job under your threshold no longer looks like it's getting documents.

## 3.10.1 (2026-10-21)
- **Windows fixes** from a real Windows 11 install: assessment dates no longer crash the map and greeting; due times work without a time-zone database (and `tzdata` is installed); text is read and written as UTF-8, so "–" and "·" no longer turn into `â€“`; no terminal window flashes for each Claude call when the dashboard runs in the background.
- **H-1B matching:** "Ernst & Young" now matches USCIS's "ERNST AND YOUNG" (also Johnson & Johnson, Marsh & McLennan, and others). Existing data is updated once, automatically.
- The setup guide (CLAUDE_SETUP.md) walks Windows users through PATH, license prompts, the Claude CLI path, power settings, the USCIS command-line import, and checking the online account; and asks non-software job seekers about their target roles and seniority first.
- Tests no longer depend on your own search settings.

## 3.10 (2026-10-21)
- **This week's applications, day by day.** Sunday to Saturday, with the count on top of every day, today highlighted, and the week's total, daily average, change from the week before, and best day. Pick any earlier week with ‹ › or the menu.
- **Time saved.** A new card estimates the hours the app saved you: reading job descriptions to judge fit, tailoring documents, filling forms, and sorting emails.
- **Applied by role family** is now a donut chart with a labeled legend; the pipeline chart shades stages from early to outcome and shows each stage's share.
- The list of jobs that can't go on the map has pages.
- On laptops and desktops the sidebar slides with the arrow above Overview; the menu button stays for narrow windows.

## 3.9 (2026-10-21)
- **Settings are separate pages:** Schedule, Pipeline and limits, Email inboxes, Online dashboard, App updates, and Getting started, each with its own address and tabs at the top. Older links such as /settings#cloud still open the right page.
- **Assessments has its own page**, with the ones still to finish and the ones you finished or dismissed.
- **Search the app from the sidebar** (⌘K or Ctrl+K): pages, sub-pages, and individual settings such as "minimum pay", "Claude usage", "Gmail", or "run times". Picking a setting opens its page and highlights it.

## 3.8 (2026-10-21)
- **A sidebar instead of the top menu.** Every page and its sub-pages are in one compact list: job filters (Ready to submit, In progress, …), email filters (Assessments, Interviews, Not matched, …), your profile files, and each Settings section. The group you're in opens on its own, and the arrows fold the others.
- Collapse it to an icon rail with the menu button or **Collapse**; it remembers your choice. In a narrow window it slides in as a drawer (Esc or a tap outside closes it).
- The top bar keeps search, status, Run now, and the theme switch.

## 3.7.1 (2026-10-21)
- Fixed: jobs already waiting to be screened (after a restart, an update, or a pause) now start screening on their own within a few minutes. The Background panel no longer asks you to click "Screen them now".
- Fixed: a screening backlog no longer blocks in-app updates.

## 3.7 (2026-10-21)
- **Screening runs to the end on its own.** No more clicking "Screen them now": after each batch (60 by default), the next batch starts by itself until every new job is screened.
- **It respects your Claude limit.** Before Claude work in the background, the app reads your real plan usage from Claude Code. At 95% of your 5-hour session or weekly limit (adjustable in Settings), it pauses and resumes on its own after the reset, so your seat always has room left for you. Form autofill is never paused.
- The Background panel shows your current Claude usage and when it resets.

## 3.6 (2026-10-21)
- **Your online dashboard account is set up from this computer.** Settings → Online dashboard (or **Set it up** on the dashboard) has one short form, pre-filled from your profile. Choose a password, type the emailed code, and you're connected. On the site you just sign in.
- The online dashboard's address is on the GitHub repo page and at the top of the README, in case you lose it.
- The computer's name defaults to its real name (for example "Ava's MacBook Air").

## 3.5.3 (2026-10-21)
- New **Applied this week** tile (weeks run Sunday to Saturday). The dashboard now has eight tiles in an even 4 × 2 grid, and so does the online dashboard.

## 3.5.2 (2026-10-21)
- Maps: amber (assessment) and red (rejected) pins are drawn on top, so a big city pin can't hide them.
- README and project report refreshed with new screenshots.

## 3.5.1 (2026-10-21)
- Fixed: searching the Email page showed "Internal Server Error".
- "See the email" (Needs you and the Assessments card) now opens that exact email, with the text saved from it.

## 3.5 (2026-10-21)
- **Assessments on the map.** The Submitted map has a new Assessments filter: pending assessments show as amber pins, and the pin's popup says when each is due. Assessments from companies you applied to outside the app are placed at the company's location.

## 3.4 (2026-10-21)
- **Online assessments, tracked properly.** A new dashboard tile counts assessments still to finish, and an "Assessments to finish" card lists each one with its exact due time (for example "due Sun Oct 25, 8:41 PM"), read from the email, with Done and Not doing it buttons.
- Assessments from companies you applied to outside the app are included too, and they land on Needs you.
- **Reminders:** a desktop notification every morning (from 9 AM) while assessments are pending, and another 3 hours before each is due (macOS). The welcome message names every pending assessment, soonest first.
- An assessment closes itself when the company later invites you to interview, sends an offer, or declines.
- The online dashboard shows the count and the list with due times.

## 3.3 (2026-10-21)
- **A guided tour for new users.** The first time you open the dashboard, a short walkthrough highlights each part (Run now, your numbers, Needs you, Jobs, the Chrome extension, Applied, Email, Profile, Settings) and explains what it does. Skip it anytime, or replay it from Settings → Getting started.

## 3.2 (2026-10-21)
- **Email updates stay in sync on your phone.** Undoing a status change, ignoring an email, or matching it to a job by hand now shows up on the online dashboard too, not only brand-new emails.
- The online dashboard marks emails whose status change you undid, and ones not matched to a job yet.

## 3.1 (2026-10-20)
- **One-click updates.** When a new version is on GitHub, the dashboard says so, shows what's new, and updates itself (your profile, data, and settings are kept).
- **Online dashboard:** sign in with more than one email (for example personal and university). Add the other address under Account.
- **Online dashboard:** password rules now match the server, so a password the site accepts always works.
- Settings → Online dashboard: choose the city shown to the site operator, or leave it blank to keep it private.
- Fixes and polish across the dashboard.

## 3.0 (2026-10-20)
- Responsive layout for phones, tablets, and 32-inch screens or TVs.
- Online dashboard: check progress from your phone and start a run or an email check on your computer.
- Secure sign-in with emailed 6-digit codes for sign-up and password reset.
