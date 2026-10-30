"""Aviso opcional de novo caso humano no Telegram (Bot API direta, sem dependência nova).

Ativo só com TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no ambiente. Melhor esforço: falha de rede nunca
derruba o atendimento — o caso já está persistido na fila e visível em /operator.
O texto vai sem PII (o resumo do ticket é montado a partir da mensagem já mascarada).
"""
from __future__ import annotations

import os
import threading

from .observability import METRICS


def _send(token: str, chat_id: str, text: str) -> None:
    import httpx

    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text[:4000]}, timeout=5).raise_for_status()
        METRICS.inc("handoff_notifications_total", channel="telegram", ok="true")
    except Exception:
        METRICS.inc("handoff_notifications_total", channel="telegram", ok="false")


def notify_new_ticket(ticket: dict) -> bool:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    text = (f"Novo caso {ticket['id']} ({ticket['reason']})\nLojista: {ticket['user_id']}\n{ticket['summary']}\n"
            f"Responda em /operator")
    threading.Thread(target=_send, args=(token, chat_id, text), daemon=True).start()
    return True
