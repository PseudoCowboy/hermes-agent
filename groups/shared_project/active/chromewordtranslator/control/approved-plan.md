# Chrome Word Translator — Plan v1

## Goal
Chrome extension that shows an English → Chinese translation tooltip when the user double-clicks an English word on any webpage. User can manage the translation dictionary through the extension icon popup.

## Constraints
- **No network calls.** No remote translation API. All translations come from a local dictionary in `chrome.storage.local`.
- Ships with a small built-in seed dictionary that the user can override or disable per-word.
- Manifest V3 (service-worker background).

## User Stories
1. Double-click a word on any page → a small tooltip appears near the cursor with the Chinese translation, or a "no translation" indicator.
2. Click extension icon → popup lists all dictionary entries, allows add / edit / delete, and allows marking a word as "no translation" (suppresses the tooltip for that word).
3. All user edits persist across browser restarts via `chrome.storage.local`.

## Architecture
- `manifest.json` (MV3): `content_scripts`, `action.default_popup`, `background.service_worker`, `storage` permission.
- `content.js` — injected into all pages; listens for `dblclick`, extracts the selected word, asks background for translation, renders an in-page tooltip.
- `background.js` — service worker; owns the dictionary; responds to `GET_TRANSLATION` messages; seeds `chrome.storage.local` with the built-in dictionary on install.
- `popup.html` + `popup.js` + `popup.css` — dictionary editor UI (list, add, edit, delete, "no translation" toggle).
- `seed-dictionary.js` — a small static EN→ZH map (~30 common words) to ship so the extension works out of the box.

## Data Model
```
chrome.storage.local:
  dictionary: {
    "hello": { zh: "你好", suppress: false },
    "world": { zh: "世界", suppress: false },
    "foo":   { zh: "",     suppress: true  }   // user chose "no translation"
  }
```

## Workstreams
1. **extension-core** (code) — manifest, content script, background service worker, seed dictionary.
2. **popup-ui** (code) — popup HTML/CSS/JS for dictionary management.
3. **packaging** (code) — README with install instructions + icons + final zip.

## Out of Scope
- Online translation fallback (explicit user constraint).
- Phrase / multi-word translation (first version: single words only).
- Other language pairs.
