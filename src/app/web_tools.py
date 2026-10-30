"""Ferramentas web do knowledge agent para perguntas gerais (clima, câmbio).

APIs públicas sem chave: Open-Meteo (geocoding + previsão) e AwesomeAPI (câmbio BRL).
Mesma regra das tools de CRM: nunca lança — erro vira dado. WEB_TOOLS=off (CI/testes) não acessa a rede
e devolve `unavailable`, para o agente responder com honestidade em vez de inventar.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .config import web_tools_mode
from .observability import METRICS, span

_CITY = re.compile(r"\b(?:in|em|para|de|at)\s+([A-ZÀ-Ú][\wÀ-ú]+(?:\s+[A-ZÀ-Ú][\wÀ-ú]+)*)")
_CURRENCIES = {"euro": "EUR", "eur": "EUR", "dólar": "USD", "dolar": "USD", "dollar": "USD", "usd": "USD",
               "libra": "GBP", "pound": "GBP", "peso": "ARS", "iene": "JPY", "yen": "JPY"}


def _get(url: str, params: dict) -> dict:
    import httpx

    r = httpx.get(url, params=params, timeout=8)
    r.raise_for_status()
    return r.json()


def weather(text: str) -> dict:
    match = _CITY.search(text)
    city = match.group(1) if match else None
    if not city:
        return {"ok": False, "error": "city_not_found"}
    if web_tools_mode() != "live":
        return {"ok": False, "error": "unavailable", "city": city}
    with span("tool", tool="weather"):
        try:
            geo = _get("https://geocoding-api.open-meteo.com/v1/search",
                       {"name": city, "count": 1, "language": "pt"}).get("results") or []
            if not geo:
                return {"ok": False, "error": "city_not_found", "city": city}
            g = geo[0]
            fc = _get("https://api.open-meteo.com/v1/forecast",
                      {"latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto",
                       "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                       "forecast_days": 2})["daily"]
        except Exception as exc:  # rede, timeout, formato
            METRICS.inc("tool_calls_total", tool="weather", ok="false")
            return {"ok": False, "error": f"{type(exc).__name__}"}
    METRICS.inc("tool_calls_total", tool="weather", ok="true")
    i = 1 if len(fc["time"]) > 1 else 0  # amanhã
    return {"ok": True, "source": "Open-Meteo", "url": "https://open-meteo.com/",
            "data": {"city": g["name"], "date": fc["time"][i], "max_c": fc["temperature_2m_max"][i],
                     "min_c": fc["temperature_2m_min"][i], "rain_prob": fc["precipitation_probability_max"][i]}}


def exchange_rate(text: str) -> dict:
    low = text.lower()
    code = next((c for k, c in _CURRENCIES.items() if re.search(rf"\b{k}\b", low)), None)
    if not code:
        return {"ok": False, "error": "currency_not_found"}
    if web_tools_mode() != "live":
        return {"ok": False, "error": "unavailable", "currency": code}
    with span("tool", tool="exchange_rate"):
        try:
            q = _get(f"https://economia.awesomeapi.com.br/json/last/{code}-BRL", {})[f"{code}BRL"]
        except Exception as exc:
            METRICS.inc("tool_calls_total", tool="exchange_rate", ok="false")
            return {"ok": False, "error": f"{type(exc).__name__}"}
    METRICS.inc("tool_calls_total", tool="exchange_rate", ok="true")
    return {"ok": True, "source": "AwesomeAPI", "url": "https://docs.awesomeapi.com.br/api-de-moedas",
            "data": {"pair": f"{code}/BRL", "bid": float(q["bid"]), "ask": float(q["ask"]),
                     "timestamp": q.get("create_date")}}


def pick(text: str) -> str | None:
    low = text.lower()
    if re.search(r"weather|forecast|clima|previs|temperatura|chover|rain", low):
        return "weather"
    if re.search(r"exchange|c[aâ]mbio|cota[cç]|d[oó]lar|dollar|euro|libra|pound", low):
        return "exchange_rate"
    return None


WEB_TOOLS = {"weather": weather, "exchange_rate": exchange_rate}


def tomorrow() -> str:
    return (date.today() + timedelta(days=1)).isoformat()
