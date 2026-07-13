# Agents Guide

## Deploying to Open WebUI


### Upload / update

```bash
mise exec -- uv run python upload.py            # dist/context.py (default)
```

Known targets upload the generated single-file artifact from `dist/`. This
pushes the file to the live Open WebUI instance via
`POST /api/v1/functions/id/<function_id>/update`. The function reloads
immediately — no restart required.

### First-time create (if the function doesn't exist yet)

```bash
mise exec -- uv run python upload.py context --create
```

## Development workflow

1. Edit source under `src/owui_manifolds/filters/`
2. Run `uv run python scripts/build_functions.py`
3. Commit both source changes and generated `dist/context.py`
4. Run `mise exec -- uv run python upload.py context` to deploy

Use `uv run python scripts/build_functions.py --check` in tests/CI to ensure
`dist/context.py` matches the modular source.

## Environment and live-system safety

Use `mise exec -- <command>` for any command that needs Open WebUI credentials
or live instance configuration. Do not rely on `source .env`,
`python-dotenv`, or `dotenv_values()` in verification scripts; this repo's
working environment is provided by mise, and `.env` may be absent, incomplete,
or intentionally different from the active shell environment.

Examples:

```bash
mise exec -- uv run python upload.py context
mise exec -- bash -lc 'BASE="${OWUI_URL%/}"; curl -s "$BASE/api/models?refresh=true" -H "Authorization: Bearer $OWUI_API_KEY"'
```

Do not run live chat completions, mutate existing chats, or upload functions
unless the user has explicitly asked for that live operation in the current
task. Read-only inspection of a user-provided chat ID is acceptable when
debugging an error, but new completions and uploads affect the live system
and should be called out before running.

When a live verification is approved, prefer creating a new chat unless the
user specifically asks to test in an existing chat. If you must use an existing
chat, state the chat ID and what will be posted before sending the request.

## Reviewing a conversation for errors

Use the Open WebUI API to fetch a chat by ID and inspect its message history,
status history, and error logs. You'll need to execute using
`mise exec -- <command>` to get the appropriate environment variables.

Always retrieve chats in two separate steps — download to a file first, then
analyze that file. Never pipe the response directly into `python`/`json.tool`;
it's inefficient and discards the raw data.

```bash
export CHAT_ID=282df76e-c702-4768-9351-b7ae11b219be

# 1. Download to a file
mise exec -- bash -lc 'BASE="${OWUI_URL%/}"; curl -s "$BASE/api/v1/chats/$CHAT_ID" -H "Authorization: Bearer $OWUI_API_KEY" -o "/tmp/chat_$CHAT_ID.json"'

# 2. Analyze the file
python3 -m json.tool /tmp/chat_$CHAT_ID.json
```


Key fields to look at in the response:

| Field | What to look for |
|---|---|
| `chat.history.messages.<id>.statusHistory` | Per-message status steps and error descriptions |
| `chat.history.messages.<id>.sources` | Attached error log citations from the manifold |
| `chat.history.messages.<id>.content` | Final assistant response (empty string = failed turn) |
| `chat.history.messages.<id>.done` | `false` means the turn never completed |

Error details (stack traces, API error messages) are captured in the `sources`
array of the assistant message under `source.name = "Error Logs"`.

## Verifying the context filter end-to-end in Open WebUI

Do not stop at local unit tests when changing the filter's live behavior. Verify
through the Open WebUI API after the user approves live verification.

Recommended workflow:

1. **Upload the updated function**
   ```bash
   mise exec -- uv run python upload.py context
   ```

2. **Run a real chat completion through Open WebUI**
   Prefer calling `/api/chat/completions` (or `/api/v1/chat/completions`) with a
   real `chat_id`, `user_message`, and assistant `id` so Open WebUI persists the
   turn exactly as the UI would, and the filter's `inlet` runs against a real
   chat's stored `meta`.

3. **Read back the stored chat message**
   Fetch the chat via `/api/v1/chats/<chat_id>` and inspect:
   - `chat.meta.context_manager` — persisted anchor/block-summary state
   - the assistant message's `statusHistory` for trim/fold status text
   - the assistant `content` for a clean, user-visible response

## Environment variables

| Variable       | Description                          |
|----------------|--------------------------------------|
| `OWUI_URL`     | Base URL of your Open WebUI instance |
| `OWUI_API_KEY` | Admin API key (`sk-...`)             |

`.env` is gitignored. Never commit credentials.
