# Observabilidade, custo e alertas

## O que já é emitido (código, não proposta)

| Sinal | Onde | Exemplo |
|---|---|---|
| Log JSON por etapa com `trace_id` | `observability.span` | `{"msg":"span","span":"agent","agent":"support","ms":3.5,"trace_id":"..."}` |
| Tokens e US$ por chamada LLM | `record_llm_usage` | `{"msg":"llm_usage","model":"claude-sonnet-5-5","input_tokens":1205,"cost_usd":0.00329}` |
| Custo do request na resposta | `ChatResponse.usage` | `{"input_tokens":9003,"output_tokens":846,"cost_usd":0.023}` (turno técnico ao vivo, 8 chamadas) |
| Métricas Prometheus | `GET /metrics` | `chat_requests_total{intent=...}`, `replans_total`, `handoffs_total`, `guardrail_blocks_total`, `tool_calls_total{ok}`, `llm_tokens_total`, `llm_cost_usd_total`, `supervisor_fallback_total`, `auth_failures_total`, `rate_limited_total`, `span_latency_ms_*` |
| Identidade não verificada | log `unverified_identity` | cada request em `AUTH_MODE=demo` sem token |

OpenTelemetry: os spans já têm nome, duração e atributos; exportar para OTLP é trocar o corpo de `span()` por
`tracer.start_as_current_span` (não implementado no relógio — declarado).

## Alertas propostos (com histerese)

Histerese = limiar de disparo diferente do de recuperação e janela mínima, para não piscar.

| Alerta | Dispara | Recupera | Por quê |
|---|---|---|---|
| Fallback do supervisor | `rate(supervisor_fallback_total[5m]) / rate(chat_requests_total[5m]) > 10%` por 10 min | < 3% por 15 min | provedor LLM degradado; o sistema segue no modo determinístico, mas pior |
| Handoff anormal | taxa de `handoffs_total` > 2× a média de 7 dias por 30 min | < 1,3× por 30 min | base de conhecimento desatualizada ou regressão do router |
| Custo por request | p95 de `usage.cost_usd` > US$ 0,05 por 15 min | < US$ 0,035 por 30 min | laço de tools ou prompt inflado |
| Latência | p95 de `span_latency_ms{span="agent"}` > 12 s por 10 min | < 8 s por 15 min | SLO de chat |
| Fila humana parada | ticket `open` mais velho > 30 min em horário comercial | fila < 10 min | HITL sem operador |
| Bloqueios de guardrail | `guardrail_blocks_total` > 5× a base por 10 min | < 2× por 30 min | ataque ou falso positivo após mudança de regra |

Painel sugerido: requests por intent, % replanejados, % handoff, p50/p95 por span, US$/hora por modelo,
tamanho e idade da fila humana.
