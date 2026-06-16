#!/usr/bin/env python3
"""Upload responses.py to Open WebUI as a pipe function.

Reads connection details from .env (copy .env.example and fill in your values).

Usage:
    python upload.py                  # update existing function
    python upload.py --create         # create instead of update (first time)
    python upload.py --id my_func_id  # override the function id
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
FUNCTION_FILE = SCRIPT_DIR / "responses.py"


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload responses.py to Open WebUI")
    parser.add_argument("--create", action="store_true", help="Create instead of update")
    parser.add_argument("--id", default="openai_responses", help="Function id in Open WebUI (default: openai_responses)")
    parser.add_argument("--name", default="OpenAI Responses API Manifold", help="Display name")
    args = parser.parse_args()

    base_url = (os.getenv("OWUI_URL") or "").rstrip("/")
    api_key = os.getenv("OWUI_API_KEY") or ""

    if not base_url:
        sys.exit("OWUI_URL is not set. Copy .env.example to .env and fill in your values.")
    if not api_key:
        sys.exit("OWUI_API_KEY is not set. Copy .env.example to .env and fill in your values.")

    content = FUNCTION_FILE.read_text(encoding="utf-8")
    print(f"Read {len(content):,} bytes from {FUNCTION_FILE.name}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "id": args.id,
        "name": args.name,
        "content": content,
        "meta": {"description": "OpenAI Responses API Manifold"},
    }

    if args.create:
        url = f"{base_url}/api/v1/functions/create"
        action = "Creating"
    else:
        url = f"{base_url}/api/v1/functions/id/{args.id}/update"
        action = "Updating"

    print(f"{action} function '{args.id}' at {base_url} ...")
    resp = requests.post(url, json=payload, headers=headers, timeout=30)

    if resp.ok:
        data = resp.json()
        print(f"Done. Function id={data.get('id')} type={data.get('type')} active={data.get('is_active')}")
    else:
        print(f"Error {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
