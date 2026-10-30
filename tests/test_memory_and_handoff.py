"""Memória multi-turno persistida e HITL completo: handoff → operador responde → conversa retoma."""
from fastapi.testclient import TestClient

from app.api import app
from app.auth import issue_token
from app.store import Store

client = TestClient(app)
OP = {"X-Operator-Token": "test-operator"}


def auth(user="u1"):
    return {"Authorization": f"Bearer {issue_token(user)}"}


def chat(message, conv=None, user="u1"):
    return client.post("/chat", json={"message": message, "conversation_id": conv}, headers=auth(user)).json()


def test_follow_up_uses_record_from_history():
    first = chat("Onde está meu pedido 1002?")
    second = chat("E pode cancelar ele?", first["conversation_id"])
    assert second["intent"] == "records"
    assert second["tool_calls"][0]["args"] == {"record_id": "1002"}
    assert second["pending_action"]


def test_history_survives_restart(tmp_path, monkeypatch):
    conv = chat("Qual o prazo de entrega?")["conversation_id"]
    from app.config import db_path
    fresh = Store(db_path())  # nova instância = processo reiniciado lendo o mesmo arquivo
    roles = [m["role"] for m in fresh.history(conv)]
    assert roles == ["user", "assistant"]


def test_pii_never_stored_raw():
    conv = chat("Meu CPF é 123.456.789-09, qual o horário de atendimento?")["conversation_id"]
    stored = client.get(f"/conversations/{conv}", headers=auth()).json()["messages"]
    assert all("123.456.789-09" not in m["content"] for m in stored)


def test_handoff_operator_reply_and_resume():
    resp = chat("Quero falar com um atendente")
    ticket = resp["handoff"]["ticket_id"]
    assert resp["handoff"]["required"] and ticket

    assert client.get("/handoffs").status_code == 403  # fila só para operador
    queue = client.get("/handoffs", headers=OP).json()
    assert [t["id"] for t in queue] == [ticket]

    r = client.post(f"/handoffs/{ticket}/reply", json={"answer": "Olá, sou a Ana. Vou te ajudar."}, headers=OP)
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert client.post(f"/handoffs/{ticket}/reply", json={"answer": "de novo"}, headers=OP).status_code == 404

    convo = client.get(f"/conversations/{resp['conversation_id']}", headers=auth()).json()
    assert convo["messages"][-1] == {**convo["messages"][-1], "role": "human_agent"}
    assert convo["open_ticket"] is None


def test_same_conversation_does_not_open_two_tickets():
    first = chat("Quero falar com um atendente")
    second = chat("Atendente, por favor", first["conversation_id"])
    assert first["handoff"]["ticket_id"] == second["handoff"]["ticket_id"]


def test_write_action_needs_owner_confirmation():
    resp = chat("Quero cancelar o pedido 1001")
    action = resp["pending_action"]
    assert client.post(f"/actions/{action}/confirm", headers=auth("u2")).status_code == 404
    done = client.post(f"/actions/{action}/confirm", headers=auth("u1")).json()
    assert done == {"ok": True, "data": {"record_id": "1001", "status": "cancel_requested"}}
    assert client.post(f"/actions/{action}/confirm", headers=auth("u1")).json()["error"] == "already_done"
