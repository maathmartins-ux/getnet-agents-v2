# ADR 0003 — BM25 medido antes de híbrido

Status: aceito · 30/10/2026

**Contexto.** Poucas centenas de chunks literais; embeddings trazem dependência pesada na imagem.

**Decisão.** BM25 próprio (auditável) como baseline; híbrido (BM25 + embeddings, RRF) só entra se Recall@3/MRR de `evals/retrieval.jsonl` mostrarem ganho. O número decide.
