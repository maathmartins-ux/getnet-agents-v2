"""Dois provedores atrás da mesma interface: offline (determinístico, CI) e Anthropic.

Os agentes chamam `get_llm(model).complete(...)` / `.create(...)` e checam `.enabled`; nunca importam o SDK.
Toda chamada real registra tokens e custo (observability.record_llm_usage).

Regras da API (conferidas na skill claude-api em 30/10/2026): sem temperature/top_p/top_k (400 desde
21/10/2026); esforço explícito em output_config.effort; structured output via output_config.format;
tool_choice só "auto" (forçado dá 400 no Sonnet 5.5/Opus 5.5); stop_reason "refusal" checado antes do conteúdo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import agent_model, effort_for, llm_provider
from .observability import record_llm_usage, span


class LLMError(RuntimeError):
    """Falha do provedor já classificada; o chamador cai no caminho offline."""


@dataclass
class LLMReply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    def json(self) -> dict:
        return json.loads(self.text)


class OfflineLLM:
    enabled = False
    model = "offline"

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1024,
                 schema: dict | None = None) -> LLMReply:
        raise LLMError("modo offline: o agente usa o caminho determinístico")

    def create(self, system: str, messages: list, tools: list[dict], max_tokens: int = 2048):
        raise LLMError("modo offline: o agente usa o caminho determinístico")


class AnthropicLLM:
    enabled = True

    def __init__(self, model: str, client=None) -> None:
        import anthropic  # import tardio: o modo offline não depende do SDK

        self._sdk = anthropic
        # 30 s por tentativa e 2 retries do SDK (429/5xx/conexão): pior caso ~90 s por chamada.
        self.client = client or anthropic.Anthropic(timeout=30.0, max_retries=2)
        self.model = model

    def _call(self, **kwargs):
        sdk = self._sdk
        output_config = {"effort": effort_for(self.model), **kwargs.pop("output_config", {})}
        with span("llm", model=self.model):
            try:
                resp = self.client.messages.create(model=self.model, output_config=output_config, **kwargs)
            except sdk.RateLimitError as exc:
                raise LLMError(f"rate_limited: {exc}") from exc
            except sdk.APIStatusError as exc:
                raise LLMError(f"api_status_{exc.status_code}: {exc.message}") from exc
            except sdk.APIConnectionError as exc:
                raise LLMError(f"connection: {exc}") from exc
        record_llm_usage(self.model, resp.usage.input_tokens, resp.usage.output_tokens)
        if resp.stop_reason == "refusal":
            raise LLMError("refusal")
        return resp

    def complete(self, system: str, messages: list[dict], max_tokens: int = 1024,
                 schema: dict | None = None) -> LLMReply:
        extra = {"output_config": {"format": {"type": "json_schema", "schema": schema}}} if schema else {}
        resp = self._call(max_tokens=max_tokens, system=system, messages=messages, **extra)
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMReply(text, self.model, resp.usage.input_tokens, resp.usage.output_tokens)

    def create(self, system: str, messages: list, tools: list[dict], max_tokens: int = 2048):
        """Uma volta do loop manual de tool use: devolve a resposta bruta (o chamador executa as tools)."""
        return self._call(max_tokens=max_tokens, system=system, messages=messages, tools=tools,
                          tool_choice={"type": "auto"})


def get_llm(model: str | None = None) -> OfflineLLM | AnthropicLLM:
    if llm_provider() == "anthropic":
        return AnthropicLLM(model or agent_model())
    return OfflineLLM()
