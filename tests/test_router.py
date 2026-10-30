import pytest

from app.router import classify_offline, detect_language


@pytest.mark.parametrize("text,lang", [
    ("I want to talk to a human agent please", "en"),
    ("My card machine is offline", "en"),
    ("How many installments can I split a sale into with the crediário?", "en"),
    ("Olá, como funciona o Pix?", "pt"),
    ("Quero falar com um atendente", "pt"),
    ("Minha maquininha está sem internet", "pt"),
])
def test_detect_language(text, lang):
    assert detect_language(text) == lang


@pytest.mark.parametrize("text,intent,plan", [
    ("My card machine won't connect to the internet", "technical_support", ["support", "knowledge"]),
    ("When will the money from yesterday's sales be deposited?", "account_data", ["support"]),
    ("What's the euro exchange rate today?", "general_info", ["knowledge"]),
    ("Quero falar com um atendente", "human_request", ["escalation"]),
    ("Me conta uma piada", "out_of_scope", []),
])
def test_offline_router(text, intent, plan):
    d = classify_offline(text, [])
    assert (d.intent, d.plan) == (intent, plan)


def test_follow_up_needs_history():
    assert classify_offline("e isso?", []).intent == "out_of_scope"
    history = [{"role": "user", "content": "maquininha sem sinal", "intent": "technical_support"}]
    assert classify_offline("e isso?", history).intent == "technical_support"
