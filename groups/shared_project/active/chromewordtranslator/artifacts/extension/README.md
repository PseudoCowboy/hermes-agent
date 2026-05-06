# Word Translator (EN → ZH)

A Chrome extension that shows a Chinese translation when you double-click an English word. No network calls — the dictionary lives in `chrome.storage.local` and is fully user-editable through the extension popup.

## Features

- **Double-click to translate.** Select any single English word; a tooltip shows the Chinese translation.
- **Offline, user-managed.** Ships with a seed of ~20 common words. Add, edit, or delete entries through the popup.
- **"No translation" flag.** Suppress the tooltip for a specific word (useful for proper nouns, false positives, etc).

## Install (Load Unpacked)

1. Open `chrome://extensions/` in Chrome (or any Chromium-based browser).
2. Enable **Developer mode** (top right).
3. Click **Load unpacked** and select this `extension/` directory.
4. Pin the extension from the toolbar if you want quick access to the dictionary editor.

## Use

- **Translate:** Double-click a single English word on any page. A dark tooltip appears near the cursor with the translation, or `(no translation)` if the word is not in the dictionary.
- **Manage the dictionary:** Click the extension icon.
  - Type an English word + Chinese translation → **Save**.
  - Check **no translation** to suppress the tooltip for that word.
  - Filter the list by typing in the filter box.
  - Click **edit** or **del** on a row to modify it.

## Design Notes

- Manifest V3. Background is a module service worker.
- Single-word only (regex: `^[A-Za-z][A-Za-z'-]{0,48}$`). Phrases are out of scope for v1.
- All edits write through to `chrome.storage.local` under key `dictionary`. The popup listens on `chrome.storage.onChanged` to stay in sync.
- No `host_permissions` are required beyond what the content script already has via `<all_urls>`; we never fetch anything.
