"""Ingestão da base de conhecimento: crawler httpx -> texto -> chunks literais -> data/knowledge/chunks.jsonl.

Uso: python -m app.ingest --refresh

Decisões:
- Texto LITERAL da fonte (nada de resumo): o Knowledge agent cita o trecho e a URL, então o chunk
  precisa ser verificável contra a página.
- Chunk de 300-800 caracteres cortado em fronteira de bloco (título/parágrafo/item); blocos curtos
  consecutivos são agrupados, blocos longos são cortados em fim de frase.
- Dedupe por sha256 do texto normalizado (menus/rodapés repetidos entre páginas viram um chunk só).
- Só stdlib + httpx (sem dependência nova). PDFs não são lidos: não há biblioteca de PDF no projeto.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .config import knowledge_dir

UA = {"User-Agent": "Mozilla/5.0 (compatible; GetnetKB/1.0; +desafio-tecnico)"}
MIN_CHARS, MAX_CHARS = 300, 800

EN_SITEMAP = "https://www.getnet.net/en/sitemap.xml"
PT_SITEMAP = "https://www.getnet.net/pt/sitemap.xml"
BR_ROOT = "https://site.getnet.com.br"
BR_PAGES = [f"{BR_ROOT}/{p}" for p in (
    "", "todas-as-maquininhas/", "pix/", "link-de-pagamento/", "get-tap/", "getcheckout/", "tef/solucao-tef/",
    "pagamentos-pelo-whatsapp/", "proteja-seu-negocio-e-venda-com-seguranca/", "get-ajuda/", "fale-conosco/",
    "ouvidoria/", "eye/", "como-consultar-cnpj/", "ia-para-pequenos-empreendedores/",
    "ideias-de-negocios-lucrativos/", "taxas-personalizadas/")]
# /pt/ do getnet.net é Portugal (pt-PT): útil para FAQ de Get Classic/Get Smart; ignoramos páginas legais/blog.
PT_SKIP = ("/legal/", "/contacto")

_BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "tr", "td", "th", "dt", "dd",
          "br", "article", "summary", "blockquote", "ul", "ol", "table", "details"}
_SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "button", "select"}


# Casca de interface do site BR (login, CTA, horário de atendimento): não é conhecimento do produto.
_UI = re.compile(r"^(J[áa] é cliente\?|Nos informe CPF|Para melhor direcionamento|VISITE O SITE|Minha Conta|"
                 r"Get Ajuda|Fale Conosco|Fale no WhatsApp|Capitais e Regi|Continuar no site|QUERO CONTRATAR|"
                 r"ESCOLHA SUA MAQUININHA|Como voc[êe] prefere continuar|Selecione para ver as taxas)", re.I)


class _Text(HTMLParser):
    """HTML -> lista de blocos de texto, ignorando scripts/nav/rodapé."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def _flush(self) -> None:
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if t:
            self.blocks.append(t)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.buf.append(data)


def html_to_blocks(html: str) -> tuple[str, list[str]]:
    p = _Text()
    p.feed(html)
    p._flush()
    title = re.sub(r"\s+", " ", p.title).strip()
    blocks = [b for b in p.blocks if len(b) > 2 and not re.fullmatch(r"[\W\d_]+", b) and not _UI.search(b)]
    half = len(title) // 2
    if title[:half].strip() and title[:half].strip() == title[half:].strip():  # <title> duplicado pelo CMS
        title = title[:half].strip()
    return title, blocks


def _split_long(block: str) -> list[str]:
    """Corta bloco > MAX_CHARS em fim de frase."""
    if len(block) <= MAX_CHARS:
        return [block]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", block):
        while len(sent) > MAX_CHARS:  # frase sem pontuação: corta em espaço
            cut = sent.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MIN_CHARS else MAX_CHARS
            parts.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if cur and len(cur) + 1 + len(sent) > MAX_CHARS:
            parts.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        parts.append(cur)
    return parts


def chunk_blocks(blocks: list[str]) -> list[str]:
    """Agrupa blocos consecutivos até 300-800 chars; mantém a ordem e o texto literal (blocos unidos por quebra)."""
    pieces = [p for b in blocks for p in _split_long(b)]
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > MAX_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
        if len(cur) >= MIN_CHARS and len(cur) + 1 >= MAX_CHARS - 150:
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    # rabo curto: gruda no anterior se couber, senão descarta (ruído de menu)
    if len(chunks) > 1 and len(chunks[-1]) < MIN_CHARS and len(chunks[-2]) + 1 + len(chunks[-1]) <= MAX_CHARS:
        chunks[-2] += "\n" + chunks.pop()
    return [c for c in chunks if len(c) >= MIN_CHARS]


def sha(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().lower().encode()).hexdigest()


def slug_of(url: str) -> str:
    s = re.sub(r"^https?://(www\.)?", "", url).strip("/")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:70] or "home"


def lang_of(url: str) -> str:
    return "en" if "/en" in url.split("getnet.net", 1)[-1][:4] else "pt"


def sitemap_urls(client: httpx.Client, url: str, limit_blog: int) -> list[str]:
    xml = client.get(url).text
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    out, blogs = [], 0
    for u in urls:
        if any(s in u for s in PT_SKIP) or "/legal/" in u:
            continue
        if "/blog/" in u:
            blogs += 1
            if blogs > limit_blog:
                continue
        out.append(u)
    return out


def crawl(urls: list[str], client: httpx.Client, today: str) -> tuple[list[dict], list[tuple[str, str]]]:
    rows, seen, failed = [], set(), []
    for url in urls:
        try:
            r = client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            failed.append((url, str(e)[:80]))
            continue
        title, blocks = html_to_blocks(r.text)
        lang = "en" if "/en" in url and "getnet.net" in url else "pt"
        for i, text in enumerate(chunk_blocks(blocks)):
            h = sha(text)
            if h in seen:
                continue
            seen.add(h)
            rows.append({"id": f"{slug_of(url)}#{i}", "title": title or slug_of(url), "url": url, "lang": lang,
                         "text": text, "fetched_at": today, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                         "_dedupe": h})
    return rows, failed


def run(refresh: bool, limit_blog: int = 12, out: Path | None = None) -> int:
    out = out or knowledge_dir() / "chunks.jsonl"
    if out.exists() and not refresh and "seed-" not in out.read_text(encoding="utf-8")[:200]:
        print(f"{out} já existe; use --refresh para recoletar")
        return 0
    today = date.today().isoformat()
    with httpx.Client(headers=UA, timeout=25, follow_redirects=True) as client:
        urls = ["https://www.getnet.net/en/"] + sitemap_urls(client, EN_SITEMAP, limit_blog)
        urls += sitemap_urls(client, PT_SITEMAP, 0) + BR_PAGES
        urls = list(dict.fromkeys(urls))
        rows, failed = crawl(urls, client, today)
    # dedupe global entre páginas já feito por _dedupe (sha do texto normalizado)
    seen, final = set(), []
    for r in rows:
        if r["_dedupe"] in seen:
            continue
        seen.add(r.pop("_dedupe"))
        final.append(r)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in final), encoding="utf-8")
    pages = len({r["url"] for r in final})
    print(f"{len(final)} chunks de {pages} páginas ({len(urls)} URLs tentadas, {len(failed)} falhas) -> {out}")
    for u, e in failed:
        print("  FALHA", u, e)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Coleta getnet.net e gera data/knowledge/chunks.jsonl")
    ap.add_argument("--refresh", action="store_true", help="recoleta e sobrescreve chunks.jsonl")
    ap.add_argument("--limit-blog", type=int, default=12, help="máx. de posts de blog em /en (default 12)")
    a = ap.parse_args(argv)
    return run(a.refresh, a.limit_blog)


if __name__ == "__main__":
    sys.exit(main())
