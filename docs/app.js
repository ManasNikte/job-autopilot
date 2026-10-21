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
  let me = null, machines = [], machineId = null, timer = null, channel = null, jobTab = "applied", jobLimit = 25, started = false;
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
    sel.onchange = () => { machineId = sel.value; refresh(); };
    TABS.forEach(([k, label]) => { const a = el("a", "tab", label); a.href = "#"; a.dataset.k = k; a.onclick = (ev) => { ev.preventDefault(); jobTab = k; jobLimit = 25; loadJobs(); }; $("#job-tabs").appendChild(a); });
    $("#job-q").addEventListener("input", () => { jobLimit = 25; loadJobs(); });
    $("#jobs-more").onclick = () => { jobLimit += 25; loadJobs(); };
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
    const tiles = [["Applied today", k.applied_today], ["Applied, all time", k.applied_total], ["Ready to submit", k.ready],
                   ["Interviewing / offers", k.interviewing], ["Waiting on your call", k.decide], ["Rejected", k.rejected]];
    const box = $("#kpis"); box.replaceChildren();
    tiles.forEach(([label, v]) => { const t = el("div", "tile"); t.append(el("div", "v", String(v ?? 0)), el("div", "k", label)); box.appendChild(t); });
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
    const bars = $("#bars"); bars.replaceChildren();
    const series = d.series || [], peak = Math.max(1, ...series.map((s) => s.n));
    series.forEach((s) => { const col = el("div", "bar"); const fill = el("span"); fill.style.height = (s.n / peak * 100) + "%"; fill.title = `${s.day}: ${s.n}`; col.append(fill, el("small", null, s.day.slice(8))); bars.appendChild(col); });
    $("#bars-note").textContent = `${series.reduce((a, s) => a + s.n, 0)} applications in the last 14 days`;
  }

  async function loadJobs() {
    document.querySelectorAll("#job-tabs .tab").forEach((t) => t.classList.toggle("on", t.dataset.k === jobTab));
    let q = sb.from("jobs").select("job_id,company,title,location,status,match_score,salary,applied_at,url,updated_at")
      .eq("machine_id", machineId).order("updated_at", { ascending: false }).limit(jobLimit + 1);
    if (jobTab === "interviewing") q = q.in("status", ["interviewing", "offer"]);
    else if (jobTab !== "all") q = q.eq("status", jobTab);
    const term = $("#job-q").value.trim().replace(/[%,()]/g, " ");
    if (term) q = q.or(`company.ilike.%${term}%,title.ilike.%${term}%`);
    const { data } = await q;
    const rows = data || [], box = $("#jobs"); box.replaceChildren();
    if (!rows.length) box.appendChild(el("p", "sub", "Nothing here yet."));
    rows.slice(0, jobLimit).forEach((j) => {
      const r = el("div", "job");
      const t = el(j.url ? "a" : "div", "job-title", j.title); if (j.url) { t.href = j.url; t.target = "_blank"; t.rel = "noopener noreferrer"; }
      const meta = el("div", "sub small", [j.company, j.location, j.salary].filter(Boolean).join(" · "));
      const right = el("div", "job-right");
      if (j.match_score != null) right.appendChild(el("span", "score", `${j.match_score}/10`));
      right.appendChild(el("span", "badge " + j.status, { closed: "Rejected", needs_decision: "Your call", below_threshold: "Below threshold", to_tailor: "Queued", tailoring: "Building" }[j.status] || j.status));
      if (j.applied_at) right.appendChild(el("span", "sub small", "applied " + ago(j.applied_at)));
      const left = el("div"); left.append(t, meta); r.append(left, right); box.appendChild(r);
    });
    $("#jobs-more").hidden = rows.length <= jobLimit;
  }

  async function loadEmails() {
    const { data } = await sb.from("email_updates").select("received_at,company,role,event,summary,deadline,new_status,state")
      .eq("machine_id", machineId).order("received_at", { ascending: false }).limit(15);
    const box = $("#emails"); box.replaceChildren();
    if (!(data || []).length) box.appendChild(el("p", "sub", "No email updates yet. Connect an inbox on your computer (Settings → Email)."));
    (data || []).forEach((e) => {
      const r = el("div", "job");
      const left = el("div"); left.append(el("div", "job-title", `${e.company || "Unknown"}${e.role ? " · " + e.role : ""}`), el("div", "sub small", e.summary || ""));
      const right = el("div", "job-right"); right.append(el("span", "badge " + e.event, { rejection: "Rejected", interview: "Interview", assessment: "Assessment", offer: "Offer 🎉", confirmation: "Received" }[e.event] || e.event), el("span", "sub small", ago(e.received_at)));
      if (e.deadline) right.appendChild(el("span", "sub small", "due " + e.deadline));
      r.append(left, right); box.appendChild(r);
    });
  }

  async function loadCommands() {
    const { data } = await sb.from("commands").select("kind,status,result,created_at").eq("machine_id", machineId).order("created_at", { ascending: false }).limit(6);
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
