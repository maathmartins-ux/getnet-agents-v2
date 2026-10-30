# Estratégia de avaliação e testes

Pergunta que este documento responde: **como sabemos que a orquestração funciona — e como saberíamos
que parou de funcionar?** Resposta curta: quatro camadas de teste, um golden set dividido por split,
um held-out cego que ninguém ajustou, um juiz LLM com concordância humana medida e um gate no CI.

Números não aparecem aqui de propósito: a fonte única é `evals/results/latest.json`, e o README é
preenchido a partir dele (`scripts/fill_readme.py`). Número copiado à mão envelhece no primeiro commit.

## 1. Pirâmide

```
                 ┌──────────────────────────────┐
                 │  Eval ao vivo (LLM real)     │  poucas execuções, custo em US$, juiz calibrado
                 ├──────────────────────────────┤
                 │  Eval offline (golden set)   │  59 casos, todo commit, gate de intent
                 ├──────────────────────────────┤
                 │  Integração da orquestração  │  pipeline inteiro com LLM falso que injeta falhas
                 ├──────────────────────────────┤
                 │  Unidade                     │  router, guardrails, tools, RAG, store, auth
                 └──────────────────────────────┘
```

| Camada | Onde | O que prova | Quando roda |
|---|---|---|---|
| Unidade | `tests/test_router.py`, `test_guardrails.py`, `test_api.py` | Regra local: classificação, máscara de PII com Luhn, rate limit, schema da API | Todo commit (`scripts/check.py`) |
| Integração | `tests/test_agents.py`, `test_memory_and_handoff.py`, `test_scenarios.py` | Agentes conversando via supervisor; memória sobrevive a restart; handoff → operador → retomada; 10 cenários oficiais via HTTP nos dois `AUTH_MODE` | Todo commit |
| Eval offline | `evals/run_evals.py` + `cases.jsonl` + `heldout_blind.jsonl` | Comportamento de ponta a ponta medido por split, com gate | Todo commit e no CI |
| Eval ao vivo | `run_evals.py --url` com `LLM_PROVIDER=anthropic` + `evals/judge.py` | Qualidade semântica da resposta gerada, custo e latência reais | Antes de release e em mudança de prompt/modelo |

**Por que essa forma:** a parte cara e não determinística (LLM) fica no topo e roda pouco. Tudo que
decide roteamento, identidade e segurança tem caminho offline determinístico (`LLM_PROVIDER=offline`),
então o CI testa a lógica sem chave, sem rede e sem flakiness.

## 2. Integração da orquestração com LLM falso que injeta falhas

O risco de um sistema multiagente não está em cada agente isolado, e sim nas **transições**: o que o
supervisor faz quando um agente devolve `insufficient` ou `error`, ou quando o próprio LLM falha.
Para testar isso sem depender do humor do modelo, os testes substituem o cliente LLM por um falso
programável (monkeypatch em `get_llm`) que devolve uma sequência roteirizada:

| Falha injetada | Comportamento esperado | Teste |
|---|---|---|
| Knowledge sem fonte (`insufficient`) | Supervisor replaneja para escalation; `replans` registra o desvio | `test_table_replans_when_no_source`, `test_no_source_escalates_with_replan` |
| Support com erro de tool | Cai para knowledge, não para o usuário | `test_table_support_error_falls_back_to_knowledge` |
| Web tool indisponível em `general_info` | Responde que não sabe; **não** abre ticket humano | `test_table_general_info_unavailable_does_not_escalate` |
| Confiança baixa do router | Acrescenta escalation ao plano | `test_low_confidence_adds_escalation` |
| LLM propõe transição proibida | Bloqueada pela tabela `ALLOWED` | `test_llm_supervisor_blocks_disallowed_transition` |
| LLM em laço | Para em `MAX_STEPS = 4` | `test_supervisor_respects_max_steps` |
| Provedor fora do ar / timeout | Cai na tabela offline, atendimento segue | `test_llm_failure_falls_back_to_table` |
| Router devolve JSON inválido | Cai na regra offline | `test_router_llm_invalid_json_falls_back` |
| Modelo tenta passar `user_id` para a tool | Ignorado; identidade vem do servidor | `test_support_llm_tool_loop_enforces_server_identity` |
| Tool inexistente | Erro vira dado, nunca exceção | `test_unknown_tool_never_raises` |

Princípio: **toda falha vira estado observável** (`status`, `replans`, `handoff.reason`, métrica
`replans_total`), nunca exceção que derruba a requisição. O eval conta `crashes` por split e o gate
reprova com qualquer crash.

## 3. Golden set por split

`evals/cases.jsonl` (47 casos) + `evals/heldout_blind.jsonl` (12 casos). Cada caso declara só o que
é verificável por máquina: `intent`, `handoff`, `blocked`, `language`, `tools` (subconjunto chamado),
`pending_action`, `replanned`, `not_contains`, `doc_ids` (Recall@3 e MRR) e `before` (multi-turno).

| Split | Casos | O que cobre | No gate? |
|---|---:|---|---|
| `official` | 10 | Os 10 cenários literais do enunciado | Sim |
| `variation` | 12 | Os mesmos pedidos parafraseados em pt e en, mais pedido de humano em en | Sim |
| `memory` | 6 | Multi-turno: seguimento com pronome, confirmação de ação, troca de assunto após handoff | Sim |
| `handoff` | 1 | Pedido explícito de humano | Sim |
| `guardrail` | 10 | Injection pt/en, dado de outro lojista (nos dois sentidos), PII (CPF, cartão com Luhn), abuso, fraude, fora de escopo | Sim |
| `hard` | 8 | Ambíguo sem contexto, dois pedidos na mesma frase, negação ("não quero atendente"), pergunta sem fonte que parece ter, injection parafraseada sem palavra-chave, ação de escrita direta | **Não** |
| `heldout` | 12 | Cego: escrito e commitado com a spec, antes de qualquer código de domínio | **Não** |

**Regras do golden set**

- **Caso não se ajusta para passar.** Quando um caso falha, ou o sistema está errado (corrige o
  sistema) ou o caso está errado (corrige o caso *com justificativa no commit*). Nunca as duas coisas
  no mesmo commit.
- **`hard` e `heldout` ficam fora do gate.** Gatear o que deveria medir generalização convida a
  ajustar o sistema a esses casos específicos — e aí deixam de medir generalização. Eles são
  reportados à parte, e a distância entre `official` e `heldout` é o número honesto.
- **100% em tudo é sinal de conjunto viciado**, não de sistema bom. O split `hard` existe para ter
  casos que falham hoje e mostram onde o sistema quebra.

**Falhas conhecidas que o golden set expõe (modo offline, antes da integração dos forks A e B):**

| Caso | Falha | Leitura |
|---|---|---|
| `var-12` "Show me my last transactions" | Router offline devolve `out_of_scope` | Regex de conta não cobre "my last transactions" — lacuna real do classificador por regra |
| `memory-05` "E qual delas eu tenho?" | `out_of_scope` | Seguimento sem pronome da lista (`delas`) perde o contexto |
| `memory-02`, `memory-04` | Intent certa, tool esperada não chamada | Seguimento herda a intent, mas o agente não refaz a consulta |
| `hard-01` dois pedidos | Só a primeira tool | Plano único por intent; pedido composto exigiria decomposição |
| `hard-04` "cadê meu dinheiro?" | `product_question` | "vendas" casa com produto antes de conta |
| `hard-05` injection parafraseada | Não bloqueia no offline | Camada regex não vê paráfrase; é o papel da camada 2 (classificador Haiku) — medir no eval ao vivo |
| `hard-06` "não quero atendente" | `human_request` | Regex não entende negação |

Estes casos ficam falhando até o sistema mudar. Rode `python -m evals.run_evals` para a lista atual.

## 4. Métricas por split

- **Roteamento:** `intent_acc`, `handoff_acc`, `blocked_acc`, `language_acc`.
- **Ação:** `tools_acc` (a tool certa foi chamada), `pending_action_acc` (escrita só com confirmação).
- **Orquestração:** `replanned_acc`.
- **Segurança:** `no_leak_acc` (nem PII nem dado de outro lojista aparece em nenhum campo da resposta).
- **Recuperação:** Recall@3 e MRR contra `doc_ids` com ids reais de `data/knowledge/chunks.jsonl`.
- **Operação:** p95 de latência, custo em US$ por split, `crashes`.

## 5. Juiz LLM calibrado

As métricas acima dizem se o sistema *roteou* certo, não se a *resposta* está correta. Para isso,
`evals/judge.py` (Opus 5.5, JSON schema) dá `grounded`, `helpful` e `score` 1–5 para cada resposta
do último eval.

Juiz sem calibração é opinião de outro modelo. Por isso `evals/judge_calibration.jsonl` traz
**20 respostas com rótulo humano**: 10 corretas e 10 com alucinação plausível (número trocado,
prazo inventado, benefício que a fonte não promete, extrapolação de escopo), todas sobre trechos
reais da base. O relatório publica `human_agreement` — a fração em que o juiz concorda com o humano.

- Concordância ≥ 0,9: o `grounded_rate` do juiz entra no relatório como evidência.
- Abaixo disso: o número do juiz é mostrado com aviso e o prompt do juiz é revisado antes de qualquer
  conclusão sobre o sistema.
- O juiz **não é gate** no CI: depende de chave e custa dinheiro. Roda antes de release.

## 6. Gate no CI

`scripts/check.py` roda ruff → pytest → `evals.run_evals` e o CI chama o mesmo script.
O gate reprova o build se:

1. `intent_acc` dos splits gateados ficar abaixo de `EVAL_GATE` (0,9), ou
2. houver **qualquer** crash em qualquer split, incluindo `hard` e `heldout`.

O piso de 0,9 se ajusta por desafio, nunca para fazer passar. `check.py --docker` acrescenta build da
imagem, `/health` e o mesmo eval via HTTP contra o container.

## 7. O que medir em produção

O eval offline protege contra regressão conhecida; produção mostra o que ninguém previu.

| Sinal | Fonte | Alerta sugerido |
|---|---|---|
| Taxa de handoff por intent | `chat_requests_total`, tickets | Salto > 2× a média de 7 dias: KB desatualizada ou router quebrado |
| Taxa de replanejamento por agente/motivo | `replans_total` | Knowledge `insufficient` subindo: base precisa de recrawl |
| Bloqueios do guardrail por camada | flags `layer_regex` / `layer_llm` | Pico súbito: ataque em curso ou falso positivo novo |
| Erros de tool | `tool_calls_total{ok="false"}` | > 5% em 15 min: CRM degradado |
| Fallback para offline | logs do `LLMError` | Qualquer fallback sustentado: provedor fora |
| Custo por request e por intent | `usage.cost_usd` | p95 acima do orçamento |
| Latência p95 por rota | spans com `trace_id` | p95 > SLO por 10 min |
| Ações de escrita confirmadas × pedidas | store de ações | Confirmação caindo: texto da confirmação confuso |
| Amostragem para o juiz | 1–5% das conversas, PII já mascarada | `grounded_rate` semanal abaixo do baseline do eval |

**Fechar o ciclo:** toda conversa que terminou em handoff, bloqueio contestado ou nota baixa do juiz
é candidata a caso novo. Ela entra no golden set com rótulo humano — no split `hard` se o sistema
ainda erra — e o próximo commit passa a medir aquilo.
