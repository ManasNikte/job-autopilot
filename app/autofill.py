"""Open the application in a real browser and fill it from your profile, page after page.

Flow:
1. Opens the posting. If it is a job page with an "Apply now" button instead of a form (e.g. Stripe's
   careers site), clicks Apply and follows it, including into a new tab or an embedded form frame.
2. Waits while you sign in or create an account (never fills pages with a password field).
3. Uploads the tailored resume and cover letter, then Claude answers every question from your master
   prompt (profile/system_prompt.md + answers.md + applicant.json + learned answers). See form_fill.py.
4. Keeps watching: when you click Next and a new page or step appears, it fills that one too.
5. Learns from what you change, and marks the job applied when the confirmation page appears.

It NEVER clicks Next, Continue, or Submit. Close the window when you are done.

Usage: python -m app.autofill <job_id>     (the dashboard's "Autofill" button runs this)
"""
from __future__ import annotations

import sys
import time

from . import db

_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:5.1f}s] {msg}", flush=True)


def main(job_id: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed: pip install playwright && python -m playwright install chromium")
    from . import form_fill as ff

    job = db.get_job(job_id)
    if not job:
        sys.exit(f"No job {job_id}")
    url = job.get("apply_url") or job["url"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        ff.status(page, "Job Autopilot: looking for the application form…")
        page.wait_for_timeout(2500)

        # 1. Job page -> application form (at most 3 hops: listing -> apply page -> form).
        for _ in range(3):
            kind = ff.page_kind(page)
            if kind["form"] or kind["password"]:
                break
            nxt = ff.click_apply(page)
            if not nxt:
                break
            log("Clicked the site's Apply button.")
            page = nxt
            ff.status(page, "Job Autopilot: clicked Apply. Waiting for the application form to load…")
            for _ in range(15):                    # embedded forms (iframes) load a few seconds later
                if ff.page_kind(page)["form"]:
                    break
                page.wait_for_timeout(1000)

        state = {"extracted": [], "filled": {}, "uploaded": False, "submitted": False, "warned_signin": False}
        fill_new_fields(page, job, state)
        print("Review each page and click Next / Submit yourself; new pages fill automatically. "
              "Close the window when you are done.", flush=True)
        watch(page, ctx, job, state)
        try:
            browser.close()
        except Exception:
            pass


def fill_new_fields(page, job: dict, state: dict) -> None:
    """Fill whatever is new on the current page (first page, next step, or a newly revealed question)."""
    from . import form_fill as ff

    kind = ff.page_kind(page)
    if kind["password"]:
        if not state["warned_signin"]:
            ff.status(page, "Job Autopilot: sign in or create your account on this page yourself. "
                            "I'll fill the application as soon as the form appears.")
            print("Sign-in page: waiting for you to sign in.", flush=True)
            state["warned_signin"] = True
        return
    state["warned_signin"] = False
    # Start only on a real application form (not a job page's search or newsletter box); once the
    # application has begun, every later step counts, even pages without name/email fields.
    if not kind["form"] and not state["extracted"]:
        return

    uploaded = []
    if not state["uploaded"]:
        ff.status(page, "Job Autopilot: found the application form. Uploading your tailored resume and cover letter…")
        log("Application form found; uploading documents.")
        uploaded = ff.upload_documents(page, job.get("resume_pdf") or "", job.get("cover_pdf") or "")
        if uploaded:
            state["uploaded"] = True
            page.wait_for_timeout(2500)   # portals parse the resume and pre-fill fields

    ff.status(page, "Job Autopilot: reading the questions on this page (including dropdown options)…")
    extracted = ff.extract(page)
    if not extracted:
        return
    ff.status(page, f"Job Autopilot: Claude is answering {len(extracted)} question(s) from your profile (about 15 seconds)…")
    log(f"Found {len(extracted)} new question(s) on {page.url[:80]}")
    try:
        answers = ff.plan(job, [f for _, f in extracted], page.url)
    except Exception as e:
        ff.status(page, f"Job Autopilot: couldn't get answers from Claude ({e}). Fill this page yourself.")
        print(f"Claude form reading failed: {e}", flush=True)
        return
    ff.status(page, f"Job Autopilot: filling {len(answers)} answer(s)…")
    log("Claude answered; filling.")
    res = ff.apply(page, extracted, answers)
    state["extracted"] += extracted
    state["filled"].update(res["values"])
    ff.banner(page, res["needs"], res["drafted"])
    log(f"Filled {len(res['filled'])} field(s)" + (f" and uploaded {', '.join(uploaded)}" if uploaded else ""))
    if res["needs"]:
        print("Left blank for you:\n  - " + "\n  - ".join(res["needs"]), flush=True)


def watch(page, ctx, job: dict, state: dict) -> None:
    """Poll every 2s until the window closes: follow new tabs, fill new pages, learn, detect confirmation."""
    from . import form_fill as ff

    last = {"vals": {}}
    while True:
        try:
            if page.is_closed():
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not open_pages:
                    break
                page = open_pages[-1]
            elif len(ctx.pages) > 1 and ctx.pages[-1] is not page and not ctx.pages[-1].is_closed():
                page = ctx.pages[-1]           # the portal opened the next step in a new tab
            snap = ff.snapshot(page)
            if snap["vals"]:
                last = snap
            kind = ff.page_kind(page)
            if snap["done"] or kind["done"]:
                if not state["submitted"]:
                    state["submitted"] = True
                    db.update_job(job["id"], status="applied", applied_at=db.now())
                    db.resolve_interventions(job["id"], ("submit",), "submitted (confirmation page seen)")
                    ff.status(page, "Job Autopilot: application submitted. Marked as applied. You can close this window.")
                    print("Confirmation page detected: marked as applied.", flush=True)
            elif kind["untagged"] or kind["password"] or (kind["form"] and not state["uploaded"] and kind["file"]):
                page.wait_for_timeout(1200)    # let the new step finish rendering
                fill_new_fields(page, job, state)
            page.wait_for_timeout(2000)
        except Exception:
            if all(pg.is_closed() for pg in ctx.pages):
                break
            continue
    learned = ff.learn(job, state["extracted"], state["filled"], last, state["submitted"])
    if learned:
        print("Learned for next time:\n  - " + "\n  - ".join(learned), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
