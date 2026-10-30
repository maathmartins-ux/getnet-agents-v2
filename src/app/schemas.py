"""Contrato público da API — Getnet Multi-Agent Support.

Congelado no commit da spec: forks NÃO alteram este arquivo (está em `frozen` no OWNERSHIP.json).
Campo novo exige decisão da sessão principal, porque todo fork, teste e eval depende desta forma.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Intenções do router. "blocked" é atribuída pelo guardrail, nunca pelo router.
Intent = Literal["product_question", "account_data", "technical_support", "general_info",
                 "human_request", "out_of_scope", "blocked"]
AgentName = Literal["knowledge", "support", "escalation"]
AgentStatus = Literal["ok", "insufficient", "error"]


class ChatRequest(BaseModel):
    """Payload do enunciado: {"message", "user_id"}.

    `user_id` do corpo só vale com AUTH_MODE=demo (marcado identity_verified=false).
    Com AUTH_MODE=required a identidade vem exclusivamente do Bearer token.
    """
    message: str = Field(min_length=1, max_length=4000)
    user_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, max_length=64)


class RouterDecision(BaseModel):
    intent: Intent
    plan: list[AgentName]
    language: Literal["pt", "en"]
    confidence: float = Field(ge=0, le=1)


class ToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    ok: bool = True


class Source(BaseModel):
    id: str
    title: str
    url: str


class Handoff(BaseModel):
    required: bool = False
    reason: str | None = None
    ticket_id: str | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ChatResponse(BaseModel):
    trace_id: str
    conversation_id: str
    user_id: str
    identity_verified: bool
    answer: str
    intent: str
    confidence: float = 1.0
    route: list[str]
    replans: list[str] = Field(default_factory=list)
    language: Literal["pt", "en"] = "pt"
    sources: list[Source] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    handoff: Handoff = Field(default_factory=Handoff)
    pending_action: str | None = None
    blocked: bool = False
    guardrails: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
