"""Contrato público da API.

Congelado no commit do contrato (minuto ~5): forks NÃO alteram este arquivo — ele está em
`frozen` no OWNERSHIP.json. Campo novo exige decisão da sessão principal, porque todo fork,
teste e eval depende desta forma.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)
    # Só vale com AUTH_REQUIRED=false (dev/eval em processo). Com auth, a identidade vem do token.
    user_id: str | None = None


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
    answer: str
    intent: str
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
