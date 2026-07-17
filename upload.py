#!/usr/bin/env python3
"""Upload an Open WebUI function file.

Reads connection details from .env (copy .env.example and fill in your values).

Usage:
    python upload.py                              # update dist/context.py
    python upload.py context --create             # create dist/context.py
    python upload.py --file custom.py             # explicit filename
    python upload.py --id my_func_id              # override the function id

The target may be given positionally or via --file, as a bare name
("context"), a filename ("context.py"), or a path. Bare names get ".py"
appended automatically. Known targets read generated artifacts from dist/.
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests is not installed. Run: pip install requests")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv not available — rely on environment variables already being set
    pass


SCRIPT_DIR = Path(__file__).parent

DEFAULTS = {
    "context.py": {
        "id": "context_window_manager_simplified",
        "name": "🚀 Context Window Manager (Simplified)",
        "description": "Keeps conversations inside the model's context window by anchoring the "
        "earliest and most recent messages verbatim and persistently, incrementally "
        "summarizing the middle.",
    },
    "anthropic.py": {
        "id": "anthropic_new",
        "name": "Anthropic API Integration",
        "description": "Anthropic API manifold pipe with tool calls, web search/fetch, "
        "prompt caching, extended thinking, and code execution.",
    },
}

# Friendly shorthands -> actual filenames (bare names get ".py" appended
# unless matched here first).
ALIASES: dict[str, str] = {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload an Open WebUI function file")
    parser.add_argument("--create", action="store_true", help="Create instead of update")
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Function to upload: bare name (context), filename (context.py), or path. Default: context",
    )
    parser.add_argument("--file", default=None, help="Function file to upload (alternative to positional target)")
    parser.add_argument("--id", default=None, help="Function id in Open WebUI")
    parser.add_argument("--name", default=None, help="Display name")
    args = parser.parse_args()

    if args.target and args.file:
        sys.exit("Provide either a positional target or --file, not both.")

    raw_target = args.target or args.file or "context"
    # Accept bare names ("gemini"), filenames ("gemini.py"), and paths.
    raw_target = ALIASES.get(raw_target, raw_target)
    if not raw_target.endswith(".py"):
        raw_target = f"{raw_target}.py"

    if raw_target in DEFAULTS:
        candidate = SCRIPT_DIR / "dist" / raw_target
        function_file = candidate if candidate.exists() else SCRIPT_DIR / raw_target
    else:
        function_file = SCRIPT_DIR / raw_target
    if not function_file.exists():
        sys.exit(f"Function file not found: {function_file}")

    defaults = DEFAULTS.get(raw_target, {}) or DEFAULTS.get(function_file.name, {})
    function_id = args.id or defaults.get("id") or function_file.stem
    function_name = args.name or defaults.get("name") or function_file.stem
    description = defaults.get("description") or function_name

    base_url = (os.getenv("OWUI_URL") or "").rstrip("/")
    api_key = os.getenv("OWUI_API_KEY") or ""

    if not base_url:
        sys.exit("OWUI_URL is not set. Copy .env.example to .env and fill in your values.")
    if not api_key:
        sys.exit("OWUI_API_KEY is not set. Copy .env.example to .env and fill in your values.")

    display_file = function_file.relative_to(SCRIPT_DIR)
    content = function_file.read_text(encoding="utf-8")
    print(f"Read {len(content):,} bytes from {display_file}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "id": function_id,
        "name": function_name,
        "content": content,
        "meta": {"description": description},
    }

    if args.create:
        url = f"{base_url}/api/v1/functions/create"
        action = "Creating"
    else:
        url = f"{base_url}/api/v1/functions/id/{function_id}/update"
        action = "Updating"

    print(f"{action} function '{function_id}' from {display_file} at {base_url} ...")
    resp = requests.post(url, json=payload, headers=headers, timeout=30)

    if resp.ok:
        data = resp.json()
        print(f"Done. Function id={data.get('id')} type={data.get('type')} active={data.get('is_active')}")
    else:
        print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
