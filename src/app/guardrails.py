"""Guardrails em duas camadas.

1. Regex (sempre, custo zero): prompt injection pt/en, pedido de dado de outro lojista, fraude, abuso;
   PII (cartão com Luhn, CPF, e-mail, telefone) mascarada antes de qualquer agente, log ou LLM.
2. Classificador Haiku 5.5 (effort low, JSON schema) quando há LLM: pega o que a regex não vê
   (paráfrase, outro idioma, texto codificado). Falha do classificador não bloqueia — a camada 1 já rodou.

`mask_output` repete a máscara na resposta: o que sai também não vaza PII.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import fast_model
from .llm import LLMError, get_llm

_INJECTION = re.compile(
    r"ignore (all |the |your )?(previous|above|prior)? ?(instructions|rules)"
    r"|ignor[ea] (as |suas )?(instru[cç][oõ]es|regras)"
    r"|esque[cç]a (as |suas |todas as )?(regras|instru[cç][oõ]es)|forget (your|all|the) (rules|instructions)"
    r"|system prompt|prompt do sistema|you are now|voc[eê] agora [eé]|developer mode|modo desenvolvedor|jailbreak",
    re.IGNORECASE,
)
_OTHER_MERCHANT = re.compile(r"\b(dados|vendas|transa[cç][oõ]es|saldo|data|sales|transactions|balance)\b.{0,40}"
                             r"\b(d[oa] (outro )?(lojista|cliente|loja)|of (another |the )?(merchant|customer|store))\b"
                             r"|\b(outro lojista|another merchant|other merchant)\b", re.IGNORECASE)
_FRAUD = re.compile(r"\b(burlar|fraudar|clonar cart[aã]o|clone (a )?card|lavar dinheiro|launder|"
                    r"bypass (the )?(anti-?fraud|fraud)|cart[aã]o de terceiros|stolen card|cart[aã]o roubado)\b",
                    re.IGNORECASE)
_ABUSE = re.compile(r"\b(idiota|imbecil|vai se f\w*|fdp|stupid bot|fuck you)\b", re.IGNORECASE)
# 13–19 dígitos cobre as bandeiras ISO/IEC 7812; Luhn evita mascarar protocolo ou valor.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9\d{4}-?\d{4}(?!\d)")

PROMPT = (Path(__file__).parent / "prompts" / "guardrail.md").read_text(encoding="utf-8")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["category"],
          "properties": {"category": {"type": "string", "enum": ["safe", "prompt_injection", "other_merchant_data",
                                                                 "fraud", "abusive", "out_of_scope"]}}}
BLOCKING = {"prompt_injection", "other_merchant_data", "fraud", "abusive"}


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_pii(text: str) -> tuple[str, list[str]]:
    flags: list[str] = []

    def card(m: re.Match) -> str:
        if _luhn_ok(m.group()):
            flags.append("pii_card")
            return "[CARTÃO]"
        return m.group()

    text = _CARD.sub(card, text)
    for name, rx, label in (("pii_cnpj", _CNPJ, "[CNPJ]"), ("pii_cpf", _CPF, "[CPF]"),
                            ("pii_email", _EMAIL, "[EMAIL]"), ("pii_phone", _PHONE, "[TELEFONE]")):
        if rx.search(text):
            flags.append(name)
            text = rx.sub(label, text)
    return text, flags


def mask_output(text: str) -> str:
    return mask_pii(text)[0]


@dataclass
class Verdict:
    text: str
    blocked: bool = False
    reason: str | None = None
    flags: list[str] = field(default_factory=list)


def _regex_layer(message: str) -> str | None:
    for reason, rx in (("prompt_injection", _INJECTION), ("other_merchant_data", _OTHER_MERCHANT),
                       ("fraud", _FRAUD), ("abusive", _ABUSE)):
        if rx.search(message):
            return reason
    return None


def _llm_layer(masked: str) -> str | None:
    llm = get_llm(fast_model())
    if not llm.enabled:
        return None
    try:
        category = llm.complete(PROMPT, [{"role": "user", "content": masked}], max_tokens=256,
                                schema=SCHEMA).json()["category"]
    except (LLMError, ValueError, KeyError):
        return None  # camada 1 já rodou; indisponibilidade do classificador não derruba o atendimento
    return category if category in BLOCKING else None


def check_input(message: str) -> Verdict:
    reason = _regex_layer(message)
    if reason:
        return Verdict(text="", blocked=True, reason=reason, flags=[reason, "layer_regex"])
    masked, flags = mask_pii(message)
    reason = _llm_layer(masked)
    if reason:
        return Verdict(text="", blocked=True, reason=reason, flags=[*flags, reason, "layer_llm"])
    return Verdict(text=masked, flags=flags)
