# ADR 0001 — Supervisor que replaneja, especialistas como tools

Status: aceito · 30/10/2026

**Contexto.** A v1 usava plano fixo por intent (faixa 7–8 na régua). O enunciado pede que o router gerencie o fluxo entre agentes.

**Decisão.** O router (Haiku 5.5, JSON schema) dá o plano inicial. O supervisor (Sonnet 5.5) roda um loop manual de tool use em que `knowledge_agent`, `support_agent` e `escalation_agent` são tools. Cada agente devolve `status ok|insufficient|error`; o supervisor decide o próximo passo, limitado a 4 chamadas e a uma tabela de transições permitidas. Offline, o mesmo laço usa uma tabela determinística — é o caminho do CI e o fallback quando o provedor cai.

**Consequência.** Replanejamento visível em `replans`; testável sem LLM; custo maior por turno no modo LLM (medido em `usage`).
