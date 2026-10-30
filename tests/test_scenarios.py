"""Os 10 cenários oficiais do enunciado via HTTP (TestClient), nos dois modos de autenticação.

demo: payload literal do enunciado {"message","user_id"} → 200 com identity_verified=false.
required: o mesmo payload sem token → 401; com Bearer → 200 com identity_verified=true e o user_id do token.
"""
import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.auth import issue_token

client = TestClient(app)
USER = "cliente1988"
SCENARIOS = [
    ("What's the difference between the Get Clássica and the Get Smart?", "product_question", None),
    ("What's the weather forecast in Porto Alegre tomorrow?", "general_info", None),
    ("When will the money from yesterday's sales be deposited?", "account_data", "get_settlement_forecast"),
    ("Do I need a bank account to receive my sales via Pix?", "product_question", None),
    ("My card machine won't connect to the internet, what should I do?", "technical_support", "get_terminal_status"),
    ("How does receivables advance (antecipação) work with Getnet?", "product_question", None),
    ("What's the euro exchange rate today?", "general_info", None),
    ("My card machine is showing a transaction decline error.", "technical_support", "get_recent_transactions"),
    ("How many installments can I split a sale into with the crediário?", "product_question", None),
    ("Can I sell through WhatsApp using the Payment Link?", "product_question", None),
]


def _check(body, intent, tool):
    assert body["intent"] == intent
    assert body["answer"].strip()
    assert body["language"] == "en"
    if tool:
        assert tool in {c["name"] for c in body["tool_calls"]}
    if intent == "product_question":
        assert body["sources"], "resposta de produto sem fonte"
    if intent == "general_info":
        assert body["handoff"]["required"] is False  # clima/câmbio não vão para humano


@pytest.mark.parametrize("message,intent,tool", SCENARIOS)
def test_official_scenario_demo_mode(monkeypatch, message, intent, tool):
    monkeypatch.setenv("AUTH_MODE", "demo")
    r = client.post("/chat", json={"message": message, "user_id": USER})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == USER and body["identity_verified"] is False
    _check(body, intent, tool)


@pytest.mark.parametrize("message,intent,tool", SCENARIOS)
def test_official_scenario_required_mode(message, intent, tool):
    assert client.post("/chat", json={"message": message, "user_id": USER}).status_code == 401
    r = client.post("/chat", json={"message": message, "user_id": "cliente2020"},
                    headers={"Authorization": f"Bearer {issue_token(USER)}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == USER and body["identity_verified"] is True  # corpo ignorado
    _check(body, intent, tool)


def test_demo_mode_token_wins_over_body(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo")
    r = client.post("/chat", json={"message": "When will the money from yesterday's sales be deposited?",
                                   "user_id": "cliente2020"}, headers={"Authorization": f"Bearer {issue_token(USER)}"})
    assert r.json()["user_id"] == USER and "99.999" not in r.json()["answer"]


def test_demo_mode_forged_token_is_rejected(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo")
    r = client.post("/chat", json={"message": "oi", "user_id": USER}, headers={"Authorization": "Bearer x.forjado"})
    assert r.status_code == 401
