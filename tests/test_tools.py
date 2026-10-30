"""FR-04 — tools de CRM: calendário de dias úteis, identidade do servidor, confirmação e acesso cruzado."""
from datetime import date, datetime

import pytest

from app import tools
from app.tools import TOOLS, confirm_action, holidays, next_business_day, run_tool, tool_specs

ME, OTHER = "cliente1988", "cliente2020"


def freeze_today(monkeypatch, d: date):
    monkeypatch.setattr(tools, "_now", lambda: datetime(d.year, d.month, d.day, 10, 0, tzinfo=tools.BRT))


# --- dias úteis ---------------------------------------------------------------------------------
def test_holidays_2026_match_the_national_calendar():
    h = holidays(2026)
    assert h[date(2026, 4, 3)] == "Sexta-feira Santa"  # Páscoa 2026 = 5/abr
    assert date(2026, 2, 16) in h and date(2026, 2, 17) in h  # Carnaval
    assert h[date(2026, 6, 4)] == "Corpus Christi"
    for d in [(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)]:
        assert date(2026, *d) in h


@pytest.mark.parametrize("sale, deposit", [
    (date(2026, 10, 28), date(2026, 10, 29)),  # quarta → quinta
    (date(2026, 10, 30), date(2026, 11, 3)),   # sexta → sáb, dom e Finados (seg 2/nov) → terça
    (date(2026, 10, 31), date(2026, 11, 3)),   # sábado → terça (Finados)
    (date(2026, 4, 2), date(2026, 4, 6)),      # quinta → Sexta-feira Santa + fim de semana → segunda
    (date(2026, 4, 20), date(2026, 4, 22)),    # segunda → Tiradentes (terça) → quarta
    (date(2026, 12, 24), date(2026, 12, 28)),  # quinta → Natal (sexta) + fim de semana → segunda
    (date(2026, 12, 31), date(2027, 1, 4)),    # vira o ano: 1/jan (sexta) + fim de semana → segunda
])
def test_next_business_day_skips_weekends_and_holidays(sale, deposit):
    assert next_business_day(sale)[0] == deposit


def test_next_business_day_reports_why_days_were_skipped():
    _, skipped = next_business_day(date(2026, 4, 2))
    assert [s["reason"] for s in skipped] == ["Sexta-feira Santa", "sábado", "domingo"]


def test_forecast_uses_yesterday_in_brasilia_time(monkeypatch):
    freeze_today(monkeypatch, date(2026, 4, 3))  # hoje é feriado; ontem (qui 2/abr) → depósito segunda 6/abr
    result, call = run_tool("get_settlement_forecast", {}, ME)
    data = result["data"]
    assert (data["sale_date"], data["deposit_date"]) == ("2026-04-02", "2026-04-06")
    assert data["amount_brl"] == 1840.50 and call.ok
    assert [s["reason"] for s in data["skipped_days"]][0] == "Sexta-feira Santa"


def test_forecast_on_monday_looks_at_sunday(monkeypatch):
    freeze_today(monkeypatch, date(2026, 10, 26))  # segunda: ontem = domingo → depósito na própria segunda (dia útil)
    data = run_tool("get_settlement_forecast", {}, ME)[0]["data"]
    assert (data["sale_date"], data["deposit_date"]) == ("2026-10-25", "2026-10-26")


# --- leitura: dados do cenário ------------------------------------------------------------------
def test_declined_transactions_carry_real_codes_with_pt_and_en_explanations():
    data = run_tool("get_recent_transactions", {"status": "declined", "limit": 10}, ME)[0]["data"]
    assert {t["code"] for t in data["transactions"]} == {"51", "05", "55"}
    t51 = next(t for t in data["transactions"] if t["code"] == "51")
    assert "insuficiente" in t51["reason_pt"] and "Insufficient" in t51["reason_en"]
    assert t51["reason"] == t51["reason_pt"] and t51["merchant_action_pt"] and t51["merchant_action_en"]
    assert data["declined_count"] == 3 and all(t["status"] == "declined" for t in data["transactions"])


def test_transactions_default_limit_and_bounds():
    assert len(run_tool("get_recent_transactions", {}, ME)[0]["data"]["transactions"]) == 5
    assert run_tool("get_recent_transactions", {"limit": 0}, ME)[0]["ok"] is False
    assert run_tool("get_recent_transactions", {"limit": 999}, ME)[0]["ok"] is False
    assert run_tool("get_recent_transactions", {"limit": "5"}, ME)[0]["ok"] is False
    assert run_tool("get_recent_transactions", {"status": "bogus"}, ME)[0]["ok"] is False


def test_terminal_status_offline_weak_wifi_with_chip_fallback():
    result, _ = run_tool("get_terminal_status", {}, ME)
    t = result["data"]
    assert (t["status"], t["connection"], t["signal"]) == ("offline", "wifi", "weak")
    assert t["chip"]["signal"] == "good" and "Wi-Fi" in t["diagnosis_pt"] and "chip" in t["diagnosis_en"]
    assert "last_seen_minutes_ago" not in t and "last_seen" in t


def test_profile_has_four_read_tools_plus_two_writes():
    assert {n for n, t in TOOLS.items() if not t["write"]} == {
        "get_customer_profile", "get_settlement_forecast", "get_recent_transactions", "get_terminal_status"}
    assert {n for n, t in TOOLS.items() if t["write"]} == {"request_receivables_advance", "restart_terminal"}


# --- identidade e acesso cruzado ----------------------------------------------------------------
@pytest.mark.parametrize("name", ["get_customer_profile", "get_settlement_forecast",
                                  "get_recent_transactions", "get_terminal_status"])
def test_server_user_id_always_wins_over_model_supplied_one(name):
    result, call = run_tool(name, {"user_id": OTHER}, user_id=ME)
    blob = str(result)
    assert result["ok"] and "user_id" not in call.args
    assert "99999" not in blob and "TX-2001" not in blob and "Recife" not in blob


def test_unknown_customer_is_not_found_on_every_tool():
    for name in TOOLS:
        result, call = run_tool(name, {}, user_id="cliente0000")
        assert result == {"ok": False, "error": "not_found"} and call.ok is False, name


def test_non_string_user_id_does_not_raise():
    for bad in (None, 123, "", ["cliente1988"]):
        result, _ = run_tool("get_customer_profile", {}, user_id=bad)
        assert result["error"] == "not_found"


def test_unknown_tool_and_garbage_input_never_raise():
    for name, args in [("rm_rf", {}), (None, {}), (123, None), ("get_customer_profile", "x"),
                       ("get_recent_transactions", {"limit": [1]}), ("get_terminal_status", {"foo": 1})]:
        result, call = run_tool(name, args, user_id=ME)
        assert isinstance(result, dict) and "ok" in result
    result, call = run_tool("rm_rf", {}, ME)
    assert result["error"] == "unknown_tool:rm_rf" and call.ok is False


def test_extra_arguments_are_rejected_not_executed():
    result, call = run_tool("get_terminal_status", {"confirmed": True}, ME)
    assert result["ok"] is False and "unknown_arguments" in result["error"] and call.ok is False
    result, _ = run_tool("restart_terminal", {"confirmed": True}, ME)  # modelo não pode se autoconfirmar
    assert result.get("needs_confirmation") is not True and result["ok"] is False


# --- escrita com confirmação --------------------------------------------------------------------
def test_write_tools_only_propose_until_confirmed():
    result, call = run_tool("restart_terminal", {}, ME, "conv-1")
    assert result["needs_confirmation"] and result["action_id"] and result["ok"] is False and call.ok is True
    assert "status" not in result  # nada foi executado
    done = confirm_action(result["action_id"], ME)
    assert done["ok"] and done["data"]["status"] == "restart_sent"
    assert confirm_action(result["action_id"], ME) == {"ok": False, "error": "already_done"}  # idempotente


def test_advance_preview_then_confirm_computes_fee():
    result, _ = run_tool("request_receivables_advance", {"amount_brl": 1000}, ME)
    assert result["needs_confirmation"] and result["preview"]["fee_brl"] == 24.90
    done = confirm_action(result["action_id"], ME)
    assert done["ok"] and done["data"]["status"] == "advance_simulated" and done["data"]["simulation"] is True
    assert done["data"]["net_brl"] == 975.10 and done["data"]["amount_brl"] == 1000.0


def test_advance_defaults_to_full_available_and_validates_amount():
    full = run_tool("request_receivables_advance", {}, ME)[0]["preview"]
    assert full["amount_brl"] == full["available_brl"] == 8200.0
    for bad, err in [(0, "invalid_amount"), (-5, "invalid_amount"), (8200.01, "amount_exceeds_available")]:
        result, call = run_tool("request_receivables_advance", {"amount_brl": bad}, ME)
        assert result["error"] == err and "action_id" not in result and call.ok is False
    assert run_tool("request_receivables_advance", {"amount_brl": "mil"}, ME)[0]["ok"] is False
    assert run_tool("request_receivables_advance", {"amount_brl": True}, ME)[0]["ok"] is False


def test_advance_for_ineligible_customer_creates_no_action():
    result, _ = run_tool("request_receivables_advance", {}, OTHER)
    assert result == {"ok": False, "error": "not_eligible"}


def test_confirmation_by_another_user_is_denied_and_action_stays_pending():
    result, _ = run_tool("restart_terminal", {}, ME, "conv-1")
    action_id = result["action_id"]
    assert confirm_action(action_id, OTHER) == {"ok": False, "error": "not_found"}
    assert confirm_action(action_id, "") == {"ok": False, "error": "not_found"}
    assert tools.get_store().get_action(action_id)["status"] == "pending"
    assert confirm_action(action_id, ME)["ok"] is True  # o dono ainda consegue


def test_confirm_unknown_action_does_not_raise():
    assert confirm_action("act_inexistente", ME) == {"ok": False, "error": "not_found"}
    assert confirm_action(None, ME)["ok"] is False


def test_write_for_unknown_customer_creates_no_action():
    result, _ = run_tool("restart_terminal", {}, "cliente0000")
    assert result == {"ok": False, "error": "not_found"}


# --- specs no formato Anthropic -----------------------------------------------------------------
def test_tool_specs_are_strict_ready():
    specs = tool_specs()
    assert len(specs) == 6 and len({s["name"] for s in specs}) == 6
    for s in specs:
        assert set(s) == {"name", "description", "input_schema", "strict"} and s["strict"] is True
        schema = s["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert "user_id" not in schema["properties"]  # o modelo nunca vê nem informa a identidade
        assert set(schema["required"]) <= set(schema["properties"])
