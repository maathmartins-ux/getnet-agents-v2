# Roteiro do vídeo (~6 min)

Regra de gravação: **todo número é lido da tela no momento da gravação**. Onde aparece `<NÚMERO>`,
o valor vem do terminal ou do arquivo indicado ao lado — nunca deste roteiro. Se o número da tela
for ruim, fala-se o número ruim.

Preparação (fora do relógio da gravação):

```bash
make check                                  # ruff + pytest + eval gate; deixa evals/results/latest.json fresco
docker compose up -d --build                # API em http://localhost:8000
python -m app.auth cliente1988              # token para o modo AUTH_MODE=required
```

Abas abertas: terminal, `README.md`, `evals/results/latest.json`, `http://localhost:8000/operator`,
`http://localhost:8000/metrics`, GitHub Actions do último commit.

---

## 0:00–0:40 — O feedback da v1 e a regra da v2

**Tela:** tabela "Cobertura da régua" do README.

> "Esta é a segunda versão do case. A primeira tirou 7,1. Dois critérios puxaram a nota para baixo:
> memória e estado, com 1, e supervisão humana, com 4,5 — eu não tinha feito. E a avaliação, que pesa
> 14%, mostrava 100% em tudo, o que o avaliador leu, com razão, como conjunto de teste viciado.
>
> A regra da v2 foi: cobertura antes de polimento. Nenhum critério que pesa 5% ou mais fica sem o
> mínimo viável, e o eval tem que ter casos que falham."

## 0:40–2:00 — Arquitetura: supervisor que replaneja

**Tela:** diagrama de arquitetura do README.

> "A mensagem entra pelo `POST /chat`. Primeiro passa por guardrails em duas camadas: regex, que é
> custo zero e mascara PII antes de qualquer modelo ver, e um classificador Haiku para o que a regex
> não pega.
>
> Depois o router — Haiku com saída em JSON schema — decide intenção, idioma, confiança e um plano
> inicial. A diferença para a v1 está aqui: o plano é só o ponto de partida. Quem executa é um
> supervisor, que chama três agentes como ferramentas — knowledge, support e escalation — e lê o
> status de cada um: ok, insufficient ou error.
>
> Se o knowledge não acha fonte, o supervisor replaneja para escalation. Se a tool de CRM falha, cai
> para o knowledge. Tudo que desvia do plano fica registrado no campo `replans` da resposta. E há
> trilhos: uma tabela de transições permitidas e um teto de 4 passos, para o modelo não entrar em laço.
>
> Sem chave de API, o mesmo fluxo roda com regras determinísticas. É esse caminho que o CI testa."

**Mostrar:** `src/app/supervisor.py` — `ALLOWED`, `MAX_STEPS`, `FALLBACK`.

## 2:00–3:40 — Demo

**Tela:** terminal com `curl` e o console `/operator` lado a lado.

1. **Três cenários oficiais** (AC-01, AC-03, AC-05):
   > "Pergunta de produto: vem com fontes do getnet.net. Quando cai o dinheiro: o support chama
   > `get_settlement_forecast` e responde em dias úteis. Maquininha sem internet: support consulta o
   > status do terminal e o knowledge completa com o procedimento — dois agentes na mesma resposta."
   - Apontar `route`, `tool_calls`, `sources` e `usage.cost_usd` no JSON: custo deste request
     `<NÚMERO>` US$.
2. **Multi-turno:** "Minha maquininha não conecta na internet" → "E se eu reiniciar ela?"
   > "A segunda mensagem não diz 'maquininha'. O sistema lembra da conversa e entende."
3. **Ação com confirmação:** a resposta traz `pending_action`. Mostrar que nada aconteceu ainda;
   chamar `POST /actions/{id}/confirm`.
   > "Escrita no sistema do lojista nunca acontece sem confirmação explícita do dono da conta."
4. **Handoff:** "Quero falar com um atendente humano" → ticket aparece em `/operator` → operador
   responde → `GET /conversations/{id}` mostra a resposta de volta na conversa.
   > "O ticket está persistido em SQLite: se o container reinicia, ele continua na fila."

## 3:40–4:40 — Eval

**Tela:** saída de `python -m evals.run_evals` e `docs/EVAL_STRATEGY.md`.

> "São `<NÚMERO>` casos em sete splits. Os oficiais, variações em português e inglês, multi-turno e
> guardrail entram no gate do CI: acurácia de intenção `<NÚMERO>`%, mínimo de 90.
>
> Dois splits ficam fora do gate de propósito. O held-out cego foi escrito e commitado junto com a
> spec, antes de qualquer código — dá para ver no git log. Nele a acurácia é `<NÚMERO>`%. E o split
> `hard` tem casos que eu sei que falham: dois pedidos na mesma frase, 'não quero falar com atendente',
> injection parafraseada. Ali está em `<NÚMERO>`%. Esses números menores são o ponto: mostram onde o
> sistema quebra, em vez de um 100% que não diz nada."

**Mostrar:** a lista de falhas impressa no fim da saída — ler dois casos em voz alta.

> "Recuperação: Recall@3 de `<NÚMERO>` e MRR de `<NÚMERO>` sobre trechos literais do site.
>
> Para a qualidade da resposta, um juiz Opus dá nota de groundedness. Mas juiz sem calibração é
> opinião de outro modelo. Então escrevi 20 respostas com rótulo humano — metade certa, metade com
> alucinação plausível — e o juiz concorda comigo em `<NÚMERO>`% delas."

Se o juiz não rodou (sem chave na gravação): dizer "o juiz não rodou nesta gravação" e mostrar
`evals/results/judge.json` com `status: skipped`. Não citar concordância.

## 4:40–5:30 — Segurança, custo e CI

**Tela:** `docs/THREAT_MODEL.md`, `/metrics`, GitHub Actions.

> "Identidade: com `AUTH_MODE=required` o `user_id` vem só do token assinado; o do corpo é ignorado.
> No modo demo, que aceita o payload do enunciado, a resposta marca `identity_verified: false`.
> As tools recebem o `user_id` do servidor — o modelo não consegue pedir dado de outro lojista, e há
> caso no eval que tenta.
>
> Rate limit por identidade. PII mascarada na entrada, no log e na saída.
>
> Custo: cada resposta traz tokens e dólares; em `/metrics` estão contadores de replanejamento,
> handoff e erro de tool, que são os sinais que eu alertaria em produção.
>
> O CI roda o mesmo `check.py`: lint, `<NÚMERO>` testes, eval com gate e smoke do container."

## 5:30–6:00 — Limitações honestas

> "O que não está pronto: o router por regra, que é o caminho do CI, erra pedidos compostos e
> negação — os casos `hard` mostram isso. A base de conhecimento é um recorte do site, não o site
> inteiro. O CRM é fictício e declarado como tal. E o juiz não é gate, porque depende de chave e custa
> dinheiro: roda antes de release, não em todo commit.
>
> O próximo passo seria decomposição de pedido composto no supervisor e busca híbrida medida contra o
> BM25 atual. Obrigado."

---

## Checklist antes de gravar

- [ ] `make check` verde no commit que vai ser gravado (anotar o hash na tela)
- [ ] Todos os `<NÚMERO>` lidos de `evals/results/latest.json` / `judge.json` / saída do pytest
- [ ] Nenhum token ou segredo visível no terminal
- [ ] Duração ≤ 6:30
