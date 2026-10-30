"""Memória multi-turno persistida e HITL completo: handoff → operador responde → conversa retoma."""
from fastapi.testclient import TestClient

from app.api import app
from app.auth import issue_token
from app.store import Store, get_store

client = TestClient(app)
OP = {"X-Operator-Token": "test-operator"}


def auth(user="cliente1988"):
    return {"Authorization": f"Bearer {issue_token(user)}"}


def chat(message, conv=None, user="cliente1988"):
    return client.post("/chat", json={"message": message, "conversation_id": conv}, headers=auth(user)).json()


def test_follow_up_uses_conversation_memory():
    first = chat("Minha maquininha não conecta na internet")
    second = chat("E se eu reiniciar ela?", first["conversation_id"])
    assert second["intent"] == "technical_support"
    assert "restart_terminal" in {c["name"] for c in second["tool_calls"]} and second["pending_action"]


def test_history_survives_restart():
    conv = chat("Como funciona o Pix?")["conversation_id"]
    from app.config import db_path
    fresh = Store(db_path())  # nova instância = processo reiniciado lendo o mesmo arquivo
    assert [m["role"] for m in fresh.history(conv)] == ["user", "assistant"]


def test_long_term_summary_spans_conversations():
    chat("Minha maquininha não conecta na internet")
    chat("Como funciona o Pix?")  # outra conversa
    summary = get_store().user_summary("cliente1988")
    assert "technical_support" in summary and "product_question" in summary
    assert get_store().user_summary("cliente2020") == ""  # memória é por lojista


def test_pii_never_stored_raw():
    conv = chat("Meu CPF é 123.456.789-09, como funciona o Pix?")["conversation_id"]
    stored = client.get(f"/conversations/{conv}", headers=auth()).json()["messages"]
    assert all("123.456.789-09" not in m["content"] for m in stored)


def test_handoff_operator_reply_and_resume():
    resp = chat("Quero falar com um atendente")
    ticket = resp["handoff"]["ticket_id"]
    assert resp["handoff"]["required"] and ticket

    assert client.get("/handoffs").status_code == 403  # fila só para operador
    queue = client.get("/handoffs", headers=OP).json()
    assert [t["id"] for t in queue] == [ticket] and queue[0]["messages"]

    r = client.post(f"/handoffs/{ticket}/reply", json={"answer": "Olá, sou a Ana. Vou te ajudar."}, headers=OP)
    assert r.status_code == 200 and r.json()["status"] == "answered"
    assert client.post(f"/handoffs/{ticket}/reply", json={"answer": "de novo"}, headers=OP).status_code == 404

    convo = client.get(f"/conversations/{resp['conversation_id']}", headers=auth()).json()
    assert convo["messages"][-1]["role"] == "human_agent" and convo["open_ticket"] is None
    # retomada: o próximo turno do lojista na mesma conversa enxerga a resposta humana no histórico
    chat("Obrigado! E como funciona o Pix?", resp["conversation_id"])
    msgs = client.get(f"/conversations/{resp['conversation_id']}", headers=auth()).json()["messages"]
    assert [m["role"] for m in msgs][-3:] == ["human_agent", "user", "assistant"]


def test_ticket_survives_restart():
    ticket = chat("Quero falar com um atendente")["handoff"]["ticket_id"]
    from app.config import db_path
    assert Store(db_path()).get_ticket(ticket)["status"] == "open"


def test_same_conversation_does_not_open_two_tickets():
    first = chat("Quero falar com um atendente")
    second = chat("Atendente, por favor", first["conversation_id"])
    assert first["handoff"]["ticket_id"] == second["handoff"]["ticket_id"]


def test_no_source_escalates_with_replan():
    resp = chat("Qual o horário de funcionamento da agência Getnet em Lisboa?")
    assert resp["replans"] and resp["handoff"]["required"]


def test_write_action_needs_owner_confirmation():
    resp = chat("Quero antecipar meus recebíveis")
    action = resp["pending_action"]
    assert action and "Nada foi feito" in resp["answer"]
    assert client.post(f"/actions/{action}/confirm", headers=auth("cliente2020")).status_code == 404
    done = client.post(f"/actions/{action}/confirm", headers=auth()).json()
    assert done["ok"] is True
    assert client.post(f"/actions/{action}/confirm", headers=auth()).json()["error"] == "already_done"


def test_telegram_notify_only_when_configured(monkeypatch):
    from app import notify

    sent = []
    monkeypatch.setattr(notify, "_send", lambda token, chat, text: sent.append(text))
    monkeypatch.setattr(notify.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: target(*args)})())
    assert notify.notify_new_ticket({"id": "T-1", "reason": "r", "user_id": "u", "summary": "s"}) is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    chat("Quero falar com um atendente")
    assert len(sent) == 1 and "/operator" in sent[0]
