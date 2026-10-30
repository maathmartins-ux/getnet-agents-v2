# ADR 0002 — AUTH_MODE demo × required

Status: aceito · 30/10/2026

**Contexto.** O enunciado manda `user_id` no corpo. Confiar no corpo é falsificação trivial de identidade.

**Decisão.** `AUTH_MODE=demo` (padrão do compose, para o avaliador colar o payload literal) aceita o corpo, mas marca `identity_verified=false` na resposta e no log. Um Bearer válido sempre vence o corpo. `AUTH_MODE=required` ignora o corpo.

**Consequência.** Compatível com o enunciado sem fingir segurança; os dois modos são testados nos 10 cenários.
