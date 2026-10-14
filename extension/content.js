// Job Autopilot content script: reads application forms on any portal, asks the local app (Claude +
// your profile) for answers, fills them, and keeps going on the next page of multi-page applications.
// It never clicks Next/Continue/Submit and never touches pages with a password field.
(() => {
  if (window.__jobAutopilot) return;
  window.__jobAutopilot = true;

  const ATS_HOSTS = /greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkday\.com|workday\.com|icims\.com|smartrecruiters\.com|jobvite\.com|taleo\.net|workable\.com|bamboohr\.com|successfactors\.(com|eu)|oraclecloud\.com|eightfold\.ai|rippling\.com|dover\.com|recruitee\.com|breezy\.hr|jazzhr\.com|applytojob\.com|paylocity\.com|ultipro\.com|adp\.com|wellfound\.com|ycombinator\.com/i;
  const APPLY_TEXT = /^\s*(apply|apply now|apply for (this )?(job|role|position)|apply to (this )?(job|role)|start (your )?application|begin application|i'?m interested|apply manually|apply with resume)\s*$/i;
  const DONE_TEXT = /thank you for (applying|your application|your interest)|application (has been |was )?(received|submitted)|we('|’)ve received your application|successfully submitted|application complete/i;
  const ADVANCE_TEXT = /^\s*(submit( application)?|next|continue|save (and|&) continue|review( application)?|send application)\b/i;
  const PREFIX = "ap" + Math.random().toString(36).slice(2, 6) + "_";
  const isTop = window.top === window;

  let counter = 0, running = false, job = null, active = false, matched = false, panelShown = false;
  const fieldsByKey = {};          // key -> field description sent to Claude
  const filledValues = {};         // key -> value we entered (to learn from your edits)
  const draftedKeys = new Set();

  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { error: chrome.runtime.lastError?.message || "no response" })); }
    catch (e) { res({ error: String(e) }); }
  });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const txt = (e) => (e ? clean(e.innerText || e.textContent) : "");
  const norm = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  // ------------------------------------------------------------------ reading the form
  function labelFor(el) {
    let t = "";
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = txt(l); }
    if (!t && el.getAttribute("aria-labelledby"))
      t = el.getAttribute("aria-labelledby").split(/\s+/).map((i) => txt(document.getElementById(i))).join(" ");
    if (!t) t = el.getAttribute("aria-label") || "";
    if (!t) { const l = el.closest("label"); if (l) t = txt(l); }
    if (!t) {   // Workday / custom widgets: label sits in the surrounding form-field container
      const box = el.closest('[data-automation-id^="formField"], [class*="form-field"], [class*="FormField"], .field, [class*="question"], fieldset');
      const l = box && box.querySelector('label, legend, [data-automation-id="formLabel"], [class*="label"]');
      if (l && !l.contains(el)) t = txt(l);
    }
    if (!t) t = el.getAttribute("placeholder") || el.getAttribute("title") || el.name || el.id || "";
    return clean(t).slice(0, 400);
  }
  const shown = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const isRequired = (el) => el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(labelFor(el));
  const passwordPage = () => [...document.querySelectorAll('input[type="password"]')].some(shown);
  // A real application form (vs. a job page that only has a search or newsletter box).
  function applicationForm() {
    if (document.querySelector('input[type="file"]')) return true;
    const inputs = [...document.querySelectorAll('input, textarea, select, button[aria-haspopup="listbox"]')]
      .filter((e) => shown(e) && !["hidden", "search", "submit"].includes(e.type) && (e.tagName !== "BUTTON" || e.getAttribute("aria-haspopup")));
    const strong = inputs.filter((e) => /first|last|full name|e-?mail|phone|linkedin|resume|\bcv\b|address|city/i.test(labelFor(e))).length;
    return strong >= 2 || (strong >= 1 && inputs.length >= 3);
  }

  function menuOptions(el) {
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    let menu = id ? document.getElementById(id) : null;
    if (!menu) {
      const box = el.closest('[class*="select__container"], [class*="select-container"], [class*="Select"], [data-automation-id^="formField"], .field, [class*="question"]');
      menu = box && box.querySelector('[role="listbox"]');
    }
    if (!menu) menu = [...document.querySelectorAll('[role="listbox"]')].find(shown) || null;
    if (!menu) return [];
    return [...menu.querySelectorAll('[role="option"]')].filter(shown).map(txt).filter(Boolean);
  }
  async function closeMenus() {
    document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.activeElement?.blur?.();
    document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    await sleep(120);
  }

  function extract() {
    const out = [], groups = {};
    const nodes = document.querySelectorAll('input, textarea, select, button[aria-haspopup="listbox"], [role="combobox"]:not(input)');
    for (const el of nodes) {
      if (el.dataset.autopilotKey || el.closest("#autopilot-banner, #autopilot-fab, #autopilot-pill")) continue;
      const tag = el.tagName.toLowerCase();
      let type = tag === "select" ? "select" : tag === "textarea" ? "textarea"
        : tag === "button" || (tag !== "input" && el.getAttribute("role") === "combobox") ? "listbox-button"
        : (el.getAttribute("type") || "text").toLowerCase();
      if (["hidden", "submit", "button", "image", "reset", "password", "file", "search"].includes(type)) continue;
      if (el.closest('.g-recaptcha, [class*="recaptcha"]') || el.disabled) continue;
      if (type === "radio" || type === "checkbox") {
        const box = el.closest('fieldset, [role="radiogroup"], [role="group"], [data-automation-id^="formField"], .field, .application-question, [class*="question"]');
        const gname = el.name || (box && box.id) || "grp" + counter;
        const gk = "g:" + gname;
        if (!groups[gk]) {
          let q = "";
          if (box) {
            const lg = box.querySelector('legend, [data-automation-id="formLabel"], label:not(:has(input)), [class*="label"]');
            q = lg ? txt(lg) : txt(box).slice(0, 300);
          }
          if (!q) q = labelFor(el);   // a lone checkbox ("I certify…") is its own question
          groups[gk] = { key: PREFIX + counter++, type, question: clean(q).slice(0, 400), required: isRequired(el), options: [] };
          out.push(groups[gk]);
        }
        const ok = groups[gk].key + "_" + groups[gk].options.length;
        el.dataset.autopilotKey = ok;
        groups[gk].options.push({ key: ok, label: labelFor(el) });
        continue;
      }
      if (!shown(el)) continue;
      const label = labelFor(el);
      const r = el.getBoundingClientRect();
      if (!label.replace(/[*\s]/g, "") && (el.tabIndex < 0 || el.getAttribute("aria-hidden") === "true" || r.width < 6 || el.readOnly)) continue;
      const key = PREFIX + counter++;
      el.dataset.autopilotKey = key;
      const f = { key, type, question: label, required: isRequired(el), value: (el.value || (type === "listbox-button" ? txt(el) : "")).slice(0, 200), options: [] };
      if (type !== "listbox-button" && (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list")) f.type = "combobox";
      if (type === "select") f.options = [...el.options].map((o) => ({ key: "", label: clean(o.text) })).filter((o) => o.label);
      if (f.type === "listbox-button" && /select one|choose|select\.\.\./i.test(f.value)) f.value = "";
      out.push(f);
    }
    return out;
  }

  async function peekOptions(fields) {
    for (const f of fields) {
      if (!["combobox", "listbox-button"].includes(f.type) || f.options.length) continue;
      const el = document.querySelector(`[data-autopilot-key="${f.key}"]`);
      if (!el) continue;
      try {
        await closeMenus();
        el.click();
        await sleep(400);
        f.options = menuOptions(el).slice(0, 80).map((label) => ({ key: "", label }));
      } catch (e) {}
      await closeMenus();
    }
  }

  // ------------------------------------------------------------------ filling
  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);   // React/Vue see the change
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function best(options, value) {
    const v = norm(value);
    return options.find((o) => norm(o) === v) || options.find((o) => v && norm(o) && (norm(o).includes(v) || v.includes(norm(o))));
  }
  function clickLike(el) {
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"])
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  }
  function tokenPick(opts, value) {   // "Austin, TX" vs "Austin, Texas, United States"
    const first = norm(value).split(" ").find((t) => t.length > 2);
    return first && opts.find((o) => norm(o).split(" ").includes(first));
  }
  async function pickFromMenu(el, value) {
    let pick = null, opts = [];
    for (let i = 0; i < 8 && !pick; i++) {      // async lists (location, school search) take a moment
      await sleep(400);
      opts = menuOptions(el);
      pick = best(opts, value) || tokenPick(opts, value);
    }
    const target = pick && [...document.querySelectorAll('[role="option"]')].filter(shown).find((o) => txt(o) === pick);
    if (target) { target.scrollIntoView({ block: "nearest" }); clickLike(target); return true; }
    if (el.tagName === "INPUT") setNative(el, "");   // never pick a random first option
    throw new Error(`no option matches "${value}"`);
  }

  async function fillOne(f, value) {
    const el = document.querySelector(`[data-autopilot-key="${f.key}"]`);
    if (f.type === "radio" || f.type === "checkbox") {
      const wanted = f.type === "checkbox" ? value.split(";;").map(clean) : [value];
      for (const w of wanted) {
        const o = f.options.find((x) => norm(x.label) === norm(w)) || f.options.find((x) => norm(x.label).includes(norm(w)) || norm(w).includes(norm(x.label)));
        if (!o) throw new Error("no option " + w);
        const box = document.querySelector(`[data-autopilot-key="${o.key}"]`);
        if (box && !box.checked) box.click();
      }
      return;
    }
    if (!el) throw new Error("field disappeared");
    el.scrollIntoView({ block: "center" });
    if (f.type === "select") {
      const o = [...el.options].find((x) => norm(x.text) === norm(value)) || [...el.options].find((x) => norm(x.text).includes(norm(value)));
      if (!o) throw new Error("no option " + value);
      el.value = o.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (f.type === "combobox") {
      el.focus(); el.click();
      setNative(el, value);
      await pickFromMenu(el, value);
      await closeMenus();
    } else if (f.type === "listbox-button") {
      el.click();
      await pickFromMenu(el, value);
      await closeMenus();
    } else {
      el.focus();
      setNative(el, value);
      el.dispatchEvent(new Event("blur", { bubbles: true }));
    }
  }

  async function uploadDocuments() {
    const inputs = [...document.querySelectorAll('input[type="file"]')].filter((i) => !i.dataset.autopilotUploaded);
    if (!inputs.length || !job?.has_documents) return [];
    const around = (i) => norm([i.id, i.name, i.getAttribute("aria-label"), labelFor(i),
      txt(i.closest('[data-automation-id^="formField"], .field, [class*="question"], [class*="upload"], fieldset'))].join(" "));
    const done = [];
    for (const kind of ["resume", "cover"]) {
      const re = kind === "resume" ? /resume|cv\b|curriculum/ : /cover/;
      let input = inputs.find((i) => re.test(around(i)) && !i.dataset.autopilotUploaded);
      if (!input && kind === "resume") input = inputs.find((i) => !/cover|transcript|portfolio|photo|image/.test(around(i)) && !i.dataset.autopilotUploaded);
      if (!input) continue;
      const r = await send({ type: "file", jobId: job.id, kind });
      if (r.error || !r.b64) continue;
      const bytes = Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], r.name, { type: "application/pdf" }));
      input.files = dt.files;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dataset.autopilotUploaded = "1";
      done.push(kind === "resume" ? "Resume" : "Cover letter");
      await sleep(1500);   // some portals parse the resume and pre-fill fields
    }
    return done;
  }

  function outline(key, color) {
    const el = document.querySelector(`[data-autopilot-key="${key}"]`);
    const box = el && (el.closest('fieldset, [data-automation-id^="formField"], .field, [class*="question"]') || el);
    if (box) { box.style.outline = `3px solid ${color}`; box.style.outlineOffset = "3px"; }
  }

  // ------------------------------------------------------------------ side panel (panel.js) + notices
  const ui = (patch) => { try { window.__apUI && window.__apUI(patch); } catch (e) {} };
  let cancelled = false;
  const pageNo = () => {
    try { const n = Number(sessionStorage.getItem("apPage") || "0") + 1; sessionStorage.setItem("apPage", String(n)); return n; }
    catch (e) { return 1; }
  };

  // ------------------------------------------------------------------ the main flow
  async function ensureJob() {
    if (job) return job;
    const r = await send({ type: "job", url: location.href, title: document.title });
    if (r.error) {
      ui({ show: true, mode: "idle", note: "Can't reach the Job Autopilot app. Start it (python -m app.cli serve) and try again.", noteKind: "warn" });
      return null;
    }
    active = r.active;
    job = r.job || null;
    if (!job) ui({ show: true, mode: "idle", ready: r.ready || [], note: "Pick which job this application is for (or add it), then click Autofill.", noteKind: "" });
    return job;
  }

  function findApplyButton() {
    return [...document.querySelectorAll('a, button, [role="button"], input[type="button"], input[type="submit"]')]
      .filter(shown).find((b) => APPLY_TEXT.test(txt(b) || b.value || b.getAttribute("aria-label") || ""));
  }

  async function run(userClicked = false) {
    if (running) return;
    running = true;
    cancelled = false;
    try {
      if (!(await ensureJob())) return;
      if (passwordPage()) {
        await send({ type: "activate", job });
        ui({ show: true, mode: "idle", note: "Sign in or create your account on this page yourself. I'll continue filling as soon as the application form appears.", noteKind: "warn" });
        return;
      }
      // Once an application is underway in this tab (earlier page filled or Apply clicked), every later
      // page counts, including steps with no name/email fields (EEO, questions, review).
      const started = Object.keys(fieldsByKey).length > 0 || active;
      if (!started && !applicationForm()) {
        const apply = findApplyButton();    // job page: the form is behind an "Apply" button
        if (apply && userClicked) {
          await send({ type: "activate", job });
          active = true;
          ui({ mode: "running", pct: 4, stage: "Clicked Apply. Waiting for the application form to load…" });
          apply.click();
          return;
        }
        if (!userClicked) return;   // you clicked Autofill on a page without an Apply button: fill what's here
      }
      let fields = extract().filter((f) => !(f.key in fieldsByKey));
      const hasFileInput = [...document.querySelectorAll('input[type="file"]')].some((i) => !i.dataset.autopilotUploaded);
      if (!fields.length && !hasFileInput) {
        if (userClicked) ui({ mode: "done", pct: 100, stage: "Nothing left to fill on this page. Review it and click Next or Submit yourself." });
        return;
      }
      await send({ type: "activate", job });
      active = true;
      const page = pageNo();
      ui({ show: true, mode: "running", pct: 6, stage: `Reading page ${page} of the application…`, page });
      ui({ stage: "Uploading your tailored resume and cover letter…", pct: 9 });
      const uploaded = await uploadDocuments();
      // Portals often re-render after a resume upload: keep fields still on the page, add any new ones.
      const stillThere = (f) => document.querySelector(`[data-autopilot-key="${(f.options[0] && f.options[0].key) || f.key}"]`);
      if (uploaded.length) fields = fields.filter(stillThere).concat(extract().filter((f) => !(f.key in fieldsByKey)));
      const unique = Object.values(Object.fromEntries(fields.map((f) => [f.key, f])));
      ui({ stage: "Reading dropdown options…", pct: 12 });
      await peekOptions(unique);
      unique.forEach((f) => (fieldsByKey[f.key] = f));
      const labelOf = (f) => (f.question || "Untitled field").replace(/\*\s*$/, "").slice(0, 110);
      const rows = unique.map((f) => ({ key: f.key, label: labelOf(f), required: !!f.required }));
      if (uploaded.length) rows.unshift({ key: "_docs", label: uploaded.join(" and ") + " uploaded", required: true, status: "filled" });
      ui({ fields: rows, page, stage: `Claude is answering ${unique.length} question(s) from your profile…`, pct: 15, tick: [15, 55, 15] });
      if (cancelled) return ui({ mode: "cancelled", stage: "Cancelled before filling." });
      const r = await send({ type: "plan", jobId: job.id, url: location.href, fields: unique });
      if (r.error) return ui({ mode: "idle", note: "Couldn't get answers from Claude: " + r.error, noteKind: "warn" });
      const answers = (r.answers || []).filter((a) => fieldsByKey[a.key]);
      let done = 0, filled = 0, needs = 0, drafted = 0, failed = 0;
      for (const a of answers) {
        if (cancelled) { ui({ mode: "cancelled", stage: `Cancelled after ${done} of ${answers.length} answers.` }); return; }
        const f = fieldsByKey[a.key];
        const target = f.options[0]?.key && (f.type === "radio" || f.type === "checkbox") ? f.options[0].key : f.key;
        const filling = a.action === "fill" && a.value && a.value.trim();
        if (!filling) {
          if (a.needs_human || (f.required && !f.value)) {
            needs++; outline(target, "#ec835a");
            ui({ field: { key: f.key, status: "needs", note: a.note || "Needs you" } });
          } else ui({ field: { key: f.key, status: "skipped" } });
        } else {
          ui({ field: { key: f.key, status: "working" } });
          try {
            await fillOne(f, a.value);
            filledValues[f.key] = a.value;
            filled++;
            if (a.drafted) {
              drafted++; draftedKeys.add(f.key); outline(target, "#2a78d6");
              ui({ field: { key: f.key, status: "drafted", note: "Drafted by Claude, give it a quick read" } });
            } else ui({ field: { key: f.key, status: "filled" } });
          } catch (e) {
            failed++; outline(target, "#ec835a");
            const msg = /no option matches/.test(String(e)) ? String(e.message || e).replace(/^Error: /, "") + "; pick it yourself" : "Couldn't fill automatically";
            ui({ field: { key: f.key, status: "failed", note: msg } });
          }
        }
        done++;
        ui({ pct: 55 + (45 * done) / Math.max(1, answers.length), stage: `Filling answers… ${done} of ${answers.length}` });
      }
      const parts = [`${filled} filled`];
      if (drafted) parts.push(`${drafted} drafted`);
      if (needs + failed) parts.push(`${needs + failed} need you`);
      ui({ mode: "done", pct: 100, stage: `Page ${page}: ${parts.join(" · ")}. Review, then click Next or Submit yourself; the next page fills automatically.` });
    } finally {
      running = false;
    }
  }

  // ------------------------------------------------------------------ learning + multi-page
  function snapshot() {
    const vals = {};
    for (const el of document.querySelectorAll("[data-autopilot-key]")) {
      const k = el.dataset.autopilotKey;
      if (el.type === "radio" || el.type === "checkbox") {
        if (!el.checked) continue;
        const g = k.replace(/_\d+$/, "");
        const lab = labelFor(el);
        vals[g] = vals[g] ? vals[g] + " ;; " + lab : lab;
      } else if (el.tagName === "SELECT") {
        vals[k] = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
      } else if (fieldsByKey[k]?.type === "listbox-button") {
        vals[k] = txt(el);
      } else if (fieldsByKey[k]?.type === "combobox") {
        const box = el.closest('[class*="select__control"], [class*="select__container"], [class*="select-container"], .field, [class*="question"]');
        const sv = box && box.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value__label"]');
        vals[k] = sv ? txt(sv) : clean(el.value);
      } else {
        vals[k] = (el.value || "").trim();
      }
    }
    return vals;
  }

  function learnNow(submitted) {
    if (!job || !Object.keys(fieldsByKey).length) return;
    const fields = Object.values(fieldsByKey).filter((f) => !draftedKeys.has(f.key));
    send({ type: "learn", payload: { job_id: job.id, fields, values: snapshot(), filled: filledValues, submitted } });
  }

  // You click Next / Continue / Submit: remember what you kept or changed on this page.
  document.addEventListener("click", (e) => {
    const b = e.target.closest?.('button, input[type="submit"], a[role="button"], [role="button"]');
    if (!b || b.closest("#autopilot-banner, #autopilot-fab, #autopilot-pill")) return;
    const t = txt(b) || b.value || "";
    if (ADVANCE_TEXT.test(t)) learnNow(/submit|send/i.test(t));
  }, true);

  function looksLikeApplication() {
    if (ATS_HOSTS.test(location.hostname)) return true;
    const fields = [...document.querySelectorAll("input, textarea, select")].filter((e) => shown(e) && !["hidden", "search", "submit", "button"].includes(e.type));
    const hasResume = [...document.querySelectorAll('input[type="file"]')].some((i) => /resume|cv/i.test(i.id + i.name + labelFor(i)));
    const context = document.title + " " + txt(document.querySelector("h1")) + " " + location.pathname;
    const words = /apply|application|resume|cover letter|job/i.test(context);
    const jobWords = /career|job|hiring|position|role|opening|vacanc|engineer|developer|analyst|scientist|intern|manager/i.test(context);
    return matched || hasResume || (words && fields.length >= 3) || !!(jobWords && findApplyButton());
  }

  function embedsAtsFrame() {
    return isTop && !!document.querySelector('iframe[src*="greenhouse"], iframe[src*="icims"], iframe[src*="lever.co"], iframe[src*="workday"], iframe[src*="ashbyhq"], iframe[src*="smartrecruiters"], iframe[src*="jobvite"], iframe[id*="grnhse"]');
  }

  let debounce = null;
  async function onChange() {
    if (DONE_TEXT.test(txt(document.body).slice(0, 20000)) && job && active) {
      learnNow(true);
      await send({ type: "applied", jobId: job.id });
      active = false;
      ui({ show: true, mode: "done", pct: 100, stage: "Application submitted.", note: `Marked ${job.company} · ${job.title} as applied. Nice work.`, noteKind: "" });
      return;
    }
    if (!panelShown && (looksLikeApplication() || embedsAtsFrame() || (!isTop && applicationForm()))) {
      panelShown = true;
      const r = await send({ type: "job", url: location.href, title: document.title });
      if (!r.error) ui({ show: true, job: r.job || job, ready: r.ready || [], matched: !!r.matched });
    }
    if (!active || running || passwordPage()) return;
    if (hasNewFields()) run(false);   // a new page / step of a multi-page application appeared
  }

  function hasNewFields() {
    const skip = ["hidden", "submit", "button", "image", "reset", "password", "search", "file"];
    const fresh = [...document.querySelectorAll('input, textarea, select, button[aria-haspopup="listbox"]')].some((el) =>
      !el.dataset.autopilotKey && shown(el) && !el.disabled && !el.closest("#autopilot-banner, #autopilot-fab, #autopilot-pill") &&
      (el.tagName === "BUTTON" || !skip.includes((el.getAttribute("type") || el.tagName).toLowerCase())) &&
      (labelFor(el).replace(/[*\s]/g, "") || el.tabIndex >= 0));
    const upload = job?.has_documents && [...document.querySelectorAll('input[type="file"]')].some((i) => !i.dataset.autopilotUploaded);
    return fresh || upload;
  }
  const observer = new MutationObserver(() => { clearTimeout(debounce); debounce = setTimeout(onChange, 1500); });

  (async () => {
    const r = await send({ type: "job", url: location.href, title: document.title });
    if (!r.error) { active = r.active; job = r.job || null; matched = !!r.matched; }
    observer.observe(document.documentElement, { childList: true, subtree: true });
    onChange();
  })();

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "run") {
      // The click reaches every frame: only the frame that holds the form (or the page itself) acts on it.
      const hasForm = hasNewFields() || Object.keys(fieldsByKey).length > 0;
      if (embedsAtsFrame() || (!isTop && !hasForm)) { sendResponse({ ok: false }); return false; }
      if (msg.job) job = msg.job;
      run(true);
      sendResponse({ ok: true });
    }
    if (msg.type === "cancel") { cancelled = true; sendResponse({ ok: true }); }
    if (msg.type === "setJob") { job = msg.job; sendResponse({ ok: true }); }
    if (msg.type === "focus") {
      const el = document.querySelector(`[data-autopilot-key="${msg.key}"], [data-autopilot-key="${msg.key}_0"]`);
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        try { el.focus({ preventScroll: true }); } catch (e) {}
        const box = el.closest('fieldset, [data-automation-id^="formField"], .field, [class*="question"]') || el;
        const old = box.style.boxShadow;
        box.style.boxShadow = "0 0 0 4px rgba(42,120,214,.45)";
        setTimeout(() => (box.style.boxShadow = old), 1600);
      }
      sendResponse({ ok: true });
    }
    if (msg.type === "status") sendResponse({ job, active, frame: isTop ? "top" : "frame", application: looksLikeApplication() });
    return false;
  });
})();
