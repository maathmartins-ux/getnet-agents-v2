"""Ingestão: html->texto, chunking 300-800 literal, dedupe e contrato de data/knowledge/chunks.jsonl."""
import json
import re

from app.config import knowledge_dir
from app.ingest import MAX_CHARS, MIN_CHARS, chunk_blocks, html_to_blocks, sha

HTML = ("<html><head><title>Get Smart</title><script>var x=1</script></head><body><nav>menu</nav>"
        "<h1>Get Smart</h1><p>" + "Maquininha com tela touch e aplicativos. " * 12 + "</p>"
        "<p>" + "Recebimento rápido das vendas no débito e crédito. " * 10 + "</p>"
        "<footer>rodapé</footer></body></html>")


def test_html_to_blocks_drops_script_nav_footer():
    title, blocks = html_to_blocks(HTML)
    joined = " ".join(blocks)
    assert title == "Get Smart"
    assert "var x" not in joined and "menu" not in joined and "rodapé" not in joined
    assert "Maquininha com tela touch" in joined


def test_chunks_are_literal_and_bounded():
    _, blocks = html_to_blocks(HTML)
    source = " ".join(blocks)
    chunks = chunk_blocks(blocks)
    assert chunks
    for c in chunks:
        assert MIN_CHARS <= len(c) <= MAX_CHARS
        assert re.sub(r"\s+", " ", c) in source  # trecho literal, não resumo


def test_sha_ignores_whitespace_and_case():
    assert sha("A  b\nc") == sha("a b c")


def test_committed_knowledge_contract():
    text = (knowledge_dir() / "chunks.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    assert len(rows) >= 80
    assert len({r["id"] for r in rows}) == len(rows)
    for r in rows:
        assert {"id", "title", "url", "lang", "text", "fetched_at", "sha256"} <= r.keys()
        assert r["lang"] in {"pt", "en"} and r["url"].startswith("https://")
        assert MIN_CHARS <= len(r["text"]) <= MAX_CHARS
