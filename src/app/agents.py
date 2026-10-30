"""Especialistas. Cada um recebe o Context e devolve AgentResult com status explícito:
"ok", "insufficient" (não tem base para responder) ou "error". O supervisor decide o que
fazer com cada status — nenhum agente chama outro.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm import LLMError, get_llm
from .rag import get_index
from .schemas import Handoff, Source, ToolCall
from .store import get_store
from .tools import run_tool

# BM25 bruto abaixo disso = sem fonte. Calibrar com Recall@K/MRR do eval, não no olho.
MIN_SCORE = 0.5
_REC_ID = re.compile(r"\b(\d{4})\b")
_CANCEL = re.compile(r"\bcancel", re.IGNORECASE)
FAQ_SYSTEM = ("Responda à pergunta do cliente usando SOMENTE as passagens numeradas. Cite [n]. "
              "Se as passagens não respondem, escreva exatamente NOT_IN_SOURCES. Responda no idioma {lang}.")


@dataclass
class Context:
    text: str
    language: str
    user_id: str
    conversation_id: str
    history: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    handoff_reason: str | None = None


@dataclass
class AgentResult:
    agent: str
    status: str
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    reason: str | None = None
    pending_action: str | None = None
    handoff: Handoff | None = None


def _t(ctx: Context, pt: str, en: str) -> str:
    return en if ctx.language == "en" else pt


def _llm_messages(ctx: Context, final_user: str) -> list[dict]:
    msgs = []
    for m in ctx.history[-10:]:  # memória curta: últimos 10 turnos bastam para referência ("ele", "isso")
        role = "user" if m["role"] == "user" else "assistant"
        content = m["content"] if m["role"] != "human_agent" else f"[atendente humano] {m['content']}"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + final_user
    else:
        msgs.append({"role": "user", "content": final_user})
    return msgs


def faq_agent(ctx: Context) -> AgentResult:
    query = " ".join([ctx.text, *ctx.facts.get("query_hints", [])])
    hits = [(d, s) for d, s in get_index().search(query, k=3) if s >= MIN_SCORE]
    if not hits:
        return AgentResult("faq", "insufficient", reason="no_source")
    sources = [Source(id=d.id, title=d.title, url=d.url) for d, _ in hits]
    llm = get_llm()
    if llm.enabled:
        passages = "\n".join(f"[{i}] {d.title}: {d.text}" for i, (d, _) in enumerate(hits, 1))
        try:
            reply = llm.complete(FAQ_SYSTEM.format(lang=ctx.language),
                                 _llm_messages(ctx, f"Passagens:\n{passages}\n\nPergunta: {ctx.text}"))
            if "NOT_IN_SOURCES" in reply.text:
                return AgentResult("faq", "insufficient", sources=sources, reason="not_in_sources")
            return AgentResult("faq", "ok", text=reply.text.strip(), sources=sources)
        except LLMError:
            pass  # cai no extrativo: resposta pior, mas fundamentada
    top = hits[0][0]
    return AgentResult("faq", "ok", text=f"{top.text} ({_t(ctx, 'Fonte', 'Source')}: {top.title})", sources=sources)


def _record_id(ctx: Context) -> str | None:
    found = _REC_ID.search(ctx.text)
    if found:
        return found.group(1)
    for m in reversed(ctx.history):  # memória: "e pode cancelar ele?" usa o pedido citado antes
        found = _REC_ID.search(m["content"])
        if found:
            return found.group(1)
    return None


def records_agent(ctx: Context) -> AgentResult:
    rid = _record_id(ctx)
    if not rid:
        return AgentResult("records", "ok", text=_t(ctx, "Qual o número do pedido?", "What is the order number?"))
    tool = "cancel_record" if _CANCEL.search(ctx.text) else "lookup_record"
    result, call = run_tool(tool, {"record_id": rid}, ctx.user_id, ctx.conversation_id)
    if result.get("needs_confirmation"):
        aid = result["action_id"]
        return AgentResult("records", "ok", tool_calls=[call], pending_action=aid, facts={"record_id": rid},
                           text=_t(ctx, f"Para cancelar o pedido {rid}, confirme a ação {aid}.",
                                   f"To cancel order {rid}, confirm action {aid}."))
    if result.get("error") == "not_found":
        return AgentResult("records", "ok", tool_calls=[call], facts={"record_not_found": rid},
                           text=_t(ctx, f"Não encontrei o pedido {rid} na sua conta.",
                                   f"I couldn't find order {rid} in your account."))
    if not result.get("ok"):
        return AgentResult("records", "error", tool_calls=[call], reason="tool_error")
    data = result["data"]
    return AgentResult("records", "ok", tool_calls=[call], facts={"record_id": rid, "status": data["status"]},
                       text=_t(ctx, f"Pedido {rid} ({data['item']}): {data['status']}.",
                               f"Order {rid} ({data['item']}): {data['status']}."))


def escalation_agent(ctx: Context) -> AgentResult:
    reason = ctx.handoff_reason or "user_request"
    ticket = get_store().open_ticket(ctx.conversation_id, ctx.user_id, reason, ctx.text[:200])
    return AgentResult("escalation", "ok", handoff=Handoff(required=True, reason=reason, ticket_id=ticket["id"]),
                       text=_t(ctx, f"Encaminhei para um atendente humano (protocolo {ticket['id']}). "
                                    "A resposta aparece nesta conversa.",
                               f"I've handed this to a human agent (ticket {ticket['id']}). "
                               "The reply will show up in this conversation."))


AGENTS = {"faq": faq_agent, "records": records_agent, "escalation": escalation_agent}
