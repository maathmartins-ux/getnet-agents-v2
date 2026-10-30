import pytest

from app.pipeline import classify, detect_language


@pytest.mark.parametrize("text,lang", [
    ("I want to talk to a human agent please", "en"),
    ("My card machine is offline", "en"),
    ("Where is my order 1002?", "en"),
    ("Olá, como funciona o produto?", "pt"),
    ("Quero falar com um atendente", "pt"),
    ("Minha maquininha está sem internet", "pt"),
])
def test_detect_language(text, lang):
    assert detect_language(text) == lang


def test_classify_follow_up_needs_history():
    assert classify("e isso?", []) == "faq"
    history = [{"role": "user", "content": "pedido 1002", "intent": "records"}]
    assert classify("e isso?", history) == "records"
