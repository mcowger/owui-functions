"""Chunk-safe summary API helpers for context and tool-result compaction."""

import asyncio
import inspect
import json
from typing import List, Optional

from owui_manifolds.filters.context_constants import SUMMARY_INPUT_TOKEN_CAP


class SummaryCallBudgetExceeded(RuntimeError):
    pass


try:
    from openai import AsyncOpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    AsyncOpenAI = None


class ContextSummaryMixin:
    async def get_api_client(self):
        if not OPENAI_AVAILABLE:
            return None

        base_url = self.valves.api_base
        api_key = self.valves.api_key
        source_id = self.valves.summary_provider_function_id.strip()
        if source_id:
            try:
                from open_webui.models.functions import Functions

                source_valves = (
                    await Functions.get_function_valves_by_id(source_id) or {}
                )
                base_url = source_valves.get("BASE_URL") or base_url
                api_key = source_valves.get("API_KEY") or api_key
            except Exception:
                self.debug_log(
                    1,
                    f"Could not load summary credentials from function {source_id}",
                    "⚠️",
                )
        if not api_key:
            return None
        return AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=self.valves.request_timeout,
        )

    async def safe_api_call(self, call_func, call_name: str):
        for attempt in range(self.valves.api_error_retry_times + 1):
            try:
                return await call_func()
            except SummaryCallBudgetExceeded:
                return None
            except Exception as e:
                if attempt < self.valves.api_error_retry_times:
                    self.debug_log(
                        1,
                        f"{call_name} failed (attempt {attempt + 1}), retrying in "
                        f"{self.valves.api_error_retry_delay}s: {str(e)[:150]}",
                        "🔄",
                    )
                    await asyncio.sleep(self.valves.api_error_retry_delay)
                else:
                    self.debug_log(
                        1, f"{call_name} failed permanently: {str(e)[:150]}", "❌"
                    )
                    return None
        return None

    def _messages_to_text(self, messages: List[dict]) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "")
            text = self.extract_text_from_content(msg.get("content", ""))
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                parts.append(
                    f"{role} tool_calls: "
                    f"{json.dumps(tool_calls, ensure_ascii=False, default=str)}"
                )
            if text:
                identity = ""
                if role == "tool":
                    identity = (
                        f" tool_call_id={msg.get('tool_call_id', '')}"
                        f" name={msg.get('name', '')}"
                    )
                parts.append(f"{role}{identity}: {text}")
        return "\n\n".join(parts)

    def _summary_input_chunks(self, text: str) -> List[str]:
        """Split without dropping content before invoking the summary model."""

        if not text:
            return []
        input_token_cap = min(
            SUMMARY_INPUT_TOKEN_CAP,
            int(self.valves.summary_input_max_tokens),
        )
        encoding = self.token_calculator.get_encoding()
        if encoding is not None:
            tokens = encoding.encode(text)
            return [
                encoding.decode(tokens[start : start + input_token_cap])
                for start in range(0, len(tokens), input_token_cap)
            ]

        # tiktoken is declared by the built artifacts; this fallback is only for
        # minimal standalone environments. A BPE token cannot represent less
        # than one source byte, so limiting UTF-8 bytes is conservative without
        # dropping or corrupting Unicode content.
        byte_limit = max(1, input_token_cap)
        chunks: List[str] = []
        start = 0
        while start < len(text):
            low = start + 1
            high = min(len(text), start + byte_limit) + 1
            while low < high:
                middle = (low + high) // 2
                if len(text[start:middle].encode("utf-8")) <= byte_limit:
                    low = middle + 1
                else:
                    high = middle
            end = max(start + 1, low - 1)
            chunks.append(text[start:end])
            start = end
        return chunks

    def _claim_summary_call(self, call_name: str) -> bool:
        used = int(getattr(self, "_summary_calls_used", 0))
        limit = int(self.valves.max_summary_calls_per_request)
        if used >= limit:
            self.debug_log(
                1,
                f"Summary-call safety budget exhausted before {call_name} ({used}/{limit})",
                "🛑",
            )
            return False
        self._summary_calls_used = used + 1
        return True

    async def _summarize_text(
        self,
        text: str,
        *,
        system_prompt: str,
        call_name: str,
        target_tokens: int | None = None,
    ) -> Optional[str]:
        client = self.get_api_client()
        if inspect.isawaitable(client):
            client = await client
        if not client or not text.strip():
            return None

        target_tokens = max(
            64,
            min(
                int(target_tokens or self.valves.summary_max_tokens),
                int(self.valves.summary_max_tokens),
            ),
        )

        async def summarize_chunk(chunk: str, index: int, count: int) -> Optional[str]:
            user_content = (
                f"Input chunk {index}/{count}. Preserve information from "
                "this chunk; do not assume omitted chunks are irrelevant.\n\n"
                f"{chunk}"
            )

            async def _call():
                if not self._claim_summary_call(f"{call_name} chunk {index}/{count}"):
                    raise SummaryCallBudgetExceeded()
                if self.valves.summary_api_style == "responses":
                    response = await client.responses.create(
                        model=self.valves.summary_model,
                        instructions=system_prompt,
                        input=user_content,
                        max_output_tokens=target_tokens,
                        store=False,
                    )
                    return response.output_text

                response = await client.chat.completions.create(
                    model=self.valves.summary_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=target_tokens,
                )
                return response.choices[0].message.content

            result = await self.safe_api_call(
                _call, f"{call_name} chunk {index}/{count}"
            )
            return (
                result.strip() if isinstance(result, str) and result.strip() else None
            )

        chunks = self._summary_input_chunks(text)
        summaries = []
        for index, chunk in enumerate(chunks, start=1):
            summaries.append(await summarize_chunk(chunk, index, len(chunks)))
        if any(summary is None for summary in summaries):
            return None

        combined = "\n\n".join(summary for summary in summaries if summary)
        # Map summaries are intermediate data. Always reduce them to the
        # caller's assigned budget rather than accumulating one summary_max
        # output per source chunk in the final context.
        if len(chunks) > 1 and self.count_tokens(combined) > target_tokens:
            return await self._summarize_text(
                combined,
                system_prompt=system_prompt,
                call_name=f"{call_name} reduction",
                target_tokens=target_tokens,
            )
        return combined.strip() or None

    async def generate_overflow_summary(
        self,
        excluded_messages: List[dict],
        target_tokens: int | None = None,
    ) -> Optional[str]:
        return await self._summarize_text(
            self._messages_to_text(excluded_messages),
            system_prompt=self.valves.summary_prompt,
            call_name="overflow summary",
            target_tokens=target_tokens,
        )

    async def generate_tool_result_summary(
        self,
        message: dict,
        target_tokens: int | None = None,
    ) -> Optional[str]:
        content = self.extract_text_from_content(message.get("content", ""))
        if not content.strip():
            return None
        identity = {
            "tool_call_id": message.get("tool_call_id"),
            "name": message.get("name"),
        }
        prompt = (
            "You are compacting one tool result so an agent can continue within its "
            "context window. Preserve concrete findings, identifiers, URLs, errors, "
            "numbers, code/configuration details, and information needed for follow-up "
            "tool calls. Remove repetition and irrelevant boilerplate. Do not invent "
            "facts. Output plain text only."
        )
        return await self._summarize_text(
            f"Tool result identity: {json.dumps(identity, ensure_ascii=False)}\n\n{content}",
            system_prompt=prompt,
            call_name="tool result summary",
            target_tokens=target_tokens,
        )
