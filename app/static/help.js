// Help chat: a small panel in the corner of every page. Questions go to /api/help, which answers from the app's
// docs and this install's current state. The conversation lives only in this browser tab (sessionStorage).
(function () {
  const root = document.getElementById("help-chat");
  if (!root) return;
  const url = root.dataset.url, fab = document.getElementById("help-fab"), panel = document.getElementById("help-panel");
  const log = document.getElementById("help-log"), form = document.getElementById("help-form"), q = document.getElementById("help-q");
  const chips = document.getElementById("help-chips"), KEY = "ja-help";
  let history = [], busy = false;

  const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } };
  const save = (open) => { try { sessionStorage.setItem(KEY, JSON.stringify({ history: history.slice(-20), open })); } catch (e) { /* private window */ } };

  // A safe subset of markdown: escape everything, then **bold**, `code`, [label](/path or GitHub), lists, paragraphs.
  function render(text) {
    const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/\[([^\]]+)\]\(((?:\/(?![\/\\])[^\s)\\]*)|https:\/\/github\.com\/[^\s)]*)\)/g, (m, label, href) =>
        `<a href="${href}"${href.startsWith("http") ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`);
    const out = []; let list = null;
    for (const raw of text.split("\n")) {
      const line = raw.trim(), m = line.match(/^(\d+[.)]|[-*•])\s+(.*)$/);
      if (m) {
        const tag = /\d/.test(m[1]) ? "ol" : "ul";
        if (!list || list.tag !== tag) { if (list) out.push(`</${list.tag}>`); list = { tag }; out.push(`<${tag}>`); }
        out.push(`<li>${inline(m[2])}</li>`);
      } else {
        if (list) { out.push(`</${list.tag}>`); list = null; }
        if (line) out.push(`<p>${inline(line)}</p>`);
      }
    }
    if (list) out.push(`</${list.tag}>`);
    return out.join("");
  }

  function add(role, text, cls) {
    const d = document.createElement("div");
    d.className = `help-msg ${role === "user" ? "me" : "bot"}${cls ? " " + cls : ""}`;
    if (role === "user") d.textContent = text; else d.innerHTML = render(text);
    log.appendChild(d); log.scrollTop = log.scrollHeight;
    return d;
  }

  function open(on) {
    panel.hidden = !on; fab.setAttribute("aria-expanded", String(on)); root.classList.toggle("open", on);
    if (on) { q.focus(); log.scrollTop = log.scrollHeight; }
    save(on);
  }

  async function ask(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true; chips.hidden = true;
    add("user", text); q.value = ""; q.style.height = "";
    const wait = add("bot", "Checking…", "typing");
    const slow = setTimeout(() => { wait.textContent = "Still thinking… answers take up to half a minute."; }, 8000);
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "help" },
                                    body: JSON.stringify({ q: text, history }) });
      const d = await r.json().catch(() => ({}));
      wait.remove();
      if (d.answer) {
        add("bot", d.answer);
        history.push({ role: "user", text }, { role: "assistant", text: d.answer });
      } else add("bot", d.error || "Something went wrong. Try again in a moment.", "error");
    } catch (e) {
      wait.remove(); add("bot", "I couldn't reach the dashboard. Is it still running?", "error");
    } finally {
      clearTimeout(slow); busy = false; save(true);
    }
  }

  fab.addEventListener("click", () => open(panel.hidden));
  document.getElementById("help-close").addEventListener("click", () => open(false));
  document.getElementById("help-clear").addEventListener("click", () => {
    history = []; log.querySelectorAll(".help-msg:not(:first-child)").forEach((m) => m.remove()); chips.hidden = false; save(true); q.focus();
  });
  chips.addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); });
  form.addEventListener("submit", (e) => { e.preventDefault(); ask(q.value); });
  q.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(q.value); } });
  q.addEventListener("input", () => { q.style.height = ""; q.style.height = Math.min(q.scrollHeight, 120) + "px"; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden && !document.body.classList.contains("touring")) open(false); });

  const saved = load();                                           // keep the conversation as you move between pages
  if (Array.isArray(saved.history) && saved.history.length) {
    history = saved.history; chips.hidden = true;
    history.forEach((h) => add(h.role === "user" ? "user" : "bot", h.text));
  }
  if (saved.open) open(true);
})();
