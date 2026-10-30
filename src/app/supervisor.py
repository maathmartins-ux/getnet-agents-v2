"""Supervisor: executa o plano, observa o status de cada agente e replaneja.

Replanejamento por código (tabela FALLBACK), não por LLM: cada desvio é determinístico,
testável e aparece em `replans` na resposta. Com LLM, troque `next_step` por uma chamada que
escolhe entre os agentes permitidos — o laço, o limite de passos e o registro continuam iguais.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .agents import AGENTS, AgentResult, Context
from .observability import METRICS, span

PLANS = {
    "faq": ["faq"],
    "records": ["records"],
    "human_request": ["escalation"],
}
# Status diferente de "ok" → próximo agente. Sem fonte ou ferramenta quebrada termina em humano.
FALLBACK = {"faq": "escalation", "records": "escalation"}
MAX_STEPS = 4  # teto contra laço: plano mais longo do kit tem 2 passos


@dataclass
class Outcome:
    results: list[AgentResult] = field(default_factory=list)
    route: list[str] = field(default_factory=list)
    replans: list[str] = field(default_factory=list)


def next_step(result: AgentResult, done: list[str]) -> str | None:
    nxt = FALLBACK.get(result.agent, "escalation")
    return None if nxt in done else nxt


def run(intent: str, ctx: Context) -> Outcome:
    out = Outcome()
    queue = list(PLANS.get(intent, ["faq"]))
    while queue and len(out.route) < MAX_STEPS:
        name = queue.pop(0)
        with span("agent", agent=name):
            result = AGENTS[name](ctx)
        out.route.append(name)
        out.results.append(result)
        ctx.facts.update(result.facts)
        if result.status != "ok":
            nxt = next_step(result, out.route)
            if nxt:
                ctx.handoff_reason = f"{name}_{result.reason or result.status}"
                queue.insert(0, nxt)
                out.replans.append(f"{name}:{result.reason or result.status}->{nxt}")
                METRICS.inc("replans_total", agent=name, reason=result.reason or result.status)
    return out
