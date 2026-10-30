"""Supervisor (tabela e LLM), agentes, modo anthropic com cliente falso, custo e juiz."""
import json
from types import SimpleNamespace as NS

from app import agents, guardrails, router, supervisor
from app.agents import AgentResult, Context
from app.llm import AnthropicLLM, LLMReply
from app.observability import new_trace_id, record_llm_usage, request_usage
from app.rag import recall_at_k, reciprocal_rank
from app.tools import run_tool


def ctx(text, intent="product_question", user="cliente1988", conv="c1"):
    return Context(text=text, language="pt", user_id=user, conversation_id=conv, intent=intent)


# --- supervisor por tabela ------------------------------------------------------------------
def test_table_replans_when_no_source():
    out = supervisor.run(["knowledge"], ctx("Qual a capital da Mongólia no ano 1300?"))
    assert out.route == ["knowledge", "escalation"]
    assert out.replans == ["knowledge:no_source->escalation"]
    assert out.results[-1].handoff.reason == "knowledge_no_source"


def test_table_support_error_falls_back_to_knowledge(monkeypatch):
    monkeypatch.setitem(supervisor.AGENTS, "support", lambda c: AgentResult("support", "error", reason="tool_error"))
    out = supervisor.run(["support"], ctx("maquininha sem internet", "technical_support"))
    assert out.route[:2] == ["support", "knowledge"] and out.replans[0] == "support:tool_error->knowledge"


def test_table_general_info_unavailable_does_not_escalate():
    out = supervisor.run(["knowledge"], ctx("Qual a previsão do tempo em Porto Alegre?", "general_info"))
    assert out.route == ["knowledge"] and not any(r.handoff for r in out.results)


def test_low_confidence_adds_escalation():
    out = supervisor.run(["knowledge"], ctx("Como funciona o Pix?"), confidence=0.3)
    assert out.route[-1] == "escalation" and out.replans[0].startswith("router:low_confidence")


def test_supervisor_respects_max_steps(monkeypatch):
    calls = []

    def always_insufficient(c):
        calls.append(1)
        return AgentResult("knowledge", "insufficient", reason="x")

    for name in ("knowledge", "support", "escalation"):
        monkeypatch.setitem(supervisor.AGENTS, name, always_insufficient)
    out = supervisor.run(["support", "knowledge"], ctx("x"))
    assert len(out.route) <= supervisor.MAX_STEPS and len(calls) <= supervisor.MAX_STEPS


def test_support_facts_reach_knowledge_query():
    c = ctx("Minha maquininha não conecta na internet", "technical_support")
    out = supervisor.run(["support", "knowledge"], c)
    assert out.route == ["support", "knowledge"] and c.facts["support"]["terminal"]["status"] == "offline"
    assert c.facts["query_hints"]


# --- modo anthropic com cliente falso (sem rede, sem chave) ---------------------------------
class FakeMessages:
    """Roteiro de respostas da API: cada create() devolve o próximo item e guarda os kwargs enviados."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return self.script.pop(0)


def resp(stop, *blocks, i=100, o=20):
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=i, output_tokens=o))


def text(t):
    return NS(type="text", text=t)


def tool_use(name, inp, bid="tu1"):
    return NS(type="tool_use", name=name, input=inp, id=bid)


def fake_llm(script):
    msgs = FakeMessages(script)
    return AnthropicLLM("claude-sonnet-5-5", client=NS(messages=msgs)), msgs


def test_anthropic_request_shape_has_effort_and_no_sampling():
    llm, msgs = fake_llm([resp("end_turn", text('{"category": "safe"}'))])
    llm.complete("sys", [{"role": "user", "content": "oi"}], schema=guardrails.SCHEMA)
    sent = msgs.sent[0]
    assert sent["output_config"]["effort"] == "medium" and sent["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k"} & sent.keys()


def test_llm_supervisor_replans_from_knowledge_to_escalation(monkeypatch):
    new_trace_id()
    llm, msgs = fake_llm([
        resp("tool_use", tool_use("knowledge_agent", {"query": "MDR farmácia 2031"})),
        resp("tool_use", tool_use("escalation_agent", {"reason": "sem fonte"}, "tu2")),
        resp("end_turn", text("Encaminhei para um especialista.")),
    ])
    monkeypatch.setattr(supervisor, "get_llm", lambda m=None: llm)
    monkeypatch.setitem(supervisor.AGENTS, "knowledge",
                        lambda c: AgentResult("knowledge", "insufficient", reason="no_source"))
    out = supervisor.run(["knowledge"], ctx("Qual o MDR exato para farmácias em 2031?"))
    assert out.engine == "llm" and out.route == ["knowledge", "escalation"]
    assert out.replans == ["knowledge->escalation (plano: None)"] and out.answer == "Encaminhei para um especialista."
    assert msgs.sent[1]["messages"][-1]["content"][0]["type"] == "tool_result"  # resultado volta ao modelo
    assert msgs.sent[0]["tool_choice"] == {"type": "auto"}  # nunca forçado (400 no Sonnet 5.5)
    assert request_usage()["cost_usd"] > 0


def test_llm_supervisor_blocks_disallowed_transition(monkeypatch):
    llm, _ = fake_llm([
        resp("tool_use", tool_use("escalation_agent", {"reason": "x"})),
        resp("tool_use", tool_use("support_agent", {"request": "y"}, "tu2")),
        resp("end_turn", text("ok")),
    ])
    monkeypatch.setattr(supervisor, "get_llm", lambda m=None: llm)
    out = supervisor.run(["escalation"], ctx("atendente"))
    assert out.route == ["escalation"]  # escalation é terminal


def test_llm_failure_falls_back_to_table(monkeypatch):
    from app.llm import LLMError

    class Broken:
        enabled, model = True, "x"

        def create(self, *a, **k):
            raise LLMError("api_status_500")

    monkeypatch.setattr(supervisor, "get_llm", lambda m=None: Broken())
    out = supervisor.run(["knowledge"], ctx("Como funciona o link de pagamento?"))
    assert out.engine == "table" and out.route == ["knowledge"]


def test_support_llm_tool_loop_enforces_server_identity(monkeypatch):
    llm, msgs = fake_llm([
        resp("tool_use", tool_use("get_settlement_forecast", {"user_id": "cliente2020"})),
        resp("end_turn", text("Cai amanhã.")),
    ])
    monkeypatch.setattr(agents, "get_llm", lambda m=None: llm)
    r = agents.support_agent(ctx("quando cai meu dinheiro?", "account_data"))
    assert r.status == "ok" and r.tool_calls[0].args == {}  # user_id do modelo descartado
    result = json.loads(msgs.sent[1]["messages"][-1]["content"][0]["content"])
    assert result["ok"] and "99999" not in json.dumps(result)


def test_router_llm_invalid_json_falls_back(monkeypatch):
    class Bad:
        enabled = True

        def complete(self, *a, **k):
            return LLMReply("não é json", "x")

    monkeypatch.setattr(router, "get_llm", lambda m=None: Bad())
    assert router.route("Quero falar com um atendente", []).intent == "human_request"


# --- tools, retrieval, custo, juiz ----------------------------------------------------------
def test_tool_identity_is_server_side():
    result, call = run_tool("get_customer_profile", {"user_id": "cliente2020"}, user_id="cliente1988")
    assert result["data"]["city"] == "Porto Alegre"


def test_unknown_tool_never_raises():
    result, call = run_tool("rm_rf", {}, user_id="cliente1988")
    assert result["ok"] is False and call.ok is False


def test_retrieval_metrics():
    assert recall_at_k(["a", "b"], ["b"], 3) == 1.0
    assert reciprocal_rank(["a", "b"], ["b"]) == 0.5 and reciprocal_rank(["a"], ["b"]) == 0.0


def test_cost_per_request_from_price_table():
    new_trace_id()
    cost = record_llm_usage("claude-sonnet-5-5", 1_000_000, 100_000)
    assert cost == 2.0 + 1.0  # US$2/MTok entrada + US$10/MTok saída × 0,1
    assert request_usage() == {"input_tokens": 1_000_000, "output_tokens": 100_000, "cost_usd": 3.0}
    assert record_llm_usage("modelo-sem-preco", 10, 10) == 0.0


def test_judge_with_fake_llm(tmp_path, monkeypatch):
    from evals import judge

    class FakeLLM:
        model = "fake"

        def complete(self, system, messages, max_tokens=1024, schema=None):
            return LLMReply(json.dumps({"grounded": True, "helpful": True, "score": 4, "reason": "ok"}), "fake")

    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"rows": [{"id": "x", "message": "Pix?", "answer": "Sim.", "source_ids": []}]}),
                      encoding="utf-8")
    calib = tmp_path / "calib.jsonl"
    calib.write_text(json.dumps({"question": "q", "answer": "a", "source_ids": [], "human_grounded": True}) + "\n",
                     encoding="utf-8")
    monkeypatch.setattr(judge, "LATEST", latest)
    monkeypatch.setattr(judge, "CALIBRATION", calib)
    report = judge.run(FakeLLM())
    assert report["grounded_rate"] == 1.0 and report["human_agreement"] == 1.0
