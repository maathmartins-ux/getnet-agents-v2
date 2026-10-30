"""Tools de CRM do support agent (FR-04).

Contrato (docs/SPEC.md): run_tool(name, args, user_id, conversation_id=None, confirmed=False) -> (dict, ToolCall);
nunca lança; user_id SEMPRE do servidor; escrita só com confirmação; tool_specs() no formato Anthropic.
CRM fictício e declarado como tal (data/crm/README.md). Nada aqui chama sistema real da Getnet.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from .config import crm_dir
from .observability import METRICS, span
from .schemas import ToolCall
from .store import get_store

BRT = timezone(timedelta(hours=-3), "BRT")  # o container roda em UTC; "ontem" é sempre o de Brasília
ADVANCE_FEE_PCT = Decimal("2.49")  # taxa FICTÍCIA da simulação de antecipação
MAX_LIMIT = 20


def _now() -> datetime:
    return datetime.now(BRT)


def _today() -> date:
    return _now().date()


# --- calendário: dias úteis e feriados nacionais -----------------------------------------------
def _easter(year: int) -> date:
    """Domingo de Páscoa (algoritmo gregoriano anônimo)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    w = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * w) // 451
    month, day = divmod(h + w - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=16)
def holidays(year: int) -> dict[date, str]:
    """Feriados nacionais + feriados bancários móveis (Carnaval e Corpus Christi: bancos fechados, sem liquidação)."""
    easter = _easter(year)
    return {
        date(year, 1, 1): "Confraternização Universal",
        easter - timedelta(days=48): "Carnaval (segunda)",
        easter - timedelta(days=47): "Carnaval (terça)",
        easter - timedelta(days=2): "Sexta-feira Santa",
        date(year, 4, 21): "Tiradentes",
        date(year, 5, 1): "Dia do Trabalhador",
        easter + timedelta(days=60): "Corpus Christi",
        date(year, 9, 7): "Independência do Brasil",
        date(year, 10, 12): "Nossa Senhora Aparecida",
        date(year, 11, 2): "Finados",
        date(year, 11, 15): "Proclamação da República",
        date(year, 11, 20): "Dia da Consciência Negra",
        date(year, 12, 25): "Natal",
    }


_WEEKEND = {5: "sábado", 6: "domingo"}


def non_business_reason(d: date) -> str | None:
    """Por que `d` não é dia útil (None se for)."""
    return holidays(d.year).get(d) or _WEEKEND.get(d.weekday())


def next_business_day(d: date) -> tuple[date, list[dict]]:
    """D+1 útil: primeiro dia útil estritamente depois de `d`, mais os dias pulados (com motivo)."""
    skipped: list[dict] = []
    cur = d + timedelta(days=1)
    while (why := non_business_reason(cur)) is not None:
        skipped.append({"date": cur.isoformat(), "reason": why})
        cur += timedelta(days=1)
    return cur, skipped


# --- decline codes: ISO 8583 de emissor/adquirente usados no mercado brasileiro -----------------
DECLINE_CODES: dict[str, dict] = {
    "05": {"category": "issuer", "retryable": False,
           "pt": "Não autorizada pelo banco emissor (recusa genérica).",
           "en": "Not authorized by the issuing bank (generic decline).",
           "action_pt": "Peça ao cliente para falar com o banco emissor ou usar outro cartão.",
           "action_en": "Ask the customer to contact the issuing bank or use another card."},
    "12": {"category": "terminal", "retryable": True,
           "pt": "Transação inválida.", "en": "Invalid transaction.",
           "action_pt": "Refaça a venda; se repetir, reinicie a maquininha.",
           "action_en": "Redo the sale; if it repeats, restart the terminal."},
    "14": {"category": "card", "retryable": True,
           "pt": "Número do cartão inválido.", "en": "Invalid card number.",
           "action_pt": "Tente ler o cartão de novo (chip/aproximação) ou use outro cartão.",
           "action_en": "Try reading the card again (chip/tap) or use another card."},
    "41": {"category": "card", "retryable": False,
           "pt": "Cartão informado como perdido.", "en": "Card reported lost.",
           "action_pt": "Não insista na venda; peça outro meio de pagamento.",
           "action_en": "Do not retry; ask for another payment method."},
    "43": {"category": "card", "retryable": False,
           "pt": "Cartão informado como roubado.", "en": "Card reported stolen.",
           "action_pt": "Não insista na venda; peça outro meio de pagamento.",
           "action_en": "Do not retry; ask for another payment method."},
    "51": {"category": "issuer", "retryable": False,
           "pt": "Saldo ou limite insuficiente no cartão do comprador.",
           "en": "Insufficient funds or credit limit on the buyer's card.",
           "action_pt": "Sugira outro cartão, Pix ou dividir o pagamento.",
           "action_en": "Suggest another card, Pix, or splitting the payment."},
    "54": {"category": "card", "retryable": False,
           "pt": "Cartão vencido.", "en": "Expired card.",
           "action_pt": "Peça outro cartão ou outro meio de pagamento.",
           "action_en": "Ask for another card or payment method."},
    "55": {"category": "card", "retryable": True,
           "pt": "Senha incorreta.", "en": "Incorrect PIN.",
           "action_pt": "Peça ao cliente para digitar a senha novamente com calma.",
           "action_en": "Ask the customer to re-enter the PIN carefully."},
    "57": {"category": "issuer", "retryable": False,
           "pt": "Transação não permitida para este cartão (ex.: parcelado ou internacional).",
           "en": "Transaction not permitted for this card (e.g. installments or international).",
           "action_pt": "Tente à vista/débito ou outro cartão.",
           "action_en": "Try a single payment/debit or another card."},
    "58": {"category": "terminal", "retryable": False,
           "pt": "Transação não permitida para este terminal/estabelecimento.",
           "en": "Transaction not permitted for this terminal/merchant.",
           "action_pt": "Fale com o suporte Getnet para revisar as permissões do terminal.",
           "action_en": "Contact Getnet support to review the terminal's permissions."},
    "61": {"category": "issuer", "retryable": False,
           "pt": "Valor acima do limite permitido pelo emissor.",
           "en": "Amount exceeds the issuer's allowed limit.",
           "action_pt": "Divida a venda em mais de um cartão ou use outro meio de pagamento.",
           "action_en": "Split the sale across cards or use another payment method."},
    "62": {"category": "card", "retryable": False,
           "pt": "Cartão com restrição.", "en": "Restricted card.",
           "action_pt": "O cliente deve falar com o banco emissor.",
           "action_en": "The customer should contact the issuing bank."},
    "65": {"category": "issuer", "retryable": False,
           "pt": "Limite de quantidade de transações excedido.", "en": "Transaction count limit exceeded.",
           "action_pt": "Tente mais tarde ou use outro cartão.",
           "action_en": "Try later or use another card."},
    "75": {"category": "card", "retryable": False,
           "pt": "Número de tentativas de senha excedido.", "en": "PIN attempts exceeded.",
           "action_pt": "O cartão foi bloqueado; o cliente deve contatar o banco emissor.",
           "action_en": "The card is locked; the customer must contact the issuing bank."},
    "91": {"category": "network", "retryable": True,
           "pt": "Banco emissor fora do ar ou sem comunicação.", "en": "Issuing bank unavailable.",
           "action_pt": "Tente novamente em alguns minutos.", "action_en": "Try again in a few minutes."},
    "96": {"category": "network", "retryable": True,
           "pt": "Falha de sistema.", "en": "System malfunction.",
           "action_pt": "Tente novamente; se persistir, fale com o suporte Getnet.",
           "action_en": "Try again; if it persists, contact Getnet support."},
}
_UNKNOWN_DECLINE = {"category": "unknown", "retryable": False,
                    "pt": "Recusa sem detalhe disponível.", "en": "Decline with no detail available.",
                    "action_pt": "Tente outro cartão; se for recorrente, fale com o suporte Getnet.",
                    "action_en": "Try another card; if recurrent, contact Getnet support."}


# --- CRM ----------------------------------------------------------------------------------------
def _merchants() -> dict:
    out: dict = {}
    for path in sorted(crm_dir().glob("*.json")):
        out.update(json.loads(path.read_text(encoding="utf-8")))
    return out


def _merchant(user_id: str) -> dict | None:
    return _merchants().get(user_id) if isinstance(user_id, str) and user_id else None


def _not_found() -> dict:
    return {"ok": False, "error": "not_found"}


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_customer_profile(user_id: str) -> dict:
    m = _merchant(user_id)
    return {"ok": True, "data": dict(m["profile"])} if m else _not_found()


def get_settlement_forecast(user_id: str) -> dict:
    m = _merchant(user_id)
    if not m:
        return _not_found()
    sale = _today() - timedelta(days=1)
    deposit, skipped = next_business_day(sale)
    note_pt = (f"Pulamos {', '.join(s['reason'] for s in skipped)}." if skipped
               else "Sem feriado ou fim de semana no caminho.")
    return {"ok": True, "data": {
        "sale_date": sale.isoformat(), "deposit_date": deposit.isoformat(),
        "rule": "D+1 em dias úteis", "amount_brl": m.get("yesterday_sales_brl", 0),
        "skipped_days": skipped, "note": note_pt,
        "calendar": "feriados nacionais, Carnaval e Corpus Christi (dias sem liquidação bancária)"}}


def get_recent_transactions(user_id: str, limit: int = 5, status: str = "all") -> dict:
    m = _merchant(user_id)
    if not m:
        return _not_found()
    today = _today()
    txs = []
    for t in m.get("transactions", []):
        if status != "all" and t.get("status") != status:
            continue
        info = _UNKNOWN_DECLINE if t.get("status") == "declined" and t.get("code") not in DECLINE_CODES \
            else DECLINE_CODES.get(t.get("code", ""))
        item = {k: t[k] for k in ("id", "amount_brl", "installments", "brand", "type", "status", "code", "time")
                if k in t}
        item["date"] = (today - timedelta(days=t.get("days_ago", 0))).isoformat()
        if t.get("status") == "declined" and info:
            item.update(reason=info["pt"], reason_pt=info["pt"], reason_en=info["en"], category=info["category"],
                        retryable=info["retryable"], merchant_action_pt=info["action_pt"],
                        merchant_action_en=info["action_en"])
        else:
            item.update(reason="aprovada", reason_pt="Aprovada.", reason_en="Approved.")
        txs.append(item)
    txs = txs[:limit]
    return {"ok": True, "data": {"transactions": txs,
                                 "declined_count": sum(1 for t in txs if t["status"] == "declined")}}


def get_terminal_status(user_id: str) -> dict:
    m = _merchant(user_id)
    if not m:
        return _not_found()
    t = dict(m.get("terminal", {}))
    ago = t.pop("last_seen_minutes_ago", None)
    if ago is not None:
        t["last_seen"] = (_now() - timedelta(minutes=ago)).isoformat(timespec="seconds")
    t["diagnosis_pt"], t["diagnosis_en"] = _diagnose(t)
    return {"ok": True, "data": t}


def _diagnose(t: dict) -> tuple[str, str]:
    if t.get("status") == "online":
        return "Terminal online e comunicando normalmente.", "Terminal online and communicating normally."
    wifi, chip = t.get("wifi") or {}, t.get("chip") or {}
    if t.get("connection") == "wifi" and wifi.get("signal") == "weak":
        chip_ok = chip.get("signal") == "good"
        alt_pt = " O chip tem sinal bom: troque a conexão para chip." if chip_ok else ""
        alt_en = " The SIM chip has a good signal: switch the connection to chip." if chip_ok else ""
        return ("Terminal offline: sinal Wi-Fi fraco. Aproxime-o do roteador ou reinicie o roteador." + alt_pt,
                "Terminal offline: weak Wi-Fi signal. Move it closer to the router or restart the router." + alt_en)
    if t.get("connection") == "chip" and chip.get("signal") in ("weak", "none"):
        return ("Terminal offline: sinal do chip fraco. Mude de lugar ou use Wi-Fi.",
                "Terminal offline: weak SIM signal. Move to another spot or use Wi-Fi.")
    return ("Terminal offline: reinicie a maquininha e confira a conexão.",
            "Terminal offline: restart the terminal and check the connection.")


# --- escrita (só via confirmação) ---------------------------------------------------------------
def _advance_quote(m: dict, amount_brl: float | None) -> dict:
    available = Decimal(str(m.get("receivables_available_brl", 0)))
    if not m["profile"].get("advance_eligible"):
        return {"ok": False, "error": "not_eligible"}
    amount = available if amount_brl is None else Decimal(str(amount_brl))
    if amount <= 0:
        return {"ok": False, "error": "invalid_amount"}
    if amount > available:
        return {"ok": False, "error": "amount_exceeds_available", "available_brl": _money(available)}
    fee = amount * ADVANCE_FEE_PCT / 100
    return {"ok": True, "data": {"simulation": True, "amount_brl": _money(amount), "fee_pct": float(ADVANCE_FEE_PCT),
                                 "fee_brl": _money(fee), "net_brl": _money(amount - fee),
                                 "available_brl": _money(available), "credit_date": _today().isoformat(),
                                 "disclaimer": "Simulação com taxa fictícia; nenhum valor é movimentado."}}


def preview_request_receivables_advance(user_id: str, amount_brl: float | None = None) -> dict:
    m = _merchant(user_id)
    return _advance_quote(m, amount_brl) if m else _not_found()


def request_receivables_advance(user_id: str, amount_brl: float | None = None) -> dict:
    result = preview_request_receivables_advance(user_id, amount_brl)
    if result["ok"]:
        result["data"]["status"] = "advance_simulated"
    return result


def preview_restart_terminal(user_id: str) -> dict:
    m = _merchant(user_id)
    if not m:
        return _not_found()
    t = m.get("terminal", {})
    return {"ok": True, "data": {"terminal_model": t.get("model"), "serial_masked": t.get("serial_masked"),
                                 "effect": "reinício remoto; a maquininha fica indisponível por cerca de 90 segundos"}}


def restart_terminal(user_id: str) -> dict:
    result = preview_restart_terminal(user_id)
    if result["ok"]:
        result["data"].update(status="restart_sent", expected_back_online_s=90)
    return result


# --- registro de tools --------------------------------------------------------------------------
def _schema(properties: dict | None = None) -> dict:
    return {"type": "object", "properties": properties or {}, "required": [], "additionalProperties": False}


TOOLS = {
    "get_customer_profile": {
        "description": "Perfil do lojista autenticado (plano, segmento, cidade, nº de terminais, elegibilidade a "
                       "antecipação). Não aceita parâmetros: a identidade vem do servidor.",
        "write": False, "input_schema": _schema(), "fn": get_customer_profile},
    "get_settlement_forecast": {
        "description": "Quando cai na conta o dinheiro das vendas de ontem (liquidação D+1 em dias úteis, "
                       "considerando fim de semana e feriados nacionais). Devolve data da venda, data do depósito e "
                       "valor.",
        "write": False, "input_schema": _schema(), "fn": get_settlement_forecast},
    "get_recent_transactions": {
        "description": "Últimas transações do lojista, mais recentes primeiro. Recusas trazem o código real do "
                       "emissor (ex.: 51, 05, 55) com explicação em pt/en e a ação sugerida ao lojista.",
        "write": False, "fn": get_recent_transactions,
        "input_schema": _schema({
            "limit": {"type": "integer", "description": "Quantas transações (1 a 20; padrão 5)."},
            "status": {"type": "string", "enum": ["all", "declined", "approved"],
                       "description": "Filtro por status; padrão all."}})},
    "get_terminal_status": {
        "description": "Status da maquininha: online/offline, conexão (Wi-Fi ou chip), sinal, último contato e "
                       "diagnóstico em pt/en. Use para problemas de conexão.",
        "write": False, "input_schema": _schema(), "fn": get_terminal_status},
    "request_receivables_advance": {
        "description": "Simula a antecipação de recebíveis. AÇÃO DE ESCRITA: não executa na hora; devolve "
                       "needs_confirmation + action_id e o lojista precisa confirmar.",
        "write": True, "fn": request_receivables_advance, "preview": preview_request_receivables_advance,
        "input_schema": _schema({"amount_brl": {
            "type": "number", "description": "Valor a antecipar em R$; se omitido, todo o disponível."}})},
    "restart_terminal": {
        "description": "Reinicia a maquininha remotamente. AÇÃO DE ESCRITA: não executa na hora; devolve "
                       "needs_confirmation + action_id e o lojista precisa confirmar.",
        "write": True, "input_schema": _schema(), "fn": restart_terminal, "preview": preview_restart_terminal},
}

_PY_TYPES = {"integer": int, "number": (int, float), "string": str, "boolean": bool}


def _validate_args(schema: dict, args: dict) -> dict:
    """Valida contra o input_schema (additionalProperties:false, tipos, enum, limites). Devolve args normalizados."""
    props = schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ValueError(f"unknown_arguments:{','.join(unknown)}")
    out = {}
    for key, val in args.items():
        if val is None:
            continue
        spec = props[key]
        expected = _PY_TYPES[spec["type"]]
        if isinstance(val, bool) or not isinstance(val, expected):
            raise ValueError(f"invalid_type:{key}")
        if "enum" in spec and val not in spec["enum"]:
            raise ValueError(f"invalid_value:{key}")
        out[key] = val
    if "limit" in out and not 1 <= out["limit"] <= MAX_LIMIT:
        raise ValueError("invalid_value:limit")
    return out


def tool_specs() -> list[dict]:
    """Formato de tool use da Anthropic (name, description, input_schema, strict)."""
    return [{"name": n, "description": t["description"], "input_schema": t["input_schema"], "strict": True}
            for n, t in TOOLS.items()]


def _execute(name: str, tool: dict, args: dict, user_id: str, conversation_id: str | None, confirmed: bool) -> dict:
    args = _validate_args(tool["input_schema"], args)
    if tool["write"] and not confirmed:
        preview = tool["preview"](user_id, **args)  # valida e confere o dono ANTES de criar a ação
        if not preview.get("ok"):
            return preview
        action_id = get_store().create_action(conversation_id, user_id, name, args)
        return {"ok": False, "needs_confirmation": True, "action_id": action_id, "preview": preview["data"]}
    return tool["fn"](**{**args, "user_id": user_id})


def run_tool(name: str, args: dict, user_id: str, conversation_id: str | None = None,
             confirmed: bool = False) -> tuple[dict, ToolCall]:
    """Executa uma tool. Nunca lança; `user_id` do servidor sobrescreve qualquer valor vindo do modelo."""
    args = {k: v for k, v in (args if isinstance(args, dict) else {}).items() if k != "user_id"}
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        result = {"ok": False, "error": f"unknown_tool:{name}"}
    else:
        with span("tool", tool=name):
            try:
                result = _execute(name, tool, args, user_id, conversation_id, confirmed)
            except ValueError as exc:
                result = {"ok": False, "error": f"invalid_arguments:{exc}"}
            except Exception as exc:  # erro vira dado para o agente decidir
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    METRICS.inc("tool_calls_total", tool=str(name), ok=str(result.get("ok")).lower())
    return result, ToolCall(name=str(name), args=args, ok=bool(result.get("ok") or result.get("needs_confirmation")))


_CONFIRM_LOCK = threading.Lock()  # dois cliques em "confirmar" não executam a ação duas vezes


def confirm_action(action_id: str, user_id: str) -> dict:
    try:
        with _CONFIRM_LOCK:
            store = get_store()
            action = store.get_action(action_id)
            if not action or action["user_id"] != user_id:  # ação de outro usuário é indistinguível de inexistente
                return {"ok": False, "error": "not_found"}
            if action["status"] != "pending":
                return {"ok": False, "error": f"already_{action['status']}"}
            result, _ = run_tool(action["tool"], action["args"], user_id, action["conversation_id"], confirmed=True)
            store.finish_action(action_id, "done" if result.get("ok") else "failed", result)
            return result
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
