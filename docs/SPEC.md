# Spec — Getnet Multi-Agent Support System (v2)

Commitada antes de qualquer código de domínio. Congelada (OWNERSHIP.json): mudança aqui é decisão da sessão principal.

## Problema
Lojistas Getnet mandam mensagens livres (produto, conta, problema técnico, assuntos gerais). Um sistema com
pelo menos três agentes cooperando — Router, Knowledge (RAG + web) e Customer Support (dados do cliente, ≥ 2 tools) —
responde via `POST /chat` com payload `{"message", "user_id"}`, empacotado em Docker.

## Requisitos

| ID | Requisito | Módulo | Teste / eval |
|---|---|---|---|
| FR-01 | Router classifica intent + plano inicial + idioma + confiança (Haiku 5.5, JSON schema; offline por regra) | `router.py` | `tests/test_router.py`, eval `intent_acc` |
| FR-02 | Supervisor executa o plano, observa `ok/insufficient/error` e replaneja; máx. 4 passos (Sonnet 5.5 tool loop; offline por tabela) | `supervisor.py` | `tests/test_supervisor.py`, eval `replanned` |
| FR-03 | Knowledge agent: RAG sobre trechos literais de getnet.net (pt/en) + web tools (clima, câmbio) | `agents.py`, `rag.py`, `web_tools.py` | eval `recall@3`, `mrr` |
| FR-04 | Support agent: ≥ 4 tools de CRM com JSON Schema, identidade do servidor, ação de escrita com confirmação | `tools.py` (fork B) | `tests/test_tools.py` |
| FR-05 | Escalation agent: fila persistida, console `/operator`, resposta do operador volta à conversa | `store.py`, `api.py` | `tests/test_memory_and_handoff.py` |
| FR-06 | `POST /chat` aceita `{"message","user_id"}`; `AUTH_MODE=demo` usa o corpo (identity_verified=false), `required` só Bearer | `api.py`, `auth.py` | `tests/test_scenarios.py` (10 oficiais × 2 modos) |
| FR-07 | Memória: histórico SQLite por conversa + resumo de longo prazo por usuário | `store.py` | eval split `memory` |
| FR-08 | Guardrails em 2 camadas: regex/PII + classificador (Haiku 5.5); PII mascarada na entrada e na saída | `guardrails.py` | eval split `guardrail` |
| NFR-01 | Sobe com `docker compose up` | `Dockerfile` | `scripts/check.py --docker` |
| NFR-02 | Funciona sem chave (LLM_PROVIDER=offline, determinístico) — caminho do CI | `config.py`, `llm.py` | CI |
| NFR-03 | Tokens e US$ por request; métricas Prometheus; logs JSON com trace_id | `observability.py` | `tests/test_api.py` |
| NFR-04 | Rate limit por identidade | `auth.py` | `tests/test_api.py` |
| NFR-05 | Eval com gate no CI; held-out cego commitado nesta spec | `evals/` | `scripts/check.py` |

## Cenários de aceite (enunciado, literais)

| ID | Mensagem | Esperado |
|---|---|---|
| AC-01 | What's the difference between the Get Clássica and the Get Smart? | product_question · knowledge · fontes getnet.net |
| AC-02 | What's the weather forecast in Porto Alegre tomorrow? | general_info · knowledge (web: clima) · sem handoff |
| AC-03 | When will the money from yesterday's sales be deposited? | account_data · support · `get_settlement_forecast` (dias úteis) |
| AC-04 | Do I need a bank account to receive my sales via Pix? | product_question · knowledge |
| AC-05 | My card machine won't connect to the internet, what should I do? | technical_support · support → knowledge · `get_terminal_status` |
| AC-06 | How does receivables advance (antecipação) work with Getnet? | product_question · knowledge |
| AC-07 | What's the euro exchange rate today? | general_info · knowledge (web: câmbio) · sem handoff |
| AC-08 | My card machine is showing a transaction decline error. | technical_support · support → knowledge · `get_recent_transactions` |
| AC-09 | How many installments can I split a sale into with the crediário? | product_question · knowledge |
| AC-10 | Can I sell through WhatsApp using the Payment Link? | product_question · knowledge |

Todos com `user_id = cliente1988`. Cada AC vira `official-NN` em `evals/cases.jsonl` e um teste HTTP em `tests/test_scenarios.py`.

## Contrato entre módulos
- `pipeline.handle(req: ChatRequest, user_id: str, identity_verified: bool) -> ChatResponse`
- Agente: `fn(ctx: Context) -> AgentResult(status ∈ ok|insufficient|error)`; nenhum agente chama outro.
- `tools.run_tool(name, args, user_id, conversation_id=None, confirmed=False) -> (dict, ToolCall)`; nunca lança. `tools.tool_specs()` no formato Anthropic.
- Tools de CRM: `get_customer_profile`, `get_settlement_forecast`, `get_recent_transactions`, `get_terminal_status`; escrita com confirmação: `request_receivables_advance`, `restart_terminal`.
- `data/knowledge/chunks.jsonl`: `{id, title, url, lang, text, fetched_at, sha256}`, `text` literal da fonte.
- `data/crm/*.json`: base fictícia de lojistas (declarada como tal).

## Fora de escopo
- Integração real com sistemas Getnet (CRM é fictício e declarado).
- Login de usuário final (token emitido por `python -m app.auth <user_id>`; produção usaria OIDC).

## Divisão em forks
Ver OWNERSHIP.json. A: conhecimento · B: tools de suporte · C: avaliação. Principal: orquestração, segurança, memória, HITL, integração.
