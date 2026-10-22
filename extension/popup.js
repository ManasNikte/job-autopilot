const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, (r) => res(r || { error: chrome.runtime.lastError?.message })));

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const ping = await send({ type: "ping" });
  if (ping.error) {
    $("server").textContent = /approve/i.test(ping.error) ? ping.error
      : "The Job Autopilot app isn't running. Start it with: python -m app.cli serve";
    $("server").className = "bad";
    return;
  }
  $("server").textContent = "Connected to your local app.";
  $("server").className = "ok";

  const r = await send({ type: "job", tabId: tab.id, url: tab.url, title: tab.title });
  let job = r.job;
  const show = () => {
    $("jobbox").style.display = job ? "block" : "none";
    $("jobbox").textContent = job ? `Applying to: ${job.company} · ${job.title}${job.has_documents ? "" : " (documents not built yet)"}` : "";
  };
  show();

  const pick = $("pick");
  pick.style.display = "block";
  pick.appendChild(new Option(job ? "Change job…" : "Which job is this application for?", ""));
  for (const j of r.ready || []) pick.appendChild(new Option(`${j.company} · ${j.title}`, j.id));
  pick.addEventListener("change", async () => {
    const j = (r.ready || []).find((x) => x.id === pick.value);
    if (!j) return;
    job = j;
    await send({ type: "setJob", tabId: tab.id, job: j });
    show();
    $("fill").disabled = false;
  });

  $("fill").disabled = !job;
  $("fill").addEventListener("click", async () => {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "run", job });
      window.close();
    } catch (e) {
      $("server").textContent = "This page isn't ready for autofill yet. Reload the page and try again.";
      $("server").className = "bad";
    }
  });
})();
