import math

from serpents.brains import Decision
from serpents.controller import Intent
from test_ai import FakeClient, fake_answer
from tracing import TraceSelector


def dec(snake, tick, probs, brain="openjev"):
    ent = -sum(p * math.log(p) for p in probs if p > 0)
    cands = [{"id": f"o{i}", "description": f"option {i}", "probability": p} for i, p in enumerate(probs)]
    return Decision(snake_id=snake, tick=tick, choice="o0", intent=Intent(id="o0", kind="eat"), candidates=cands,
                    entropy_nats=ent, brain=brain, latency_ms=100)


def test_uncertain_decisions_are_traced_after_window_as_survived():
    sel = TraceSelector(tps=8, sample_rate=0.0)
    sel.on_decision(dec("a", 10, [0.25, 0.25, 0.25, 0.25]), {})   # max entropy -> uncertain
    sel.on_decision(dec("a", 12, [0.97, 0.01, 0.01, 0.01]), {})   # confident, not sampled
    assert sel.on_tick(20) == []                                   # window (16 ticks) not over yet
    out = sel.on_tick(26)
    assert [(d.tick, reason, outcome) for d, _, reason, outcome in out] == [(10, "uncertain", "survived")]


def test_decisions_before_death_are_all_traced():
    sel = TraceSelector(tps=8, sample_rate=0.0)
    for t in (2, 30, 36, 40):
        sel.on_decision(dec("a", t, [0.97, 0.01, 0.01, 0.01]), {})
    sel.on_decision(dec("b", 40, [0.97, 0.01, 0.01, 0.01]), {})
    out = sel.on_death("a", 44)
    assert [(d.tick, r, o) for d, _, r, o in out] == [(30, "before_death", "died_within_2s"),
                                                     (36, "before_death", "died_within_2s"),
                                                     (40, "before_death", "died_within_2s")]
    assert [d.snake_id for d, *_ in sel.on_death("b", 44)] == ["b"]   # per-snake buffers


def test_heuristic_decisions_never_traced_and_cap_respected():
    sel = TraceSelector(tps=8, sample_rate=1.0, max_traces=3)
    sel.on_decision(dec("h", 1, [1.0, 0.0], brain="heuristic"), {})
    for t in range(1, 10):
        sel.on_decision(dec("a", t, [0.9, 0.1]), {})
    out = sel.on_tick(100)
    assert len(out) == 3 and all(d.snake_id == "a" for d, *_ in out)


def test_gateway_request_tags_are_forwarded():
    resp = fake_answer({"q": {"type": "noul", "noul": 0.2}})
    c = FakeClient([(200, resp)])
    c.call("s", {"q": {"type": "noul", "instructions": "?"}}, tags={"game_id": "g1", "tick": 7})
    assert c.tags == {"game_id": "g1", "tick": 7}


class FakeSpan:
    def end(self, outputs=None, end_time_ns=None, **kw):
        self.outputs, self.end_ns = outputs, end_time_ns


class FakeMlflow:
    def __init__(self):
        self.spans = []

    def start_span_no_context(self, name, **kw):
        s = FakeSpan.__new__(FakeSpan)
        s.name, s.kw, s.trace_id = name, kw, "tr-1"
        self.spans.append(s)
        return s


def test_write_trace_builds_three_spans_with_real_timestamps():
    from datetime import datetime
    from tracing import write_trace

    m = FakeMlflow()
    row = {"ts": datetime(2026, 11, 4, 10, 0, 0), "latency_ms": 120.0, "game_id": "g", "mode": "human_vs_ai",
           "snake_id": "viper", "tick": 40, "persona": "viper", "order_text": None, "order_kind": "none",
           "reason": "before_death", "outcome": "died_within_2s", "model": "m", "state": {"you": "x"},
           "candidates": [{"id": "eat_f1", "description": "Eat", "probability": 0.7},
                          {"id": "hunt_you", "description": "Ambush", "probability": 0.3}],
           "choice": "eat_f1", "confidence": 0.6, "entropy_nats": 0.61, "outlook_score": 1.2, "danger": 0.7,
           "input_tokens": 1900}
    assert write_trace(m, "exp", row) == "tr-1"
    names = [s.name for s in m.spans]
    assert names == ["snake_decision", "perception (code)", "openjev.systemone"]
    root = m.spans[0]
    assert root.kw["tags"]["outcome"] == "died_within_2s" and root.kw["experiment_id"] == "exp"
    assert root.end_ns - root.kw["start_time_ns"] == 121_000_000           # 120 ms call + 1 ms perception
    assert root.end_ns == int(datetime(2026, 11, 4, 10, 0, 0, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1e9)


def test_tracer_records_selection_to_delta_logger():
    from tracing import DecisionTracer

    class L:
        enabled = True
        rows = []

        def trace(self, *a):
            self.rows.append(a)

    lg = L()
    t = DecisionTracer(lg, export_job_id=None)
    t.emit("g", "human_vs_ai", [(dec("a", 3, [0.5, 0.5]), {"persona": "viper"}, "uncertain", "survived")])
    assert t.selected == 1 and lg.rows[0][4] == "uncertain"
