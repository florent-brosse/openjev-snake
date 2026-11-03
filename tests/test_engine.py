from serpents.engine import KILL_POINTS, Game, GameConfig, Item


def make(players=None, **cfg):
    players = players or [{"id": "a", "name": "A", "color": "#f00", "kind": "ai"},
                          {"id": "b", "name": "B", "color": "#0f0", "kind": "ai"}]
    g = Game(GameConfig(**cfg), players)
    g.items.clear()  # deterministic tests: start with an empty board
    g._refill_food = lambda: None
    g._next_gold = g._next_powerup = 10 ** 9
    return g


def place(g, sid, body, direction):
    s = g.snakes[sid]
    s.body, s.direction, s.alive, s.shield_until = list(body), direction, True, 0
    return s


def test_move_and_grow():
    g = make()
    a = place(g, "a", [(5, 5), (4, 5), (3, 5)], "right")
    place(g, "b", [(20, 20), (19, 20), (18, 20)], "right")
    g.items["f1"] = Item("f1", "food", (6, 5))
    g.step()
    assert a.head == (6, 5) and a.score == 1 and a.grow == 1
    g.step()
    assert a.head == (7, 5) and len(a.body) == 4


def test_wall_death_and_respawn():
    g = make()
    a = place(g, "a", [(31, 5), (30, 5), (29, 5)], "right")
    place(g, "b", [(10, 20), (9, 20), (8, 20)], "right")
    events = g.step()
    assert not a.alive and any(e["type"] == "death" and e["cause"] == "wall" for e in events)
    for _ in range(g._s(g.cfg.respawn_s) + 1):
        g.step()
    assert a.alive and len(a.body) == 3


def test_body_collision_gives_kill_points():
    g = make()
    a = place(g, "a", [(5, 5), (5, 6), (5, 7)], "up")       # will move into b's body at (5,4)
    b = place(g, "b", [(6, 4), (5, 4), (4, 4), (3, 4)], "right")
    g.step()
    assert not a.alive and b.alive and b.score == KILL_POINTS and b.kills == 1


def test_head_on_longer_survives():
    g = make()
    a = place(g, "a", [(5, 5), (4, 5), (3, 5), (2, 5)], "right")
    b = place(g, "b", [(7, 5), (8, 5), (9, 5)], "left")  # both heads enter (6,5)
    g.step()
    assert a.alive and not b.alive and a.score == KILL_POINTS


def test_head_on_equal_both_die():
    g = make()
    a = place(g, "a", [(5, 5), (4, 5), (3, 5)], "right")
    b = place(g, "b", [(7, 5), (8, 5), (9, 5)], "left")
    g.step()
    assert not a.alive and not b.alive


def test_shield_absorbs_one_hit():
    g = make()
    a = place(g, "a", [(5, 5), (5, 6), (5, 7)], "up")
    place(g, "b", [(6, 4), (5, 4), (4, 4), (3, 4)], "right")
    a.shield_until = g.tick + 40
    events = g.step()
    assert a.alive and a.shield_until == 0 and any(e["type"] == "shield_saved" for e in events)


def test_boost_moves_two_cells():
    g = make()
    a = place(g, "a", [(5, 5), (4, 5), (3, 5)], "right")
    place(g, "b", [(20, 20), (19, 20), (18, 20)], "right")
    a.boost_until = g.tick + 10
    g.step()
    assert a.head == (7, 5)


def test_gold_expires():
    g = make()
    place(g, "a", [(5, 5), (4, 5), (3, 5)], "right")
    place(g, "b", [(20, 20), (19, 20), (18, 20)], "right")
    g._new_item("gold", (15, 15), ttl_s=1)
    for _ in range(g.cfg.tps + 1):
        g.step()
    assert not any(it.kind == "gold" for it in g.items.values())


def test_no_reverse_input():
    g = make([{"id": "you", "name": "You", "color": "#f00", "kind": "human"}])
    you = place(g, "you", [(5, 5), (4, 5), (3, 5)], "right")
    g.queue_direction("you", "left")
    g.step()
    assert you.head == (6, 5)
    g.queue_direction("you", "up")
    g.step()
    assert you.head == (6, 4)


def test_seeded_determinism():
    players = [{"id": "a", "name": "A", "color": "#f00", "kind": "ai"}, {"id": "b", "name": "B", "color": "#0f0", "kind": "ai"}]
    g1, g2 = Game(GameConfig(seed=42), players), Game(GameConfig(seed=42), players)
    for _ in range(50):
        g1.step(), g2.step()
    assert g1.snapshot() == g2.snapshot()


def test_game_over_and_winner():
    g = make(duration_s=1, tps=4)
    place(g, "a", [(5, 5), (4, 5), (3, 5)], "right")
    place(g, "b", [(5, 15), (4, 15), (3, 15)], "right")
    g.snakes["a"].score = 3
    for _ in range(4):
        g.step()
    assert g.over and g.winner() == "a"
