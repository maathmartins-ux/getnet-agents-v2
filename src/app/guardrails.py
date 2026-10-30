"""Guardrails de entrada: barram prompt injection e mascaram PII antes de qualquer agente ou log."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_INJECTION = re.compile(
    r"ignore (all |the )?(previous|above|prior) instructions|ignore (as )?instru[cç][oõ]es"
    r"|system prompt|prompt do sistema|you are now|voc[eê] agora [eé]|developer mode",
    re.IGNORECASE,
)
# 13–19 dígitos cobre todas as bandeiras ISO/IEC 7812; Luhn evita mascarar pedido ou protocolo.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


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
    if _CPF.search(text):
        flags.append("pii_cpf")
        text = _CPF.sub("[CPF]", text)
    if _EMAIL.search(text):
        flags.append("pii_email")
        text = _EMAIL.sub("[EMAIL]", text)
    return text, flags


@dataclass
class Verdict:
    text: str
    blocked: bool = False
    reason: str | None = None
    flags: list[str] = field(default_factory=list)


def check_input(message: str) -> Verdict:
    if _INJECTION.search(message):
        return Verdict(text="", blocked=True, reason="prompt_injection", flags=["prompt_injection"])
    masked, flags = mask_pii(message)
    return Verdict(text=masked, flags=flags)
