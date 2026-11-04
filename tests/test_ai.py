import asyncio

import pytest

from serpents.brains import HeuristicBrain, OpenJevBrain
from serpents.controller import Intent, choose_direction, flood_size
from serpents.engine import Game, GameConfig, Item
from serpents.intents import MAX_OPTIONS, build_candidates, build_questions, build_state
from serpents.session import run_lockstep
from systemone import SystemOneClient, SystemOneError, entropy_nats

PLAYERS = [{"id": "you", "name": "You", "color": "#f00", "kind": "human"},
           {"id": "viper", "name": "Viper", "color": "#0ff", "kind": "ai", "persona": "viper"},
           {"id": "turtle", "name": "Turtle", "color": "#0f0", "kind": "ai", "persona": "turtle"}]


def game(seed=3):
    return Game(GameConfig(seed=seed), PLAYERS)


# ------------------------------------------------------------------ controller
def test_controller_moves_toward_item():
    g = game()
    g.items.clear()
    me = g.snakes["viper"]
    me.body, me.direction = [(10, 10), (9, 10), (8, 10)], "right"
    for s in ("you", "turtle"):
        g.snakes[s].body = [(1 + 3 * (s == "turtle"), 20), (1 + 3 * (s == "turtle"), 21)]
    g.items["f1"] = Item("f1", "food", (10, 4))
    d, valid = choose_direction(g, me, Intent(id="eat_f1", kind="eat", item_id="f1"))
    assert valid and d == "up"


def test_controller_avoids_trap():
    g = game()
    g.items.clear()
    me = g.snakes["viper"]
    # Heading right along row 1 with a wall of body above: going up is a 1-cell pocket.
    me.body, me.direction = [(5, 1), (4, 1), (3, 1), (2, 1), (2, 2), (3, 2), (4, 2), (5, 2), (6, 2)], "right"
    g.snakes["you"].body = [(6, 0), (7, 0), (7, 1), (8, 1)]
    g.snakes["turtle"].body = [(20, 20), (21, 20)]
    d, _ = choose_direction(g, me, None)
    assert d != "up"


def test_invalid_intent_when_item_gone():
    g = game()
    me = g.snakes["viper"]
    _, valid = choose_direction(g, me, Intent(id="eat_zz", kind="eat", item_id="zz"))
    assert valid is False


def test_flood_size_bounded():
    g = game()
    assert 0 < flood_size(g, (16, 12), set()) <= 400


# --------------------------------------------------------------------- intents
def test_candidates_unique_and_bounded():
    for seed in range(5):
        g = game(seed)
        for sid in ("viper", "turtle"):
            cands = build_candidates(g, g.snakes[sid])
            ids = [c.id for c in cands]
            assert 2 <= len(cands) <= MAX_OPTIONS
            assert len(set(ids)) == len(ids)
            assert "survive" in ids
            for c in cands:  # every candidate resolves to an executable step
                d, _ = choose_direction(g, g.snakes[sid], c)
                assert d in ("up", "down", "left", "right")


def test_state_and_questions_shape():
    g = game()
    me = g.snakes["viper"]
    st = build_state(g, me, "Viper persona", "hunt You")
    assert st["commander_order"] == "hunt You" and "@" in st["local_view"]
    q = build_questions(me, build_candidates(g, me))
    assert q["intent"]["type"] == "choice" and q["outlook"]["type"] == "score" and q["danger"]["type"] == "noul"


# ------------------------------------------------------------------ System One
class FakeClient(SystemOneClient):
    """SystemOneClient with the HTTP layer replaced by canned (status, payload) responses."""

    def __init__(self, responses, **kw):
        super().__init__(workspace_client=object(), **kw)
        self.responses, self.bodies = list(responses), []

    def _post(self, body, tags=None):
        self.bodies.append(body)
        self.tags = tags
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_answer(question_ids_probs):
    return {"model": "databricks-openjev-qwen35-4b", "usage": {"input_tokens": 900, "output_tokens": 0},
            "answers": question_ids_probs}


def test_client_body_entropy_and_retry():
    resp = fake_answer({"q": {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.5, "b": 0.5}}})
    c = FakeClient([(429, {"detail": "slow down"}), TimeoutError("read timeout"), (200, resp)],
                   model="system.ai.openjev-qwen35-4b")
    c.retries = 2
    out = c.call({"x": 1}, {"q": {"type": "choice", "instructions": "?", "criteria": {"a": None, "b": None}}})
    assert len(c.bodies) == 3 and c.bodies[0]["model"] == "system.ai.openjev-qwen35-4b"
    assert out["answers"]["q"]["entropy_nats"] == pytest.approx(0.6931, abs=1e-3)
    assert "latency_ms" in out and c.calls == 1


def test_client_validation_error_keeps_status_and_detail():
    c = FakeClient([(422, {"detail": [{"loc": ["questions", "q", "type"], "msg": "bad type"}]})])
    with pytest.raises(SystemOneError) as e:
        c.call("s", {"q": {"type": "bogus"}})
    assert e.value.status == 422 and "bad type" in str(e.value) and len(c.bodies) == 1  # no retry on 4xx


def test_client_gives_up_after_retries():
    c = FakeClient([(503, "unavailable")] * 3)
    with pytest.raises(SystemOneError) as e:
        c.call("s", {"q": {"type": "noul", "instructions": "?"}})
    assert e.value.status == 503 and len(c.bodies) == 3 and c.errors == 1


def test_entropy_uniform():
    assert entropy_nats({"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}) == pytest.approx(1.3863, abs=1e-3)


class EchoClient:
    """Answers like OpenJev would, preferring the first 'hunt' option."""

    model = "fake"

    def call(self, state, questions, model=None, tags=None):
        self.last_tags = tags
        if "kind" in questions:  # commander-order classification
            return {"latency_ms": 1.0, "answers": {"kind": {"type": "choice", "choice": "safe", "confidence": 0.9,
                                                            "probabilities": {"safe": 0.9, "none": 0.1}}}}
        opts = list(questions["intent"]["criteria"])
        pick = next((o for o in opts if o.startswith("hunt")), opts[0])
        probs = {o: (0.7 if o == pick else 0.3 / (len(opts) - 1)) for o in opts}
        return {"latency_ms": 1.0, "usage": {"input_tokens": 10},
                "answers": {"intent": {"type": "choice", "choice": pick, "confidence": 0.7, "probabilities": probs},
                            "outlook": {"type": "score", "score": 1.0, "probabilities": {"0": .2, "1": .6, "2": .2}},
                            "danger": {"type": "noul", "noul": 0.1}}}

    async def acall(self, *a, **k):
        return self.call(*a, **k)


def test_openjev_brain_maps_choice():
    g = game()
    dec = OpenJevBrain(EchoClient()).decide(g, g.snakes["viper"], "viper", "")
    assert dec.choice.startswith("hunt") and dec.intent.kind == "hunt"
    assert abs(sum(c["probability"] for c in dec.candidates) - 1) < 1e-3
    assert dec.danger == 0.1 and dec.outlook == 1.0


def test_heuristic_brain_picks_something():
    g = game()
    dec = HeuristicBrain().decide(g, g.snakes["turtle"])
    assert dec.choice in {c["id"] for c in dec.candidates}


def test_lockstep_match_runs():
    res = run_lockstep(GameConfig(seed=5, duration_s=10), [{"persona": "viper", "brain": "openjev"},
                                                           {"persona": "glutton", "brain": "heuristic"}],
                       client=EchoClient())
    assert res["ticks"] == 80 and len(res["players"]) == 2
    assert all(p["decisions"] > 0 for p in res["players"])


def test_async_brain():
    g = game()
    dec = asyncio.run(OpenJevBrain(EchoClient()).adecide(g, g.snakes["viper"], "viper", ""))
    assert dec.intent is not None
