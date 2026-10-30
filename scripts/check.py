"""Check único do projeto: lint + testes + gate do eval [+ build e smoke do container].

Uso:
  python scripts/check.py            # rápido, a cada merge de fork (~10 s)
  python scripts/check.py --docker   # completo: imagem, /health com dados, eval via HTTP

Funciona sem `make` (Windows). Sai com código 1 se qualquer etapa falhar e imprime o resumo.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
IMAGE = "app-check"
PORT = 8765  # porta fora do padrão 8000 para não colidir com um `docker compose up` aberto
# Console do Windows é cp1252: sem isto, subprocesso que imprime acento/seta quebra com UnicodeEncodeError.
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, check=False, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=ENV, **kw)


def step(name: str, cmd: list[str]) -> tuple[str, bool, float, str]:
    t0 = time.perf_counter()
    r = sh(cmd)
    out = (r.stdout + r.stderr).strip()
    return name, r.returncode == 0, time.perf_counter() - t0, out


def wait_health(url: str, timeout_s: float = 60) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read())
        except Exception:
            time.sleep(1)
    return None


def docker_steps() -> list[tuple[str, bool, float, str]]:
    results = [step("docker build", ["docker", "build", "-q", "-t", IMAGE, "."])]
    if not results[-1][1]:
        return results
    sh(["docker", "rm", "-f", IMAGE])
    run = step("docker run", ["docker", "run", "-d", "--name", IMAGE, "-p", f"{PORT}:8000", IMAGE])
    results.append(run)
    if not run[1]:
        return results
    try:
        t0 = time.perf_counter()
        health = wait_health(f"http://localhost:{PORT}/health")
        ok = bool(health) and health.get("data_files", 0) > 0
        logs = "" if health else sh(["docker", "logs", IMAGE]).stdout[-2000:]
        detail = json.dumps(health) if health else f"sem resposta em 60 s: {logs}"
        results.append(("container /health com dados", ok, time.perf_counter() - t0, detail))
        if ok:
            results.append(step("eval via HTTP", [PY, "-m", "evals.run_evals", "--url", f"http://localhost:{PORT}"]))
    finally:
        sh(["docker", "rm", "-f", IMAGE])
    return results


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # console cp1252 do Windows quebra acentos
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--docker", action="store_true", help="inclui build e smoke do container")
    args = ap.parse_args()

    results = [
        step("ruff", [PY, "-m", "ruff", "check", "."]),
        step("pytest", [PY, "-m", "pytest", "-q"]),
        step("eval gate", [PY, "-m", "evals.run_evals"]),
    ]
    if args.docker:
        results += docker_steps()

    failed = [r for r in results if not r[1]]
    for name, _ok, _, out in failed:
        print(f"\n=== {name} FALHOU ===\n{out[-3000:]}")
    print("\n| etapa | resultado | s |\n|---|---|---:|")
    for name, ok, secs, _ in results:
        print(f"| {name} | {'PASS' if ok else 'FAIL'} | {secs:.1f} |")
    print(f"\nCHECK {'VERDE' if not failed else 'VERMELHO'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
