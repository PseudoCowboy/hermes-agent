#!/usr/bin/env python3
"""Telegram Web end-to-end test driver for the Hermes bot.

This script drives a real Telegram Web session from the user's side (not the
bot side) so we can validate that messages flow through the gateway and come
back with the expected replies — without a test-only BotAPI backdoor.

How it works
------------
Playwright launches Chromium with a **persistent user-data directory** so the
Telegram Web login survives across runs.  On first launch the user scans the
QR code once; subsequent runs reuse the session cookie/storage.

The driver opens the bot chat, sends each scripted message, waits for the
bot's reply bubble(s), and checks for expected substrings.

Usage
-----
    python scripts/telegram_e2e.py --bot pseudo_duck_bot
    python scripts/telegram_e2e.py --bot pseudo_duck_bot \
        --scenarios scripts/telegram_e2e_scenarios.yaml

First run: pass --headed so you can scan the QR code.  Subsequent runs can
omit --headed.

Scenarios file (YAML, optional) schema:
    - send: "ping"
      expect:
        - "Pong"
      timeout: 30

If no scenarios file is provided, a minimal smoke suite is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # Scenarios file is optional — only needed for --scenarios


DEFAULT_PROFILE_DIR = Path.home() / ".telegram-e2e-profile"


DEFAULT_SCENARIOS: list[dict] = [
    {
        "send": "ping",
        "expect": ["Pong", "Hermes"],
        "timeout": 30,
    },
    {
        "send": "/help",
        "expect": ["command"],
        "timeout": 30,
    },
]


# Telegram Web stable selectors (validated 2026-04-22 against web.telegram.org/k/)
SEL_INPUT = '.input-message-input[contenteditable="true"]'
SEL_BUBBLES = '.bubbles-inner .bubble'
SEL_BUBBLE_IN = '.bubbles-inner .bubble.is-in'
SEL_BUBBLE_MESSAGE = '.message'


@dataclass
class Scenario:
    send: str
    expect: List[str] = field(default_factory=list)
    timeout: int = 30

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        return cls(
            send=d["send"],
            expect=list(d.get("expect", [])),
            timeout=int(d.get("timeout", 30)),
        )


@dataclass
class Result:
    scenario: Scenario
    sent_ok: bool
    reply_text: Optional[str]
    matched: List[str]
    missing: List[str]
    elapsed_s: float

    @property
    def passed(self) -> bool:
        return self.sent_ok and not self.missing


def load_scenarios(path: Optional[Path]) -> List[Scenario]:
    if path is None:
        return [Scenario.from_dict(d) for d in DEFAULT_SCENARIOS]
    if yaml is None:
        sys.exit("PyYAML is required for --scenarios. `pip install pyyaml`.")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list):
        sys.exit(f"Scenarios file {path} must contain a YAML list.")
    return [Scenario.from_dict(d) for d in data]


def run_scenario(page, scenario: Scenario) -> Result:
    """Send one message and wait for the bot reply(s).

    Strategy:
      1. Record current bubble count.
      2. Type + Enter in the input.
      3. Poll bubble count until it grows by ≥2 (our send + at least one reply)
         OR timeout.  The reply text is the concatenation of all new `.is-in`
         bubbles that appeared after our send.
    """
    start = time.monotonic()

    before = page.evaluate(f"document.querySelectorAll('{SEL_BUBBLES}').length")

    input_el = page.query_selector(SEL_INPUT)
    if input_el is None:
        return Result(scenario, False, None, [], scenario.expect, 0.0)

    input_el.focus()
    page.evaluate(
        """(text) => {
            const el = document.querySelector('.input-message-input[contenteditable=\"true\"]');
            el.focus();
            el.innerHTML = '';
            document.execCommand('insertText', false, text);
        }""",
        scenario.send,
    )
    page.keyboard.press("Enter")

    deadline = time.monotonic() + scenario.timeout
    reply_text = ""
    while time.monotonic() < deadline:
        snapshot = page.evaluate(
            """(before) => {
                const all = Array.from(document.querySelectorAll('.bubbles-inner .bubble'));
                const news = all.slice(before);
                return news.map(b => ({
                    isIn: b.classList.contains('is-in'),
                    isOut: b.classList.contains('is-out'),
                    text: (b.querySelector('.message')?.innerText || '').trim(),
                }));
            }""",
            before,
        )
        # Collect reply text from all new incoming bubbles.
        reply_parts = [b["text"] for b in snapshot if b.get("isIn") and b.get("text")]
        reply_text = "\n\n".join(reply_parts)
        if reply_text:
            # We have at least one reply.  Wait a beat in case multiple bubbles
            # are landing (e.g., "home channel" hint + actual reply), then bail.
            time.sleep(1.5)
            snapshot = page.evaluate(
                """(before) => {
                    const all = Array.from(document.querySelectorAll('.bubbles-inner .bubble'));
                    const news = all.slice(before);
                    return news.map(b => ({
                        isIn: b.classList.contains('is-in'),
                        text: (b.querySelector('.message')?.innerText || '').trim(),
                    }));
                }""",
                before,
            )
            reply_text = "\n\n".join(
                b["text"] for b in snapshot if b.get("isIn") and b.get("text")
            )
            break
        time.sleep(0.5)

    matched = [kw for kw in scenario.expect if kw.lower() in reply_text.lower()]
    missing = [kw for kw in scenario.expect if kw.lower() not in reply_text.lower()]

    return Result(
        scenario=scenario,
        sent_ok=True,
        reply_text=reply_text or None,
        matched=matched,
        missing=missing,
        elapsed_s=time.monotonic() - start,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bot", required=True, help="Bot username, e.g. pseudo_duck_bot")
    ap.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"Persistent Chromium profile directory (default: {DEFAULT_PROFILE_DIR}).",
    )
    ap.add_argument(
        "--scenarios",
        type=Path,
        default=None,
        help="YAML file with test scenarios (optional; built-in smoke suite if omitted).",
    )
    ap.add_argument("--headed", action="store_true", help="Show browser window (use on first run to scan QR).")
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON results to stdout (in addition to human-readable output on stderr).",
    )
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright not installed.  Run:\n"
            "  pip install playwright pyyaml\n"
            "  playwright install chromium"
        )

    scenarios = load_scenarios(args.scenarios)
    args.profile_dir.mkdir(parents=True, exist_ok=True)

    chat_url = f"https://web.telegram.org/k/#@{args.bot}"

    results: List[Result] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir),
            headless=not args.headed,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.goto(chat_url, wait_until="domcontentloaded")
        # Wait for either the login QR screen or the chat input.
        try:
            page.wait_for_selector(SEL_INPUT, timeout=30_000)
        except Exception:
            if page.query_selector('text=Log in to Telegram') is not None:
                sys.stderr.write(
                    "\n[!] Telegram Web is showing the login screen.  Re-run with --headed,\n"
                    "    scan the QR code (or log in by phone), then re-run normally.\n\n"
                )
                ctx.close()
                return 2
            raise

        # Small settle delay for chat history to render before we start.
        time.sleep(2)

        for scenario in scenarios:
            sys.stderr.write(f"→ send: {scenario.send!r}\n")
            res = run_scenario(page, scenario)
            results.append(res)
            status = "PASS" if res.passed else "FAIL"
            sys.stderr.write(
                f"  [{status}] reply={(res.reply_text or '')[:120]!r} "
                f"matched={res.matched} missing={res.missing} "
                f"elapsed={res.elapsed_s:.1f}s\n"
            )
            # Brief gap between scenarios so the bot can settle.
            time.sleep(2)

        ctx.close()

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    sys.stderr.write(f"\nResult: {passed}/{total} scenarios passed\n")

    if args.json:
        json.dump(
            [
                {
                    "send": r.scenario.send,
                    "expect": r.scenario.expect,
                    "reply": r.reply_text,
                    "matched": r.matched,
                    "missing": r.missing,
                    "elapsed_s": round(r.elapsed_s, 2),
                    "passed": r.passed,
                }
                for r in results
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
