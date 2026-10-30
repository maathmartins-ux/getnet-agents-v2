"""Logs JSON com trace_id, spans por etapa e métricas em formato Prometheus."""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

from .config import PRICES_PER_MTOK

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_usage: ContextVar[dict | None] = ContextVar("usage", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": round(record.created, 3), "level": record.levelname,
                   "msg": record.getMessage(), "trace_id": _trace_id.get()}
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload, ensure_ascii=False)


log = logging.getLogger("app")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(JsonFormatter())
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def new_trace_id() -> str:
    tid = uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    _usage.set({"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    return tid


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[tuple, float] = defaultdict(float)
        self.latency: dict[str, list[float]] = defaultdict(list)

    def inc(self, name: str, **labels: str) -> None:
        self.add(name, 1, **labels)

    def add(self, name: str, value: float, **labels: str) -> None:
        self.counters[(name, tuple(sorted(labels.items())))] += value

    def observe(self, span: str, ms: float) -> None:
        self.latency[span].append(ms)

    def render(self) -> str:
        lines = []
        for (name, labels), value in sorted(self.counters.items()):
            lab = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{lab}}} {value:g}")
        for span, values in sorted(self.latency.items()):
            lines.append(f'span_latency_ms_sum{{span="{span}"}} {sum(values):.3f}')
            lines.append(f'span_latency_ms_count{{span="{span}"}} {len(values)}')
        return "\n".join(lines) + "\n"


METRICS = Metrics()


@contextmanager
def span(name: str, **fields):
    start = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - start) * 1000
        METRICS.observe(name, ms)
        log.info("span", extra={"extra_fields": {"span": name, "ms": round(ms, 2), **fields}})


def record_llm_usage(model: str, input_tokens: int, output_tokens: int) -> float:
    """Contabiliza tokens e custo da chamada no request atual, nas métricas e no log."""
    price = PRICES_PER_MTOK.get(model)
    cost = (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000 if price else 0.0
    METRICS.add("llm_tokens_total", input_tokens, model=model, kind="input")
    METRICS.add("llm_tokens_total", output_tokens, model=model, kind="output")
    METRICS.add("llm_cost_usd_total", cost, model=model)
    if price is None:
        METRICS.inc("llm_unpriced_calls_total", model=model)
    usage = _usage.get()
    if usage is not None:
        usage["input_tokens"] += input_tokens
        usage["output_tokens"] += output_tokens
        usage["cost_usd"] = round(usage["cost_usd"] + cost, 6)
    log.info("llm_usage", extra={"extra_fields": {"model": model, "input_tokens": input_tokens,
                                                  "output_tokens": output_tokens, "cost_usd": round(cost, 6)}})
    return cost


def request_usage() -> dict:
    return dict(_usage.get() or {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
