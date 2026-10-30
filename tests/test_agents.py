import json

from app import supervisor
from app.agents import AgentResult, Context
from app.llm import LLMReply
from app.observability import new_trace_id, record_llm_usage, request_usage
from app.rag import get_index, recall_at_k, reciprocal_rank
from app.tools import run_tool


def ctx(text, user="u1", conv="c1"):
    return Context(text=text, language="pt", user_id=user, conversation_id=conv)


def test_supervisor_replans_when_no_source():
    out = supervisor.run("faq", ctx("Qual a capital da Mongólia?"))
    assert out.route == ["faq", "escalation"]
    assert out.replans == ["faq:no_source->escalation"]
    assert out.results[-1].handoff.reason == "faq_no_source"


def test_supervisor_respects_max_steps(monkeypatch):
    calls = []

    def always_insufficient(c):
        calls.append(1)
        return AgentResult("faq", "insufficient", reason="x")

    monkeypatch.setitem(supervisor.AGENTS, "faq", always_insufficient)
    monkeypatch.setitem(supervisor.AGENTS, "escalation", always_insufficient)
    out = supervisor.run("faq", ctx("x"))
    assert len(out.route) <= supervisor.MAX_STEPS and len(calls) == 2  # não entra em laço


def test_tool_identity_is_server_side():
    result, call = run_tool("lookup_record", {"record_id": "2001", "user_id": "u2"}, user_id="u1")
    assert result == {"ok": False, "error": "not_found"}  # args do modelo não trocam a identidade


def test_unknown_tool_never_raises():
    result, call = run_tool("rm_rf", {}, user_id="u1")
    assert result["ok"] is False and call.ok is False


def test_bm25_ranks_relevant_first_and_metrics():
    ids = [d.id for d, _ in get_index().search("Posso trocar um livro?")]
    assert ids[0] == "faq-troca"
    assert recall_at_k(ids, ["faq-troca"], 3) == 1.0
    assert reciprocal_rank(["a", "faq-troca"], ["faq-troca"]) == 0.5
    assert reciprocal_rank(["a"], ["faq-troca"]) == 0.0


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
            grounded = "24 horas" not in messages[0]["content"] and "30 dias" not in messages[0]["content"]
            return LLMReply(json.dumps({"grounded": grounded, "helpful": True, "score": 4, "reason": "ok"}), "fake")

    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"rows": [{"id": "x", "message": "Prazo?", "answer": "3 a 5 dias úteis.",
                                            "source_ids": ["faq-entrega"]}]}), encoding="utf-8")
    monkeypatch.setattr(judge, "LATEST", latest)
    report = judge.run(FakeLLM())
    assert report["grounded_rate"] == 1.0 and report["human_agreement"] == 1.0
