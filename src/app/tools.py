"""Ferramentas com JSON Schema, identidade do servidor e confirmação para escrita.

Regras: `run_tool` nunca lança exceção (erro vira dado: {ok, data, error}); o `user_id` é
sempre sobrescrito pelo do chamador autenticado; ferramenta de escrita não executa sem
confirmação explícita do dono (POST /actions/{id}/confirm).
"""
from __future__ import annotations

import json
from functools import lru_cache

from .config import data_dir
from .observability import METRICS, span
from .schemas import ToolCall
from .store import get_store

_ID = {"type": "string", "pattern": r"^\d{4}$", "description": "Número do registro (4 dígitos)"}


@lru_cache(maxsize=4)
def _records(path: str) -> dict:
    return json.loads(open(path, encoding="utf-8").read())


def records() -> dict:
    return _records(str(data_dir() / "records.json"))


def _lookup(record_id: str, user_id: str) -> dict:
    rec = records().get(record_id)
    if not rec or rec["owner"] != user_id:  # mesma resposta para inexistente e de outro dono
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "data": {"record_id": record_id, **{k: v for k, v in rec.items() if k != "owner"}}}


def _cancel(record_id: str, user_id: str) -> dict:
    found = _lookup(record_id, user_id)
    if not found["ok"]:
        return found
    return {"ok": True, "data": {"record_id": record_id, "status": "cancel_requested"}}


TOOLS = {
    "lookup_record": {"description": "Consulta um registro do cliente autenticado.", "write": False,
                      "input_schema": {"type": "object", "properties": {"record_id": _ID},
                                       "required": ["record_id"], "additionalProperties": False},
                      "fn": _lookup},
    "cancel_record": {"description": "Solicita cancelamento. Exige confirmação do cliente.", "write": True,
                      "input_schema": {"type": "object", "properties": {"record_id": _ID},
                                       "required": ["record_id"], "additionalProperties": False},
                      "fn": _cancel},
}


def tool_specs() -> list[dict]:
    """Formato de tool use da Anthropic (name, description, input_schema)."""
    return [{"name": n, "description": t["description"], "input_schema": t["input_schema"]} for n, t in TOOLS.items()]


def run_tool(name: str, args: dict, user_id: str, conversation_id: str | None = None,
             confirmed: bool = False) -> tuple[dict, ToolCall]:
    tool = TOOLS.get(name)
    if tool is None:
        result = {"ok": False, "error": f"unknown_tool:{name}"}
    elif tool["write"] and not confirmed:
        action_id = get_store().create_action(conversation_id, user_id, name, args)
        result = {"ok": False, "needs_confirmation": True, "action_id": action_id}
    else:
        with span("tool", tool=name):
            try:
                result = tool["fn"](**{**args, "user_id": user_id})  # identidade do servidor, nunca do modelo
            except Exception as exc:  # erro vira dado para o agente decidir
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    METRICS.inc("tool_calls_total", tool=name, ok=str(result.get("ok")).lower())
    return result, ToolCall(name=name, args=args, ok=bool(result.get("ok")))


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
