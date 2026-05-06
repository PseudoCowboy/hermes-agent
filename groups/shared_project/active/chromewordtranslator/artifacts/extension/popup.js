// popup.js — dictionary editor. All data lives in chrome.storage.local.

const STORAGE_KEY = "dictionary";
const WORD_RE = /^[A-Za-z][A-Za-z'-]{0,47}$/;

const $ = (id) => document.getElementById(id);

async function loadDict() {
  const { [STORAGE_KEY]: dict } = await chrome.storage.local.get(STORAGE_KEY);
  return dict || {};
}

async function saveDict(dict) {
  await chrome.storage.local.set({ [STORAGE_KEY]: dict });
}

function setMsg(text, ok = false) {
  const el = $("msg");
  el.textContent = text || "";
  el.classList.toggle("ok", !!ok);
}

function render(dict, filter = "") {
  const items = $("items");
  items.innerHTML = "";
  const keys = Object.keys(dict)
    .filter((k) => !filter || k.includes(filter.toLowerCase()))
    .sort();

  if (keys.length === 0) {
    const li = document.createElement("li");
    li.textContent = filter ? "(no matches)" : "(dictionary empty)";
    items.appendChild(li);
    return;
  }

  for (const k of keys) {
    const entry = dict[k];
    const li = document.createElement("li");
    const pair = document.createElement("div");
    pair.className = "pair";

    const en = document.createElement("span");
    en.className = "en";
    en.textContent = k;
    pair.appendChild(en);

    if (entry.suppress) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "no translation";
      pair.appendChild(tag);
    } else {
      const zh = document.createElement("span");
      zh.className = "zh";
      zh.textContent = entry.zh || "(empty)";
      pair.appendChild(zh);
    }

    const editBtn = document.createElement("button");
    editBtn.textContent = "edit";
    editBtn.addEventListener("click", () => {
      $("en").value = k;
      $("zh").value = entry.zh || "";
      $("suppress").checked = !!entry.suppress;
      $("en").focus();
    });

    const delBtn = document.createElement("button");
    delBtn.textContent = "del";
    delBtn.className = "del";
    delBtn.addEventListener("click", async () => {
      delete dict[k];
      await saveDict(dict);
      render(dict, $("filter").value);
      setMsg(`deleted ${k}`, true);
    });

    li.appendChild(pair);
    li.appendChild(editBtn);
    li.appendChild(delBtn);
    items.appendChild(li);
  }
}

async function init() {
  let dict = await loadDict();
  render(dict);

  $("addBtn").addEventListener("click", async () => {
    const en = ($("en").value || "").trim();
    const zh = ($("zh").value || "").trim();
    const suppress = $("suppress").checked;

    if (!WORD_RE.test(en)) {
      setMsg("English must be 1–48 letters (optionally with ' or -), starting with a letter");
      return;
    }
    if (!suppress && !zh) {
      setMsg("Provide a Chinese translation or check 'no translation'");
      return;
    }

    const key = en.toLowerCase();
    dict[key] = { zh: suppress ? "" : zh, suppress };
    await saveDict(dict);
    setMsg(`saved ${key}`, true);
    $("en").value = "";
    $("zh").value = "";
    $("suppress").checked = false;
    render(dict, $("filter").value);
  });

  $("filter").addEventListener("input", () => render(dict, $("filter").value));

  // Keep popup in sync if storage changes (e.g. background seeding).
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[STORAGE_KEY]) {
      dict = changes[STORAGE_KEY].newValue || {};
      render(dict, $("filter").value);
    }
  });
}

init();
