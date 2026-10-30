"""Preenche o README com números lidos do eval e do git log — nunca digitados à mão.

Uso: python scripts/fill_readme.py   (depois de `python -m evals.run_evals`)

Substitui o que está entre os marcadores <!-- eval:begin --> / <!-- eval:end --> e
<!-- timeline:begin --> / <!-- timeline:end -->. Idempotente: rodar duas vezes dá o mesmo arquivo.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_evals import RESULTS, to_markdown  # noqa: E402


def timeline() -> str:
    r = subprocess.run(["git", "log", "--reverse", "--format=%aI|%s"], cwd=ROOT, check=False, text=True,
                       capture_output=True, encoding="utf-8")
    rows = [line.split("|", 1) for line in r.stdout.splitlines() if "|" in line]
    if not rows:
        return "_Sem commits ainda._"
    first, last = datetime.fromisoformat(rows[0][0]), datetime.fromisoformat(rows[-1][0])
    lines = ["| hora | min | commit |", "|---|---:|---|"]
    for ts, subject in rows:
        t = datetime.fromisoformat(ts)
        lines.append(f"| {t:%H:%M:%S} | {(t - first).total_seconds() / 60:.0f} | {subject} |")
    lines.append(f"\nDo primeiro ao último commit: **{(last - first).total_seconds() / 60:.0f} min**.")
    return "\n".join(lines)


def replace_block(text: str, tag: str, body: str) -> str:
    pattern = re.compile(rf"(<!-- {tag}:begin -->)(.*?)(<!-- {tag}:end -->)", re.DOTALL)
    if not pattern.search(text):
        sys.exit(f"ERRO: README sem os marcadores <!-- {tag}:begin --> e <!-- {tag}:end -->.")
    return pattern.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", text)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # console cp1252 do Windows quebra acentos
        stream.reconfigure(encoding="utf-8")
    if not RESULTS.exists():
        sys.exit("ERRO: rode `python -m evals.run_evals` antes; evals/results/latest.json não existe.")
    report = json.loads(RESULTS.read_text(encoding="utf-8"))
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    text = replace_block(text, "eval", f"Modo: `{report['mode']}`\n\n" + to_markdown(report))
    text = replace_block(text, "timeline", timeline())
    readme.write_text(text, encoding="utf-8")
    print("OK: README atualizado com eval e linha do tempo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
