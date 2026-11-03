"""Background, batched logging of decisions / events / games into Delta via a SQL warehouse.

Rows are buffered in memory and flushed every few seconds as one
``INSERT ... SELECT inline(from_json(:rows, <schema>))`` per table, so values are
always passed as a bound parameter (no SQL string building with user text).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("decision_log")

SCHEMAS = {
    "decisions": (
        "ts TIMESTAMP, game_id STRING, mode STRING, tick INT, snake_id STRING, brain STRING, persona STRING, "
        "order_text STRING, model STRING, state STRING, "
        "candidates ARRAY<STRUCT<id: STRING, description: STRING, probability: DOUBLE>>, "
        "choice STRING, confidence DOUBLE, entropy_nats DOUBLE, outlook_score DOUBLE, danger DOUBLE, "
        "input_tokens INT, latency_ms DOUBLE"
    ),
    "events": "ts TIMESTAMP, game_id STRING, tick INT, snake_id STRING, type STRING, detail STRING",
    "games": (
        "game_id STRING, mode STRING, started_at TIMESTAMP, ended_at TIMESTAMP, ticks INT, winner STRING, "
        "players ARRAY<STRUCT<snake_id: STRING, name: STRING, kind: STRING, persona: STRING, brain: STRING, "
        "score INT, kills INT, deaths INT>>"
    ),
}


def ddl(table_prefix: str) -> list[str]:
    return [f"CREATE TABLE IF NOT EXISTS {table_prefix}.{t} ({cols})" for t, cols in SCHEMAS.items()]


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


class DecisionLogger:
    def __init__(self, table_prefix: str, warehouse_id: str, workspace_client=None,
                 flush_s: float = 4.0, batch: int = 40):
        self.prefix = table_prefix
        self.warehouse_id = warehouse_id
        self._w = workspace_client
        self.flush_s, self.batch = flush_s, batch
        self.q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=20000)
        self.enabled = bool(table_prefix and warehouse_id)
        self.written = {t: 0 for t in SCHEMAS}
        self.last_error: Optional[str] = None
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="decision-log").start()

    @property
    def w(self):
        if self._w is None:
            from databricks.sdk import WorkspaceClient

            self._w = WorkspaceClient()
        return self._w

    # ---------------------------------------------------------------- producers
    def _put(self, table: str, row: dict):
        if not self.enabled:
            return
        try:
            self.q.put_nowait((table, row))
        except queue.Full:
            pass

    def decision(self, game_id: str, ai, dec, mode: str = "human_vs_ai"):
        self._put("decisions", {
            "ts": _iso(dec.ts), "game_id": game_id, "mode": mode, "tick": dec.tick, "snake_id": ai.snake_id,
            "brain": dec.brain, "persona": ai.persona, "order_text": ai.order or None, "model": dec.model,
            "state": json.dumps(dec.state) if dec.state else None, "candidates": dec.candidates,
            "choice": dec.choice, "confidence": dec.confidence, "entropy_nats": dec.entropy_nats,
            "outlook_score": dec.outlook, "danger": dec.danger, "input_tokens": dec.input_tokens,
            "latency_ms": dec.latency_ms,
        })

    def event(self, game_id: str, tick: int, snake_id: Optional[str], type_: str, detail: dict):
        self._put("events", {"ts": _iso(time.time()), "game_id": game_id, "tick": tick, "snake_id": snake_id,
                             "type": type_, "detail": json.dumps(detail) if detail else None})

    def game(self, session):
        s = session.summary()
        self._put("games", {**s, "started_at": _iso(s["started_at"]), "ended_at": _iso(s["ended_at"])})

    def game_row(self, row: dict):
        self._put("games", row)

    # ----------------------------------------------------------------- consumer
    def _worker(self):
        buf: dict[str, list[dict]] = {t: [] for t in SCHEMAS}
        last = time.time()
        while True:
            try:
                table, row = self.q.get(timeout=0.5)
                buf[table].append(row)
            except queue.Empty:
                pass
            if time.time() - last >= self.flush_s or any(len(v) >= self.batch for v in buf.values()):
                for t, rows in buf.items():
                    if rows:
                        self._insert(t, rows)
                        buf[t] = []
                last = time.time()

    def flush_now(self, rows_by_table: dict[str, list[dict]]):
        for t, rows in rows_by_table.items():
            if rows:
                self._insert(t, rows)

    def _insert(self, table: str, rows: list[dict]):
        from databricks.sdk.service.sql import StatementParameterListItem

        stmt = f"INSERT INTO {self.prefix}.{table} SELECT * FROM (SELECT inline(from_json(:rows, '{array_schema(table)}')))"
        try:
            r = self.w.statement_execution.execute_statement(
                warehouse_id=self.warehouse_id, statement=stmt, wait_timeout="50s",
                parameters=[StatementParameterListItem(name="rows", value=json.dumps(rows, default=str))],
            )
            state = r.status.state.value if r.status and r.status.state else "UNKNOWN"
            deadline = time.time() + 120
            while state in ("PENDING", "RUNNING") and time.time() < deadline:
                time.sleep(2)
                r = self.w.statement_execution.get_statement(r.statement_id)
                state = r.status.state.value if r.status and r.status.state else "UNKNOWN"
            if state != "SUCCEEDED":
                raise RuntimeError(f"{state}: {r.status.error.message if r.status and r.status.error else ''}")
            self.written[table] += len(rows)
        except Exception as exc:
            self.last_error = f"{table}: {exc}"
            log.warning("decision log insert failed (%s rows into %s): %s", len(rows), table, exc)


def array_schema(table: str) -> str:
    """'a INT, b STRING' -> 'ARRAY<STRUCT<a: INT, b: STRING>>' (nested types kept as-is)."""
    cols, depth, cur = [], 0, ""
    for ch in SCHEMAS[table]:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cols.append(cur.strip())
    fields = []
    for c in cols:
        name, typ = c.split(" ", 1)
        fields.append(f"{name}: {typ}")
    return f"ARRAY<STRUCT<{', '.join(fields)}>>"
