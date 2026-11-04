"""Selective MLflow tracing of AI decisions.

Tracing every decision (~2/s per snake) would flood the experiment, so we keep the ones worth
inspecting:

* ``sample``        a random fraction of decisions (default 5 %),
* ``uncertain``     decisions where OpenJev hesitated (normalised entropy above a threshold),
* ``before_death``  every decision of the last 2 s before a snake died.

A decision is only emitted once its outcome is known (died within the window, or survived it), so
each trace carries an ``outcome`` tag.

Why a job does the MLflow export: MLflow stores span data of classic experiments in cloud storage
through pre-signed URLs, and Databricks Apps egress may block that storage endpoint. So the app only
records *which* decisions to trace (table ``decision_traces``, the decision itself is already in
``decisions``) and triggers the ``serpents_traces`` job at the end of each match. The job (serverless)
rebuilds each trace with its real timestamps via ``write_trace``.
"""
from __future__ import annotations

import logging
import math
import random
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("tracing")


@dataclass
class _Item:
    dec: object  # serpents.brains.Decision
    meta: dict
    reason: Optional[str]
    emitted: bool = False


@dataclass
class TraceSelector:
    """Pure selection logic (no MLflow): decides which decisions become traces, and when."""

    tps: int
    sample_rate: float = 0.05
    entropy_threshold: float = 0.8
    window_s: float = 2.0
    max_traces: int = 150
    seed: Optional[int] = None
    emitted: int = 0
    _recent: dict = field(default_factory=lambda: defaultdict(deque))

    def __post_init__(self):
        self._rng = random.Random(self.seed)
        self._window = max(1, int(self.window_s * self.tps))

    def on_decision(self, dec, meta: dict) -> None:
        reason = None
        if getattr(dec, "brain", "") == "openjev":
            n = len(dec.candidates or [])
            if n > 1 and dec.entropy_nats is not None and dec.entropy_nats / math.log(n) > self.entropy_threshold:
                reason = "uncertain"
            elif self._rng.random() < self.sample_rate:
                reason = "sample"
        self._recent[dec.snake_id].append(_Item(dec, meta, reason))

    def _take(self, item: _Item, reason: str, outcome: str) -> Optional[tuple]:
        if item.emitted or self.emitted >= self.max_traces:
            return None
        item.emitted = True
        self.emitted += 1
        return item.dec, item.meta, reason, outcome

    def on_death(self, snake_id: str, tick: int) -> list[tuple]:
        """Every OpenJev decision of this snake in the last window becomes a 'before_death' trace."""
        out = []
        for item in self._recent.get(snake_id, ()):
            if item.dec.tick >= tick - self._window and getattr(item.dec, "brain", "") == "openjev":
                reason = "before_death" if item.reason is None else f"{item.reason}+before_death"
                got = self._take(item, reason, "died_within_2s")
                if got:
                    out.append(got)
        self._recent[snake_id].clear()
        return out

    def on_tick(self, tick: int) -> list[tuple]:
        """Selected decisions whose window passed without a death are emitted as 'survived'."""
        out = []
        for dq in self._recent.values():
            while dq and dq[0].dec.tick + self._window <= tick:
                item = dq.popleft()
                if item.reason:
                    got = self._take(item, item.reason, "survived")
                    if got:
                        out.append(got)
        return out

    def flush(self) -> list[tuple]:
        out = []
        for dq in self._recent.values():
            while dq:
                item = dq.popleft()
                if item.reason:
                    got = self._take(item, item.reason, "game_over")
                    if got:
                        out.append(got)
        return out


class DecisionTracer:
    """App side: records selected decisions to Delta and triggers the trace-export job after a match."""

    def __init__(self, logger=None, export_job_id: Optional[str] = None, sample_rate: float = 0.05,
                 experiment_id: Optional[str] = None, workspace_client=None):
        self.logger = logger
        self.export_job_id = export_job_id or None
        self.experiment_id = experiment_id or None
        self.sample_rate = sample_rate
        self.enabled = bool(logger is not None and getattr(logger, "enabled", False))
        self.selected = 0
        self.export_runs = 0
        self.last_error: Optional[str] = None
        self._w = workspace_client
        self._lock = threading.Lock()

    def selector(self, tps: int) -> Optional[TraceSelector]:
        return TraceSelector(tps=tps, sample_rate=self.sample_rate) if self.enabled else None

    def emit(self, game_id: str, mode: str, items: list[tuple]) -> None:
        for dec, meta, reason, outcome in items:
            self.logger.trace(game_id, mode, dec, meta, reason, outcome)
            self.selected += 1

    def trigger_export(self) -> None:
        """Start the export job unless a run is already active (non-blocking, best effort)."""
        if not (self.enabled and self.export_job_id):
            return
        threading.Thread(target=self._trigger, daemon=True, name="trace-export-trigger").start()

    def _trigger(self):
        import time

        with self._lock:
            try:
                if self._w is None:
                    from databricks.sdk import WorkspaceClient

                    self._w = WorkspaceClient()
                time.sleep(10)  # let the decision logger flush the last rows first
                job_id = int(self.export_job_id)
                if any(True for _ in self._w.jobs.list_runs(job_id=job_id, active_only=True)):
                    return
                self._w.jobs.run_now(job_id=job_id)
                self.export_runs += 1
            except Exception as exc:
                self.last_error = str(exc)[:300]
                log.warning("could not trigger trace export job: %s", exc)


def write_trace(mlflow, experiment_id: str, d: dict) -> str:
    """Create one MLflow trace from a joined decision row (decisions ⋈ decision_traces).

    ``d`` keys: ts (datetime/epoch s), latency_ms, game_id, mode, snake_id, tick, persona, order_text, order_kind,
    reason, outcome, model, state (dict), candidates (list of {id, description, probability}), choice,
    confidence, entropy_nats, outlook_score, danger, input_tokens.
    """
    t = d["ts"]
    if hasattr(t, "timestamp"):
        if getattr(t, "tzinfo", None) is None:
            from datetime import timezone

            t = t.replace(tzinfo=timezone.utc)  # Spark returns naive UTC datetimes
        ts = t.timestamp()
    else:
        ts = float(t)
    end_ns = int(ts * 1e9)
    start_ns = end_ns - int((d.get("latency_ms") or 0) * 1e6) - 1_000_000  # + ~1 ms of perception
    cands = d.get("candidates") or []
    probs = {c["id"]: c["probability"] for c in cands}
    chosen = next((c for c in cands if c["id"] == d["choice"]), {})
    tags = {"game_id": d["game_id"], "snake_id": d["snake_id"], "mode": d.get("mode") or "", "reason": d["reason"],
            "outcome": d["outcome"], "persona": str(d.get("persona") or "")[:60],
            "choice_kind": d["choice"].split("_")[0], "order_kind": str(d.get("order_kind") or "none")}
    root = mlflow.start_span_no_context(
        "snake_decision", span_type="AGENT", experiment_id=experiment_id, start_time_ns=start_ns, tags=tags,
        inputs={"snake": d["snake_id"], "tick": d["tick"], "persona": d.get("persona"),
                "commander_order": d.get("order_text"), "order_kind": d.get("order_kind"), "state": d.get("state")},
        attributes={"model": d.get("model") or "", "latency_ms": d.get("latency_ms") or 0})
    perception = mlflow.start_span_no_context(
        "perception (code)", span_type="TOOL", parent_span=root, start_time_ns=start_ns,
        inputs={"tick": d["tick"]}, attributes={"options": len(cands)})
    perception.end(outputs={"options": {c["id"]: c["description"] for c in cands}}, end_time_ns=start_ns + 1_000_000)
    call = mlflow.start_span_no_context(
        "openjev.systemone", span_type="LLM", parent_span=root, start_time_ns=start_ns + 1_000_000,
        inputs={"model": d.get("model"), "questions": ["intent (choice)", "outlook (score)", "danger (noul)"]},
        attributes={"input_tokens": d.get("input_tokens") or 0, "entropy_nats": d.get("entropy_nats") or 0.0})
    call.end(outputs={"intent_probabilities": probs, "outlook_score": d.get("outlook_score"), "danger": d.get("danger")},
             end_time_ns=end_ns)
    root.end(outputs={"choice": d["choice"], "intent": chosen.get("description"), "confidence": d.get("confidence"),
                      "outlook": d.get("outlook_score"), "danger": d.get("danger"), "outcome": d["outcome"]},
             end_time_ns=end_ns)
    return root.trace_id
