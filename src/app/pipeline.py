"""Orquestração de um turno: guardrails → memória → router → supervisor → persistência.

Os forks trocam agentes e regras do router; a assinatura de `handle` (ChatRequest, user_id
autenticado -> ChatResponse) não muda.
"""
from __future__ import annotations

import re
import uuid

from . import supervisor
from .agents import Context
from .guardrails import check_input
from .observability import METRICS, new_trace_id, request_usage, span
from .schemas import ChatRequest, ChatResponse, Handoff, Usage
from .store import get_store

_HUMAN = re.compile(r"\b(atendente|humano|pessoa real|human|agent|representative)\b", re.IGNORECASE)
_RECORDS = re.compile(r"\b(pedido|pedidos|order|orders|cancel\w*|\d{4})\b", re.IGNORECASE)
_FOLLOWUP = re.compile(r"\b(ele|ela|isso|esse|essa|it|that|this one)\b", re.IGNORECASE)
# Palavras funcionais frequentes de cada idioma; nenhuma de uma letra só ("a" existe nos dois).
_EN = re.compile(r"\b(the|what|how|why|when|my|is|are|can|you|please|want|to|talk|with|it|do|does"
                 r"|of|and|for|this|that|me|need|help|where|order)\b", re.IGNORECASE)
_PT = re.compile(r"\b(os|as|um|uma|de|do|da|que|como|com|para|por|não|meu|minha|quero|está"
                 r"|falar|ajuda|preciso|sem|no|na|qual|onde|posso|pedido)\b", re.IGNORECASE)
_ACCENT = re.compile(r"[ãõáéíóúâêôç]", re.IGNORECASE)

REFUSAL = {
    "pt": "Não posso atender a esse pedido. Posso ajudar com dúvidas sobre o produto ou sua conta.",
    "en": "I can't help with that request. I can help with product or account questions.",
}


def detect_language(text: str) -> str:
    # Acento vale 1 ponto para pt, não decide sozinho (nome próprio acentuado em frase inglesa).
    en = len(_EN.findall(text))
    pt = len(_PT.findall(text)) + min(1, len(_ACCENT.findall(text)))
    return "en" if en > pt else "pt"


def classify(text: str, history: list[dict]) -> str:
    if _HUMAN.search(text):
        return "human_request"
    if _RECORDS.search(text):
        return "records"
    last_intent = next((m["intent"] for m in reversed(history) if m["role"] == "user" and m["intent"]), None)
    if last_intent == "records" and _FOLLOWUP.search(text):  # memória: pergunta de seguimento
        return "records"
    return "faq"


def handle(req: ChatRequest, user_id: str) -> ChatResponse:
    trace_id = new_trace_id()
    store = get_store()
    conversation_id = req.conversation_id or uuid.uuid4().hex[:12]
    owner = store.conversation_owner(conversation_id)
    if owner and owner != user_id:
        raise PermissionError("conversation belongs to another user")
    language = detect_language(req.message)

    with span("guardrails"):
        verdict = check_input(req.message)
    if verdict.blocked:
        METRICS.inc("chat_requests_total", intent="blocked")
        METRICS.inc("guardrail_blocks_total", reason=verdict.reason or "unknown")
        return ChatResponse(trace_id=trace_id, conversation_id=conversation_id, answer=REFUSAL[language],
                            intent="blocked", route=["guardrails"], language=language, blocked=True,
                            guardrails=verdict.flags)

    history = store.history(conversation_id)
    with span("router"):
        intent = classify(verdict.text, history)
    ctx = Context(text=verdict.text, language=language, user_id=user_id, conversation_id=conversation_id,
                  history=history)
    outcome = supervisor.run(intent, ctx)

    answer = "\n\n".join(r.text for r in outcome.results if r.text)
    handoff = next((r.handoff for r in outcome.results if r.handoff), None) or Handoff()
    store.add_message(conversation_id, user_id, "user", verdict.text, intent)  # texto já mascarado
    store.add_message(conversation_id, user_id, "assistant", answer, intent)

    METRICS.inc("chat_requests_total", intent=intent)
    if handoff.required:
        METRICS.inc("handoffs_total", reason=handoff.reason or "unknown")
    return ChatResponse(
        trace_id=trace_id, conversation_id=conversation_id, answer=answer, intent=intent,
        route=["router", *outcome.route], replans=outcome.replans, language=language,
        sources=[s for r in outcome.results for s in r.sources],
        tool_calls=[c for r in outcome.results for c in r.tool_calls], handoff=handoff,
        pending_action=next((r.pending_action for r in outcome.results if r.pending_action), None),
        guardrails=verdict.flags, usage=Usage(**request_usage()))
