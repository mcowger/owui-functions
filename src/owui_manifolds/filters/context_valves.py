"""Pydantic valve models exposed through Filter.Valves and Filter.UserValves."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ContextValves(BaseModel):
    enable_processing: bool = Field(
        default=True, description="🔄 Enable context-window trimming"
    )
    excluded_models: str = Field(
        default="", description="🚫 Excluded model list (comma separated substrings)"
    )

    # ========== Model matching ==========
    model_token_table: str = Field(
        default=(
            "# pattern,max_tokens,cap_percent,warning_percent\n"
            "# pattern is a glob (fnmatch-style *), matched against the lowercased\n"
            "# model name. First match wins - list more specific patterns (e.g.\n"
            "# claude-sonnet-4-5*) before broader ones (e.g. claude-sonnet-*).\n"
            "gpt-5*,1000000,92,80\n"
            "gpt-4o*,128000,92,80\n"
            "gpt-4*,8192,92,80\n"
            "claude-sonnet-4-5*,1000000,92,80\n"
            "claude-sonnet-5*,1000000,92,80\n"
            "claude-sonnet-*,200000,92,80\n"
            "claude-opus-4-5*,1000000,92,80\n"
            "claude-opus-5*,1000000,92,80\n"
            "claude-opus-*,200000,92,80\n"
            "claude-haiku-*,200000,92,80\n"
            "claude-fable-5*,1000000,92,80\n"
            "claude-fable-*,200000,92,80\n"
            "claude-4*,200000,92,80\n"
            "claude-3*,200000,92,80\n"
            "claude*,200000,92,80\n"
            "doubao*vision*,128000,92,80\n"
            "doubao*,50000,92,80\n"
            "glm-5.2*,500000,92,80\n"
            "glm-5.1*,262000,92,80\n"
            "gemini*,1000000,92,80\n"
            "qwen*vl*,32000,92,80\n"
            "*,300000,92,80"
        ),
        description="📋 Model→context-window table, one 'pattern,max_tokens,cap_percent,"
        "warning_percent' row per line (# comments allowed). Edit this to add/adjust "
        "models without a code change. cap_percent is the safety margin applied to "
        "max_tokens; warning_percent is where the early-compression prompt fires, as a "
        "percent of the resulting budget. The trailing '*' row is the catch-all for "
        "unrecognized models - keep it last, or replace it with your own default.",
    )
    default_image_tokens: int = Field(
        default=1500,
        description="🖼️ Flat per-image token estimate for multimodal messages",
    )
    min_target_tokens: int = Field(
        default=10000, description="⚖️ Absolute floor for the history token budget"
    )

    # ========== Response buffer ==========
    response_buffer_percent: int = Field(
        default=6,
        ge=1,
        le=100,
        description="📝 Reserved response space, as a percent of the model limit",
    )
    response_buffer_max: int = Field(
        default=3000, description="📝 Max reserved response space (tokens)"
    )
    response_buffer_min: int = Field(
        default=1000, description="📝 Min reserved response space (tokens)"
    )

    # ========== Anchor / recent message defaults ==========
    anchor_message_count_default: int = Field(
        default=2,
        description="⚓ Default number of earliest history messages to always keep verbatim "
        "(users can override via their own valve)",
    )
    recent_message_count_default: int = Field(
        default=20,
        description="🕐 Default number of most-recent history messages to always keep verbatim "
        "(users can override via their own valve)",
    )

    # ========== Overflow summarization ==========
    enable_overflow_summary: bool = Field(
        default=True,
        description="📚 Summarize history that doesn't fit the anchor/recent window instead of dropping it",
    )
    api_base: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3",
        description="🔗 API base URL",
    )
    api_key: str = Field(default="", description="🔑 API key")
    summary_model: str = Field(
        default="doubao-1-5-lite-32k-250115",
        description="📝 Model used for overflow summarization",
    )
    summary_api_style: Literal["chat_completions", "responses"] = Field(
        default="chat_completions",
        description="🧭 Native OpenAI API style used by the summary backend.",
    )
    summary_provider_function_id: str = Field(
        default="",
        description=(
            "🔐 Optional installed pipe function ID whose BASE_URL/API_KEY valves "
            "supply summary credentials without duplicating secrets."
        ),
    )
    summary_prompt: str = Field(
        default="You are compressing an older portion of a conversation so it can be "
        "dropped from the active context window without losing important information.\n\n"
        "The transcript below contains multiple separate exchanges. Treat this as a "
        "checklist: list every exchange in order, and for each one, either (a) summarize "
        'it, or (b) explicitly note "small talk, nothing to preserve" - never silently '
        "skip one.\n\n"
        "For each exchange, preserve concrete facts, decisions, constraints, numbers, "
        "names, and technical/code/config details, and any open questions. Do not use "
        "vague filler like 'they discussed various topics' - be specific. Output plain "
        "text only, no preamble.",
        description="📝 System prompt for the overflow-summary call",
    )
    summary_max_tokens: int = Field(
        default=500, description="📝 Max output tokens for the overflow summary"
    )
    summary_input_max_tokens: int = Field(
        default=250000,
        ge=1000,
        le=250000,
        description=(
            "🧩 Maximum input tokens per map-reduce summary chunk. All supported "
            "summary models are expected to provide at least 262K context."
        ),
    )
    max_summary_calls_per_request: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "🛑 Safety ceiling for summary-model calls during one request. If "
            "compaction cannot converge within it, the provider request is refused."
        ),
    )
    request_timeout: int = Field(
        default=90, description="⏱️ API request timeout (seconds)"
    )
    api_error_retry_times: int = Field(
        default=2, description="🔄 API error retry count"
    )
    api_error_retry_delay: float = Field(
        default=1.0, description="⏱️ API error retry delay (seconds)"
    )

    # ========== Early warning prompt ==========
    enable_warning_prompt: bool = Field(
        default=True,
        description="⚠️ Before the hard budget cap is hit, ask the user whether to compress "
        "early (requires an active browser session; silently skipped otherwise)",
    )
    warning_reprompt_interval_turns: int = Field(
        default=10,
        ge=1,
        description="⚠️ Minimum number of turns (user+assistant message pairs) between "
        "warning prompts, so accepting/declining once doesn't silence the warning forever",
    )

    debug_level: int = Field(default=0, description="🐛 Debug log verbosity (0-2)")


class ContextUserValves(BaseModel):
    context_target_percent: int = Field(
        default=50,
        ge=1,
        le=100,
        description="🎯 Percent of the model's context window to actually use for conversation "
        "history (e.g. 50 = target half the window, even on a 1M-token model). Lower is cheaper.",
    )
    anchor_message_count: Optional[int] = Field(
        default=None,
        description="⚓ Number of earliest history messages to always keep verbatim. "
        "Leave blank to use the admin-configured default.",
    )
    recent_message_count: Optional[int] = Field(
        default=None,
        description="🕐 Number of most-recent history messages to always keep verbatim. "
        "Leave blank to use the admin-configured default.",
    )
