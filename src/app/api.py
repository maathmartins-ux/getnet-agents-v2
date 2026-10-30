"""API HTTP.

Cliente (Bearer token): POST /chat · GET /conversations/{id} · POST /actions/{id}/confirm
Operador (X-Operator-Token): GET /handoffs · POST /handoffs/{id}/reply
Infra: GET /health (com contagem de dados) · GET /metrics
"""
from __future__ import annotations

import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import LIMITER, verify_token
from .config import auth_required, data_dir, llm_provider, operator_token
from .observability import METRICS
from .pipeline import handle
from .schemas import ChatRequest, ChatResponse
from .store import get_store
from .tools import confirm_action

app = FastAPI(title="app", version=__version__)


class OperatorReply(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


def _identity(authorization: str | None, fallback: str | None = None) -> str:
    if not auth_required():
        return fallback or "anonymous"
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    user_id = verify_token(token)
    if not user_id:
        METRICS.inc("auth_failures_total")
        raise HTTPException(401, "token ausente ou inválido", headers={"WWW-Authenticate": "Bearer"})
    return user_id


def _require_operator(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, operator_token()):
        raise HTTPException(403, "operador não autorizado")


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str | None = Header(default=None)) -> ChatResponse:
    user_id = _identity(authorization, req.user_id)  # com auth, o user_id do corpo é ignorado
    if not LIMITER.allow(user_id):
        METRICS.inc("rate_limited_total")
        raise HTTPException(429, "muitas requisições; tente em 1 minuto", headers={"Retry-After": "60"})
    try:
        return handle(req, user_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, authorization: str | None = Header(default=None)) -> dict:
    store = get_store()
    owner = store.conversation_owner(conversation_id)
    user_id = _identity(authorization, owner)
    if owner != user_id:
        raise HTTPException(404, "conversa não encontrada")
    open_ticket = store.ticket_for(conversation_id, "open")
    return {"conversation_id": conversation_id, "messages": store.history(conversation_id, limit=100),
            "open_ticket": open_ticket["id"] if open_ticket else None}


@app.post("/actions/{action_id}/confirm")
def confirm(action_id: str, authorization: str | None = Header(default=None)) -> dict:
    action = get_store().get_action(action_id)
    user_id = _identity(authorization, action["user_id"] if action else None)
    result = confirm_action(action_id, user_id)
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(404, "ação não encontrada")
    return result


@app.get("/handoffs")
def handoffs(status: str = "open", x_operator_token: str | None = Header(default=None)) -> list[dict]:
    _require_operator(x_operator_token)
    return get_store().list_tickets(status)


@app.post("/handoffs/{ticket_id}/reply")
def reply(ticket_id: str, body: OperatorReply, x_operator_token: str | None = Header(default=None)) -> dict:
    _require_operator(x_operator_token)
    ticket = get_store().answer_ticket(ticket_id, body.answer)
    if not ticket:
        raise HTTPException(404, "ticket não encontrado ou já respondido")
    METRICS.inc("handoff_replies_total")
    return ticket


@app.get("/health")
def health() -> dict:
    # data_files > 0 é o que o smoke do container confere: prova que o caminho dos dados
    # resolve dentro da imagem, não só na máquina de quem desenvolveu.
    files = [p for p in data_dir().rglob("*") if p.is_file() and p.name != ".gitkeep"]
    return {"status": "ok", "version": __version__, "mode": llm_provider(), "data_files": len(files)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return METRICS.render()
