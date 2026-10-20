# Hosting the online dashboard

The online dashboard lets people check their Job Autopilot progress from a phone, tablet, or TV, and ask
their computer to start a run or check email. It costs nothing to host:

| Piece | Free service | What it does |
|---|---|---|
| Website | GitHub Pages (the `docs/` folder) | Static HTML/CSS/JS. Sign in, sign up, reset password, dashboard |
| Accounts + database | Supabase (free tier) | Email/password auth with emailed 6-digit codes, Postgres with row-level security, realtime |
| Email delivery | Gmail SMTP, Brevo, or Resend | Sends the sign-up and password-reset codes |

Each computer connects **out** to Supabase every 30 seconds while it's awake. Nothing on the internet can
connect in. If **Wake every N minutes** is on in Settings, a Mac wakes briefly on that schedule to pick up
requests from the phone.

You only do this once, as the site operator. Everyone else just creates an account on your site and
connects their computer in **Settings → Online dashboard**.

---

## 1. Create the Supabase project

1. Sign up at https://supabase.com and click **New project**. Pick the region closest to your users and
   save the database password in your password manager. The app never needs it.
2. Open **SQL Editor → New query**, paste the whole of [`schema.sql`](schema.sql), and click **Run**. It
   creates the tables, row-level security policies, rate limit, and account-deletion function. Running it
   again is safe, and it's how you apply updates to the schema.

## 2. Auth settings (Authentication in the left sidebar)

**Sign In / Providers → Email**
- **Enable email provider:** on. **Confirm email:** on.
- **Secure email change** and **Secure password change:** on.
- **Minimum password length:** `10`. **Password requirements:** *Lowercase, uppercase letters and digits* (the site checks the same
  rule before it submits).
- **Email OTP expiration:** `900` seconds (15 minutes). **Email OTP length:** `6`.

**Emails → Templates.** The site asks for a 6-digit code, not a link, so each template must contain
`{{ .Token }}`:

- **Confirm signup**
  - Subject: `Your Job Autopilot code`
  - Body: `<p>Your confirmation code is <b>{{ .Token }}</b>. It expires in 15 minutes.</p>`
- **Reset password**
  - Subject: `Reset your Job Autopilot password`
  - Body: `<p>Your password reset code is <b>{{ .Token }}</b>. It expires in 15 minutes. If you didn't ask for this, ignore this email.</p>`

**URL Configuration**
- **Site URL:** `https://YOUR-GITHUB-USERNAME.github.io/job-autopilot/`
- **Redirect URLs:** add the same address.

**Rate limits:** the defaults are fine. They cap how many codes and sign-ins one address can request.

## 3. Email delivery (custom SMTP)

Supabase's built-in mailer only sends a couple of emails an hour, and only to your own team. Turn on
**Authentication → Emails → SMTP Settings → Enable custom SMTP** and pick one:

| Provider | Host / port | Username | Password | Notes |
|---|---|---|---|---|
| Gmail | `smtp.gmail.com` / `465` | your Gmail address | an app password from https://myaccount.google.com/apppasswords | About 500 emails a day; sender is your Gmail address |
| Brevo | `smtp-relay.brevo.com` / `587` | your Brevo SMTP login | your SMTP key | 300 a day free; verify a single sender email, no domain needed |
| Resend | `smtp.resend.com` / `465` | `resend` | your API key | Needs a domain you own, verified in Resend |

Set **Sender name** to `Job Autopilot`. Then create a test account on the site to make sure the code
arrives. Check the spam folder the first time.

## 4. Connect the website and the app to the project

In **Project Settings → API**, copy the **Project URL** and the **anon public** key. The anon key is
meant to be public: every table is locked down by row-level security, so it can only reach the signed-in
user's own rows. **Never put the `service_role` key anywhere in this repo.**

- `docs/config.js`:

  ```js
  window.JA_CONFIG = {
    supabaseUrl: "https://YOUR-PROJECT.supabase.co",
    anonKey: "YOUR-ANON-PUBLIC-KEY",
  };
  ```

- `config/cloud.json` (copy [`config/cloud.example.json`](../config/cloud.example.json)): the same URL and
  key, plus `site_url`, the GitHub Pages address.

Commit both files. Anyone who clones the repo then joins your shared site automatically. If someone wants
their own site, they repeat this guide with their own project.

## 5. Publish the website (GitHub Pages)

On GitHub, open the repo's **Settings → Pages**. Under **Build and deployment**, choose **Deploy from a
branch**, branch `main`, folder `/docs`, then **Save**. After a minute the site is live at
`https://YOUR-GITHUB-USERNAME.github.io/job-autopilot/`. GitHub Pages serves it over HTTPS.

## 6. Optional: get an email when someone joins

**Easiest (no extra service):** add yourself as the operator (step 7) and connect Gmail on your own computer
(**Settings → Email**). Your Job Autopilot then checks for new sign-ups and newly connected computers every
minute while it's awake, and emails you a short summary from your own Gmail to your own Gmail. It uses the
app password you already saved, which Gmail also accepts for sending.

**Instant, even while your computer sleeps:** use Resend from the database instead, as below.

[`notify.sql`](notify.sql) emails you when someone creates an account (name, email, whether they opted in to
updates) and when someone connects a new computer. It uses Resend's free API, called from the database.

1. Create a free account at https://resend.com and an API key with *sending access*. Without a domain,
   Resend only delivers to your own address, which is all this needs.
2. In the SQL editor, store the key and your address in Supabase Vault (encrypted; never in the repo):

   ```sql
   select vault.create_secret('re_your_key_here', 'resend_api_key');
   select vault.create_secret('you@example.com', 'operator_email');
   ```

3. Paste and run [`notify.sql`](notify.sql).

A failed notification never blocks a sign-up. To see everyone at any time, look at **Authentication →
Users**. To list the people who agreed to updates, run:

```sql
select u.email, p.display_name, p.opt_in_at
from public.profiles p join auth.users u on u.id = p.id
where p.marketing_opt_in order by p.opt_in_at desc;
```

Only email people who ticked that box, and include a way to stop in every message.

## 7. Optional: see how many people use your site

Add your own account as the site operator (once, in the SQL editor, after you've created your account):

```sql
insert into public.operators (user_id)
select id from auth.users where email = 'you@example.com'
on conflict do nothing;
```

The footer of your local dashboard then shows **👥 users · 🟢 active**. *Users* counts confirmed accounts, not
counting extra sign-in emails. *Active* counts people who applied to at least one job in the last 14 days.
Nobody else sees it: the operators table can't be read or changed through the API, and the counts come back
empty for every other account.

---

## Security summary

- **Passwords** are hashed by Supabase Auth (bcrypt). Sign-up and reset require a 6-digit code emailed to the
  address, so an account can't be created for someone else's email. Error messages don't reveal whether an
  email is registered.
- After a password reset, every other signed-in device is signed out.
- **Sessions** use Supabase's PKCE flow. The site has a strict Content-Security-Policy (scripts only from
  itself and one pinned, integrity-checked supabase-js file), refuses to be framed, and renders all data as
  text, never as HTML.
- **Row-level security** on every table: a user reads and writes only their own rows. The one exception is
  **extra sign-in emails**: an owner can add up to 5 of their other addresses. Each is its own confirmed account
  with its own password; it can read the owner's rows and queue requests, but can't change data or add emails. A trigger stops anyone
  from sending a request to a computer that isn't theirs, and limits requests to 30 an hour.
- **Computers** keep only a renewable refresh token in the OS keychain, never the password. They accept
  exactly four requests (run, process the queue, check email, resume), ignore anything older than 6 hours,
  and run them through the same code as the local buttons.
- **Data minimization:** only counts, job metadata, and one-line email summaries are uploaded. Resumes,
  cover letters, the master prompt, email bodies, and contacts never leave the computer.
- **Deletion:** disconnecting a computer deletes its rows, and **Delete my account** on the site removes the
  account and everything stored for it.
