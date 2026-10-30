"""Router: intenção + plano inicial + idioma + confiança.

Com LLM: Haiku 5.5 (effort low) com saída em JSON schema. Offline (CI e fallback): regras
determinísticas abaixo. O plano é só o ponto de partida — quem decide os passos seguintes é o supervisor.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import fast_model
from .llm import LLMError, get_llm
from .schemas import RouterDecision

PROMPT = (Path(__file__).parent / "prompts" / "router.md").read_text(encoding="utf-8")
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["intent", "plan", "language", "confidence"],
    "properties": {
        "intent": {"type": "string", "enum": ["product_question", "account_data", "technical_support",
                                              "general_info", "human_request", "out_of_scope"]},
        "plan": {"type": "array", "items": {"type": "string", "enum": ["knowledge", "support", "escalation"]}},
        "language": {"type": "string", "enum": ["pt", "en"]},
        "confidence": {"type": "number"},
    },
}
PLANS = {
    "product_question": ["knowledge"],
    "account_data": ["support"],
    "technical_support": ["support", "knowledge"],  # dados do terminal antes do manual
    "general_info": ["knowledge"],
    "human_request": ["escalation"],
    "out_of_scope": [],
}

_HUMAN = re.compile(r"\b(atendente|humano|pessoa real|falar com algu[eé]m|human|representative|real person"
                    r"|operador|operator|talk to (an? )?agent)\b", re.I)
_GENERAL = re.compile(r"\b(weather|forecast|clima|previs[aã]o do tempo|temperatura|chover|rain|exchange rate"
                      r"|c[aâ]mbio|cota[cç][aã]o|d[oó]lar|dollar|euro|news|not[ií]cia)\b", re.I)
_DEVICE = re.compile(r"\b(maquininha|m[aá]quina|card machine|machine|terminal|pos|pinpad)\b", re.I)
_TROUBLE = re.compile(r"\b(connect\w*|conect\w*|internet|wi-?fi|sinal|signal|erro|error|decline\w*|negad\w*"
                      r"|recusad\w*|n[aã]o liga|desliga\w*|travad\w*|reinici\w*|restart|offline|chip|tela)\b", re.I)
_ACCOUNT = re.compile(r"\b(my sales|minhas vendas|deposit\w*|depositad\w*|get paid|cai na conta|cair na conta"
                      r"|receber minhas|meus receb[ií]veis|my receivables|quero antecipar|want to advance"
                      r"|sold|vendi|saldo|balance|extrato|statement|minhas transa[cç][oõ]es|my transactions)\b", re.I)
_PRODUCT = re.compile(r"\b(getnet|get \w+|maquininha|card machine|pix|link de pagamento|payment link|credi[aá]rio"
                      r"|antecipa\w*|receivables|taxa\w*|fee\w*|mdr|parcel\w*|installments?|whatsapp|conta digital"
                      r"|bank account|conta banc[aá]ria|aproxima[cç][aã]o|contactless|tap|boleto|cart[aã]o|card"
                      r"|plano|plan|vend\w*|sell\w*)\b", re.I)
# Pergunta genérica sobre como algo funciona ("Do I need a bank account to receive my sales via Pix?")
# é produto, mesmo citando "my sales": o lojista quer a regra, não o dado da conta dele.
_GENERIC_Q = re.compile(r"^\s*(do i need|can i|how (does|do|many|much)|what is|is it possible|preciso (de|ter)"
                        r"|posso|como funciona|quantas|é poss[ií]vel)\b", re.I)
_FOLLOWUP = re.compile(r"\b(ela|ele|isso|essa|esse|it|that|this)\b", re.I)

_EN = re.compile(r"\b(the|what|how|why|when|my|is|are|can|you|please|want|to|do|does|of|and|for|this|that|me"
                 r"|need|help|where|will|won't|from|with|using|into|i)\b", re.I)
_PT = re.compile(r"\b(os|as|um|uma|de|do|da|que|como|com|para|por|n[aã]o|meu|minha|quero|est[aá]|falar|ajuda"
                 r"|preciso|sem|no|na|qual|onde|posso|vocês|ela|ele|pra|mim)\b", re.I)
_ACCENT = re.compile(r"[ãõáéíóúâêôç]", re.I)


def detect_language(text: str) -> str:
    # Acento vale 1 ponto para pt, não decide sozinho ("crediário" numa frase em inglês).
    en = len(_EN.findall(text))
    pt = len(_PT.findall(text)) + min(1, len(_ACCENT.findall(text)))
    return "en" if en > pt else "pt"


def classify_offline(text: str, history: list[dict]) -> RouterDecision:
    lang = detect_language(text)

    def d(intent: str, conf: float) -> RouterDecision:
        return RouterDecision(intent=intent, plan=PLANS[intent], language=lang, confidence=conf)

    if _HUMAN.search(text):
        return d("human_request", 0.95)
    if _GENERAL.search(text):
        return d("general_info", 0.9)
    if _TROUBLE.search(text) and (_DEVICE.search(text) or _FOLLOWUP.search(text)):
        return d("technical_support", 0.85)
    if _ACCOUNT.search(text) and not (_GENERIC_Q.search(text) and _PRODUCT.search(text)):
        return d("account_data", 0.85)
    if _PRODUCT.search(text):
        return d("product_question", 0.8)
    last = next((m["intent"] for m in reversed(history) if m["role"] == "user" and m.get("intent")), None)
    if last and _FOLLOWUP.search(text):  # memória: seguimento herda a intenção anterior
        return d(last, 0.6) if last in PLANS else d("out_of_scope", 0.5)
    return d("out_of_scope", 0.7)


def route(text: str, history: list[dict]) -> RouterDecision:
    llm = get_llm(fast_model())
    if not llm.enabled:
        return classify_offline(text, history)
    recent = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in history[-4:])
    try:
        raw = llm.complete(PROMPT, [{"role": "user", "content": f"Histórico recente:\n{recent or '(vazio)'}"
                                                                 f"\n\nMensagem: {text}"}],
                           max_tokens=512, schema=SCHEMA).json()
        raw["confidence"] = min(1.0, max(0.0, float(raw["confidence"])))
        return RouterDecision(**raw)
    except (LLMError, ValueError, KeyError, TypeError):
        return classify_offline(text, history)
