// First-run walkthrough: dims the page, spotlights one part of the dashboard at a time, and explains it.
// Steps come from the page (#tour-steps JSON). Back / Next / Skip, arrow keys, and Esc. The server remembers
// when it's finished or skipped (POST /tour/<done|skipped>), so it shows once; Settings can replay it.
(function () {
  const data = document.getElementById("tour-steps");
  if (!data) return;
  const steps = JSON.parse(data.textContent);
  const finishUrl = data.dataset.finish;
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let i = 0, target = null, openedNav = false;

  const root = document.createElement("div");
  root.className = "tour";
  root.innerHTML =
    '<div class="tour-block"></div><div class="tour-spot" aria-hidden="true"></div>' +
    '<div class="tour-card" role="dialog" aria-modal="true" aria-labelledby="tour-title" aria-describedby="tour-text">' +
    '  <div class="tour-top"><span class="tour-count"></span><button type="button" class="tour-skip">Skip intro</button></div>' +
    '  <h2 id="tour-title"></h2><p id="tour-text"></p>' +
    '  <div class="tour-foot"><div class="tour-dots" aria-hidden="true"></div>' +
    '    <div class="tour-btns"><button type="button" class="btn small tour-back">Back</button>' +
    '    <button type="button" class="btn primary small tour-next">Next</button></div></div>' +
    "</div>";
  const $ = (s) => root.querySelector(s);
  const card = $(".tour-card"), spot = $(".tour-spot");
  steps.forEach(() => $(".tour-dots").appendChild(document.createElement("i")));

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
  }

  function findTarget(sel) {
    if (!sel) return null;
    let el = document.querySelector(sel);
    if (el && !visible(el) && el.closest("#primary-nav")) {      // phones: the menu is collapsed; open it
      const header = document.querySelector("header.top");
      if (header && !header.classList.contains("open")) {
        header.classList.add("open"); openedNav = true;
      }
    }
    return el && visible(el) ? el : null;
  }

  function place() {
    const pad = 8, vw = window.innerWidth, vh = window.innerHeight;
    if (!target) {                                                // a centered card, no spotlight
      spot.style.cssText = `left:${vw / 2}px;top:${vh / 2}px;width:0;height:0`;
      card.classList.add("center"); card.style.left = card.style.top = "";
      return;
    }
    card.classList.remove("center");
    const r = target.getBoundingClientRect();
    const L = Math.max(3, r.left - pad), T = Math.max(3, r.top - pad);            // keep the ring on screen
    const R = Math.min(vw - 3, r.right + pad), Bm = Math.min(vh - 3, r.bottom + pad);
    spot.style.cssText = `left:${L}px;top:${T}px;width:${Math.max(0, R - L)}px;height:${Math.max(0, Bm - T)}px`;
    if (vw < 600) { card.style.left = card.style.top = ""; card.classList.add("sheet"); return; }   // phones: bottom sheet
    card.classList.remove("sheet");
    const cw = card.offsetWidth, ch = card.offsetHeight, gap = 14;
    let top = r.bottom + pad + gap;
    if (top + ch > vh - 12) top = Math.max(12, r.top - pad - gap - ch);   // no room below: go above
    let left = Math.min(Math.max(12, r.left + r.width / 2 - cw / 2), vw - cw - 12);
    card.style.left = left + "px"; card.style.top = top + "px";
  }

  function show(n) {
    i = Math.max(0, Math.min(steps.length - 1, n));
    const st = steps[i];
    target = findTarget(st.target);
    if (target) target.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    $("#tour-title").textContent = st.title;
    $("#tour-text").textContent = st.text;
    $(".tour-count").textContent = `${i + 1} of ${steps.length}`;
    $(".tour-back").hidden = i === 0;
    $(".tour-next").textContent = i === steps.length - 1 ? (st.cta || "Done") : "Next";
    root.querySelectorAll(".tour-dots i").forEach((d, k) => d.classList.toggle("on", k === i));
    setTimeout(place, reduce ? 0 : 320);                          // after the smooth scroll settles
    place();
    $(".tour-next").focus({ preventScroll: true });
  }

  function end(how) {
    root.remove();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    if (openedNav) document.querySelector("header.top")?.classList.remove("open");
    document.body.classList.remove("touring");
    try { fetch(finishUrl.replace("__how__", how), { method: "POST", headers: { "X-Requested-With": "tour" } }); } catch (e) { /* offline */ }
    const st = steps[i];
    if (how === "done" && st.go) location.href = st.go;
  }

  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); end("skipped"); }
    else if (e.key === "ArrowRight" || (e.key === "Enter" && document.activeElement?.closest(".tour-card") == null)) { e.preventDefault(); next(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); show(i - 1); }
    else if (e.key === "Tab") {                                   // keep focus inside the card
      const f = [...card.querySelectorAll("button:not([hidden])")];
      const k = f.indexOf(document.activeElement);
      e.preventDefault();
      f[(k + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    }
  }
  const next = () => (i === steps.length - 1 ? end("done") : show(i + 1));

  $(".tour-next").addEventListener("click", next);
  $(".tour-back").addEventListener("click", () => show(i - 1));
  $(".tour-skip").addEventListener("click", () => end("skipped"));
  $(".tour-block").addEventListener("click", (e) => e.stopPropagation());

  function start() {
    document.body.appendChild(root);
    document.body.classList.add("touring");
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    show(0);
  }
  if (document.readyState === "complete") setTimeout(start, 300);
  else window.addEventListener("load", () => setTimeout(start, 300));
})();
