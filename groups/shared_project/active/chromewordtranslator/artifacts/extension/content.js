// content.js — listens for double-click on a word, queries the background
// service worker for a translation, and renders a small tooltip.

(() => {
  const TOOLTIP_ID = "__word_translator_tip__";
  const WORD_RE = /^[A-Za-z][A-Za-z'-]{0,48}$/;

  function removeTooltip() {
    const el = document.getElementById(TOOLTIP_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function showTooltip(x, y, text) {
    removeTooltip();
    const tip = document.createElement("div");
    tip.id = TOOLTIP_ID;
    tip.textContent = text;
    tip.style.left = `${Math.max(0, x)}px`;
    tip.style.top = `${Math.max(0, y)}px`;
    document.body.appendChild(tip);
    // Auto-dismiss after 4s or on any click elsewhere.
    setTimeout(removeTooltip, 4000);
  }

  document.addEventListener("click", (e) => {
    const tip = document.getElementById(TOOLTIP_ID);
    if (tip && !tip.contains(e.target)) removeTooltip();
  });

  document.addEventListener("dblclick", (e) => {
    const sel = window.getSelection();
    if (!sel) return;
    const raw = (sel.toString() || "").trim();
    if (!raw || !WORD_RE.test(raw)) return;

    const x = e.pageX + 8;
    const y = e.pageY + 16;

    chrome.runtime.sendMessage(
      { type: "GET_TRANSLATION", word: raw },
      (resp) => {
        if (chrome.runtime.lastError) return;
        if (!resp || !resp.found) {
          showTooltip(x, y, `${raw} — (no translation)`);
          return;
        }
        if (resp.suppress) {
          // User explicitly marked this word as "no translation".
          return;
        }
        showTooltip(x, y, `${raw} → ${resp.zh || "(empty)"}`);
      },
    );
  });
})();
