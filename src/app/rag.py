"""Busca BM25 sobre data/knowledge/*.jsonl ({id, title, url, text, ...}) + métricas de retrieval.

Lexical de propósito: com dezenas de documentos, BM25 é auditável e sem dependência. A troca
por hybrid (BM25 + embeddings) só se justifica se Recall@K/MRR do eval mostrarem perda.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import knowledge_dir

_STOP = set("a o os as um uma de do da dos das e é em no na para por com que como qual quais meu minha "
            "the a an of to in on for is are what how my your can i do does with get getnet".split())
K1, B = 1.5, 0.75  # parâmetros clássicos do BM25 (Robertson); sem tuning sem eval que justifique
TITLE_WEIGHT = 2  # título repetido 2x: termo no título indica o assunto do documento


def stem(t: str) -> str:
    """Radical leve por sufixo: "trocar"/"troca" -> "troc", "entregas" -> "entreg".

    Prefixo fixo (t[:5]) foi descartado: juntava "capital" e "capitais" e o FAQ de entrega
    respondia "capital da Mongólia" (falso positivo pego pelo teste de replanejamento).
    """
    if len(t) > 4 and t.endswith("s"):
        t = t[:-1]
    if len(t) > 5 and t[-2:] in ("ar", "er", "ir"):
        t = t[:-2]
    if len(t) > 4 and t[-1] in "aeo":
        t = t[:-1]
    return t


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in re.findall(r"\w+", text.lower()) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True)
class Doc:
    id: str
    title: str
    url: str
    text: str


class Index:
    def __init__(self, docs: list[Doc]) -> None:
        self.docs = docs
        self.tfs = [Counter(tokenize((d.title + " ") * TITLE_WEIGHT + d.text)) for d in docs]
        self.lengths = [sum(tf.values()) for tf in self.tfs]
        self.avg = sum(self.lengths) / len(docs) if docs else 0
        df = Counter(term for tf in self.tfs for term in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 3) -> list[tuple[Doc, float]]:
        terms = tokenize(query)
        scored = []
        for doc, tf, length in zip(self.docs, self.tfs, self.lengths, strict=True):
            s = sum(self.idf.get(t, 0) * tf[t] * (K1 + 1) / (tf[t] + K1 * (1 - B + B * length / self.avg))
                    for t in terms if t in tf)
            if s > 0:
                scored.append((doc, s))
        return sorted(scored, key=lambda x: -x[1])[:k]


def load_docs(directory: Path) -> list[Doc]:
    docs = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if {"id", "title", "url", "text"} <= row.keys():
                    docs.append(Doc(row["id"], row["title"], row["url"], row["text"]))
    return docs


@lru_cache(maxsize=4)
def _index_for(directory: str, stamp: tuple) -> Index:
    return Index(load_docs(Path(directory)))


def get_index() -> Index:
    d = knowledge_dir()
    stamp = tuple((p.name, p.stat().st_mtime) for p in sorted(d.glob("*.jsonl")))  # recarrega se mudar
    return _index_for(str(d), stamp)


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    return len(set(retrieved[:k]) & set(relevant)) / len(relevant) if relevant else 0.0


def reciprocal_rank(retrieved: list[str], relevant: list[str]) -> float:
    for i, doc_id in enumerate(retrieved, 1):
        if doc_id in relevant:
            return 1 / i
    return 0.0
