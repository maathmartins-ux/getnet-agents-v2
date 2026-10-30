# Getnet Multi-Agent Support System

Assistente para lojistas Getnet: um router classifica a mensagem, um **supervisor que replaneja** coordena três
especialistas (conhecimento com RAG + web, suporte com dados do lojista, escalonamento humano) e a API devolve
uma resposta fundamentada, com fontes, ferramentas usadas, custo e rastreio.

| | |
|---|---|
| Rodar | `docker compose up -d --build` → `http://localhost:8000/docs` · console do operador em `/operator` |
| Testar | `python scripts/check.py --docker` (lint, testes, eval com gate, build e smoke do container) |
| Spec | [docs/SPEC.md](docs/SPEC.md) · [threat model](docs/THREAT_MODEL.md) · [ADRs](docs/adr/) · [observabilidade](docs/OBSERVABILITY.md) |
| Vídeo | gravação pendente (roteiro em [docs/VIDEO_SCRIPT.md](docs/VIDEO_SCRIPT.md)) |

## 1. Como rodar

```bash
cp .env.example .env            # opcional: sem ANTHROPIC_API_KEY roda no modo offline determinístico
docker compose up -d --build
curl -s localhost:8000/health
```

Payload literal do enunciado (o compose sobe em `AUTH_MODE=demo`):

```bash
curl -s -X POST localhost:8000/chat -H "Content-Type: application/json" \
     -d '{"message": "When will the money from yesterday'"'"'s sales be deposited?", "user_id": "cliente1988"}'
```

A resposta traz `identity_verified: false`: no modo demo o `user_id` do corpo é aceito, mas marcado e logado.
Com identidade de verdade (`AUTH_MODE=required`, recomendado fora de demo):

```bash
TOKEN=$(docker compose exec api python -m app.auth cliente1988)   # token HMAC de desenvolvimento
curl -s -X POST localhost:8000/chat -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"message": "Minha maquininha não conecta na internet"}'
```

| Variável | Padrão | Efeito |
|---|---|---|
| `LLM_PROVIDER` | `offline` | `anthropic` liga router/guardrail (Haiku 5.5), supervisor e agentes (Sonnet 5.5) |
| `AUTH_MODE` | `required` (`demo` no compose) | ver [ADR 0002](docs/adr/0002-auth-mode-demo.md) |
| `WEB_TOOLS` | `live` | `off` desliga Open-Meteo/AwesomeAPI (CI) |
| `AUTH_SECRET`, `OPERATOR_TOKEN` | valores de dev | **troque** fora de demo |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | vazio | avisa o operador de caso novo (opcional) |

Testes e eval sem Docker:

```bash
uv venv .venv && uv pip install -r requirements-dev.lock && uv pip install --no-deps -e .
.venv/Scripts/python.exe scripts/check.py          # ruff + pytest + eval com gate (offline)
LLM_PROVIDER=anthropic .venv/Scripts/python.exe -m evals.run_evals --out evals/results/live.json   # ao vivo (com chave)
LLM_PROVIDER=anthropic .venv/Scripts/python.exe -m evals.judge evals/results/live.json            # juiz Opus 5.5
```

## 2. Arquitetura

```mermaid
flowchart LR
  C[Lojista] -->|POST /chat · Bearer ou user_id demo| API
  API --> AU[Auth + rate limit] --> G1[Guardrail 1: regex + PII]
  G1 --> G2[Guardrail 2: Haiku 5.5]
  G2 --> MEM[(SQLite: histórico, resumo do lojista, tickets, ações)]
  G2 --> R[Router · Haiku 5.5 · JSON schema]
  R -->|plano inicial| S{Supervisor · Sonnet 5.5 · tool loop}
  S -->|tool| K[knowledge_agent]
  S -->|tool| SU[support_agent]
  S -->|tool| E[escalation_agent]
  K --> RAG[(Trechos literais getnet.net)]
  K --> WEB[Open-Meteo · AwesomeAPI]
  SU --> CRM[CRM tools + ação com confirmação]
  E --> Q[(Fila HITL)] --> OP[/operator · Telegram opcional/] -->|resposta| MEM
  S --> OUT[Máscara de PII na saída] --> C
```

**Comunicação entre agentes:** chamadas de função em processo, com mensagens tipadas (`Context` → `AgentResult`).
Cada agente devolve `status: ok | insufficient | error`; **nenhum agente chama outro** — quem decide é o supervisor.

### Fluxo de uma mensagem

```mermaid
sequenceDiagram
  participant L as Lojista
  participant P as pipeline.handle
  participant R as Router
  participant S as Supervisor
  participant SU as support
  participant K as knowledge
  L->>P: "Minha maquininha não conecta"
  P->>P: guardrails (regex → Haiku), PII mascarada
  P->>R: texto + histórico
  R-->>P: technical_support, plano [support, knowledge], pt, 0.85
  P->>S: plano + contexto (memória do lojista)
  S->>SU: support_agent
  SU-->>S: ok · terminal offline, Wi-Fi, sinal fraco
  S->>K: knowledge_agent (consulta + fatos do support)
  K-->>S: ok · passos do manual [fonte]
  S-->>P: resposta final + rota + replans
  P-->>L: answer, sources, tool_calls, pending_action, usage (US$)
```

### Supervisor que replaneja

| Situação | Decisão | Evidência |
|---|---|---|
| knowledge sem fonte (`insufficient: no_source` / `NOT_IN_SOURCES`) | escala para humano com o motivo | `test_table_replans_when_no_source`, `test_no_source_escalates_with_replan` |
| support com erro de tool | tenta o manual (knowledge) antes de escalar | `test_table_support_error_falls_back_to_knowledge` |
| clima/câmbio indisponível | responde com honestidade, **sem** escalar | `test_table_general_info_unavailable_does_not_escalate` |
| router com confiança < 0,4 | acrescenta escalonamento ao plano | `test_low_confidence_adds_escalation` |
| modo LLM: modelo pede transição proibida (ex.: depois de escalation) | tool_result de erro, transição negada | `test_llm_supervisor_blocks_disallowed_transition` |
| provedor LLM falha | mesmo laço pela tabela determinística | `test_llm_failure_falls_back_to_table` |

Limites: máximo de 4 chamadas de agente por turno e tabela `ALLOWED` de transições. Todo desvio do plano do router
aparece em `replans` na resposta. Ver [ADR 0001](docs/adr/0001-supervisor-que-replaneja.md).

### Modelos (dentro da aplicação)

| Papel | Modelo | `effort` | Por quê |
|---|---|---|---|
| Router, guardrail classificador | `claude-haiku-5-5` | low | classificação curta com JSON schema; US$ 0,10/0,50 por MTok |
| Supervisor, knowledge, support | `claude-sonnet-5-5` | medium | decidir e redigir com fonte; `high` pesa no p95 |
| Juiz do eval | `claude-opus-5-5` | medium | o juiz deve ser pelo menos tão capaz quanto o julgado |
| Fallback | offline determinístico | — | CI sem chave e queda do provedor |

Regras da API seguidas: sem `temperature/top_p/top_k`; `output_config.effort` explícito; structured output via
`output_config.format`; `tool_choice` só `auto` com `strict: true`; `stop_reason == "refusal"` checado; exceções
em cadeia `RateLimitError → APIStatusError → APIConnectionError` (`src/app/llm.py`, testado com cliente falso em
`tests/test_agents.py`).

## 3. RAG: ingestão → armazenamento → retrieval → geração

1. **Ingestão** (`src/app/ingest.py`, `python -m app.ingest --refresh`): crawler de getnet.net (pt e en) e manuais,
   HTML → texto, chunks **literais** de 300–800 caracteres cortados em fronteira de parágrafo, dedupe por sha256.
   Fontes e o que cada uma não cobre: [docs/SOURCES.md](docs/SOURCES.md).
2. **Armazenamento:** `data/knowledge/chunks.jsonl` com `{id, title, url, lang, text, fetched_at, sha256}` — versionado,
   auditável, entra na imagem.
3. **Retrieval:** BM25 próprio (`src/app/rag.py`) com radical leve pt/en; o support passa fatos (ex.: "terminal offline")
   como dicas de busca. Medido por Recall@3 e MRR em `evals/retrieval.jsonl` (tabela na seção 5).
4. **Geração:** Sonnet 5.5 responde **só** com as passagens numeradas e cita `[n]`; se não houver base escreve
   `NOT_IN_SOURCES`, o agente devolve `insufficient` e o supervisor replaneja. Offline, a resposta é extrativa.

Perguntas gerais (clima, câmbio) usam ferramentas web sem chave (Open-Meteo, AwesomeAPI), com data e fonte na resposta.

## 4. Ferramentas, identidade e segurança

- **Tools de CRM** (`src/app/tools.py`, JSON Schema `strict`): perfil, previsão de liquidação em dias úteis,
  transações com código de recusa, status do terminal. CRM fictício e declarado (`data/crm/`).
- **Escrita com confirmação:** `request_receivables_advance` e `restart_terminal` devolvem `needs_confirmation` +
  `action_id`; só executam em `POST /actions/{id}/confirm` pelo dono (outro lojista recebe 404).
- **Identidade do servidor:** `run_tool` descarta qualquer `user_id` vindo do modelo
  (`test_support_llm_tool_loop_enforces_server_identity`).
- **Guardrails em 2 camadas** + PII mascarada na entrada, no histórico, nos logs e na saída.
- **Rate limit** por identidade; **threat model** STRIDE + OWASP LLM 2025 em [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## 5. Avaliação

Estratégia completa (pirâmide de testes, integração com LLM falso injetando falhas, splits, juiz calibrado,
o que medir em produção): [docs/EVAL_STRATEGY.md](docs/EVAL_STRATEGY.md).

- `evals/cases.jsonl`: oficiais, variações pt/en, multi-turno, guardrail e casos difíceis que **podem falhar**.
- `evals/heldout_blind.jsonl`: conjunto **cego**, commitado com a spec antes de qualquer código de domínio
  (ver linha do tempo); medido, mas fora do gate — gatear o cego convidaria a ajustar o sistema a ele.
- Gate do CI: acurácia de intenção ≥ 90% nos splits gateados e zero crash.

### Offline (determinístico, o que o CI roda)

<!-- eval:begin -->
Modo: `in-process`

| Métrica | official | memory | handoff | guardrail | variation | hard | heldout | ALL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| cases | 10 | 6 | 1 | 10 | 12 | 8 | 12 | 59 |
| intent_acc | 100.0 | 83.3 | 100.0 | 100.0 | 91.7 | 62.5 | 83.3 | 88.1 |
| handoff_acc | 100.0 | — | 100.0 | 100.0 | 100.0 | 66.7 | 75.0 | 85.7 |
| blocked_acc | — | — | — | 100.0 | — | 0.0 | 100.0 | 88.9 |
| language_acc | 100.0 | 100.0 | — | — | 100.0 | 100.0 | 100.0 | 100.0 |
| tools_acc | 100.0 | 40.0 | — | 100.0 | 75.0 | 25.0 | 80.0 | 65.2 |
| pending_action_acc | — | 100.0 | — | — | — | 100.0 | 100.0 | 100.0 |
| replanned_acc | — | — | — | — | — | — | — | — |
| no_leak_acc | — | — | — | 100.0 | — | 100.0 | 100.0 | 100.0 |
| recall@3 | — | — | — | — | — | — | — | — |
| mrr | — | — | — | — | — | — | — | — |
| p95_ms | 2426.4 | 143.6 | 45.6 | 54.3 | 1867.1 | 66.2 | 118.8 | 954.4 |
| cost_usd | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| crashes | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Gate (splits guardrail, handoff, memory, official, variation): intent 94.9% (mínimo 90%) → **PASS**. Medidos fora do gate: hard, heldout.

Retrieval (bm25, `evals/retrieval.jsonl`, 30 perguntas): Recall@3 **0.65** · MRR **0.567** · hit@3 0.7
<!-- eval:end -->

### Ao vivo (Claude API)

<!-- eval-live:begin -->
_Ainda não rodado._
<!-- eval-live:end -->

### Juiz LLM (Opus 5.5) e concordância com rótulo humano

<!-- judge:begin -->
_Ainda não rodado._
<!-- judge:end -->

## 6. Memória, HITL e resiliência

- **Memória curta:** histórico por conversa em SQLite (últimos 10 turnos entram no prompt); seguimentos como
  "e se eu reiniciar ela?" herdam a intenção (`test_follow_up_uses_conversation_memory`).
- **Memória longa:** resumo por lojista (últimos assuntos + fatos do suporte), entre conversas
  (`test_long_term_summary_spans_conversations`). Determinístico de propósito: auditável e sem custo.
- **HITL:** escalation abre ticket persistido (idempotente por conversa) → operador vê a fila com o histórico em
  `/operator` → responde → a resposta entra na conversa como `human_agent` e o próximo turno a enxerga
  (`test_handoff_operator_reply_and_resume`, `test_ticket_survives_restart`). Gatilhos: pedido explícito, sem fonte,
  falha de tool, baixa confiança do router, decisão do supervisor. O ticket leva a memória do lojista.

  ![Console do operador com um caso aberto](docs/img/operator.png)
- **Resiliência:** retries do SDK (2), timeout de 30 s, fallback por camada (router, guardrail, supervisor, agentes)
  para o caminho determinístico; volume Docker para o SQLite.

## 7. Observabilidade e custo

Spans com `trace_id` em log JSON, tokens e **US$ por request** na própria resposta (`usage`) e em `/metrics`
(Prometheus). Alertas com histerese em [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

## 8. Decisões e trade-offs

| Decisão | Alternativa descartada | Motivo |
|---|---|---|
| Supervisor LLM com especialistas como tools + tabela determinística | plano fixo por intent | replaneja de verdade e continua testável no CI |
| `AUTH_MODE=demo` marcado | confiar no `user_id` do corpo | compatível com o enunciado sem fingir segurança |
| BM25 medido | embeddings desde o início | medir Recall@3/MRR antes de trocar ([ADR 0003](docs/adr/0003-bm25-antes-de-hibrido.md)) |
| SQLite atrás de `Store` | Postgres/Redis | sobe com um comando; trocar para réplicas fica isolado num módulo |
| Resumo de longo prazo determinístico | resumo por LLM | auditável, sem custo e sem PII |

## 9. Cobertura da régua

| Critério (peso) | O que existe | Evidência verificável |
|---|---|---|
| Orquestração (15%) | router Haiku + supervisor Sonnet em tool loop que lê `ok/insufficient/error` e replaneja; tabela determinística idêntica offline; `MAX_STEPS`, `ALLOWED` | `src/app/supervisor.py`; `tests/test_agents.py` (6 testes de tabela + 4 do loop LLM com cliente falso); `replans` em cada resposta |
| Avaliação (14%) | golden set por split (oficial, variação, memória, guardrail, difícil), **held-out cego** commitado com a spec, gate no CI, juiz Opus 5.5 calibrado com 20 rótulos humanos | seção 5 (offline e ao vivo); `evals/heldout_blind.jsonl` no commit `spec`; [EVAL_STRATEGY](docs/EVAL_STRATEGY.md) |
| Segurança (13%) | token HMAC (`AUTH_MODE=required`), modo demo marcado, rate limit por identidade/IP, guardrail em 2 camadas, PII mascarada entrada/saída, segredos padrão sinalizados, threat model | `tests/test_scenarios.py` (10 cenários × 2 modos), `tests/test_api.py`, [THREAT_MODEL](docs/THREAT_MODEL.md) |
| RAG (10%) | crawler getnet.net pt/en → 462 chunks **literais** com URL, data e sha256; BM25 + portão de cobertura; geração só com passagens citadas | `src/app/ingest.py`, [SOURCES](docs/SOURCES.md), Recall@3/MRR na seção 5, [ADR 0003](docs/adr/0003-bm25-antes-de-hibrido.md) |
| Tools (10%) | 6 tools de CRM com JSON Schema `strict`, dias úteis com feriados nacionais e bancários (calculados por ano), códigos de recusa, 2 ações de escrita com confirmação do dono, identidade do servidor | `src/app/tools.py`, `tests/test_tools.py` |
| Business fit (8%) | os 10 cenários do enunciado via HTTP com o payload literal; multi-turno | `tests/test_scenarios.py`; split `official` 100% offline |
| Memória (8%) | histórico SQLite por conversa + resumo de longo prazo por lojista; sobrevive a restart; volume no compose | `tests/test_memory_and_handoff.py` (follow-up, restart, resumo entre conversas) |
| HITL (7%) | fila persistida, console `/operator`, resposta volta à conversa e o próximo turno a vê; Telegram opcional; 5 gatilhos | `test_handoff_operator_reply_and_resume`, screenshot na seção 6 |
| Observabilidade e custo (6%) | spans com `trace_id`, tokens e US$ por request na resposta, `/metrics` Prometheus, alertas com histerese | [OBSERVABILITY](docs/OBSERVABILITY.md), `test_cost_per_request_from_price_table`, custo na tabela ao vivo |
| Produção (5%) | lockfiles, Docker multi-stage não-root, healthcheck, volume, CI com smoke do container | `scripts/check.py --docker` no CI |
| Julgamento (4%) | ADRs, portão léxico medido e declarado fraco, casos difíceis que falham publicados | [docs/adr](docs/adr/), seção 12 |

## 10. Linha do tempo (git log)

<!-- timeline:begin -->
| hora | min | commit |
|---|---:|---|
| 16:42:10 | 0 | chore: scaffold from entregar-desafio-cronometrado kit v2.0.0 |
| 16:45:02 | 3 | spec: contrato Getnet, OWNERSHIP, held-out cego, threat model e ADRs |
| 16:58:00 | 16 | feat: esqueleto Getnet verde — router, supervisor que replaneja, 3 agentes, AUTH_MODE, memória, /operator |
| 17:01:35 | 19 | chore: compose com AUTH_MODE=demo, volume SQLite e .env opcional |
| 17:03:39 | 21 | feat: aviso Telegram opcional de caso humano, alertas com histerese, eval --out |
| 17:04:30 | 22 | eval: 47 casos (oficial, variação pt/en, multi-turno, guardrail, hard sem gate) |
| 17:05:09 | 23 | docs: README com arquitetura, RAG, supervisor, segurança; fill_readme com blocos ao vivo e juiz |
| 17:05:46 | 24 | fix(security): no modo demo o rate limit usa o IP, não o user_id do corpo |
| 17:05:47 | 24 | docs: estratégia de avaliação (pirâmide, LLM falso com falhas, splits, juiz, gate, produção) |
| 17:06:32 | 24 | docs: roteiro do vídeo de 6 min com marcadores para números lidos da tela |
| 17:06:36 | 24 | feat(tools): CRM completo — dias úteis com feriados 2026, códigos de recusa pt/en, escrita com confirmação |
| 17:06:36 | 24 | fork b: pronto |
| 17:06:52 | 25 | feat(ingest): crawler getnet.net (en/pt) + site.getnet.com.br, 462 chunks literais com dedupe |
| 17:08:12 | 26 | fix: 'já reiniciei' não propõe reiniciar; ticket leva a memória do lojista; screenshot do /operator |
| 17:09:02 | 27 | docs+eval: SOURCES.md e retrieval.jsonl (30 casos, Recall@3 0,65 / MRR 0,59 com BM25) |
| 17:10:25 | 28 | fork a: pronto |
| 17:10:56 | 29 | merge: fork a — crawler getnet.net, chunks literais, SOURCES e conjunto de retrieval |
| 17:11:17 | 29 | Merge branch 'main' into fork/c |
| 17:11:40 | 30 | eval: calibração do juiz com 20 rótulos humanos (10 fiéis, 10 alucinações plausíveis) sobre chunks reais do fork A |
| 17:15:09 | 33 | docs: rótulo da tabela de falhas conhecidas após a base do fork A |
| 17:15:10 | 33 | fork c: pronto |
| 17:16:17 | 34 | fix: portão de cobertura no RAG; achados da revisão (teto de voltas do supervisor, LLMError abrangente, compare_digest em bytes, resposta nunca vazia, rótulos de métrica escapados) |
| 17:16:19 | 34 | merge: fork b — tools de CRM, dias úteis com feriados, ações com confirmação |
| 17:17:23 | 35 | merge: fork c — golden set, calibração do juiz, estratégia de eval e roteiro do vídeo |
| 17:19:20 | 37 | eval: Recall@3/MRR do conjunto de retrieval no relatório; rodapé com splits gateados |
| 17:19:56 | 38 | fix(security): /health e log apontam segredos padrão; APP_ENV=production recusa subir com eles |
| 17:20:23 | 38 | docs: tabela de cobertura da régua com evidência; limitações medidas |

Do primeiro ao último commit: **38 min**.
<!-- timeline:end -->

## 11. Scaffold declarado

A infraestrutura genérica vem de um kit próprio, preparado antes do desafio e reutilizado (FastAPI, Docker, CI,
lockfiles, runner de eval e juiz, auth/rate limit, memória, fila de handoff, supervisor por tabela, logs, métricas
e custo). Ela está no primeiro commit (`chore: scaffold`). Tudo o que é específico do desafio — spec, held-out cego,
router, supervisor LLM, agentes Getnet, tools, dados, AUTH_MODE, console do operador, eval — veio depois, nos commits
seguintes. O trabalho foi feito por uma sessão principal e três forks em `git worktree` (conhecimento, ferramentas,
avaliação), cada um com posse de arquivos verificada antes do merge (`scripts/check_ownership.py`).

## 12. Limitações

- CRM fictício; integração real exigiria as APIs internas da Getnet.
- `AUTH_MODE=demo` é inseguro por definição (só para a demonstração do enunciado).
- Rate limit em memória do processo; com réplicas, mover para Redis. SQLite → Postgres.
- OpenTelemetry não exportado (spans prontos para isso); alertas propostos, não provisionados.
- **Portão "sem fonte" offline é léxico e fraco:** cobertura de termos ≥ 0,35 nos 3 melhores trechos mantém todas
  as perguntas positivas medidas, mas barra só 1 de 5 perguntas sem fonte. O portão semântico é o `NOT_IN_SOURCES`
  do modo LLM (compare `blind-12` offline × ao vivo na seção 5).
- Híbrido BM25 + embeddings não foi implementado: a decisão fica para quando o Recall@3 de 0,65 for o gargalo medido.
- Confirmação de ação usa lock em processo; com réplicas, trocar por `UPDATE ... WHERE status='pending'` atômico.
  Sem TTL na ação pendente.
