"""Tools de CRM do support agent — esqueleto da sessão principal (o fork B substitui pela versão completa).

Contrato (docs/SPEC.md): run_tool(name, args, user_id, conversation_id=None, confirmed=False) -> (dict, ToolCall);
nunca lança; user_id SEMPRE do servidor; escrita só com confirmação; tool_specs() no formato Anthropic.
CRM fictício e declarado como tal (data/crm/).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .config import crm_dir
from .observability import METRICS, span
from .schemas import ToolCall
from .store import get_store


def _merchants() -> dict:
    out: dict = {}
    for path in sorted(crm_dir().glob("*.json")):
        out.update(json.loads(path.read_text(encoding="utf-8")))
    return out


def _merchant(user_id: str) -> dict | None:
    return _merchants().get(user_id)


def _not_found() -> dict:
    return {"ok": False, "error": "not_found"}


def get_customer_profile(user_id: str) -> dict:
    m = _merchant(user_id)
    return {"ok": True, "data": m["profile"]} if m else _not_found()


def _next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def get_settlement_forecast(user_id: str) -> dict:
    m = _merchant(user_id)
    if not m:
        return _not_found()
    sale = date.today() - timedelta(days=1)
    return {"ok": True, "data": {"sale_date": sale.isoformat(), "deposit_date": _next_business_day(sale).isoformat(),
                                 "rule": "D+1 em dias úteis", "amount_brl": m.get("yesterday_sales_brl", 0)}}


def get_recent_transactions(user_id: str, limit: int = 5) -> dict:
    m = _merchant(user_id)
    return {"ok": True, "data": {"transactions": m.get("transactions", [])[:limit]}} if m else _not_found()


def get_terminal_status(user_id: str) -> dict:
    m = _merchant(user_id)
    return {"ok": True, "data": m.get("terminal", {})} if m else _not_found()


def request_receivables_advance(user_id: str, amount_brl: float | None = None) -> dict:
    m = _merchant(user_id)
    return {"ok": True, "data": {"status": "advance_requested", "amount_brl": amount_brl}} if m else _not_found()


def restart_terminal(user_id: str) -> dict:
    m = _merchant(user_id)
    return {"ok": True, "data": {"status": "restart_sent"}} if m else _not_found()


_OBJ = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
TOOLS = {
    "get_customer_profile": {"description": "Perfil do lojista autenticado (plano, terminais, conta).",
                             "write": False, "input_schema": _OBJ, "fn": get_customer_profile},
    "get_settlement_forecast": {"description": "Quando cai o dinheiro das vendas (liquidação em dias úteis).",
                                "write": False, "input_schema": _OBJ, "fn": get_settlement_forecast},
    "get_recent_transactions": {"description": "Últimas transações, com código e motivo de recusa.",
                                "write": False, "fn": get_recent_transactions,
                                "input_schema": {"type": "object", "additionalProperties": False, "required": [],
                                                 "properties": {"limit": {"type": "integer"}}}},
    "get_terminal_status": {"description": "Status de conexão da maquininha (online/offline, chip, Wi-Fi).",
                            "write": False, "input_schema": _OBJ, "fn": get_terminal_status},
    "request_receivables_advance": {"description": "Pede antecipação de recebíveis. Exige confirmação do lojista.",
                                    "write": True, "fn": request_receivables_advance,
                                    "input_schema": {"type": "object", "additionalProperties": False, "required": [],
                                                     "properties": {"amount_brl": {"type": "number"}}}},
    "restart_terminal": {"description": "Reinicia a maquininha remotamente. Exige confirmação do lojista.",
                         "write": True, "input_schema": _OBJ, "fn": restart_terminal},
}


def tool_specs() -> list[dict]:
    """Formato de tool use da Anthropic (name, description, input_schema, strict)."""
    return [{"name": n, "description": t["description"], "input_schema": t["input_schema"], "strict": True}
            for n, t in TOOLS.items()]


def run_tool(name: str, args: dict, user_id: str, conversation_id: str | None = None,
             confirmed: bool = False) -> tuple[dict, ToolCall]:
    tool = TOOLS.get(name)
    args = {k: v for k, v in (args or {}).items() if k != "user_id"}  # identidade nunca vem do modelo
    if tool is None:
        result = {"ok": False, "error": f"unknown_tool:{name}"}
    elif tool["write"] and not confirmed:
        action_id = get_store().create_action(conversation_id, user_id, name, args)
        result = {"ok": False, "needs_confirmation": True, "action_id": action_id}
    else:
        with span("tool", tool=name):
            try:
                result = tool["fn"](**{**args, "user_id": user_id})
            except Exception as exc:  # erro vira dado para o agente decidir
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    METRICS.inc("tool_calls_total", tool=name, ok=str(result.get("ok")).lower())
    return result, ToolCall(name=name, args=args, ok=bool(result.get("ok") or result.get("needs_confirmation")))


def confirm_action(action_id: str, user_id: str) -> dict:
    store = get_store()
    action = store.get_action(action_id)
    if not action or action["user_id"] != user_id:
        return {"ok": False, "error": "not_found"}
    if action["status"] != "pending":
        return {"ok": False, "error": f"already_{action['status']}"}
    result, _ = run_tool(action["tool"], action["args"], user_id, action["conversation_id"], confirmed=True)
    store.finish_action(action_id, "done" if result.get("ok") else "failed", result)
    return result
