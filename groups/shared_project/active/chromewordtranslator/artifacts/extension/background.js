// background.js — MV3 service worker. Owns the local dictionary and
// responds to GET_TRANSLATION messages from content scripts.

import { SEED_DICTIONARY } from "./seed-dictionary.js";

const STORAGE_KEY = "dictionary";

// On install: seed storage only if the dictionary is empty.
chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(STORAGE_KEY);
  if (existing && existing[STORAGE_KEY]) return;
  const seeded = {};
  for (const [en, zh] of Object.entries(SEED_DICTIONARY)) {
    seeded[en.toLowerCase()] = { zh, suppress: false };
  }
  await chrome.storage.local.set({ [STORAGE_KEY]: seeded });
});

async function lookup(word) {
  const key = (word || "").toLowerCase().trim();
  if (!key) return { found: false };
  const store = await chrome.storage.local.get(STORAGE_KEY);
  const dict = (store && store[STORAGE_KEY]) || {};
  const entry = dict[key];
  if (!entry) return { found: false, word: key };
  if (entry.suppress) return { found: true, word: key, suppress: true };
  return { found: true, word: key, zh: entry.zh || "" };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "GET_TRANSLATION") return false;
  lookup(msg.word).then(sendResponse);
  return true; // async response
});
