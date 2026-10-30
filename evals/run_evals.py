"""Runner de avaliação: mede o sistema contra evals/cases.jsonl e aplica o gate do CI.

Uso:
  python -m evals.run_evals                               # em processo (offline, determinístico)
  python -m evals.run_evals --url http://localhost:8000   # contra a API rodando (smoke, com token)

Campos de `expect`: intent, handoff, blocked, language, tools (subconjunto chamado),
pending_action, replanned, not_contains, doc_ids (relevantes → Recall@3 e MRR).
`before`: mensagens enviadas antes, na mesma conversa (casos de memória).

Grava evals/results/latest.json — a fonte única dos números do README (scripts/fill_readme.py).
Sai com código 1 se a acurácia de intenção ficar abaixo de EVAL_GATE ou se houver crash.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Importa o src/ DESTA pasta, não o pacote instalado: num worktree de fork o `pip install -e`
# aponta para a pasta principal e o eval mediria o código errado.
sys.path.insert(0, str(HERE.parent / "src"))
CASES = HERE / "cases.jsonl"
HELDOUT = HERE / "heldout_blind.jsonl"  # cego: commitado com a spec, antes de qualquer ajuste
# Splits medidos mas fora do gate: gatear o cego ou os difíceis convidaria a ajustar o sistema a eles.
UNGATED = {"heldout", "hard"}
RESULTS = HERE / "results" / "latest.json"
# 0.9 é o piso usual de take-home; ajuste por desafio, nunca para "fazer passar".
GATE = float(os.getenv("EVAL_GATE", "0.9"))
K = 3  # Recall@3: o agente usa as 3 primeiras passagens


def load_cases(paths: tuple[Path, ...] = (CASES, HELDOUT)) -> list[dict]:
    cases = [json.loads(line) for path in paths if path.exists()
             for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    path = paths[0]
    ids = [c["id"] for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"ids duplicados em {path.name}: {sorted(dup)}")
    for c in cases:
        missing = {"id", "split", "message", "expect"} - c.keys()
        if missing:
            raise ValueError(f"caso {c.get('id', '?')} sem campos {sorted(missing)}")
    return cases


class Client:
    def __init__(self, url: str | None) -> None:
        self.url = url.rstrip("/") if url else None

    def send(self, message: str, user_id: str, conversation_id: str | None) -> dict:
        from app.auth import issue_token
        from app.schemas import ChatRequest

        if self.url:
            import httpx
            r = httpx.post(f"{self.url}/chat", json={"message": message, "user_id": user_id,
                                                     "conversation_id": conversation_id},
                           headers={"Authorization": f"Bearer {issue_token(user_id)}"}, timeout=60)
            r.raise_for_status()
            return r.json()
        from app.pipeline import handle
        return handle(ChatRequest(message=message, conversation_id=conversation_id), user_id, True).model_dump()


def score(case: dict, got: dict) -> dict[str, bool]:
    exp = case["expect"]
    checks = {"intent": got.get("intent") == exp["intent"]}
    if "handoff" in exp:
        checks["handoff"] = (got.get("handoff") or {}).get("required", False) == exp["handoff"]
    for key in ("blocked", "language"):
        if key in exp:
            checks[key] = got.get(key) == exp[key]
    if "tools" in exp:
        called = {c["name"] for c in got.get("tool_calls", [])}
        checks["tools"] = set(exp["tools"]) <= called
    if "pending_action" in exp:
        checks["pending_action"] = bool(got.get("pending_action")) == exp["pending_action"]
    if "replanned" in exp:
        checks["replanned"] = bool(got.get("replans")) == exp["replanned"]
    if "not_contains" in exp:
        blob = json.dumps(got, ensure_ascii=False)
        checks["no_leak"] = not any(s in blob for s in exp["not_contains"])
    return checks


def run(url: str | None = None) -> dict:
    from app.rag import recall_at_k, reciprocal_rank

    if not url:  # banco descartável: o eval não herda conversas de execuções anteriores
        os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="eval-")) / "eval.db")
    client, rows = Client(url), []
    for case in load_cases():
        t0 = time.perf_counter()
        try:
            conv, user = None, case.get("user_id", "cliente1988")
            for prior in case.get("before", []):
                conv = client.send(prior, user, conv)["conversation_id"]
            got, crash = client.send(case["message"], user, conv), None
        except Exception as exc:  # crash vira métrica, não derruba o relatório
            got, crash = {}, f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - t0) * 1000
        checks = score(case, got) if not crash else {"intent": False}
        row = {"id": case["id"], "split": case["split"], "message": case["message"], "ms": round(ms, 1),
               "checks": checks, "crash": crash, "got_intent": got.get("intent"),
               "answer": got.get("answer", ""), "source_ids": [s["id"] for s in got.get("sources", [])],
               "cost_usd": (got.get("usage") or {}).get("cost_usd", 0)}
        relevant = case["expect"].get("doc_ids")
        if relevant:
            row["recall_at_k"] = recall_at_k(row["source_ids"], relevant, K)
            row["rr"] = reciprocal_rank(row["source_ids"], relevant)
        rows.append(row)

    by_split: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_split[r["split"]].append(r)
        by_split["ALL"].append(r)

    def rate(items: list[dict], key: str) -> float | None:
        vals = [r["checks"][key] for r in items if key in r["checks"]]
        return round(100 * sum(vals) / len(vals), 1) if vals else None

    def mean(items: list[dict], key: str) -> float | None:
        vals = [r[key] for r in items if key in r]
        return round(sum(vals) / len(vals), 3) if vals else None

    metrics = {}
    for split, items in by_split.items():
        lat = sorted(r["ms"] for r in items)
        metrics[split] = {
            "cases": len(items),
            **{f"{k}_acc": rate(items, k) for k in ("intent", "handoff", "blocked", "language", "tools",
                                                     "pending_action", "replanned", "no_leak")},
            f"recall@{K}": mean(items, "recall_at_k"),
            "mrr": mean(items, "rr"),
            "p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))],
            "cost_usd": round(sum(r["cost_usd"] for r in items), 6),
            "crashes": sum(1 for r in items if r["crash"]),
        }
    gated = [r for r in rows if r["split"] not in UNGATED]
    overall = rate(gated, "intent") / 100 if gated else 0.0
    report = {"mode": "http" if url else "in-process", "gate": GATE, "intent_acc": overall,
              "gated_splits": sorted({r["split"] for r in gated}), "ungated_splits": sorted(UNGATED),
              "passed": overall >= GATE and metrics["ALL"]["crashes"] == 0, "metrics": metrics, "rows": rows,
              "failures": [r for r in rows if r["crash"] or not all(r["checks"].values())]}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def to_markdown(report: dict) -> str:
    splits = [s for s in report["metrics"] if s != "ALL"] + ["ALL"]
    keys = list(report["metrics"]["ALL"].keys())
    out = ["| Métrica | " + " | ".join(splits) + " |", "|---|" + "---:|" * len(splits)]
    for k in keys:
        cells = ["—" if report["metrics"][s].get(k) is None else str(report["metrics"][s][k]) for s in splits]
        out.append(f"| {k} | " + " | ".join(cells) + " |")
    verdict = "PASS" if report["passed"] else "FAIL"
    out.append(f"\nGate: intent {report['intent_acc']:.1%} (mínimo {report['gate']:.0%}) → **{verdict}**")
    return "\n".join(out)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # console cp1252 do Windows quebra acentos
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="avaliar a API rodando em vez do pipeline em processo")
    report = run(ap.parse_args().url)
    print(to_markdown(report))
    for f in report["failures"]:
        print(f"- {f['id']}: got intent={f['got_intent']} checks={f['checks']} crash={f['crash']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
