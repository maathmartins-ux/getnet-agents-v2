"""Orquestração de um turno: guardrails → memória → router → supervisor → saída segura → persistência.

Assinatura estável (docs/SPEC.md): handle(ChatRequest, user_id, identity_verified) -> ChatResponse.
"""
from __future__ import annotations

import uuid

from . import supervisor
from .agents import Context
from .guardrails import check_input, mask_output
from .observability import METRICS, log, new_trace_id, request_usage, span
from .router import detect_language, route
from .schemas import ChatRequest, ChatResponse, Handoff, Usage
from .store import get_store

REFUSAL = {
    "pt": "Não posso atender a esse pedido. Posso ajudar com produtos Getnet ou com os dados da sua própria conta.",
    "en": "I can't help with that request. I can help with Getnet products or your own account data.",
}
OUT_OF_SCOPE = {
    "pt": "Sou o assistente de lojistas Getnet: ajudo com maquininhas, vendas, recebíveis, Pix, link de pagamento "
          "e suporte técnico. Esse assunto está fora do que eu atendo.",
    "en": "I'm the Getnet merchant assistant: I help with card machines, sales, receivables, Pix, payment links "
          "and technical support. That topic is outside what I handle.",
}
NO_ANSWER = {
    "pt": "Não encontrei base para responder com segurança. Posso encaminhar para um especialista humano.",
    "en": "I couldn't find a reliable basis to answer. I can hand this to a human specialist.",
}
SUMMARY_TURNS = 5


def _update_summary(store, user_id: str, intent: str, text: str, facts: dict) -> None:
    """Memória de longo prazo determinística: últimos assuntos + fatos do suporte (ex.: terminal offline).

    Determinística de propósito: auditável, sem custo e sem PII (o texto já chega mascarado).
    """
    items = [i for i in store.user_summary(user_id).split(" | ") if i]
    items.append(f"{intent}: {text[:80]}")
    if facts.get("support"):
        items.append("fatos: " + ", ".join(f"{k}={v}" for k, v in facts["support"].items())[:160])
    store.set_user_summary(user_id, " | ".join(items[-SUMMARY_TURNS:]))


def handle(req: ChatRequest, user_id: str, identity_verified: bool = True) -> ChatResponse:
    trace_id = new_trace_id()
    store = get_store()
    conversation_id = req.conversation_id or uuid.uuid4().hex[:12]
    owner = store.conversation_owner(conversation_id)
    if owner and owner != user_id:
        raise PermissionError("conversation belongs to another user")
    language = detect_language(req.message)
    base = {"trace_id": trace_id, "conversation_id": conversation_id, "user_id": user_id,
            "identity_verified": identity_verified}
    if not identity_verified:
        log.info("unverified_identity", extra={"extra_fields": {"user_id": user_id, "auth_mode": "demo"}})

    with span("guardrails"):
        verdict = check_input(req.message)
    if verdict.blocked:
        METRICS.inc("chat_requests_total", intent="blocked")
        METRICS.inc("guardrail_blocks_total", reason=verdict.reason or "unknown")
        return ChatResponse(**base, answer=REFUSAL[language], intent="blocked", route=["guardrails"],
                            language=language, blocked=True, guardrails=verdict.flags, usage=Usage(**request_usage()))

    history = store.history(conversation_id)
    with span("router"):
        decision = route(verdict.text, history)
    language = decision.language
    ctx = Context(text=verdict.text, language=language, user_id=user_id, conversation_id=conversation_id,
                  intent=decision.intent, history=history, user_summary=store.user_summary(user_id))
    outcome = supervisor.run(list(decision.plan), ctx, decision.confidence)

    if decision.intent == "out_of_scope" and not outcome.results:
        answer = OUT_OF_SCOPE[language]
    else:
        answer = outcome.answer or "\n\n".join(r.text for r in outcome.results if r.text)
    if not answer.strip():  # nunca 200 vazio: vazio no histórico vira 400 da API no turno seguinte
        answer = NO_ANSWER[language]
    answer = mask_output(answer)
    handoff = next((r.handoff for r in outcome.results if r.handoff), None) or Handoff()
    store.add_message(conversation_id, user_id, "user", verdict.text, decision.intent)  # texto já mascarado
    store.add_message(conversation_id, user_id, "assistant", answer, decision.intent)
    _update_summary(store, user_id, decision.intent, verdict.text, ctx.facts)

    METRICS.inc("chat_requests_total", intent=decision.intent)
    if handoff.required:
        METRICS.inc("handoffs_total", reason=handoff.reason or "unknown")
    return ChatResponse(
        **base, answer=answer, intent=decision.intent, confidence=decision.confidence,
        route=["router", *outcome.route], replans=outcome.replans, language=language,
        sources=[s for r in outcome.results for s in r.sources],
        tool_calls=[c for r in outcome.results for c in r.tool_calls], handoff=handoff,
        pending_action=next((r.pending_action for r in outcome.results if r.pending_action), None),
        guardrails=verdict.flags, usage=Usage(**request_usage()))
