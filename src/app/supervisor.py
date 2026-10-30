"""Supervisor: executa o plano do router, observa o status de cada agente e replaneja.

Dois motores atrás da mesma saída (Outcome):
- LLM (Sonnet 5.5, effort medium): loop manual de tool use em que os especialistas são tools
  (`knowledge_agent`, `support_agent`, `escalation_agent`). O modelo lê cada status e decide o próximo passo.
- Tabela (offline/CI e fallback de falha do provedor): o mesmo laço, com transições determinísticas.

Os dois respeitam MAX_STEPS e ALLOWED (transições permitidas); todo desvio do plano vai para `replans`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .agents import AGENTS, AgentResult, Context
from .config import agent_model
from .llm import LLMError, get_llm
from .observability import METRICS, span

MAX_STEPS = 4  # teto contra laço: o plano mais longo tem 2 passos + 1 replanejamento + escalonamento
# Status != ok → próximo agente. Sem fonte vira humano; falha de dado do cliente tenta o manual antes.
FALLBACK = {"knowledge": "escalation", "support": "knowledge"}
# Transições permitidas (de → para). Escalation é terminal.
ALLOWED = {None: {"knowledge", "support", "escalation"}, "knowledge": {"support", "escalation"},
           "support": {"knowledge", "escalation"}, "escalation": set()}
LOW_CONFIDENCE = 0.4
SYSTEM = (Path(__file__).parent / "prompts" / "supervisor.md").read_text(encoding="utf-8")
TOOLS = [
    {"name": "knowledge_agent", "strict": True,
     "description": "Especialista em produtos/serviços Getnet (RAG sobre getnet.net com citações) e em perguntas "
                    "gerais via web (clima, câmbio). Devolve {status, text, sources}.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                      "properties": {"query": {"type": "string", "description": "Pergunta + fatos já obtidos"}}}},
    {"name": "support_agent", "strict": True,
     "description": "Especialista em dados do PRÓPRIO lojista autenticado: vendas, liquidação, transações/recusas, "
                    "status do terminal; propõe ações (antecipação, reiniciar) que exigem confirmação.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["request"],
                      "properties": {"request": {"type": "string"}}}},
    {"name": "escalation_agent", "strict": True,
     "description": "Abre caso para atendente humano (fila /operator). Use em pedido explícito, sem fonte, "
                    "falha repetida ou risco.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                      "properties": {"reason": {"type": "string"}}}},
]
_TOOL_TO_AGENT = {"knowledge_agent": "knowledge", "support_agent": "support", "escalation_agent": "escalation"}


@dataclass
class Outcome:
    results: list[AgentResult] = field(default_factory=list)
    route: list[str] = field(default_factory=list)
    replans: list[str] = field(default_factory=list)
    answer: str | None = None  # resposta final redigida pelo supervisor LLM (None = juntar textos dos agentes)
    engine: str = "table"


def _call(name: str, ctx: Context, out: Outcome) -> AgentResult:
    with span("agent", agent=name):
        result = AGENTS[name](ctx)
    out.route.append(name)
    out.results.append(result)
    for key, value in result.facts.items():  # fatos acumulam entre agentes (support → knowledge)
        if isinstance(value, dict) and isinstance(ctx.facts.get(key), dict):
            ctx.facts[key].update(value)
        else:
            ctx.facts[key] = value
    return result


def next_step(result: AgentResult, ctx: Context, done: list[str]) -> str | None:
    if result.agent == "knowledge" and ctx.intent == "general_info":
        return None  # clima/câmbio indisponível: resposta honesta, humano não resolve isso
    nxt = FALLBACK.get(result.agent, "escalation")
    if nxt in done:
        nxt = "escalation"
    return None if nxt in done or nxt not in ALLOWED[result.agent] else nxt


def run_table(plan: list[str], ctx: Context, confidence: float = 1.0) -> Outcome:
    out = Outcome()
    queue = list(plan)
    if confidence < LOW_CONFIDENCE and queue and "escalation" not in queue:
        queue.append("escalation")
        ctx.handoff_reason = "low_confidence"
        out.replans.append(f"router:low_confidence({confidence:.2f})->escalation")
    while queue and len(out.route) < MAX_STEPS:
        name = queue.pop(0)
        result = _call(name, ctx, out)
        if result.status != "ok":
            nxt = next_step(result, ctx, out.route)
            if nxt:
                ctx.handoff_reason = f"{name}_{result.reason or result.status}"
                if nxt in queue:
                    queue.remove(nxt)
                queue.insert(0, nxt)
                out.replans.append(f"{name}:{result.reason or result.status}->{nxt}")
                METRICS.inc("replans_total", agent=name, reason=result.reason or result.status)
    return out


def run_llm(plan: list[str], ctx: Context) -> Outcome:
    llm = get_llm(agent_model())
    out = Outcome(engine="llm")
    user = (f"Mensagem do lojista: {ctx.text}\nIntent: {ctx.intent}\nPlano sugerido pelo router: {plan}\n"
            f"Memória do lojista: {ctx.user_summary or '(nenhuma)'}")
    messages: list = [{"role": "user", "content": user}]
    last: str | None = None
    while len(out.route) < MAX_STEPS:
        resp = llm.create(SYSTEM.format(language=ctx.language), messages, TOOLS, max_tokens=2048)
        if resp.stop_reason != "tool_use":
            out.answer = "".join(b.text for b in resp.content if b.type == "text").strip() or None
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            name = _TOOL_TO_AGENT.get(block.name)
            if name is None or name not in ALLOWED[last] or len(out.route) >= MAX_STEPS:
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"transição não permitida: {last}->{block.name}"})
                continue
            if name == "escalation":
                ctx.handoff_reason = str(block.input.get("reason", "supervisor"))[:80]
            elif name == "knowledge" and block.input.get("query"):
                ctx.facts.setdefault("query_hints", []).append(str(block.input["query"])[:300])
            planned = plan[len(out.route)] if len(out.route) < len(plan) else None
            if name != planned:
                out.replans.append(f"{last or 'start'}->{name} (plano: {planned})")
                METRICS.inc("replans_total", agent=last or "start", reason="llm_decision")
            result = _call(name, ctx, out)
            last = name
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result.for_supervisor()})
        messages.append({"role": "user", "content": results})
    return out


def run(plan: list[str], ctx: Context, confidence: float = 1.0) -> Outcome:
    if get_llm(agent_model()).enabled and plan:
        try:
            return run_llm(plan, ctx)
        except LLMError:
            METRICS.inc("supervisor_fallback_total")
            ctx.facts.clear()  # recomeça limpo no motor determinístico
    return run_table(plan, ctx, confidence)


def debug(outcome: Outcome) -> str:
    return json.dumps({"route": outcome.route, "replans": outcome.replans, "engine": outcome.engine})
