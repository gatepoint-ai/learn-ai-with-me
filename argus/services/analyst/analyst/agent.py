"""The agent loop: question -> tool calls -> answer -> number check.

Deliberately small. One model, one loop, a hard cap on tool rounds, and no
memory beyond the conversation. The interesting parts are the constraints:
the tools it has, the tools it does not, and the check that runs after it
answers.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from . import verify
from .tools import Toolbox

SYSTEM_PROMPT = """You are the Argus analyst. You answer questions about revenue assurance for \
the operations and finance teams, using only your tools.

Rules -- these are not style preferences:
1. Every number in your answer must come from a tool result. Never calculate, add up, \
average or estimate a figure yourself. If a total or a difference is needed, get it with a \
query_lens aggregate. Your answer is checked: any figure not found in the tool results is \
flagged to the reader.
2. Live figures come from the data tools. Policy, process and responsibility come from \
search_knowledge. Do not answer a policy question from numbers, or a numbers question from \
policy text.
3. When you use search_knowledge, cite each passage you rely on as [Document > Section], \
exactly as the citation field gives it.
4. If the tools do not give you the answer, say so plainly. "The data does not show that" and \
"The documents do not cover that" are good answers. Never guess.
5. Be brief and concrete: lead with the answer, then the evidence. Name sites, dates and \
alert IDs. Say "the data shows" rather than describing models or algorithms.

Today is {today}. The data covers recent daily history; use the tools to find its range."""


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: Any
    duration_ms: int
    is_error: bool

    def preview(self, limit: int = 1500) -> str:
        return json.dumps(self.result, default=str)[:limit]


@dataclass
class AgentResult:
    answer: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    numbers: verify.NumberCheck | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    stopped: str = "answered"  # answered | iteration_limit | error


class OpenAICompatClient:
    """Chat completions with tool calling, over plain HTTP.

    Nearly every free LLM host speaks this wire format, so switching backend
    is a base URL and a model name -- no SDK, no code change.
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        self.model = model
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        for attempt in range(6):
            r = self.http.post("/chat/completions", json={
                "model": self.model, "messages": messages, "tools": tools,
                "tool_choice": "auto", "temperature": 0.1,
            })
            if r.status_code == 429 and attempt < 5:
                # Free tiers limit tokens per minute. Wait as long as the
                # provider says, not a guess -- a guess just burns retries.
                time.sleep(min(30.0, _retry_after(r) + 0.5))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"LLM {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError("LLM rate limited")  # pragma: no cover

    def close(self) -> None:
        self.http.close()


def _retry_after(r: httpx.Response) -> float:
    header = r.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r"try again in ([\d.]+)(ms|s)", r.text)
    if m:
        value = float(m.group(1))
        return value / 1000 if m.group(2) == "ms" else value
    return 5.0


def run(client: OpenAICompatClient, toolbox: Toolbox, question: str,
        history: list[dict[str, Any]], max_iterations: int) -> AgentResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=date.today().isoformat())},
        *history,
        {"role": "user", "content": question},
    ]
    result = AgentResult(answer="")
    for _ in range(max_iterations):
        response = client.chat(messages, toolbox.schemas())
        usage = response.get("usage") or {}
        result.input_tokens += usage.get("prompt_tokens", 0)
        result.output_tokens += usage.get("completion_tokens", 0)
        message = response["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            result.answer = (message.get("content") or "").strip()
            break
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            started = time.perf_counter()
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
                output = toolbox.run(name, arguments)
                is_error = isinstance(output, dict) and "error" in output
            except Exception as exc:  # a broken tool call is data for the model
                arguments, output, is_error = {}, {"error": str(exc)}, True
            result.tool_calls.append(ToolCallRecord(
                name, arguments, output, int((time.perf_counter() - started) * 1000), is_error))
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(output, default=str)})
    else:
        result.stopped = "iteration_limit"
        result.answer = ("I stopped before finishing: the question needed more tool calls than "
                         f"the limit of {max_iterations}. Try a narrower question.")

    result.numbers = verify.check(result.answer, [c.result for c in result.tool_calls])
    return result
