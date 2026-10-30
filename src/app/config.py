"""Configuração lida do ambiente a cada chamada (testes trocam variáveis sem reimportar)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# USD por 1M de tokens (entrada, saída). Fonte: tabela de preços da Anthropic, consultada em
# 30/10/2026 (cache de 06/10/2026). Confira antes de publicar números de custo; modelo fora da
# tabela é contado com custo 0 e aparece como "unpriced" nas métricas.
PRICES_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def data_dir() -> Path:
    # Caminho explícito: no container o pacote não mora ao lado de data/, e um índice vazio
    # passa despercebido até o smoke test (lição do desafio de 30/10/2026).
    return Path(os.getenv("DATA_DIR", ROOT / "data"))


def db_path() -> Path:
    return Path(os.getenv("DB_PATH", ROOT / "var" / "app.db"))


def llm_provider() -> str:
    # "offline" é determinístico e roda no CI sem chave; o modo com LLM nunca é pré-requisito.
    return os.getenv("LLM_PROVIDER", "offline")


def agent_model() -> str:
    return os.getenv("AGENT_MODEL", "claude-sonnet-5-5")


def judge_model() -> str:
    return os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")


def auth_required() -> bool:
    return os.getenv("AUTH_REQUIRED", "true").lower() == "true"


def auth_secret() -> str:
    return os.getenv("AUTH_SECRET", "dev-secret-change-me")


def operator_token() -> str:
    return os.getenv("OPERATOR_TOKEN", "dev-operator-change-me")


def rate_limit_per_min() -> int:
    # 30/min por usuário: folgado para conversa humana, corta script em loop.
    return int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
