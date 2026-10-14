// Relays content-script requests to the local Job Autopilot app and remembers, per tab, which job
// is being applied to and whether autofill should continue on the next page of the application.
const API = "http://127.0.0.1:5055/api/ext";
const HEADERS = { "Content-Type": "application/json", "X-Autopilot": "1" };

async function api(path, opts = {}) {
  const r = await fetch(API + path, { ...opts, headers: { ...HEADERS, ...(opts.headers || {}) } });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r;
}

const tabKey = (tabId) => `tab_${tabId}`;
async function getTab(tabId) {
  const k = tabKey(tabId);
  return (await chrome.storage.session.get(k))[k] || null;
}
async function setTab(tabId, value) {
  await chrome.storage.session.set({ [tabKey(tabId)]: value });
}

function toBase64(buf) {
  let s = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function handle(msg, sender) {
  const tabId = msg.tabId ?? sender.tab?.id;
  switch (msg.type) {
    case "ping":
      return (await api("/ping")).json();
    case "job": {
      const t = await getTab(tabId);
      const r = await (await api(`/jobs?url=${encodeURIComponent(msg.url || "")}&title=${encodeURIComponent(msg.title || "")}`)).json();
      const job = t?.job || r.match || null;
      return { job, active: !!t?.active, ready: r.ready, matched: r.match };
    }
    case "setJob": {
      const t = (await getTab(tabId)) || {};
      await setTab(tabId, { ...t, job: msg.job });
      return { ok: true };
    }
    case "activate": {
      const t = (await getTab(tabId)) || {};
      await setTab(tabId, { ...t, job: msg.job || t.job, active: true });
      return { ok: true };
    }
    case "deactivate": {
      const t = (await getTab(tabId)) || {};
      await setTab(tabId, { ...t, active: false });
      return { ok: true };
    }
    case "addJob":
      return (await api("/add_job", { method: "POST", body: JSON.stringify(msg.payload) })).json();
    case "jobInfo":
      return (await api(`/job?id=${encodeURIComponent(msg.id)}`)).json();
    case "broadcast":        // panel (top frame) -> every frame of this tab, e.g. "run" for an embedded form
      chrome.tabs.sendMessage(tabId, msg.msg);
      return { ok: true };
    case "ui":               // an embedded form frame -> the side panel in the top frame
      chrome.tabs.sendMessage(tabId, { type: "ui", patch: msg.patch }, { frameId: 0 });
      return { ok: true };
    case "plan":
      return (await api("/plan", { method: "POST", body: JSON.stringify({ job_id: msg.jobId, url: msg.url, fields: msg.fields }) })).json();
    case "file": {
      const r = await api(`/file?job_id=${encodeURIComponent(msg.jobId)}&kind=${msg.kind}`);
      const name = (r.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)"?/)?.[1] || `${msg.kind}.pdf`;
      return { name, b64: toBase64(await r.arrayBuffer()) };
    }
    case "learn":
      return (await api("/learn", { method: "POST", body: JSON.stringify(msg.payload) })).json();
    case "applied": {
      await api("/applied", { method: "POST", body: JSON.stringify({ job_id: msg.jobId }) });
      const t = (await getTab(tabId)) || {};
      await setTab(tabId, { ...t, active: false });
      return { ok: true };
    }
  }
  return { error: "unknown message" };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handle(msg, sender).then(sendResponse, (e) => sendResponse({ error: String(e.message || e) }));
  return true; // async response
});

chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(tabKey(tabId)));
