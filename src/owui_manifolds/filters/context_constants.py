"""Shared constants for the context window manager filter."""

# Flat per-message token overhead to roughly account for chat-formatting tokens
# (role markers, separators) that tiktoken's plain-text encoding doesn't capture.
MESSAGE_OVERHEAD_TOKENS = 20

# Conservative cap on how much of the "excluded middle" text we hand to the
# summarization model in one call, so an extremely long conversation can't blow
# out the summarizer's own context window. If exceeded, only the most recent
# portion (still ordered oldest->newest) of the excluded messages is sent.
SUMMARY_INPUT_TOKEN_CAP = 250000

# Key under chat.meta where persisted anchor/block-summary state is stored.
CONTEXT_MANAGER_META_KEY = "context_manager"

# Last-resort fallback if ContextValves.model_token_table has no rows at all
# (e.g. an admin clears it) - the table's own trailing "*" row is the intended
# catch-all and normally makes this dead code.
NO_TABLE_ROWS_FALLBACK_LIMIT = 200000
NO_TABLE_ROWS_FALLBACK_CAP_PERCENT = 92
NO_TABLE_ROWS_FALLBACK_WARNING_PERCENT = 80
