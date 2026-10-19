# Set up Job Autopilot with Claude Code

This file lets **Claude Code** install and configure Job Autopilot on your computer: macOS (where it was
built), Windows, or Linux. You run one command; Claude does the rest and asks you before anything that needs
your password, your files, or a decision.

## For you (the human): one command

**Before you start:** install [Claude Code](https://claude.com/claude-code) and sign in with a Claude Pro,
Max, Team, or Enterprise seat. The app uses that seat for every AI call, so there's no API bill.

```bash
# macOS / Linux
curl -fsSL https://claude.ai/install.sh | bash
```

```powershell
# Windows (PowerShell)
irm https://claude.ai/install.ps1 | iex
```

Then run `claude`, type `/login`, finish in the browser, and type `/exit`.

**The one command.** Paste it into Terminal (macOS/Linux) or PowerShell (Windows):

```bash
claude "Set up Job Autopilot on this computer. Read https://raw.githubusercontent.com/ManasNikte/job-autopilot/main/CLAUDE_SETUP.md and follow the 'Instructions for Claude Code' section step by step."
```

Claude asks for permission before running commands, and asks you questions when it needs your background,
your work authorization, or a password-protected step. Plan on 15–30 minutes, most of it filling in your profile.

**What you'll do by hand, because these steps need a person:**

1. Approve Claude's commands as it works.
2. Give Claude your resume or LinkedIn text, so it can fill in your master prompt.
3. Load the Chrome extension: four clicks, and Claude tells you which ones.
4. Optional: download the USCIS H-1B data in your browser and upload it on the Sources page. The government
   site blocks automated downloads.

---

## Instructions for Claude Code

You are setting up **Job Autopilot** (https://github.com/ManasNikte/job-autopilot): a local Flask app that
finds new job postings, screens them with Claude through the user's own Claude Code login, and builds
tailored resume and cover-letter PDFs. Work through the steps in order. After each step, check that it worked
before moving on. Keep the user informed in plain language.

### Ground rules

- **Ask before** anything that needs admin rights (sudo, an elevated PowerShell), installs system packages,
  changes power or wake settings, or creates background services. Explain what the step does in one sentence.
- **Never** sign in to websites, accept license agreements, or enter passwords on the user's behalf. Tell them
  what to do.
- The user's personal data stays local. `profile/` (except `profile/examples/`), `data/`, and `.env` are
  gitignored. Never commit or push anything from this setup.
- Use the project's virtual environment for every Python command: `.venv/bin/python` on macOS/Linux and
  `.venv\Scripts\python.exe` on Windows. Calling it directly avoids PowerShell's activation-script policy.
- If a step fails, read the error, fix the cause, and retry. Only ask the user when the fix needs them.

### Step 1: Detect the platform

Find out the OS and package manager, and choose the matching path in every later step:

| OS | How to tell | Package manager | Project folder |
|---|---|---|---|
| macOS | `uname` prints `Darwin` | Homebrew (`brew`); install it from https://brew.sh if missing, after asking | `~/job-autopilot` |
| Windows | PowerShell; `$env:OS` is `Windows_NT` | `winget` | `$HOME\job-autopilot` |
| Linux | `uname` prints `Linux`; read `/etc/os-release` | `apt` (Debian/Ubuntu), `dnf` (Fedora/RHEL), or `pacman` (Arch) | `~/job-autopilot` |

On macOS, don't use `~/Downloads`, `~/Documents`, or `~/Desktop` as the project folder. macOS blocks
background jobs there.

### Step 2: Install prerequisites (only what's missing)

Required: **Python 3.11 or newer**, **git**, and **Google Chrome** (for the autofill extension). Check each with
`python3 --version` (Windows: `py -3 --version`), `git --version`, and whether Chrome is installed.

| | Python 3.12 | git |
|---|---|---|
| macOS | `brew install python@3.12` | `brew install git` (or `xcode-select --install`) |
| Windows | `winget install -e --id Python.Python.3.12` | `winget install -e --id Git.Git` |
| Debian/Ubuntu | `sudo apt install -y python3 python3-venv python3-pip` | `sudo apt install -y git` |
| Fedora | `sudo dnf install -y python3 python3-pip` | `sudo dnf install -y git` |
| Arch | `sudo pacman -S --needed python python-pip` | `sudo pacman -S --needed git` |

On Windows, open a new PowerShell window after installing, so `PATH` picks up the new tools.

### Step 3: Get the code

```bash
# macOS / Linux
git clone https://github.com/ManasNikte/job-autopilot.git ~/job-autopilot && cd ~/job-autopilot
```

```powershell
# Windows
git clone https://github.com/ManasNikte/job-autopilot.git "$HOME\job-autopilot"; Set-Location "$HOME\job-autopilot"
```

If the folder already exists and is a clone of this repo, run `git pull` instead.

### Step 4: Create the virtual environment and install dependencies

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

```powershell
# Windows
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

On Linux, Playwright may also need system libraries: `.venv/bin/python -m playwright install-deps chromium`.
That command uses sudo, so ask first.

### Step 5: Make sure the resume font is available

The PDFs use **Times New Roman**. The app looks for it in the standard font folders, and falls back to
**Liberation Serif**, which has identical letter widths.

- **macOS and Windows:** Times New Roman is built in. Nothing to do.
- **Linux:** check with `fc-list | grep -i -E "times new roman|liberation serif"`. If neither is listed:
  - Debian/Ubuntu: `sudo apt install -y fonts-liberation`. For true Times New Roman, the optional
    `ttf-mscorefonts-installer` package asks the user to accept Microsoft's EULA, so let the user decide and
    accept it themselves.
  - Fedora: `sudo dnf install -y liberation-serif-fonts`. Arch: `sudo pacman -S --needed ttf-liberation`.

### Step 6: Connect Claude (the user's seat)

1. Confirm the CLI works and is signed in: `claude -p "Reply with OK"` should print OK. If it says the user
   isn't logged in, ask them to run `claude`, then `/login`, then `/exit`.
2. Create the settings file: copy `.env.example` to `.env`. Keep `LLM_BACKEND=auto`. The app uses the Claude
   Code CLI when no API key is set.
3. If the app can't find `claude` later, put its full path in `.env` as `CLAUDE_CLI_PATH=...`. Get the path
   from `command -v claude` on macOS/Linux or `(Get-Command claude).Source` on Windows. On Windows, prefer the
   native installer's `claude.exe` over an npm `claude.cmd` shim.

### Step 7: Create the user's profile (the important part)

On first run, the app copies templates from `profile/examples/` into `profile/`. Trigger that now by running
the check in Step 8, or copy the files yourself. Then fill them in together with the user:

1. **`profile/system_prompt.md`** is the master prompt: a complete template with every rule for screening,
   resumes, cover letters, and the one-page layout already written.
   - Ask the user for their resume (PDF or text), their LinkedIn "About" and experience text, and links to
     their best projects (README files are ideal).
   - Fill **Section I** (contact, education, every role as a fact list with real numbers, skills by evidence
     level, certifications, dated achievements, and work authorization) and **Section I-B** (one entry per
     project).
   - Fill the **Positioning Doctrine**, **Experience Math**, the **Experience Selection Rule**, and the project
     **Selection Rule**.
   - Search for `[` to find the remaining placeholders. Use only facts the user gives you. Where a number is
     unknown, write `PENDING`; never invent one.
   - Keep every section heading. The app refers to them (Section I, I-B, III-0, IX-V, XI, XV).
2. **`profile/applicant.json`**: name, email, phone, location, links, school, start date, and the fixed form
   answers (work authorization, sponsorship, EEO). For EEO questions, use exactly what the user chooses,
   including "Decline to self identify."
3. **Work authorization:** if the user does **not** need visa sponsorship, set `"require_h1b_signal": false` in
   `config/feeds.json`, and simplify GATE 1, WORK AUTHORIZATION, and SECTION XV in the prompt. The H-1B
   features then stay out of the way.
4. **Search settings:** adjust `config/search.json` (role families, title exclusions, `max_years_required`) and
   `config/companies.json` (curated company boards) to the user's targets. Pay floor, match threshold, and run
   times can be changed later on the dashboard's **Settings** page.

### Step 8: Verify

```bash
# macOS / Linux
.venv/bin/python -m app.cli check
.venv/bin/python -m pytest -q
```

```powershell
# Windows
.venv\Scripts\python.exe -m app.cli check
.venv\Scripts\python.exe -m pytest -q
```

`check` must report the Claude backend, load the prompt, get a reply from Claude, and build a test PDF. All
tests should pass. A Linux machine that has only Liberation Serif still passes.

### Step 9: Run the dashboard, and start it automatically at login

First run it in the foreground once and open http://127.0.0.1:5055:

```bash
.venv/bin/python -m app.cli serve          # macOS / Linux
```

```powershell
.venv\Scripts\python.exe -m app.cli serve  # Windows
```

Port 5055 is fixed, because the Chrome extension talks to it. Stop the server with Ctrl+C, then install the
always-on service for the user's OS.

**How scheduled runs work on each OS.** The daily run times live in `config/schedule.json` and can be changed on
the **Settings** page.

- **macOS:** launchd starts each run, even when the dashboard is closed.
- **Windows and Linux:** the dashboard's built-in clock starts each run while the dashboard is running. A run
  missed while the computer slept (up to 90 minutes) starts as soon as it wakes. That's why the dashboard
  should start at login.

#### macOS

```bash
./scripts/install_schedule.sh       # daily-run agent + always-on dashboard service (starts at login)
sudo ./scripts/install_wake.sh      # optional: wake the Mac 2 minutes before each run, even with the lid closed
```

The second command asks for the user's Mac password, so run it only after the user agrees. Tell them to keep
the Mac plugged in overnight; macOS shortens lid-closed wakes on battery.

#### Windows

Create a per-user task that starts the dashboard at logon, hidden, and restarts it if it stops (no admin
rights needed):

```powershell
$root = "$HOME\job-autopilot"
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" -Argument "-m app.cli serve" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "JobAutopilot Dashboard" -Action $action -Trigger $trigger -Settings $settings -Force
Start-ScheduledTask -TaskName "JobAutopilot Dashboard"
```

Optional, with the user's OK: wake the PC from sleep 2 minutes before each run. Create one task per time in
`config/schedule.json`, each with a wake timer, that does nothing except wake the machine:

```powershell
$times = (Get-Content "$HOME\job-autopilot\config\schedule.json" | ConvertFrom-Json).times
foreach ($t in $times) {
  $at = ([datetime]::ParseExact($t, "HH:mm", $null)).AddMinutes(-2)
  $trigger = New-ScheduledTaskTrigger -Daily -At $at
  $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName "JobAutopilot Wake $($t.Replace(':',''))" -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c exit") -Trigger $trigger -Settings $settings -Force
}
```

Wake timers must be allowed in Power Options. The user can check with `powercfg /waketimers`, and allow them
under Control Panel → Power Options → Change plan settings → Advanced → Sleep → Allow wake timers. If the run
times change on the Settings page, re-run this block. Remove tasks with
`Unregister-ScheduledTask -TaskName "JobAutopilot*" -Confirm:$false`.

#### Linux (systemd)

A user service starts the dashboard at login and restarts it if it stops:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/job-autopilot.service <<EOF
[Unit]
Description=Job Autopilot dashboard
After=network-online.target

[Service]
WorkingDirectory=%h/job-autopilot
ExecStart=%h/job-autopilot/.venv/bin/python -m app.cli serve
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now job-autopilot.service
```

To keep it running when the user isn't logged in, `sudo loginctl enable-linger $USER` (ask first).

Optional, with the user's OK and sudo: a system timer that wakes the machine 2 minutes before each run.
Write one `OnCalendar=` line per time in `config/schedule.json`, minus 2 minutes:

```bash
sudo tee /etc/systemd/system/job-autopilot-wake.service >/dev/null <<'EOF'
[Unit]
Description=Wake for Job Autopilot
[Service]
Type=oneshot
ExecStart=/bin/true
EOF
sudo tee /etc/systemd/system/job-autopilot-wake.timer >/dev/null <<'EOF'
[Unit]
Description=Wake the computer before Job Autopilot runs
[Timer]
OnCalendar=*-*-* 07:58:00
OnCalendar=*-*-* 14:58:00
WakeSystem=true
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now job-autopilot-wake.timer
```

`WakeSystem=` needs hardware support for RTC wake alarms. Check with `systemctl list-timers job-autopilot-wake.timer`.

### Step 10: Chrome extension (the user clicks; you explain)

Tell the user:

1. Open `chrome://extensions` in Chrome and turn on **Developer mode** (top right).
2. Click **Load unpacked** and choose the `extension` folder inside the project (give them the full path).
3. Pin **Job Autopilot** from the puzzle-piece menu.
4. Open applications from a job's **Open application** button on the dashboard, then click **Autofill** in
   the side panel. It fills the form; the user always presses Submit.

### Step 11: Optional extras

- **USCIS H-1B data** (only if the user needs sponsorship): ask them to download the yearly CSVs from
  https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub in their browser, then upload them on
  the dashboard's **Sources** page. Several files can go up at once. The site blocks scripted downloads, so
  don't try to fetch it yourself.
- **Track application replies by email:** on the dashboard's **Settings → Email**, the user connects Gmail with an
  app password they create themselves at https://myaccount.google.com/apppasswords, and/or Outlook / Microsoft 365.
  Outlook works through their own Azure app registration (5 steps listed in the card), or on macOS through
  Apple's Mail app when their organization blocks app registration. Never type passwords for them. Install the
  extra packages if they're missing: `pip install msal keyring`.
- **Try the UI with sample data first:** `python scripts/demo_seed.py`, then serve with
  `JOB_AUTOPILOT_DATA=demo/data JOB_AUTOPILOT_PROFILE=demo/profile` (Windows: set those as environment
  variables first). On Windows/Linux, use a different `PORT` for the demo if the real dashboard is running.

### Step 12: Finish

Give the user a short summary:

- The dashboard address: http://127.0.0.1:5055
- When the next scheduled run is (shown on the Settings page)
- How runs start on their OS, and whether wake-from-sleep was set up
- Anything left for them to do: the extension, the USCIS import, and any `PENDING` facts in their prompt
- How to start their first run: **Run now** on the dashboard, then work through the **Needs you** page

### Troubleshooting

| Problem | Fix |
|---|---|
| `Address already in use` on port 5055 | Another copy is running. macOS: `launchctl list \| grep jobautopilot`; Windows: `Get-ScheduledTask "JobAutopilot*"`; Linux: `systemctl --user status job-autopilot` |
| `Claude CLI not found` | Set `CLAUDE_CLI_PATH` in `.env` (Step 6) |
| `Claude CLI is not logged in` | Have the user run `claude`, then `/login` |
| `Times New Roman (R) font file not found` | Install a font (Step 5) |
| Usage limit reached | Work pauses and resumes at the reset time automatically; **Resume now** on the dashboard lifts the pause early |
| No internet during a run | The app pauses and retries every 5 minutes; nothing is marked failed |
| PowerShell blocks `Activate.ps1` | Don't activate; call `.venv\Scripts\python.exe` directly |
| Scheduled run didn't happen (Windows/Linux) | The dashboard must be running at that time, or wake within 90 minutes; check the Background page's activity log |
