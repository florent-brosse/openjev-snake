from serpents.engine import Game, GameConfig, Item
from serpents.intents import build_candidates, build_state


def blank():
    players = [{"id": "me", "name": "Me", "color": "#fff", "kind": "ai"},
               {"id": "r", "name": "Rival", "color": "#f00", "kind": "ai"}]
    g = Game(GameConfig(seed=1), players)
    g.items.clear()
    return g


def put(g, sid, body, d):
    s = g.snakes[sid]
    s.body, s.direction, s.alive, s.shield_until = list(body), d, True, 0
    return s


def ids(cands):
    return {c.id: c for c in cands}


def test_ambush_offered_only_when_interception_possible():
    g = blank()
    me = put(g, "me", [(10, 8), (9, 8), (8, 8), (7, 8), (6, 8)], "right")
    put(g, "r", [(12, 2), (12, 1), (12, 0)], "down")          # rival heading down column 12
    c = ids(build_candidates(g, me))
    assert "hunt_r" in c and c["hunt_r"].target[0] == 12 and "Ambush" in c["hunt_r"].description
    g2 = blank()
    me2 = put(g2, "me", [(2, 20), (1, 20), (0, 20)], "right")
    put(g2, "r", [(25, 2), (25, 1), (25, 0)], "down")          # far away, longer path than ours: impossible
    assert "hunt_r" not in ids(build_candidates(g2, me2))


def test_flee_only_for_close_longer_rival():
    g = blank()
    me = put(g, "me", [(10, 10), (9, 10), (8, 10)], "right")
    put(g, "r", [(20, 20), (21, 20), (22, 20), (23, 20)], "left")       # longer but far
    assert not any(k.startswith("flee") for k in ids(build_candidates(g, me)))
    put(g, "r", [(12, 10), (13, 10), (14, 10), (15, 10)], "left")       # longer and 2 steps away
    assert "flee_r" in ids(build_candidates(g, me))


def test_food_cluster_and_best_value_flag():
    g = blank()
    me = put(g, "me", [(5, 5), (4, 5), (3, 5)], "right")
    put(g, "r", [(30, 22), (29, 22), (28, 22)], "right")
    for i, pos in enumerate([(15, 5), (16, 5), (15, 6), (16, 6)]):
        g.items[f"f{i}"] = Item(f"f{i}", "food", pos)
    g.items["fx"] = Item("fx", "food", (7, 5))
    c = build_candidates(g, me)
    assert any(x.id.startswith("cluster_") and "4 food" in x.description for x in c)
    flagged = [x for x in c if "BEST VALUE" in x.description]
    assert len(flagged) == 1 and flagged[0].id == "eat_fx"          # 2 steps away = 0.50 points/step


def test_state_has_full_map_and_gap():
    g = blank()
    me = put(g, "me", [(5, 5), (4, 5), (3, 5)], "right")
    put(g, "r", [(30, 22), (29, 22), (28, 22)], "right")
    g.snakes["r"].score = 7
    st = build_state(g, me, "p", "")
    rows = st["arena_map"].split("\n")
    assert len(rows) == g.h and all(len(r) == g.w for r in rows) and rows[5][5] == "@"
    assert st["score_gap"] == "you are 7 points behind Rival"


def test_order_kind_tags_matching_options_and_drops_value_hint():
    g = blank()
    me = put(g, "me", [(5, 5), (4, 5), (3, 5)], "right")
    put(g, "r", [(30, 22), (29, 22), (28, 22)], "right")
    g.items["fx"] = Item("fx", "food", (7, 5))
    plain = build_candidates(g, me)
    assert any("BEST VALUE" in c.description for c in plain)
    safe = build_candidates(g, me, order_kind="safe")
    tagged = [c.id for c in safe if "ORDERED BY YOUR COMMANDER" in c.description]
    assert tagged == ["survive"] and not any("BEST VALUE" in c.description for c in safe)
    # an order that matches nothing on the board falls back to the value hint
    gold = build_candidates(g, me, order_kind="gold")
    assert any("BEST VALUE" in c.description for c in gold)


def test_session_classifies_order_once():
    import asyncio
    from serpents.session import GameSession
    from serpents.engine import GameConfig
    from test_ai import EchoClient

    async def run():
        s = GameSession(GameConfig(duration_s=2, tps=10, seed=3), [{"persona": "viper", "order": "play it safe"}],
                        with_human=True, client=EchoClient(), decision_interval_s=0.2)
        s.countdown = 0
        q = s.subscribe()
        last = None
        while True:
            import json
            last = json.loads(await asyncio.wait_for(q.get(), 5))
            if last["type"] == "game_over":
                return last
    last = asyncio.run(run())
    assert last["ai"]["viper"]["order_kind"] == "safe"
