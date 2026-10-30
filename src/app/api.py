"""API HTTP.

Cliente: POST /chat · GET /conversations/{id} · POST /actions/{id}/confirm
  AUTH_MODE=required → identidade só do Bearer token assinado.
  AUTH_MODE=demo     → Bearer válido vence; sem ele, user_id do corpo (identity_verified=false).
Operador (X-Operator-Token): GET /operator (console HTML) · GET /handoffs · POST /handoffs/{id}/reply
Infra: GET /health (com contagem de dados) · GET /metrics
"""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import LIMITER, verify_token
from .config import auth_mode, data_dir, llm_provider, operator_token
from .observability import METRICS
from .pipeline import handle
from .schemas import ChatRequest, ChatResponse
from .store import get_store
from .tools import confirm_action

app = FastAPI(title="Getnet Multi-Agent Support", version=__version__)
OPERATOR_HTML = (Path(__file__).parent / "static" / "operator.html").read_text(encoding="utf-8")


class OperatorReply(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


def _identity(authorization: str | None, fallback: str | None = None) -> tuple[str, bool]:
    """(user_id, identity_verified). Token presente e inválido é sempre 401, também no modo demo."""
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    if token:
        user_id = verify_token(token)
        if user_id:
            return user_id, True
    elif auth_mode() == "demo" and fallback:
        return fallback, False
    METRICS.inc("auth_failures_total")
    raise HTTPException(401, "token ausente ou inválido", headers={"WWW-Authenticate": "Bearer"})


def _require_operator(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, operator_token()):
        raise HTTPException(403, "operador não autorizado")


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request, authorization: str | None = Header(default=None)) -> ChatResponse:
    user_id, verified = _identity(authorization, req.user_id)  # em required, o corpo é ignorado
    # Sem identidade verificada, trocar o user_id do corpo não pode zerar o limite: a chave vira o IP.
    key = user_id if verified else f"ip:{request.client.host if request.client else 'unknown'}"
    if not LIMITER.allow(key):
        METRICS.inc("rate_limited_total")
        raise HTTPException(429, "muitas requisições; tente em 1 minuto", headers={"Retry-After": "60"})
    try:
        return handle(req, user_id, verified)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, user_id: str | None = None,
                 authorization: str | None = Header(default=None)) -> dict:
    # Demo: ?user_id= faz o papel do corpo do /chat; quem não é dono recebe 404 nos dois modos.
    store = get_store()
    owner = store.conversation_owner(conversation_id)
    user_id, _ = _identity(authorization, user_id)
    if owner != user_id:
        raise HTTPException(404, "conversa não encontrada")
    open_ticket = store.ticket_for(conversation_id, "open")
    return {"conversation_id": conversation_id, "messages": store.history(conversation_id, limit=100),
            "open_ticket": open_ticket["id"] if open_ticket else None}


@app.post("/actions/{action_id}/confirm")
def confirm(action_id: str, user_id: str | None = None, authorization: str | None = Header(default=None)) -> dict:
    user_id, _ = _identity(authorization, user_id)
    result = confirm_action(action_id, user_id)
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(404, "ação não encontrada")
    return result


@app.get("/operator", response_class=HTMLResponse)
def operator_console() -> str:
    # Página estática; os dados vêm de /handoffs com o token digitado pelo operador (nunca embutido no HTML).
    return OPERATOR_HTML


@app.get("/handoffs")
def handoffs(status: str = "open", x_operator_token: str | None = Header(default=None)) -> list[dict]:
    _require_operator(x_operator_token)
    store = get_store()
    return [{**t, "messages": store.history(t["conversation_id"], limit=10)} for t in store.list_tickets(status)]


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
    return {"status": "ok", "version": __version__, "mode": llm_provider(), "auth_mode": auth_mode(),
            "data_files": len(files)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return METRICS.render()
