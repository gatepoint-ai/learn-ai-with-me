"""Analyst -- the AI agent.

Answers questions with tools only: data through Prism, Pulse and Sentry,
knowledge through Codex. It holds no analytics or knowledge credentials --
the only database it writes is its own audit log of conversations and tool
calls, so every answer can be traced to what the tools returned.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from argus_core.clients.postgres import PostgresClient
from argus_core.logging import configure_logging, get_logger
from argus_core.settings import LLMSettings, LogSettings, PostgresSettings, ServiceUrls

from . import agent, verify
from .tools import DENIED, Toolbox

log = get_logger("analyst")

API = "/analyst/v1"


class State:
    llm: LLMSettings
    client: agent.OpenAICompatClient | None = None
    toolbox: Toolbox | None = None
    db: PostgresClient | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = LogSettings(service_name="analyst")
    configure_logging(settings.service_name, settings.log_level, settings.log_json)
    state.llm = LLMSettings()
    if state.llm.llm_provider != "openai_compat":
        raise RuntimeError(f"provider {state.llm.llm_provider!r} is not implemented yet; use openai_compat")
    if state.llm.openai_api_key:
        state.client = agent.OpenAICompatClient(
            state.llm.openai_base_url, state.llm.openai_api_key, state.llm.openai_model,
            state.llm.request_timeout)
    else:
        # Start without a key so the rest of the stack stays usable; /ask says why it can't answer.
        log.warning("no_llm_key", hint="set OPENAI_API_KEY in .env")
    urls = ServiceUrls()
    state.toolbox = Toolbox(urls.prism_url, urls.pulse_url, urls.sentry_url, urls.codex_url)
    pg = PostgresSettings()
    state.db = PostgresClient(f"postgresql://{pg.postgres_user}:{pg.postgres_password}"
                              f"@{pg.postgres_host}:{pg.postgres_port}/analyst")
    log.info("analyst_ready", model=state.llm.openai_model, tools=sorted(state.toolbox.tools))
    yield
    state.toolbox.close()
    if state.client:
        state.client.close()
    state.db.close()


app = FastAPI(title="Argus Analyst", description="AI agent", version="0.1.0", lifespan=lifespan)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    conversation_id: uuid.UUID | None = None


def _history(conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = state.db.fetch_all(
        "SELECT role, content FROM message WHERE conversation_id = %(c)s ORDER BY created_at, id",
        {"c": conversation_id},
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows][-12:]


def _record(conversation_id: uuid.UUID, question: str, result: agent.AgentResult) -> None:
    with state.db.connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation (id, title, provider, model) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET updated_at = now()",
            (conversation_id, question[:120], state.llm.llm_provider, state.llm.openai_model),
        )
        cur.execute("INSERT INTO message (conversation_id, role, content) VALUES (%s, 'user', %s)",
                    (conversation_id, question))
        cur.execute(
            "INSERT INTO message (conversation_id, role, content, agent_mode, tool_calls, "
            "input_tokens, output_tokens) VALUES (%s, 'assistant', %s, 'native', %s, %s, %s)",
            (conversation_id, result.answer,
             json.dumps([{"name": c.name, "arguments": c.arguments} for c in result.tool_calls]),
             result.input_tokens, result.output_tokens),
        )
        for c in result.tool_calls:
            rows = c.result.get("row_count", c.result.get("count")) if isinstance(c.result, dict) else None
            cur.execute(
                "INSERT INTO tool_call (conversation_id, tool_name, arguments, result_preview, "
                "row_count, duration_ms, is_error, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (conversation_id, c.name, json.dumps(c.arguments), c.preview(), rows,
                 c.duration_ms, c.is_error,
                 json.dumps(c.result.get("error"), default=str)[:500] if c.is_error else None),
            )


@app.post(f"{API}/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    if state.client is None:
        raise HTTPException(503, {"error": "no_llm_key",
                                  "message": "Set OPENAI_API_KEY in argus/.env and restart the analyst."})
    conversation_id = req.conversation_id or uuid.uuid4()
    history = _history(conversation_id) if req.conversation_id else []
    try:
        result = agent.run(state.client, state.toolbox, req.question, history,
                           state.llm.max_tool_iterations)
    except RuntimeError as exc:
        raise HTTPException(502, {"error": "llm_failed", "message": str(exc)}) from exc
    _record(conversation_id, req.question, result)
    retrieved = sorted({h["citation"] for c in result.tool_calls if c.name == "search_knowledge"
                        and isinstance(c.result, dict) for h in c.result.get("hits", [])})
    # A passage counts as cited when its section heading appears in the answer.
    answer_text = verify.normalise(result.answer)
    cited = [c for c in retrieved if c.split(" > ")[-1] in answer_text]
    return {
        "conversation_id": str(conversation_id),
        "answer": result.answer,
        "numbers": result.numbers.as_dict() if result.numbers else None,
        "tools_used": [{"name": c.name, "arguments": c.arguments, "ms": c.duration_ms,
                        "error": c.is_error} for c in result.tool_calls],
        "sources": cited,
        "retrieved": retrieved,
        "model": state.llm.openai_model,
        "tokens": {"input": result.input_tokens, "output": result.output_tokens},
        "stopped": result.stopped,
    }


@app.get(f"{API}/tools")
def tools() -> dict[str, Any]:
    """What the agent can do -- and, just as important, what it cannot."""
    return {
        "granted": [{"name": t.name, "family": t.family, "description": t.description}
                    for t in state.toolbox.tools.values()],
        "denied": DENIED,
        "max_tool_iterations": state.llm.max_tool_iterations,
    }


@app.get(f"{API}/conversations/{{conversation_id}}")
def conversation(conversation_id: uuid.UUID) -> dict[str, Any]:
    return {
        "messages": state.db.fetch_all(
            "SELECT role, content, tool_calls, created_at FROM message "
            "WHERE conversation_id = %(c)s ORDER BY created_at, id", {"c": conversation_id}),
        "tool_calls": state.db.fetch_all(
            "SELECT tool_name, arguments, row_count, duration_ms, is_error, created_at FROM tool_call "
            "WHERE conversation_id = %(c)s ORDER BY created_at, id", {"c": conversation_id}),
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "llm": "configured" if state.client else "no key",
            "model": state.llm.openai_model, "tools": len(state.toolbox.tools) if state.toolbox else 0,
            "database": "up" if state.db and state.db.ping() else "unavailable"}
