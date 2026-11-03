import asyncio
import json

from serpents.brains import OpenJevBrain
from serpents.engine import Game, GameConfig
from serpents.session import GameSession
from test_ai import EchoClient, game


class FakeLogger:
    def __init__(self):
        self.decisions, self.events, self.games = [], [], []

    def decision(self, game_id, ai, dec, mode="x"):
        self.decisions.append(dec)

    def event(self, *a):
        self.events.append(a)

    def game(self, session):
        self.games.append(session.summary())


def make_session(duration_s=2, logger=None):
    s = GameSession(GameConfig(duration_s=duration_s, tps=10, seed=3),
                    [{"persona": "viper"}, {"persona": "turtle"}], with_human=True,
                    client=EchoClient(), logger=logger, decision_interval_s=0.2)
    s.countdown = 0
    return s


def test_full_async_match_streams_json_and_logs_game():
    async def run():
        lg = FakeLogger()
        s = make_session(logger=lg)
        q = s.subscribe()
        msgs = []
        while True:
            m = json.loads(await asyncio.wait_for(q.get(), 5))  # every queue item must be JSON text
            msgs.append(m)
            if m["type"] == "game_over":
                break
        late = json.loads(s.subscribe().get_nowait())          # late joiner after the end
        return lg, msgs, late
    lg, msgs, late = asyncio.run(run())
    assert msgs[-1]["type"] == "game_over" and late["type"] == "game_over"
    assert len(lg.games) == 1 and len(lg.decisions) >= 4
    assert any(m["ai"]["viper"]["decision"] for m in msgs)


def test_abandoned_match_is_not_logged_as_game():
    async def run():
        lg = FakeLogger()
        s = make_session(duration_s=60, logger=lg)
        q = s.subscribe()
        await asyncio.wait_for(q.get(), 5)
        s.unsubscribe(q)
        s.started_at -= 10                                      # pretend nobody has watched for 10 s
        await asyncio.wait_for(asyncio.gather(*s._tasks, return_exceptions=True), 5)
        return lg, s
    lg, s = asyncio.run(run())
    assert s.ended_at is not None and lg.games == []


def test_late_subscriber_during_match_gets_json():
    async def run():
        s = make_session(duration_s=5)
        q1 = s.subscribe()
        await asyncio.wait_for(q1.get(), 5)
        q2 = s.subscribe()
        first = q2.get_nowait()
        for t in s._tasks:
            t.cancel()
        return first
    assert isinstance(json.loads(asyncio.run(run())), dict)


def test_unknown_model_choice_falls_back_to_known_candidate():
    g = game()
    me = g.snakes["viper"]
    brain = OpenJevBrain(EchoClient())
    cands, state, _ = brain.prepare(g, me, "viper", "")
    resp = {"answers": {"intent": {"choice": "ghost", "probabilities": {"ghost": 0.9, cands[1].id: 0.1}}}}
    dec = brain.interpret(g, me, cands, state, resp)
    assert dec.choice == cands[1].id


def test_crowded_board_spawn_does_not_crash():
    players = [{"id": f"s{i}", "name": f"S{i}", "color": "#fff", "kind": "ai"} for i in range(6)]
    g = Game(GameConfig(width=6, height=6, seed=1), players)
    for _ in range(30):
        g.step()
