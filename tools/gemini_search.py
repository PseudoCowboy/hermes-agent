"""Gemini-CLI-backed web search tool.

Wraps the `gemini` CLI in non-interactive (headless) mode to perform a
grounded web search using a fast Gemini model. The CLI's built-in
`google_web_search` tool produces the answer and citations; we just
forward the user's query and capture stdout.

Registered as the `gemini_search` tool under the `gemini` toolset.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from tools.registry import registry


DEFAULT_MODEL = "gemini-3-flash-preview"
DEFAULT_TIMEOUT = 90  # seconds


def check_gemini_cli() -> bool:
    """Tool only registers if the `gemini` binary is on PATH."""
    return shutil.which("gemini") is not None


def gemini_search_tool(query: str, model: str = DEFAULT_MODEL,
                       timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a web search via the Gemini CLI and return the answer text.

    Args:
        query: The search query / question.
        model: Gemini model id. Defaults to gemini-2.5-flash (fast).
        timeout: Max seconds to wait for the CLI.
    """
    query = (query or "").strip()
    if not query:
        return json.dumps({"success": False, "error": "query is required"})

    prompt = (
        "Use the google_web_search tool to research the following query, then "
        "answer concisely. End with a 'Sources:' section listing the top URLs "
        "you used.\n\nQuery: " + query
    )

    cmd = [
        "gemini",
        "-m", model or DEFAULT_MODEL,
        "-y",                # auto-approve tool calls (web_search)
        "-o", "text",
        "-p", prompt,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "success": False,
            "error": f"gemini CLI timed out after {timeout}s",
        })
    except FileNotFoundError:
        return json.dumps({
            "success": False,
            "error": "gemini CLI not found on PATH",
        })

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    # The CLI prints a couple of YOLO-mode banner lines to stdout; strip them.
    cleaned_lines = [
        ln for ln in stdout.splitlines()
        if ln.strip() and not ln.strip().startswith("YOLO mode is enabled")
    ]
    answer = "\n".join(cleaned_lines).strip()

    if proc.returncode != 0 and not answer:
        return json.dumps({
            "success": False,
            "error": f"gemini CLI exited {proc.returncode}",
            "stderr": stderr[-2000:],
        })

    return json.dumps({
        "success": True,
        "model": model or DEFAULT_MODEL,
        "query": query,
        "answer": answer,
    })


GEMINI_SEARCH_SCHEMA = {
    "name": "gemini_search",
    "description": (
        "Web search powered by the Gemini CLI's grounded google_web_search tool. "
        "Uses a fast Gemini model (default: gemini-2.5-flash) to research the "
        "query and return a concise answer with source URLs. Good for quick "
        "factual lookups, current events, and grounded summaries — the model "
        "performs the search and synthesis in one shot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query or question to research.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Gemini model id. Defaults to 'gemini-2.5-flash' for speed. "
                    "Use 'gemini-2.5-pro' for harder questions."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait for the CLI (default 90).",
                "minimum": 10,
                "maximum": 300,
            },
        },
        "required": ["query"],
    },
}


registry.register(
    name="gemini_search",
    toolset="gemini",
    schema=GEMINI_SEARCH_SCHEMA,
    handler=lambda args, **kw: gemini_search_tool(
        query=args.get("query", ""),
        model=args.get("model", DEFAULT_MODEL),
        timeout=int(args.get("timeout", DEFAULT_TIMEOUT)),
    ),
    check_fn=check_gemini_cli,
    requires_env=[],
    emoji="✨",
    max_result_size_chars=100_000,
)
