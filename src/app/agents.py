"""Especialistas. Cada um recebe o Context e devolve AgentResult com status explícito:
"ok", "insufficient" (não tem base para responder) ou "error". O supervisor decide o que fazer com
cada status — nenhum agente chama outro.

- knowledge: RAG (BM25 sobre trechos literais de getnet.net) + web tools (clima, câmbio).
- support: tools de CRM do lojista autenticado; escrita só propõe (action_id + confirmação).
- escalation: abre caso na fila persistida; o operador responde em /operator e a conversa retoma.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import agent_model
from .llm import LLMError, get_llm
from .rag import get_index
from .schemas import Handoff, Source, ToolCall
from .store import get_store
from .tools import run_tool, tool_specs
from .web_tools import WEB_TOOLS, pick

# BM25 bruto abaixo disso = sem fonte. Calibrar com Recall@3/MRR de evals/retrieval.jsonl, não no olho.
MIN_SCORE = 1.0
_PROMPTS = Path(__file__).parent / "prompts"
KNOWLEDGE_SYSTEM = (_PROMPTS / "knowledge.md").read_text(encoding="utf-8")
SUPPORT_SYSTEM = (_PROMPTS / "support.md").read_text(encoding="utf-8")
MAX_TOOL_ROUNDS = 3


@dataclass
class Context:
    text: str
    language: str
    user_id: str
    conversation_id: str
    intent: str = "product_question"
    history: list[dict] = field(default_factory=list)
    user_summary: str = ""
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

    def for_supervisor(self) -> str:
        """O que o supervisor LLM enxerga como tool_result: status, texto e fatos — nada de dado bruto extra."""
        return json.dumps({"status": self.status, "reason": self.reason, "text": self.text, "facts": self.facts,
                           "sources": [s.id for s in self.sources], "pending_action": self.pending_action},
                          ensure_ascii=False)


def _t(ctx: Context, pt: str, en: str) -> str:
    return en if ctx.language == "en" else pt


def llm_history(ctx: Context, final_user: str) -> list[dict]:
    """Memória curta: últimos 10 turnos + resumo de longo prazo do lojista, em mensagens alternadas."""
    msgs: list[dict] = []
    for m in ctx.history[-10:]:
        role = "user" if m["role"] == "user" else "assistant"
        content = m["content"] if m["role"] != "human_agent" else f"[atendente humano] {m['content']}"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    if msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    if ctx.user_summary:
        final_user = f"[memória do lojista] {ctx.user_summary}\n\n{final_user}"
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + final_user
    else:
        msgs.append({"role": "user", "content": final_user})
    return msgs


# --- knowledge -----------------------------------------------------------------------------
def _web_answer(ctx: Context, tool: str) -> AgentResult:
    result = WEB_TOOLS[tool](ctx.text)
    call = ToolCall(name=tool, args={}, ok=bool(result.get("ok")))
    if not result.get("ok"):
        return AgentResult("knowledge", "insufficient", tool_calls=[call], reason=f"web_{result.get('error')}",
                           facts={"web_tool": tool},
                           text=_t(ctx, "Não consegui consultar essa informação ao vivo agora. "
                                        "Tente de novo em instantes; para assuntos da sua conta Getnet, "
                                        "sigo à disposição.",
                                   "I couldn't fetch that live information right now. Please try again shortly; "
                                   "I'm here for anything about your Getnet account."))
    d = result["data"]
    if tool == "weather":
        text = _t(ctx, f"Previsão para {d['city']} em {d['date']}: mínima {d['min_c']}°C, máxima {d['max_c']}°C, "
                       f"chance de chuva {d['rain_prob']}% (fonte: {result['source']}).",
                  f"Forecast for {d['city']} on {d['date']}: low {d['min_c']}°C, high {d['max_c']}°C, "
                  f"{d['rain_prob']}% chance of rain (source: {result['source']}).")
    else:
        text = _t(ctx, f"Cotação {d['pair']}: compra R$ {d['bid']:.4f}, venda R$ {d['ask']:.4f} "
                       f"({d['timestamp']}, fonte: {result['source']}).",
                  f"{d['pair']} rate: bid R$ {d['bid']:.4f}, ask R$ {d['ask']:.4f} "
                  f"({d['timestamp']}, source: {result['source']}).")
    return AgentResult("knowledge", "ok", text=text, tool_calls=[call],
                       sources=[Source(id=tool, title=result["source"], url=result["url"])])


def knowledge_agent(ctx: Context) -> AgentResult:
    tool = pick(ctx.text) if ctx.intent == "general_info" else None
    if tool:
        return _web_answer(ctx, tool)
    if ctx.intent == "general_info":
        return AgentResult("knowledge", "insufficient", reason="no_web_tool",
                           text=_t(ctx, "Não tenho uma ferramenta para esse assunto.",
                                   "I have no tool for that topic."))
    query = " ".join([ctx.text, *ctx.facts.get("query_hints", [])])
    hits = [(d, s) for d, s in get_index().search(query, k=3) if s >= MIN_SCORE]
    if not hits:
        return AgentResult("knowledge", "insufficient", reason="no_source")
    sources = [Source(id=d.id, title=d.title, url=d.url) for d, _ in hits]
    llm = get_llm(agent_model())
    if llm.enabled:
        passages = "\n".join(f"[{i}] {d.title} ({d.url}): {d.text}" for i, (d, _) in enumerate(hits, 1))
        facts = f"\nFatos do suporte: {json.dumps(ctx.facts.get('support', {}), ensure_ascii=False)}" \
            if ctx.facts.get("support") else ""
        try:
            reply = llm.complete(KNOWLEDGE_SYSTEM.format(language=ctx.language),
                                 llm_history(ctx, f"<passagens>\n{passages}\n</passagens>{facts}\n\n"
                                                  f"Pergunta: {ctx.text}"), max_tokens=1500)
            if "NOT_IN_SOURCES" in reply.text:
                return AgentResult("knowledge", "insufficient", sources=sources, reason="not_in_sources")
            return AgentResult("knowledge", "ok", text=reply.text.strip(), sources=sources)
        except LLMError:
            pass  # cai no extrativo: resposta pior, mas fundamentada
    top = hits[0][0]
    return AgentResult("knowledge", "ok", sources=sources,
                       text=f"{top.text}\n({_t(ctx, 'Fonte', 'Source')}: {top.title} — {top.url})")


# --- support -------------------------------------------------------------------------------
_RULES = [  # (regex, tool) — ordem importa: ação explícita antes de leitura
    (re.compile(r"reinici|restart|reboot", re.I), "restart_terminal"),
    (re.compile(r"quero antecipar|antecipar meus|want to advance|advance my|request (an )?advance", re.I),
     "request_receivables_advance"),
    (re.compile(r"recus|negad|decline|denied|transa[cç]", re.I), "get_recent_transactions"),
    (re.compile(r"conect|connect|internet|wi-?fi|sinal|signal|chip|offline|n[aã]o liga", re.I), "get_terminal_status"),
    (re.compile(r"deposit|cair|cai na|receb|paid|pago|liquida|settle|vend|sold|sales", re.I),
     "get_settlement_forecast"),
]


def _offline_tool(ctx: Context) -> str:
    for rx, tool in _RULES:
        if rx.search(ctx.text):
            return tool
    if ctx.intent == "technical_support":  # seguimento ("e agora?") sobre o terminal
        return "get_terminal_status"
    return "get_customer_profile"


def _describe(ctx: Context, tool: str, data: dict) -> tuple[str, dict, list[str]]:
    """Resposta determinística + fatos para o próximo agente + dicas de busca para o knowledge."""
    if tool == "get_settlement_forecast":
        amt = data.get("amount_brl")
        val = f" (R$ {amt:,.2f})".replace(",", "X").replace(".", ",").replace("X", ".") if amt else ""
        return (_t(ctx, f"As vendas de {data.get('sale_date')}{val} caem na sua conta em {data.get('deposit_date')} "
                        f"({data.get('rule', 'dias úteis')}).",
                   f"Sales from {data.get('sale_date')}{val} will be deposited on {data.get('deposit_date')} "
                   f"({data.get('rule', 'business days')})."), {"deposit_date": data.get("deposit_date")}, [])
    if tool == "get_terminal_status":
        st = {k: data.get(k) for k in ("model", "status", "connection", "signal") if data.get(k)}
        return (_t(ctx, f"Seu terminal: {', '.join(f'{k}={v}' for k, v in st.items())}.",
                   f"Your terminal: {', '.join(f'{k}={v}' for k, v in st.items())}."),
                {"terminal": st}, ["maquininha conexão internet wi-fi chip sinal reiniciar"])
    if tool == "get_recent_transactions":
        txs = data.get("transactions", [])
        declined = [t for t in txs if str(t.get("status", "")).startswith(("declin", "recus", "negad"))]
        if declined:
            t = declined[0]
            reason = t.get("reason") or t.get("reason_pt") or t.get("message") or ""
            return (_t(ctx, f"Última recusa: {t.get('id')} — código {t.get('code')}: {reason}.",
                       f"Last decline: {t.get('id')} — code {t.get('code')}: {reason}."),
                    {"decline_code": t.get("code")}, ["transação negada recusada erro código banco emissor"])
        return (_t(ctx, "Não há transações recusadas recentes na sua conta.",
                   "There are no recent declined transactions on your account."), {}, [])
    summary = ", ".join(f"{k}: {v}" for k, v in list(data.items())[:6] if not isinstance(v, dict | list))
    return summary or json.dumps(data, ensure_ascii=False)[:300], {}, []


def _support_llm(ctx: Context) -> AgentResult:
    """Loop manual de tool use (Sonnet 5.5): o modelo escolhe as tools de CRM; o servidor impõe a identidade."""
    llm = get_llm(agent_model())
    messages: list = llm_history(ctx, ctx.text)
    calls: list[ToolCall] = []
    pending = None
    for _ in range(MAX_TOOL_ROUNDS):
        resp = llm.create(SUPPORT_SYSTEM.format(user_id=ctx.user_id, language=ctx.language), messages, tool_specs())
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            facts = {"support": {c.name: c.ok for c in calls}}
            if any(c.name == "get_terminal_status" for c in calls):
                facts["query_hints"] = ["maquininha conexão internet wi-fi chip reiniciar"]
            if any(c.name == "get_recent_transactions" for c in calls):
                facts["query_hints"] = ["transação negada recusada código"]
            ok = any(c.ok for c in calls) or bool(pending)
            return AgentResult("support", "ok" if ok else "insufficient", text=text, tool_calls=calls,
                               pending_action=pending, facts=facts, reason=None if ok else "no_data")
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            result, call = run_tool(block.name, dict(block.input), ctx.user_id, ctx.conversation_id)
            calls.append(call)
            pending = result.get("action_id") or pending
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False), "is_error": not call.ok})
        messages.append({"role": "user", "content": results})
    return AgentResult("support", "error", tool_calls=calls, reason="tool_loop_limit")


def support_agent(ctx: Context) -> AgentResult:
    if get_llm(agent_model()).enabled:
        try:
            return _support_llm(ctx)
        except LLMError:
            pass  # provedor caiu: mesmo atendimento pelo caminho determinístico
    tool = _offline_tool(ctx)
    result, call = run_tool(tool, {}, ctx.user_id, ctx.conversation_id)
    if result.get("needs_confirmation"):
        aid = result["action_id"]
        what = _t(ctx, "reiniciar sua maquininha" if tool == "restart_terminal" else "antecipar seus recebíveis",
                  "restart your terminal" if tool == "restart_terminal" else "advance your receivables")
        return AgentResult("support", "ok", tool_calls=[call], pending_action=aid, facts={"pending": tool},
                           text=_t(ctx, f"Posso {what}. Para executar, confirme a ação {aid} "
                                        f"(POST /actions/{aid}/confirm). Nada foi feito ainda.",
                                   f"I can {what}. To proceed, confirm action {aid} "
                                   f"(POST /actions/{aid}/confirm). Nothing has been done yet."))
    if result.get("error") == "not_found":
        return AgentResult("support", "insufficient", tool_calls=[call], reason="customer_not_found",
                           text=_t(ctx, "Não encontrei esse dado na sua conta.",
                                   "I couldn't find that in your account."))
    if not result.get("ok"):
        return AgentResult("support", "error", tool_calls=[call], reason="tool_error")
    text, facts, hints = _describe(ctx, tool, result.get("data", {}))
    return AgentResult("support", "ok", text=text, tool_calls=[call],
                       facts={"support": facts, **({"query_hints": hints} if hints else {})})


# --- escalation ----------------------------------------------------------------------------
def escalation_agent(ctx: Context) -> AgentResult:
    reason = ctx.handoff_reason or "user_request"
    summary = f"[{ctx.intent}] {ctx.text[:300]}"
    if ctx.facts.get("support"):
        summary += f" | fatos: {json.dumps(ctx.facts['support'], ensure_ascii=False)[:200]}"
    ticket = get_store().open_ticket(ctx.conversation_id, ctx.user_id, reason, summary)
    return AgentResult("escalation", "ok", handoff=Handoff(required=True, reason=reason, ticket_id=ticket["id"]),
                       text=_t(ctx, f"Encaminhei para um especialista humano (protocolo {ticket['id']}). "
                                    "A resposta aparece nesta mesma conversa.",
                               f"I've handed this to a human specialist (ticket {ticket['id']}). "
                               "The reply will show up in this same conversation."))


AGENTS = {"knowledge": knowledge_agent, "support": support_agent, "escalation": escalation_agent}
