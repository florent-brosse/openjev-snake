"""Regression tests for issues found in code review (collision invariants, controller safety)."""
import random

from serpents.controller import Intent, choose_direction
from serpents.engine import DIRS, OPPOSITE, Game, GameConfig


def blank(n=2, **cfg):
    players = [{"id": f"s{i}", "name": f"S{i}", "color": "#fff", "kind": "ai"} for i in range(n)]
    g = Game(GameConfig(**cfg), players)
    g.items.clear()
    g._refill_food = lambda: None
    g._next_gold = g._next_powerup = 10 ** 9
    return g


def put(g, sid, body, d, shield=False):
    s = g.snakes[sid]
    s.body, s.direction, s.alive = list(body), d, True
    s.shield_until = g.tick + 40 if shield else 0
    return s


def no_overlap(g):
    cells = [c for s in g.snakes.values() if s.alive for c in s.body]
    return len(cells) == len(set(cells))


def test_shielded_head_on_never_shares_a_cell():
    g = blank()
    put(g, "s0", [(4, 5), (3, 5), (2, 5), (1, 5)], "right")          # longer, unshielded
    put(g, "s1", [(6, 5), (7, 5), (8, 5)], "left", shield=True)      # shorter, shielded
    g.step()
    assert no_overlap(g)
    assert g.snakes["s1"].alive and g.snakes["s1"].shield_until == 0  # shield consumed, survived


def test_shield_bounces_instead_of_overlapping_a_body():
    g = blank()
    a = put(g, "s0", [(5, 5), (5, 6), (5, 7)], "up", shield=True)
    put(g, "s1", [(6, 4), (5, 4), (4, 4), (3, 4)], "right")
    g.step()
    assert a.alive and no_overlap(g) and a.shield_until == 0


def test_head_swap_no_double_kill_credit():
    g = blank()
    put(g, "s0", [(4, 5), (3, 5), (2, 5)], "right")
    put(g, "s1", [(5, 5), (6, 5), (7, 5)], "left")
    g.step()
    s0, s1 = g.snakes["s0"], g.snakes["s1"]
    assert not s0.alive and not s1.alive and s0.score == 0 and s1.score == 0


def test_head_swap_longer_survives():
    g = blank()
    put(g, "s0", [(4, 5), (3, 5), (2, 5), (1, 5)], "right")
    put(g, "s1", [(5, 5), (6, 5), (7, 5)], "left")
    g.step()
    assert g.snakes["s0"].alive and not g.snakes["s1"].alive and g.snakes["s0"].kills == 1 and no_overlap(g)


def test_fuzz_no_overlap_with_random_steering():
    for seed in range(40):
        g = Game(GameConfig(width=16, height=12, seed=seed),
                 [{"id": f"s{i}", "name": f"S{i}", "color": "#fff", "kind": "ai"} for i in range(4)])
        rng = random.Random(seed)
        steer = lambda game, s: rng.choice([d for d in DIRS if d != OPPOSITE[s.direction]])
        for _ in range(300):
            g.step(steer)
            assert no_overlap(g), f"overlap seed={seed} tick={g.tick}"


def test_controller_avoids_head_on_with_longer_rival():
    g = blank()
    me = put(g, "s0", [(11, 10), (10, 10), (9, 10)], "right")
    put(g, "s1", [(12, 11), (12, 12), (12, 13), (12, 14), (12, 15)], "up")  # longer, its next head may be (12,10)
    d, _ = choose_direction(g, me, Intent(id="explore", kind="explore", target=(20, 10)))
    assert d != "right"


def test_flee_moves_away():
    g = blank()
    me = put(g, "s0", [(10, 10), (9, 10), (8, 10)], "right")
    put(g, "s1", [(14, 10), (15, 10), (16, 10)], "left")
    d, _ = choose_direction(g, me, Intent(id="flee_s1", kind="flee", rival_id="s1"))
    assert d in ("up", "down")
