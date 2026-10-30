"""LLM-as-judge: nota semântica das respostas do último eval, calibrada contra rótulos humanos.

Uso: python -m evals.judge    (depois de python -m evals.run_evals)

Precisa de LLM_PROVIDER=anthropic e credencial; sem isso grava status "skipped" e sai com 0 —
o juiz é evidência adicional, não gate. Calibração: evals/judge_calibration.jsonl traz respostas
com rótulo humano (grounded true/false); a concordância juiz × humano sai no relatório e
diz quanto confiar no juiz.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
LATEST = HERE / "results" / "latest.json"
OUT = HERE / "results" / "judge.json"
CALIBRATION = HERE / "judge_calibration.jsonl"

SYSTEM = ("Você avalia respostas de um assistente de suporte. Dadas a pergunta, as fontes e a resposta, "
          "julgue: grounded = toda afirmação da resposta está nas fontes (ou a resposta só encaminha para "
          "humano / pede dado / recusa); helpful = a resposta atende à pergunta. score de 1 a 5.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["grounded", "helpful", "score", "reason"],
          "properties": {"grounded": {"type": "boolean"}, "helpful": {"type": "boolean"},
                         "score": {"type": "integer"}, "reason": {"type": "string"}}}


def _sources_text(ids: list[str]) -> str:
    from app.rag import get_index
    docs = {d.id: d for d in get_index().docs}
    return "\n".join(f"- {docs[i].title}: {docs[i].text}" for i in ids if i in docs) or "(nenhuma fonte)"


def judge_one(llm, question: str, answer: str, source_ids: list[str]) -> dict:
    prompt = f"Pergunta: {question}\n\nFontes:\n{_sources_text(source_ids)}\n\nResposta: {answer}"
    return llm.complete(SYSTEM, [{"role": "user", "content": prompt}], max_tokens=1024, schema=SCHEMA).json()


def run(llm) -> dict:
    rows = [r for r in json.loads(LATEST.read_text(encoding="utf-8"))["rows"] if r["answer"]]
    verdicts = [{"id": r["id"], **judge_one(llm, r["message"], r["answer"], r["source_ids"])} for r in rows]
    calib = [json.loads(line) for line in CALIBRATION.read_text(encoding="utf-8").splitlines() if line.strip()]
    agree = [judge_one(llm, c["question"], c["answer"], c["source_ids"])["grounded"] == c["human_grounded"]
             for c in calib]
    n = len(verdicts) or 1
    return {"status": "ran", "model": llm.model, "cases": len(verdicts),
            "grounded_rate": round(sum(v["grounded"] for v in verdicts) / n, 3),
            "helpful_rate": round(sum(v["helpful"] for v in verdicts) / n, 3),
            "mean_score": round(sum(v["score"] for v in verdicts) / n, 2),
            "human_agreement": round(sum(agree) / len(agree), 3) if agree else None,
            "calibration_cases": len(agree), "verdicts": verdicts}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    from app.config import judge_model, llm_provider
    from app.llm import LLMError, get_llm

    if not LATEST.exists():
        sys.exit("ERRO: rode `python -m evals.run_evals` antes.")
    if llm_provider() != "anthropic":
        report = {"status": "skipped", "reason": "LLM_PROVIDER != anthropic (sem juiz no modo offline)"}
    else:
        try:
            report = run(get_llm(judge_model()))
        except LLMError as exc:
            report = {"status": "error", "reason": str(exc)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "verdicts"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
