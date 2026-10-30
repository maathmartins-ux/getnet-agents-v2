"""Estado persistido em SQLite: histórico por conversa, fila de handoff e ações pendentes.

Sobrevive a restart do processo. Para várias réplicas, troque por Postgres atrás desta mesma
interface (é o único módulo que conhece SQL).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, intent TEXT, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, reason TEXT NOT NULL,
  summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', answer TEXT,
  created_at REAL NOT NULL, answered_at REAL);
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY, conversation_id TEXT, user_id TEXT NOT NULL, tool TEXT NOT NULL,
  args TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result TEXT, created_at REAL NOT NULL);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- conversa ---------------------------------------------------------------------
    def add_message(self, conversation_id: str, user_id: str, role: str, content: str,
                    intent: str | None = None) -> None:
        with self._db() as conn:
            conn.execute("INSERT INTO messages (conversation_id, user_id, role, content, intent, ts) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (conversation_id, user_id, role, content, intent, time.time()))

    def history(self, conversation_id: str, limit: int = 20) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute("SELECT role, content, intent, ts FROM messages WHERE conversation_id = ? "
                                "ORDER BY id DESC LIMIT ?", (conversation_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def conversation_owner(self, conversation_id: str) -> str | None:
        with self._db() as conn:
            row = conn.execute("SELECT user_id FROM messages WHERE conversation_id = ? ORDER BY id LIMIT 1",
                               (conversation_id,)).fetchone()
        return row["user_id"] if row else None

    # --- handoff ----------------------------------------------------------------------
    def open_ticket(self, conversation_id: str, user_id: str, reason: str, summary: str) -> dict:
        existing = self.ticket_for(conversation_id, status="open")
        if existing:  # idempotente: a mesma conversa não abre dois casos
            return existing
        tid = "T-" + hashlib.sha1(f"{conversation_id}|{reason}|{time.time()}".encode()).hexdigest()[:8]
        with self._db() as conn:
            conn.execute("INSERT INTO tickets (id, conversation_id, user_id, reason, summary, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (tid, conversation_id, user_id, reason, summary, time.time()))
        return self.get_ticket(tid)

    def get_ticket(self, ticket_id: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def ticket_for(self, conversation_id: str, status: str = "open") -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE conversation_id = ? AND status = ? "
                               "ORDER BY created_at DESC LIMIT 1", (conversation_id, status)).fetchone()
        return dict(row) if row else None

    def list_tickets(self, status: str = "open") -> list[dict]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM tickets WHERE status = ? ORDER BY created_at", (status,)).fetchall()
        return [dict(r) for r in rows]

    def answer_ticket(self, ticket_id: str, answer: str) -> dict | None:
        ticket = self.get_ticket(ticket_id)
        if not ticket or ticket["status"] != "open":
            return None
        with self._db() as conn:
            conn.execute("UPDATE tickets SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
                         (answer, time.time(), ticket_id))
        # A resposta humana entra no histórico: o próximo turno do cliente já a enxerga (retomada).
        self.add_message(ticket["conversation_id"], ticket["user_id"], "human_agent", answer)
        return self.get_ticket(ticket_id)

    # --- ações que exigem confirmação -------------------------------------------------
    def create_action(self, conversation_id: str | None, user_id: str, tool: str, args: dict) -> str:
        aid = "A-" + uuid.uuid4().hex[:8]
        with self._db() as conn:
            conn.execute("INSERT INTO actions (id, conversation_id, user_id, tool, args, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (aid, conversation_id, user_id, tool,
                                                       json.dumps(args, ensure_ascii=False), time.time()))
        return aid

    def get_action(self, action_id: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not row:
            return None
        action = dict(row)
        action["args"] = json.loads(action["args"])
        return action

    def finish_action(self, action_id: str, status: str, result: dict) -> None:
        with self._db() as conn:
            conn.execute("UPDATE actions SET status = ?, result = ? WHERE id = ?",
                         (status, json.dumps(result, ensure_ascii=False), action_id))


@lru_cache(maxsize=8)
def _store_for(path: str) -> Store:
    return Store(Path(path))


def get_store() -> Store:
    # Cache por caminho: testes trocam DB_PATH e recebem um banco novo sem reiniciar o processo.
    return _store_for(str(db_path()))
