// Job Autopilot online dashboard. Auth and data: Supabase (row-level security: you only ever see your own rows).
// Rendering uses textContent only, so nothing stored in the database is ever interpreted as HTML.
"use strict";
(() => {
  if (window.top !== window.self) { window.top.location = window.self.location; return; }   // no framing (clickjacking)
  const cfg = window.JA_CONFIG || {};
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const views = ["signin", "signup", "forgot", "dash"];
  const show = (v) => views.forEach((n) => { $("#view-" + n).hidden = n !== v; });
  if (!cfg.supabaseUrl || !cfg.anonKey || !window.supabase) { $("#not-configured").hidden = false; return; }

  const sb = window.supabase.createClient(cfg.supabaseUrl, cfg.anonKey, {
    auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false, flowType: "pkce" },
  });
  const say = (form, text, ok) => { const m = $(".msg", form); m.textContent = text || ""; m.classList.toggle("ok", !!ok); };
  const busy = (form, on) => form.querySelectorAll("button, input").forEach((x) => { x.disabled = on; });
  const strong = (p) => p.length >= 10 && /[a-z]/.test(p) && /[A-Z]/.test(p) && /[0-9]/.test(p);   // matches Supabase: lower + upper + digit
  const ago = (iso) => {
    if (!iso) return "never";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + "m ago" : s < 86400 ? Math.round(s / 3600) + "h ago" : Math.round(s / 86400) + "d ago";
  };

  // ------------------------------------------------------------------ routing
  async function route() {
    const { data: { session } } = await sb.auth.getSession();
    const hash = location.hash.replace("#", "");
    $("#account").hidden = !session;
    if (session) { show("dash"); startDashboard(session); return; }
    show(["signup", "forgot"].includes(hash) ? hash : "signin");
  }
  window.addEventListener("hashchange", route);
  let resetting = false;                                      // a password reset signs in before the new password is saved
  sb.auth.onAuthStateChange((evt) => {
    if (evt === "SIGNED_OUT" && started) { location.hash = "#signin"; location.reload(); return; }   // drop the last account's data
    if (resetting) return;
    if (evt === "SIGNED_OUT" || evt === "SIGNED_IN") route();
  });

  // ------------------------------------------------------------------ sign in
  $("#form-signin").addEventListener("submit", async (e) => {
    e.preventDefault(); const f = e.target; say(f, ""); busy(f, true);
    const { error } = await sb.auth.signInWithPassword({ email: f.email.value.trim(), password: f.password.value });
    busy(f, false);
    if (error) say(f, /confirm/i.test(error.message) ? "Confirm your email first (check your inbox for the code)." : "Wrong email or password.");
    else { f.reset(); location.hash = ""; }
  });

  // ------------------------------------------------------------------ sign up + email code
  let pendingEmail = "";
  $("#form-signup").addEventListener("submit", async (e) => {
    e.preventDefault(); const f = e.target; say(f, "");
    if (!f.name.value.trim()) return say(f, "Tell us your name.");
    if (!strong(f.password.value)) return say(f, "Use at least 10 characters, with a lowercase letter, an uppercase letter, and a number.");
    if (f.password.value !== f.password2.value) return say(f, "The passwords don't match.");
    if (!f.privacy.checked) return say(f, "Please read and accept the privacy note.");
    busy(f, true);
    pendingEmail = f.email.value.trim();
    const { error } = await sb.auth.signUp({ email: pendingEmail, password: f.password.value,
      options: { data: { display_name: f.name.value.trim().slice(0, 60), marketing_opt_in: f.optin.checked } } });
    busy(f, false);
    if (error && !/already/i.test(error.message)) return say(f, error.message);
    f.hidden = true; const c = $("#form-confirm"); c.hidden = false; $(".email-echo", c).textContent = pendingEmail; c.code.focus();
  });
  $("#form-confirm").addEventListener("submit", async (e) => {
    e.preventDefault(); const f = e.target; say(f, ""); busy(f, true);
    const { error } = await sb.auth.verifyOtp({ email: pendingEmail, token: f.code.value.trim(), type: "signup" });
    busy(f, false);
    if (error) say(f, "That code didn't work. Check it, or wait a minute and sign up again for a new one.");
    else location.hash = "";
  });

  // ------------------------------------------------------------------ forgot password: email OTP
  let resetEmail = "", resetVerified = false;
  $("#form-forgot").addEventListener("submit", async (e) => {
    e.preventDefault(); const f = e.target; say(f, ""); busy(f, true);
    resetEmail = f.email.value.trim();
    await sb.auth.resetPasswordForEmail(resetEmail);           // same answer whether or not the account exists
    busy(f, false);
    f.hidden = true; const r = $("#form-reset"); r.hidden = false; $(".email-echo", r).textContent = resetEmail; r.code.focus();
  });
  $("#form-reset").addEventListener("submit", async (e) => {
    e.preventDefault(); const f = e.target; say(f, "");
    if (!strong(f.password.value)) return say(f, "Use at least 10 characters, with a lowercase letter, an uppercase letter, and a number.");
    if (f.password.value !== f.password2.value) return say(f, "The passwords don't match.");
    busy(f, true); resetting = true;
    try {
      if (!resetVerified) {                                   // a code works once; on a retry, just set the password
        const v = await sb.auth.verifyOtp({ email: resetEmail, token: f.code.value.trim(), type: "recovery" });
        if (v.error) return say(f, "That code didn't work or has expired. Request a new one.");
        resetVerified = true; f.code.disabled = true;
      }
      const u = await sb.auth.updateUser({ password: f.password.value });
      if (u.error) return say(f, /different|same/i.test(u.error.message) ? "Choose a password you haven't used here before." : u.error.message);
      await sb.auth.signOut({ scope: "others" });             // sign out every other device after a reset
      resetVerified = false; location.hash = ""; resetting = false; route();
    } finally { busy(f, false); resetting = false; }
  });

  $("#signout").addEventListener("click", async () => { location.hash = "#signin"; await sb.auth.signOut(); location.reload(); });

  // ------------------------------------------------------------------ dashboard
  let me = null, machines = [], machineId = null, timer = null, channel = null, jobTab = "applied", jobPage = 0, emailPage = 0, cmdPage = 0, weekStart = null, started = false;
  const JOB_PER = 10, EMAIL_PER = 6, CMD_PER = 6;

  // Page controls under a list: "‹ Prev  11–20 of 47  Next ›" (hidden when everything fits on one page).
  function pager(box, page, per, total, go) {
    box.replaceChildren();
    const pages = Math.ceil((total || 0) / per);
    if (pages < 2) return;
    const btn = (txt, to, off) => { const b = el("button", "btn small", txt); b.type = "button"; b.disabled = off; b.onclick = () => go(to); return b; };
    box.append(btn("‹ Prev", page - 1, page === 0), el("span", "sub small", `${page * per + 1}–${Math.min(total, page * per + per)} of ${total}`),
               btn("Next ›", page + 1, page >= pages - 1));
  }
  const TABS = [["ready", "Ready"], ["applied", "Applied"], ["interviewing", "Interviewing"], ["closed", "Rejected"], ["all", "All"]];

  async function startDashboard(session) {
    if (started) return; started = true;
    $("#who").textContent = session.user.email;
    const { data } = await sb.from("machines").select("id,user_id,name,last_seen,platform").order("last_seen", { ascending: false, nullsFirst: false });
    machines = data || [];
    const sel = $("#machine"); sel.replaceChildren();
    machines.forEach((m) => sel.appendChild(Object.assign(el("option", null, m.name), { value: m.id })));
    sel.hidden = machines.length < 2;
    $("#no-machine").hidden = machines.length > 0; $("#dash-body").hidden = machines.length === 0;
    me = session.user;
    loadAliases();
    $("#form-alias").addEventListener("submit", addAlias);
    $("#leave-shared").onclick = leaveShared;
    const prof = await sb.from("profiles").select("display_name").eq("id", me.id).maybeSingle();
    if (!machines.length) { $("#hello-title").textContent = `Welcome${prof.data?.display_name ? ", " + prof.data.display_name : ""} 👋`; return; }
    machineId = sel.value || machines[0].id;
    sel.onchange = () => { machineId = sel.value; jobPage = emailPage = cmdPage = 0; refresh(); };
    TABS.forEach(([k, label]) => { const a = el("a", "tab", label); a.href = "#"; a.dataset.k = k; a.onclick = (ev) => { ev.preventDefault(); jobTab = k; jobPage = 0; loadJobs(); }; $("#job-tabs").appendChild(a); });
    $("#job-q").addEventListener("input", () => { jobPage = 0; loadJobs(); });
    document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => send(b.dataset.cmd, b)));
    $("#delete-account").onclick = deleteAccount;
    channel = sb.channel("live").on("postgres_changes", { event: "*", schema: "public", table: "snapshots" }, refresh)
      .on("postgres_changes", { event: "*", schema: "public", table: "commands" }, loadCommands).subscribe();
    refresh();
    timer = setInterval(refresh, 30000);
  }

  async function refresh() {
    if (!machineId) return;
    const [{ data: snap }, { data: m }] = await Promise.all([
      sb.from("snapshots").select("data,updated_at").eq("machine_id", machineId).maybeSingle(),
      sb.from("machines").select("name,last_seen").eq("id", machineId).maybeSingle(),
    ]);
    const online = m?.last_seen && Date.now() - new Date(m.last_seen).getTime() < 3 * 60 * 1000;
    $("#machine-status").textContent = `${m?.name || "Your computer"}: ${online ? "🟢 online" : "⚪ last seen " + ago(m?.last_seen)}`;
    $("#machine-status").dataset.online = online ? "1" : "0";
    const d = snap?.data;
    if (d) renderSnapshot(d);
    loadJobs(); loadEmails(); loadCommands();
  }

  function renderSnapshot(d) {
    $("#hello-title").textContent = d.greeting?.title || "Welcome";
    $("#hello-text").textContent = d.greeting?.text || "";
    const k = d.kpis || {};
    const tiles = [["Applied today", k.applied_today], ["Applied this week", k.applied_week], ["Applied, all time", k.applied_total], ["Ready to submit", k.ready],
                   [k.interviews != null ? (k.offers ? `Interviews · ${k.offers} offer${k.offers === 1 ? "" : "s"}` : "Interviews") : "Interviewing / offers",
                    k.interviews != null ? k.interviews : k.interviewing], ["Waiting on your call", k.decide], ["Rejected", k.rejected],
                   ["Assessments to finish", k.assessments]];
    const box = $("#kpis"); box.replaceChildren();
    tiles.forEach(([label, v]) => { const t = el("div", "tile"); t.append(el("div", "v", String(v ?? 0)), el("div", "k", label)); box.appendChild(t); });
    const al = d.assessments || [], abox = $("#assess-list");
    $("#assess-card").hidden = !al.length; abox.replaceChildren();
    al.forEach((a) => {
      const due = a.due_at ? new Date(a.due_at) : null;
      const hrs = due ? (due - Date.now()) / 3600000 : null;
      const when = due ? due.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "no due date";
      const rel = hrs == null ? "" : hrs < 6 ? ` (in ${Math.max(0, Math.floor(hrs))}h ${Math.max(0, Math.round((hrs % 1) * 60))}m)` : hrs < 48 ? (new Date().toDateString() === due.toDateString() ? " (today)" : " (tomorrow)") : ` (in ${Math.round(hrs / 24)} days)`;
      const r = el("div", "job" + (hrs != null && hrs < 36 ? " urgent" : ""));
      const left = el("div"); left.append(el("div", "job-title", a.company + (a.role ? " · " + a.role : "")), el("div", "sub small", "Due " + when + rel));
      r.append(left); abox.appendChild(r);
    });
    const bg = $("#background"); bg.replaceChildren();
    const b = d.background || {};
    if (b.paused_until) bg.appendChild(el("p", "warn", "Paused: Claude usage limit reached. It resumes automatically."));
    $("#resume-btn").hidden = !b.paused_until;
    if (b.run) bg.appendChild(el("p", null, `Run in progress: ${b.run.label || b.run.phase}` + (b.run.total ? ` (${b.run.done} of ${b.run.total})` : "")));
    if (b.mail) bg.appendChild(el("p", null, `✉ Checking email: ${b.mail.step || ""}` + (b.mail.found ? ` · ${b.mail.found} found` : "")));
    (b.active || []).forEach((a) => {
      const row = el("div", "prog-row"); row.appendChild(el("div", "spread-line", `${a.company} · ${a.title}`));
      const tr = el("div", "prog-track"), fi = el("div", "prog-fill"); fi.style.width = Math.min(99, a.pct || 0) + "%"; tr.appendChild(fi);
      row.append(tr, el("div", "sub small", `${a.kind === "screen" ? "Screening" : "Documents"} · ${a.stage}`)); bg.appendChild(row);
    });
    if (!b.run && !b.mail && !(b.active || []).length) bg.appendChild(el("p", "sub", b.queued ? `${b.queued} job(s) waiting for the next run.` : "Nothing running right now."));
    const meta = el("p", "sub small");
    meta.textContent = [d.last_run ? `Last run ${ago(d.last_run.finished_at || d.last_run.started_at)} (${d.last_run.status})` : "",
                        d.next_run ? `next scheduled ${new Date(d.next_run).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : "",
                        d.open_items ? `${d.open_items} item(s) in Needs you` : ""].filter(Boolean).join(" · ");
    bg.appendChild(meta);
    drawCharts(d);
  }

  // ---- charts (same as the dashboard on your computer): this week, time saved, role families, pipeline
  function drawCharts(d) {
    const weeks = d.weeks || [];
    $("#week-card").hidden = !weeks.length;
    if (weeks.length) {
      const sel = $("#wk-select");
      const labels = weeks.map((w, i) => i === 0 ? "This week" : i === 1 ? "Last week" : "Week of " + new Date(w.start + "T12:00").toLocaleDateString([], { month: "short", day: "numeric" }));
      if (sel.options.length !== weeks.length) { sel.replaceChildren(); weeks.forEach((w, i) => sel.appendChild(Object.assign(el("option", null, labels[i]), { value: w.start }))); }
      if (!weekStart || !weeks.some((w) => w.start === weekStart)) weekStart = weeks[0].start;
      const go = (start) => { weekStart = start; drawWeek(weeks.find((w) => w.start === start)); sel.value = start;
        $("#wk-prev").disabled = sel.selectedIndex >= weeks.length - 1; $("#wk-next").disabled = sel.selectedIndex <= 0; };
      sel.onchange = () => go(sel.value);
      $("#wk-prev").onclick = () => { if (sel.selectedIndex < weeks.length - 1) go(weeks[sel.selectedIndex + 1].start); };
      $("#wk-next").onclick = () => { if (sel.selectedIndex > 0) go(weeks[sel.selectedIndex - 1].start); };
      go(weekStart);
    }
    const ts = d.time_saved;
    $("#saved-card").hidden = !ts;
    if (ts) {
      $("#saved-num").textContent = String(ts.hours);
      $("#saved-sub").textContent = `≈ ${ts.days} work days you didn't spend reading job descriptions, writing documents, and filling forms`;
      const rows = $("#saved-rows"); rows.replaceChildren();
      ts.rows.forEach((r) => {
        const row = el("div", "saved-row"); const head = el("div", "spread small"); head.append(el("span", null, r.label), el("b", null, r.hours.toFixed(1) + " h"));
        const tr = el("div", "saved-track"), fill = el("span"); fill.style.width = r.pct + "%"; tr.appendChild(fill);
        row.append(head, tr, el("div", "sub small", `${r.n} ${r.verb} × ~${r.per} min each by hand`)); rows.appendChild(row);
      });
    }
    const fam = d.families || { slices: [] }, donut = $("#donut");
    $("#family-card").hidden = !fam.slices?.length; donut.replaceChildren();
    $("#fam-total").textContent = fam.total ? `(${fam.total})` : "";
    if (fam.slices?.length) {
      const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", "0 0 180 180"); svg.setAttribute("class", "donut"); svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", "Applications by role family: " + fam.slices.map((x) => `${x.fam} ${x.n}`).join(", "));
      fam.slices.forEach((x) => { const path = document.createElementNS(NS, "path"); path.setAttribute("d", x.d); path.setAttribute("class", "slice s" + x.slot);
        const t = document.createElementNS(NS, "title"); t.textContent = `${x.fam}: ${x.n} (${x.pct}%)`; path.appendChild(t); svg.appendChild(path); });
      const txt = (y, cls, v) => { const t = document.createElementNS(NS, "text"); t.setAttribute("x", "90"); t.setAttribute("y", y); t.setAttribute("text-anchor", "middle"); t.setAttribute("class", cls); t.textContent = v; svg.appendChild(t); };
      txt("86", "donut-n", String(fam.total)); txt("104", "donut-k", "applied");
      const ul = el("ul", "legend");
      fam.slices.forEach((x) => { const li = el("li"); li.append(el("i", "sw s" + x.slot), el("span", null, x.fam), el("b", null, String(x.n)), el("span", "sub", x.pct + "%")); ul.appendChild(li); });
      donut.append(svg, ul);
    }
    const pipe = d.pipeline || {}, labels = { to_screen: "Waiting for screen", needs_decision: "Your call", to_tailor: "Queued for documents",
      tailoring: "Building documents", ready: "Ready to submit", applied: "Applied", interviewing: "Interviewing", offer: "Offer", closed: "Rejected" };
    const tone = { to_screen: 1, needs_decision: 2, to_tailor: 2, tailoring: 2, ready: 3, applied: 4, interviewing: 5, offer: 5, closed: 3 };
    const keys = Object.keys(labels), max = Math.max(1, ...keys.map((k) => pipe[k] || 0)), sum = keys.reduce((a, k) => a + (pipe[k] || 0), 0) || 1;
    const pb = $("#pipeline"); pb.replaceChildren();
    keys.forEach((k) => {
      const row = el("div", "funnel-row"), tr = el("span", "funnel-track"), fill = el("span", "funnel-fill t" + tone[k]);
      fill.style.width = ((pipe[k] || 0) / max * 100) + "%"; tr.appendChild(fill);
      const n = el("span", "n", String(pipe[k] || 0)); n.appendChild(el("span", "sub", ` ${Math.round((pipe[k] || 0) / sum * 100)}%`));
      row.append(el("span", "lbl", labels[k]), tr, n); pb.appendChild(row);
    });
  }

  function drawWeek(w) {
    const chart = $("#wk-chart"); chart.replaceChildren();
    const peak = Math.max(1, ...w.days.map((x) => x.n));
    w.days.forEach((x) => {
      const col = el("div", "wk-col" + (x.today ? " today" : "") + (x.future ? " future" : ""));
      col.appendChild(el("span", "wk-val", x.future ? "" : String(x.n)));
      const bar = el("div", "wk-bar"); bar.style.height = (x.future ? 0 : Math.max(x.n / peak * 100, x.n ? 4 : 0)) + "%"; col.appendChild(bar);
      const lab = el("div", "wk-lab"); lab.append(el("b", null, x.dow), el("span", null, x.today ? "Today" : String(x.day))); col.appendChild(lab);
      col.title = `${x.label}: ${x.future ? "still to come" : x.n + " applied"}`; chart.appendChild(col);
    });
    chart.setAttribute("aria-label", `Applications per day, ${w.title}: ` + w.days.map((x) => `${x.dow} ${x.n}`).join(", "));
    $("#wk-sub").textContent = w.title + (w.current ? " · this week" : "");
    const stats = $("#wk-stats"); stats.replaceChildren(); const delta = w.total - w.prev_total;
    const box = (v, k, cls) => { const b = el("div", "wk-stat " + (cls || "")); b.append(el("b", null, v), el("span", null, k)); stats.appendChild(b); };
    box(String(w.total), "applied"); box(String(w.avg), "per day");
    box((delta > 0 ? "▲ " : delta < 0 ? "▼ " : "") + Math.abs(delta), "vs week before", delta > 0 ? "up" : delta < 0 ? "down" : "");
    if (w.best) box(w.best.dow, `best day (${w.best.n})`);
    const t = $("#wk-table"); t.replaceChildren();
    const hr = el("tr"); hr.append(el("th", null, "Day"), el("th", "num", "Applied")); t.appendChild(hr);
    w.days.forEach((x) => { const r = el("tr"); r.append(el("td", null, x.label), el("td", "num", x.future ? "—" : String(x.n))); t.appendChild(r); });
  }

  // The page to go back to when the one you were on no longer exists (fewer rows now): the last real page.
  function lastPage(count, per, page) {
    return typeof count === "number" ? Math.max(0, Math.min(page - 1, Math.ceil(count / per) - 1)) : page - 1;
  }

  async function loadJobs() {
    document.querySelectorAll("#job-tabs .tab").forEach((t) => t.classList.toggle("on", t.dataset.k === jobTab));
    let q = sb.from("jobs").select("job_id,company,title,location,status,match_score,salary,applied_at,url,updated_at", { count: "exact" })
      .eq("machine_id", machineId).order("updated_at", { ascending: false });
    if (jobTab === "interviewing") q = q.in("status", ["interviewing", "offer"]);
    else if (jobTab !== "all") q = q.eq("status", jobTab);
    const term = $("#job-q").value.trim().replace(/[%,()]/g, " ");
    if (term) q = q.or(`company.ilike.%${term}%,title.ilike.%${term}%`);
    const { data, count } = await q.range(jobPage * JOB_PER, jobPage * JOB_PER + JOB_PER - 1);
    if (!(data && data.length) && jobPage > 0) { jobPage = lastPage(count, JOB_PER, jobPage); return loadJobs(); }   // list shrank
    const rows = data || [], box = $("#jobs"); box.replaceChildren();
    if (!rows.length) box.appendChild(el("p", "sub", "Nothing here yet."));
    rows.forEach((j) => {
      const r = el("div", "job");
      const t = el(j.url ? "a" : "div", "job-title", j.title); if (j.url) { t.href = j.url; t.target = "_blank"; t.rel = "noopener noreferrer"; }
      const meta = el("div", "sub small", [j.company, j.location, j.salary].filter(Boolean).join(" · "));
      const right = el("div", "job-right");
      if (j.match_score != null) right.appendChild(el("span", "score", `${j.match_score}/10`));
      right.appendChild(el("span", "badge " + j.status, { closed: "Rejected", needs_decision: "Your call", below_threshold: "Below threshold", to_tailor: "Queued", tailoring: "Building" }[j.status] || j.status));
      if (j.applied_at) right.appendChild(el("span", "sub small", "applied " + ago(j.applied_at)));
      const left = el("div"); left.append(t, meta); r.append(left, right); box.appendChild(r);
    });
    pager($("#jobs-pager"), jobPage, JOB_PER, count, (to) => { jobPage = to; loadJobs(); });
  }

  async function loadEmails() {
    const { data, count } = await sb.from("email_updates").select("received_at,company,role,event,summary,deadline,new_status,state", { count: "exact" })
      .eq("machine_id", machineId).order("received_at", { ascending: false }).range(emailPage * EMAIL_PER, emailPage * EMAIL_PER + EMAIL_PER - 1);
    if (!(data && data.length) && emailPage > 0) { emailPage = lastPage(count, EMAIL_PER, emailPage); return loadEmails(); }
    pager($("#emails-pager"), emailPage, EMAIL_PER, count, (to) => { emailPage = to; loadEmails(); });
    const box = $("#emails"); box.replaceChildren();
    if (!(data || []).length) box.appendChild(el("p", "sub", "No email updates yet. Connect an inbox on your computer (Settings → Email)."));
    (data || []).forEach((e) => {
      const r = el("div", "job");
      const left = el("div"); left.append(el("div", "job-title", `${e.company || "Unknown"}${e.role ? " · " + e.role : ""}`),
        el("div", "sub small", (e.summary || "") + (e.state === "undone" ? " (you undid the status change)" : e.state === "unmatched" ? " (not matched to a job yet)" : "")));
      const right = el("div", "job-right"); right.append(el("span", "badge " + e.event, { rejection: "Rejected", interview: "Interview", assessment: "Assessment", offer: "Offer 🎉", confirmation: "Received" }[e.event] || e.event), el("span", "sub small", ago(e.received_at)));
      if (e.deadline) right.appendChild(el("span", "sub small", "due " + e.deadline));
      r.append(left, right); box.appendChild(r);
    });
  }

  async function loadCommands() {
    const { data, count } = await sb.from("commands").select("kind,status,result,created_at", { count: "exact" }).eq("machine_id", machineId)
      .order("created_at", { ascending: false }).range(cmdPage * CMD_PER, cmdPage * CMD_PER + CMD_PER - 1);
    if (!(data && data.length) && cmdPage > 0) { cmdPage = lastPage(count, CMD_PER, cmdPage); return loadCommands(); }
    pager($("#commands-pager"), cmdPage, CMD_PER, count, (to) => { cmdPage = to; loadCommands(); });
    const box = $("#commands"); box.replaceChildren();
    if (!(data || []).length) box.appendChild(el("p", "sub", "Requests you send from here show up with their status."));
    const names = { run: "Run now", process: "Process queue", mail: "Check email", resume: "Resume" };
    (data || []).forEach((c) => {
      const r = el("div", "job");
      const left = el("div"); left.append(el("div", "job-title", names[c.kind] || c.kind), el("div", "sub small", c.result || (c.status === "pending" ? "Waiting for your computer to pick it up" : "")));
      const right = el("div", "job-right");
      right.append(el("span", "badge " + c.status, c.status), el("span", "sub small", ago(c.created_at)));
      r.append(left, right);
      box.appendChild(r);
    });
  }

  async function send(kind, btn) {
    const { data: { session } } = await sb.auth.getSession();
    btn.disabled = true;
    const owner = machines.find((m) => m.id === machineId)?.user_id || session.user.id;   // shared dashboards queue for the owner
    const { error } = await sb.from("commands").insert({ user_id: owner, machine_id: machineId, kind });
    btn.disabled = false;
    const online = $("#machine-status").dataset.online === "1";
    $("#cmd-msg").textContent = error ? (error.message.includes("too many") ? "Too many requests this hour. Try again later." : "Couldn't send: " + error.message)
      : online ? "Sent. Your computer picks it up within 30 seconds." : "Sent. Your computer is asleep or offline; it runs at its next wake-up.";
    loadCommands();
  }

  // ---- extra sign-in emails (account_emails): mine to manage, or shared with me to leave
  async function loadAliases() {
    const { data } = await sb.from("account_emails").select("owner_id,email,created_at").order("created_at");
    const rows = data || [];
    const mine = rows.filter((r) => r.owner_id === me.id);
    $("#shared-with-me").hidden = !rows.some((r) => r.owner_id !== me.id);
    const box = $("#aliases"); box.replaceChildren();
    if (!mine.length) box.appendChild(el("p", "sub small", "None yet."));
    mine.forEach((r) => {
      const row = el("div", "job"); const btn = el("button", "btn small", "Remove"); btn.type = "button";
      btn.onclick = async () => { btn.disabled = true; await sb.from("account_emails").delete().eq("owner_id", me.id).eq("email", r.email); loadAliases(); };
      row.append(el("div", null, r.email), btn); box.appendChild(row);
    });
  }

  async function addAlias(ev) {
    ev.preventDefault();
    const f = ev.target, msg = $("#alias-msg"), email = f.email.value.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { msg.textContent = "Enter a valid email."; return; }
    if (email === (me.email || "").toLowerCase()) { msg.textContent = "That's the email you're signed in with."; return; }
    const { error } = await sb.from("account_emails").insert({ owner_id: me.id, email });
    msg.textContent = error ? (error.code === "23505" ? "Already added." : error.message.includes("at most") ? "You can add up to 5 emails." : "Couldn't add it. Try again.")
      : `Added. Now create an account with ${email} on this site (or sign in, if it already has one).`;
    if (!error) f.reset();
    loadAliases();
  }

  async function leaveShared() {
    if (!confirm("Stop seeing the shared dashboard? The owner can add your email again later.")) return;
    await sb.from("account_emails").delete().eq("email", (me.email || "").toLowerCase()).neq("owner_id", me.id);
    location.reload();
  }

  async function deleteAccount() {
    if (!confirm("Delete your account and everything stored for it here? Your computer keeps its own data.")) return;
    const { error } = await sb.rpc("delete_my_account");
    if (error) { alert("Couldn't delete: " + error.message); return; }
    await sb.auth.signOut(); location.hash = "#signin";
  }

  route();
})();
