// Job Autopilot side panel (Jobright-style), rendered in the top frame inside a shadow root so the
// site's CSS can't touch it. Form frames (e.g. an embedded Greenhouse iframe) send their progress
// here through the background worker. Built with DOM calls only (CSP / Trusted Types safe).
(() => {
  if (window.__apPanelLoaded) return;
  window.__apPanelLoaded = true;
  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { error: chrome.runtime.lastError?.message })); }
    catch (e) { res({ error: String(e) }); }
  });

  if (window.top !== window) {            // inside a frame: forward UI updates to the top-frame panel
    window.__apUI = (patch) => send({ type: "ui", patch });
    return;
  }

  const WIDTH = 380;
  const state = {
    visible: false, collapsed: false, job: null, ready: [], matched: false, adding: false,
    mode: "idle", stage: "", pct: 0, fields: [], page: 1, summary: null, note: "",
  };
  let host = null, root = null, pollTimer = null, tickTimer = null;

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .panel { position: fixed; top: 0; right: 0; bottom: 0; width: ${WIDTH}px; z-index: 2147483647; background: #fff; color: #111;
      border-left: 1px solid #e3e2dd; box-shadow: -6px 0 24px rgba(0,0,0,.10); display: flex; flex-direction: column; font-size: 14px; line-height: 1.4; }
    .head { display: flex; align-items: center; gap: 10px; padding: 14px 16px; border-bottom: 1px solid #efeee9; }
    .logo { width: 28px; height: 28px; border-radius: 8px; background: #2a78d6; color: #fff; display: grid; place-items: center; font-weight: 800; }
    .brand { font-weight: 800; font-size: 17px; flex: 1; }
    .icon-btn { border: 1px solid #e3e2dd; background: #fff; border-radius: 8px; padding: 4px 9px; cursor: pointer; font-size: 13px; color: #333; text-decoration: none; }
    .icon-btn:hover { border-color: #2a78d6; }
    .body { flex: 1; overflow-y: auto; padding: 14px 16px 18px; }
    .card { border: 1px solid #ecebe6; border-radius: 12px; padding: 14px; margin-bottom: 14px; background: #fcfcfb; }
    .co { color: #555; font-size: 13px; }
    .title { font-weight: 700; font-size: 15px; margin: 2px 0 8px; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip { font-size: 12px; padding: 2px 9px; border-radius: 999px; background: #f0efec; color: #333; }
    .chip.good { background: #e3f6e3; color: #11660f; } .chip.risk { background: #fff3d6; color: #7a5300; } .chip.no { background: #fde8e8; color: #9b1c1c; }
    .btn { width: 100%; border: none; border-radius: 10px; padding: 11px 14px; font-weight: 700; font-size: 14px; cursor: pointer; margin-top: 10px; }
    .btn.primary { background: #2a78d6; color: #fff; } .btn.primary:disabled { opacity: .55; cursor: default; }
    .btn.add { background: #e7f8ef; color: #0b6b3a; }
    .sub { color: #666; font-size: 13px; text-align: center; margin-top: 6px; }
    select { width: 100%; margin-top: 8px; padding: 8px; border-radius: 8px; border: 1px solid #d8d7d2; font-size: 13px; background: #fff; }
    .prog-head { display: flex; align-items: center; gap: 10px; font-weight: 700; }
    .prog-head .pct { color: #2a78d6; }
    .prog-head .cancel { margin-left: auto; font-weight: 500; color: #333; text-decoration: underline; cursor: pointer; background: none; border: none; font-size: 14px; }
    .bar { height: 8px; border-radius: 4px; background: #efeee9; margin: 8px 0 6px; overflow: hidden; }
    .fill { height: 8px; background: #2a78d6; border-radius: 4px; transition: width .5s linear; }
    .stage { color: #555; font-size: 13px; }
    .group { font-weight: 700; margin: 16px 0 6px; }
    .row { display: flex; gap: 10px; align-items: flex-start; padding: 6px 4px; border-radius: 8px; cursor: pointer; }
    .row:hover { background: #f5f5f2; }
    .st { width: 18px; height: 18px; flex: none; margin-top: 1px; border-radius: 50%; display: grid; place-items: center; font-size: 11px; font-weight: 800; color: #fff; }
    .st.pending { border: 2px solid #c9c8c2; }
    .st.working { border: 2px solid #c9e0fa; border-top-color: #2a78d6; animation: spin .8s linear infinite; }
    .st.filled { background: #12a150; } .st.drafted { background: #2a78d6; } .st.needs, .st.failed { background: #e8782f; }
    .st.skipped { border: 2px solid #c9c8c2; color: #999; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .lbl { flex: 1; } .lbl .note { display: block; color: #8a5a00; font-size: 12px; } .lbl .note.blue { color: #1d5fae; }
    .foot { border-top: 1px solid #efeee9; padding: 10px 16px; color: #777; font-size: 12px; }
    .notice { background: #f3f8fe; border: 1px solid #d6e6fa; color: #1d4f8f; border-radius: 10px; padding: 10px 12px; font-size: 13px; margin-bottom: 12px; }
    .notice.warn { background: #fff4ec; border-color: #f6d3b9; color: #8a3d0b; }
    .tab { position: fixed; right: 0; top: 40%; z-index: 2147483647; background: #2a78d6; color: #fff; border: none; cursor: pointer;
      border-radius: 10px 0 0 10px; padding: 12px 8px; writing-mode: vertical-rl; font-weight: 700; font-size: 13px; box-shadow: -2px 2px 10px rgba(0,0,0,.2); }
  `;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  function mount() {
    if (host) return;
    host = document.createElement("div");
    host.id = "job-autopilot-panel";
    root = host.attachShadow({ mode: "open" });
    try { const sh = new CSSStyleSheet(); sh.replaceSync(CSS); root.adoptedStyleSheets = [sh]; }
    catch (e) { const st = document.createElement("style"); st.textContent = CSS; root.appendChild(st); }
    document.documentElement.appendChild(host);
  }

  function setPageMargin(on) {
    // Make room for the panel like a docked sidebar; restored when collapsed.
    document.documentElement.style.setProperty("margin-right", on ? WIDTH + "px" : "", on ? "important" : "");
  }

  function statusChip(job) {
    if (job.status === "ready" || job.status === "applied") return ["good", job.status_text];
    if (["rejected", "error", "closed"].includes(job.status)) return ["no", job.status_text];
    return ["", job.status_text + (job.progress != null ? ` ${job.progress}%` : "")];
  }

  function render() {
    if (!state.visible) { if (host) host.remove(); host = null; setPageMargin(false); return; }
    mount();
    root.querySelectorAll(".panel, .tab").forEach((n) => n.remove());
    if (state.collapsed) {
      setPageMargin(false);
      const t = el("button", "tab", "Job Autopilot");
      t.addEventListener("click", () => { state.collapsed = false; savePref(); render(); });
      root.appendChild(t);
      return;
    }
    setPageMargin(true);
    const p = el("div", "panel");

    // Header
    const head = el("div", "head");
    head.append(el("div", "logo", "A"), el("div", "brand", "Job Autopilot"));
    const dash = el("a", "icon-btn", "Dashboard");
    dash.href = state.job ? `http://127.0.0.1:5055/jobs/${encodeURIComponent(state.job.id).replace(/%3A/g, ":")}` : "http://127.0.0.1:5055/";
    dash.target = "_blank";
    const col = el("button", "icon-btn", "›");
    col.title = "Collapse";
    col.addEventListener("click", () => { state.collapsed = true; savePref(); render(); });
    head.append(dash, col);
    p.appendChild(head);

    const body = el("div", "body");
    if (state.note) body.appendChild(el("div", "notice" + (state.noteKind === "warn" ? " warn" : ""), state.note));

    // Job card
    const card = el("div", "card");
    if (state.job) {
      const j = state.job;
      card.append(el("div", "co", j.company), el("div", "title", j.title));
      const chips = el("div", "chips");
      if (j.match_score != null) {
        const cls = j.verdict === "APPLY" ? "good" : j.verdict === "APPLY WITH RISK" ? "risk" : j.verdict ? "no" : "";
        chips.appendChild(el("span", "chip " + cls, `Match ${j.match_score}/10`));
      }
      const [cls, txt] = statusChip(j);
      chips.appendChild(el("span", "chip " + cls, txt));
      if (j.final_call) chips.appendChild(el("span", "chip", `Recruiter: ${j.final_call}`));
      card.appendChild(chips);
      if (!j.has_documents && !["rejected", "error"].includes(j.status))
        card.appendChild(el("div", "sub", "Autofill works now; your tailored resume is attached once it's built."));
      if (state.ready.length > 1) card.appendChild(jobSelect("Not this job? Pick another…"));
    } else {
      card.appendChild(el("div", "title", "This job isn't in your tracker yet"));
      const add = el("button", "btn add", state.adding ? "Adding…" : "＋  Add this job in one click");
      add.disabled = state.adding;
      add.addEventListener("click", addJob);
      card.append(add, el("div", "sub", "Scores the match and builds a tailored resume and cover letter"));
      if (state.ready.length) card.appendChild(jobSelect("Or pick one of your jobs…"));
    }
    body.appendChild(card);

    // Autofill button / progress
    if (state.mode === "running" || state.mode === "done" || state.mode === "cancelled") {
      const box = el("div", "card");
      const ph = el("div", "prog-head");
      const label = state.mode === "running" ? "Autofilling…" : state.mode === "done" ? "Autofill complete" : "Autofill cancelled";
      ph.append(el("span", "", label), el("span", "pct", `${Math.round(state.pct)}%`));
      if (state.mode === "running") {
        const c = el("button", "cancel", "Cancel");
        c.addEventListener("click", () => send({ type: "broadcast", msg: { type: "cancel" } }));
        ph.appendChild(c);
      }
      const bar = el("div", "bar"), fill = el("div", "fill");
      fill.style.width = `${Math.min(100, state.pct)}%`;
      bar.appendChild(fill);
      box.append(ph, bar, el("div", "stage", state.stage || ""));
      if (state.mode !== "running") {
        const again = el("button", "btn primary", "Autofill again");
        again.addEventListener("click", runAutofill);
        box.appendChild(again);
      }
      body.appendChild(box);
    } else {
      const go = el("button", "btn primary", "Autofill this application");
      go.disabled = !state.job;
      go.addEventListener("click", runAutofill);
      body.appendChild(go);
      body.appendChild(el("div", "sub", state.job ? "Clicks Apply if needed, uploads your resume, answers every question" : "Add or pick the job first"));
    }

    // Field checklist
    if (state.fields.length) {
      const req = state.fields.filter((f) => f.required), opt = state.fields.filter((f) => !f.required);
      for (const [name, list] of [[`Required${state.page > 1 ? ` · page ${state.page}` : ""}`, req], ["Optional", opt]]) {
        if (!list.length) continue;
        body.appendChild(el("div", "group", name));
        for (const f of list) {
          const row = el("div", "row");
          const st = el("span", "st " + (f.status || "pending"), { filled: "✓", drafted: "✎", needs: "!", failed: "!", skipped: "–" }[f.status] || "");
          const lbl = el("span", "lbl", f.label || "Untitled field");
          if (f.note && ["needs", "failed", "drafted"].includes(f.status)) lbl.appendChild(el("span", "note" + (f.status === "drafted" ? " blue" : ""), f.note));
          row.append(st, lbl);
          row.title = "Jump to this field";
          row.addEventListener("click", () => send({ type: "broadcast", msg: { type: "focus", key: f.key } }));
          body.appendChild(row);
        }
      }
    }
    p.appendChild(body);
    p.appendChild(el("div", "foot", "Never clicks Next or Submit and never fills sign-in pages. Your edits are remembered."));
    root.appendChild(p);
  }

  function jobSelect(placeholder) {
    const sel = el("select");
    sel.appendChild(new Option(placeholder, ""));
    for (const j of state.ready) sel.appendChild(new Option(`${j.company} · ${j.title}`, j.id));
    sel.addEventListener("change", async () => {
      const j = state.ready.find((x) => x.id === sel.value);
      if (!j) return;
      state.job = j;
      await send({ type: "setJob", job: j });
      send({ type: "broadcast", msg: { type: "setJob", job: j } });
      render();
    });
    return sel;
  }

  function runAutofill() {
    state.mode = "running"; state.pct = 1; state.stage = "Starting…"; state.fields = []; state.note = "";
    render();
    send({ type: "broadcast", msg: { type: "run", job: state.job } });
  }

  async function addJob() {
    state.adding = true; render();
    const company = document.querySelector('meta[property="og:site_name"]')?.content || "";
    const h1 = document.querySelector("h1")?.innerText?.trim() || "";
    const r = await send({ type: "addJob", payload: {
      url: location.href, title: h1 || document.title, company,
      description: (document.body.innerText || "").slice(0, 30000),
    } });
    state.adding = false;
    if (r.error) { state.note = "Couldn't add the job: " + r.error; state.noteKind = "warn"; render(); return; }
    state.job = r;
    await send({ type: "setJob", job: r });
    send({ type: "broadcast", msg: { type: "setJob", job: r } });
    state.note = "Added. Claude is scoring the match now; documents build automatically if it's a fit.";
    state.noteKind = "";
    render();
    pollJob();
  }

  function pollJob() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      if (!state.job) return;
      const r = await send({ type: "jobInfo", id: state.job.id });
      if (r.error) return;
      state.job = r;
      if (["ready", "rejected", "error", "applied", "needs_decision"].includes(r.status)) {
        clearInterval(pollTimer);
        state.note = r.status === "ready" ? "Tailored resume and cover letter are ready." :
          r.status === "rejected" ? "Claude's verdict: not a fit (see the dashboard for why). You can still autofill." : "";
      }
      render();
    }, 4000);
  }

  async function savePref() { try { await chrome.storage.local.set({ apCollapsed: state.collapsed }); } catch (e) {} }

  // Progress ticking while Claude is answering (it doesn't report partial progress).
  function startTick(from, to, secs) {
    clearInterval(tickTimer);
    const t0 = Date.now();
    tickTimer = setInterval(() => {
      if (state.mode !== "running") return clearInterval(tickTimer);
      state.pct = Math.max(state.pct, from + (to - from) * Math.min((Date.now() - t0) / (secs * 1000), 0.95));
      const f = root && root.querySelector(".fill"), pc = root && root.querySelector(".pct");
      if (f) f.style.width = state.pct + "%";
      if (pc) pc.textContent = Math.round(state.pct) + "%";
    }, 400);
  }

  function apply(patch) {
    if (patch.show && !state.visible) state.visible = true;
    if (patch.field) {
      const f = state.fields.find((x) => x.key === patch.field.key);
      if (f) Object.assign(f, patch.field);
    }
    if (patch.fields) { state.fields = patch.fields.map((f) => ({ status: "pending", ...f })); }
    for (const k of ["job", "ready", "matched", "mode", "stage", "pct", "page", "note", "noteKind"])
      if (k in patch) state[k] = patch[k];
    if (patch.tick) startTick(...patch.tick);
    if (patch.mode === "running") state.visible = true;
    render();
  }

  window.__apUI = apply;
  chrome.runtime.onMessage.addListener((msg) => { if (msg.type === "ui") apply(msg.patch); });
  chrome.storage.local.get("apCollapsed").then((r) => { state.collapsed = !!r.apCollapsed; }).catch(() => {});
})();
