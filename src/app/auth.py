"""Identidade e limite de uso.

Token = "<user_id>.<HMAC-SHA256(user_id)>": o user_id vem assinado pelo servidor, nunca do
corpo da requisição nem do modelo. Em produção troque por JWT/OIDC do provedor de identidade;
a interface (verify_token -> user_id) é a mesma.

Uso: python -m app.auth u1   → imprime um token de desenvolvimento para o usuário u1.
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import threading
import time
from collections import defaultdict, deque

from .config import auth_secret, rate_limit_per_min


def _sign(user_id: str) -> str:
    return hmac.new(auth_secret().encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(user_id: str) -> str:
    return f"{user_id}.{_sign(user_id)}"


def verify_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    user_id, _, sig = token.rpartition(".")
    # bytes, não str: compare_digest com str não-ASCII levanta TypeError (500 em vez de 401).
    return user_id if user_id and hmac.compare_digest(sig.encode(), _sign(user_id).encode()) else None


class RateLimiter:
    """Janela deslizante de 60 s por chave. Em memória: com réplicas, mover para Redis."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now, limit = time.monotonic(), rate_limit_per_min()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


LIMITER = RateLimiter()

if __name__ == "__main__":
    print(issue_token(sys.argv[1] if len(sys.argv) > 1 else "u1"))
