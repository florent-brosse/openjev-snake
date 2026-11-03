"""Perception: turn the game into an OpenJev System One request.

The application does the "seeing" (distances, races, open space) and writes the
facts in English; OpenJev only judges which option fits best.
"""
from __future__ import annotations

import re
from typing import Optional

from .controller import Intent, bfs, blocked_cells, flood_size
from .engine import DIRS, Game, Snake

MAX_OPTIONS = 12

PERSONAS = {
    "viper": "Viper, an aggressive hunter who still plays to win on points: sets up ambushes when an interception "
             "is possible (+10 when the rival crashes into you), grabs gold, and eats efficiently between hunts. "
             "Never chases a rival it cannot cut off.",
    "turtle": "Turtle, a careful player who still plays to win on points: eats the best-value food and gold that it "
              "can reach first, avoids head-on fights with longer snakes, and only escapes when a longer rival is "
              "really close.",
    "glutton": "Glutton, a greedy eater: maximises points per step (food clusters, nearest food, every reachable "
               "gold) and ignores rivals unless they are in the way.",
}

RULES = ("Delta Serpents: real-time snake arena on a {w}x{h} grid. Food +1 point (and +1 length), gold +5, "
         "speed boost doubles speed for 3 s, shield survives one crash for 5 s. If a rival crashes into your body "
         "you get +10. Hitting a wall or any body kills you (respawn after 3 s). In a head-on crash the longer "
         "snake survives. Highest score when time runs out wins.")

OUTLOOK_LEVELS = ["losing", "even", "winning"]

# Commander orders are first classified by OpenJev (a small text-classification question), then code
# tags the matching options. Decompose, then combine in code: small models follow this far better than
# free-text orders mixed into a big state (e.g. "never fight" next to a +10 ambush).
ORDER_KINDS = {
    "eat": "Eat food, grow, collect points from food.",
    "gold": "Go for the gold.",
    "hunt": "Attack, hunt, ambush, cut off or kill a rival snake.",
    "safe": "Play safe: avoid danger, survive, circle, flee, stop fighting.",
    "powerup": "Grab a speed boost or a shield.",
    "explore": "Explore the map.",
    "none": "No specific instruction, or general encouragement.",
}
ORDER_MATCH = {"eat": {"eat"}, "gold": {"gold"}, "hunt": {"hunt"}, "safe": {"survive", "flee"},
               "powerup": {"boost", "shield"}, "explore": {"explore"}}


def order_question() -> dict:
    return {"kind": {"type": "choice", "instructions": "What does this commander order ask the snake to do?",
                     "criteria": ORDER_KINDS}}


def _manhattan(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def local_view(game: Game, me: Snake, radius: int = 4) -> str:
    """ASCII window around my head. @ head, o my body, A/a rival head/body, * food, $ gold, > boost, + shield, # wall."""
    occ = {}
    for s in game.snakes.values():
        if s.alive:
            for i, c in enumerate(s.body):
                mine = s.id == me.id
                occ[c] = ("@" if i == 0 else "o") if mine else ("A" if i == 0 else "a")
    glyph = {"food": "*", "gold": "$", "boost": ">", "shield": "+"}
    items = {it.pos: glyph[it.kind] for it in game.items.values()}
    rows = []
    hx, hy = me.head
    for y in range(hy - radius, hy + radius + 1):
        row = []
        for x in range(hx - radius, hx + radius + 1):
            c = (x, y)
            row.append("#" if not game.in_bounds(c) else occ.get(c) or items.get(c) or ".")
        rows.append(" ".join(row))
    return "\n".join(rows)


def arena_map(game: Game, me: Snake) -> str:
    """The whole arena, one character per cell (same legend as the local view)."""
    grid = [["." for _ in range(game.w)] for _ in range(game.h)]
    glyph = {"food": "*", "gold": "$", "boost": ">", "shield": "+"}
    for it in game.items.values():
        grid[it.pos[1]][it.pos[0]] = glyph[it.kind]
    for s in game.snakes.values():
        if s.alive:
            for i, (x, y) in enumerate(s.body):
                grid[y][x] = ("@" if i == 0 else "o") if s.id == me.id else ("A" if i == 0 else "a")
    return "\n".join("".join(r) for r in grid)


def _straight_path(game: Game, r: Snake, blocked, k: int = 10) -> list:
    dx, dy = DIRS[r.direction]
    path, (x, y) = [], r.head
    for _ in range(k):
        x, y = x + dx, y + dy
        if not game.in_bounds((x, y)) or (x, y) in blocked:
            break
        path.append((x, y))
    return path


def _intercept(game: Game, me: Snake, r: Snake, my_dist, blocked):
    """Earliest cell on the rival's straight path that we reach strictly before it and still cover
    with our body when it arrives (so it crashes into us). Returns (cell, my_steps, its_steps)."""
    for k, cell in enumerate(_straight_path(game, r, blocked), start=1):
        d = my_dist.get(cell)
        if d is not None and d < k <= d + len(me.body) - 1:
            return cell, d, k
    return None


def build_candidates(game: Game, me: Snake, order_kind: Optional[str] = None) -> list[Intent]:
    blocked = blocked_cells(game, me)
    my_dist = bfs(game, me.head, blocked)
    rivals = [s for s in game.snakes.values() if s.alive and s.id != me.id]
    rival_dist = {r.id: bfs(game, r.head, blocked) for r in rivals}
    nb = blocked | {me.head}

    def race(cell, d):
        """(text, lost) for the nearest rival to ``cell``."""
        best = None
        for r in rivals:
            rd = rival_dist[r.id].get(cell)
            if rd is not None and (best is None or rd < best[1]):
                best = (r, rd)
        if best is None:
            return "uncontested", False
        r, rd = best
        if rd < d:
            return f"{r.name} is closer ({rd} steps), you will probably lose the race", True
        if rd == d:
            return f"tied race with {r.name} ({rd} steps)", False
        return f"you get there first (nearest rival {r.name} at {rd} steps)", False

    reachable = sorted(((my_dist[it.pos], it) for it in game.items.values() if it.pos in my_dist),
                       key=lambda t: (t[0], t[1].id))
    out: list[Intent] = []

    # Special items (gold, boost, shield) anywhere on the map.
    for d, it in reachable:
        if it.kind == "food":
            continue
        where = f"({it.pos[0]},{it.pos[1]})"
        text, lost = race(it.pos, d)
        if it.kind == "gold":
            ttl = (it.expires_at - game.tick) / game.cfg.tps if it.expires_at else 0
            late = " You cannot reach it in time." if d / game.cfg.tps > ttl else ""
            out.append(Intent(id=f"gold_{it.id}", kind="gold", item_id=it.id, description=(
                f"Race for GOLD at {where}: +5 points (= 5 food), {d} steps away ({5 / max(d, 1):.2f} points/step); "
                f"{text}; disappears in {ttl:.0f} s.{late}")))
        elif it.kind == "boost":
            out.append(Intent(id=f"boost_{it.id}", kind="boost", item_id=it.id, description=(
                f"Grab the speed boost at {where}: {d} steps away; doubles your speed for 3 s (more food per second); {text}.")))
        else:
            out.append(Intent(id=f"shield_{it.id}", kind="shield", item_id=it.id, description=(
                f"Grab the shield at {where}: {d} steps away; survive one crash for 5 s; {text}.")))

    # Food clusters: several food close together (e.g. the remains of a dead snake).
    foods = [(d, it) for d, it in reachable if it.kind == "food"]
    used: set = set()
    clusters = []
    for d, it in foods:
        group = [(d2, f) for d2, f in foods if abs(f.pos[0] - it.pos[0]) + abs(f.pos[1] - it.pos[1]) <= 3]
        if len(group) >= 3:
            clusters.append((len(group), -min(g[0] for g in group), group))
    for n, _, group in sorted(clusters, key=lambda c: (c[0], c[1]), reverse=True):
        ids = {f.id for _, f in group}
        if ids & used:
            continue
        used |= ids
        d0, first = min(group, key=lambda g: g[0])
        text, lost = race(first.pos, d0)
        out.append(Intent(id=f"cluster_{first.id}", kind="eat", item_id=first.id, description=(
            f"Food cluster: {n} food around ({first.pos[0]},{first.pos[1]}), the first one {d0} steps away; "
            f"about +{n} points in ~{d0 + 2 * n} steps ({n / (d0 + 2 * n):.2f} points/step); {text}.")))
        if len([c for c in out if c.id.startswith("cluster_")]) >= 2:
            break

    # Best single food: uncontested first, then by distance.
    ranked = sorted(foods, key=lambda t: (race(t[1].pos, t[0])[1], t[0]))
    for d, it in ranked[:6]:
        text, lost = race(it.pos, d)
        space = flood_size(game, it.pos, nb)
        trap = " Dead-end area: risky." if space < len(me.body) + 4 else ""
        out.append(Intent(id=f"eat_{it.id}", kind="eat", item_id=it.id, description=(
            f"Eat food at ({it.pos[0]},{it.pos[1]}): {d} steps away, +1 point ({1 / max(d, 1):.2f} points/step); {text}.{trap}")))

    # Ambush only when an interception is geometrically possible; head-on attack only on shorter rivals.
    for r in rivals:
        hit = _intercept(game, me, r, my_dist, blocked)
        if hit:
            cell, d, k = hit
            out.append(Intent(id=f"hunt_{r.id}", kind="hunt", rival_id=r.id, target=cell, description=(
                f"Ambush {r.name}: reach ({cell[0]},{cell[1]}) in {d} steps, {k - d} step(s) before it does; if it keeps "
                f"going straight it crashes into your body and you get +10. It may still turn away.")))
        elif len(r.body) < len(me.body) and _manhattan(me.head, r.head) <= 4:
            out.append(Intent(id=f"hunt_{r.id}", kind="hunt", rival_id=r.id, description=(
                f"Attack {r.name} head-on: it is shorter ({len(r.body)} vs your {len(me.body)}) and only "
                f"{_manhattan(me.head, r.head)} steps away; a head-on crash kills it and gives you +10.")))

    # Escape only from a real threat: a rival at least as long whose head is very close.
    if not me.shielded(game.tick):
        threats = sorted((r for r in rivals if len(r.body) >= len(me.body) and _manhattan(me.head, r.head) <= 3),
                         key=lambda r: _manhattan(me.head, r.head))
        if threats:
            r = threats[0]
            out.append(Intent(id=f"flee_{r.id}", kind="flee", rival_id=r.id, description=(
                f"Escape from {r.name}: its head is {_manhattan(me.head, r.head)} steps away and it is at least as long "
                f"as you, so a head-on crash would kill you. No points.")))

    space_now = flood_size(game, me.head, blocked - {me.head})
    out.append(Intent(id="survive", kind="survive",
                      description=f"Play it safe: circle in the open area ({space_now} free cells) and wait. No points."))
    if len(reachable) < 3:
        center = _open_center(game, blocked)
        if center:
            d = my_dist.get(center, _manhattan(me.head, center))
            out.append(Intent(id="explore", kind="explore", target=center, description=(
                f"Explore: head to the open middle at ({center[0]},{center[1]}), {d} steps, to find new items. No points now.")))

    # Code does the arithmetic, OpenJev does the judgement: flag the best points-per-step option that we
    # can actually win (not a lost race, not too late, not a dead end). The persona / order can override it.
    best, best_v = None, 0.0
    for c in out:
        m = re.search(r"\(([\d.]+) points/step\)", c.description)
        if not m or re.search(r"lose the race|cannot reach it in time|Dead-end", c.description):
            continue
        if float(m.group(1)) > best_v:
            best, best_v = c, float(m.group(1))
    if order_kind in ORDER_MATCH and any(c.kind in ORDER_MATCH[order_kind] for c in out):
        for c in out:  # the commander's order replaces the value hint
            if c.kind in ORDER_MATCH[order_kind]:
                c.description += " ORDERED BY YOUR COMMANDER."
    elif best:
        best.description += " BEST VALUE right now."

    # Keep at most MAX_OPTIONS: drop the lowest-ranked single food first.
    while len(out) > MAX_OPTIONS:
        food_idx = [i for i, c in enumerate(out) if c.id.startswith("eat_")]
        out.pop(food_idx[-1] if food_idx else len(out) - 2)
    return out


def _open_center(game: Game, blocked) -> Optional[tuple[int, int]]:
    cx, cy = game.w // 2, game.h // 2
    for r in range(0, 6):
        for dx in range(-r, r + 1):
            for dy in (-r, r) if abs(dx) != r else range(-r, r + 1):
                c = (cx + dx, cy + dy)
                if game.in_bounds(c) and c not in blocked:
                    return c
    return None


def build_state(game: Game, me: Snake, persona_text: str, order: str) -> dict:
    rivals = []
    for s in game.snakes.values():
        if s.id == me.id:
            continue
        if s.alive:
            rivals.append(f"{s.name}: head at ({s.head[0]},{s.head[1]}) moving {s.direction}, length {len(s.body)}, "
                          f"score {s.score}, {_manhattan(me.head, s.head)} steps from you"
                          + (", SHIELDED" if s.shielded(game.tick) else "") + (", BOOSTED" if s.boosted(game.tick) else ""))
        else:
            rivals.append(f"{s.name}: dead, respawning; score {s.score}")
    ranking = sorted(game.snakes.values(), key=lambda s: s.score, reverse=True)
    pos = [s.id for s in ranking].index(me.id) + 1
    best_other = max((s for s in ranking if s.id != me.id), key=lambda s: s.score, default=None)
    if best_other is None:
        gap = "no rivals"
    elif me.score > best_other.score:
        gap = f"you lead {best_other.name} by {me.score - best_other.score} points"
    elif me.score == best_other.score:
        gap = f"tied with {best_other.name}"
    else:
        gap = f"you are {best_other.score - me.score} points behind {best_other.name}"
    return {
        "rules": RULES.format(w=game.w, h=game.h),
        "you": (f"You are {me.name}. Head at ({me.head[0]},{me.head[1]}) moving {me.direction}, length {len(me.body)}, "
                f"score {me.score} (rank {pos} of {len(ranking)})"
                + (", shield active" if me.shielded(game.tick) else "") + (", boost active" if me.boosted(game.tick) else "")),
        "rivals": rivals,
        "time_left": f"{game.time_left_s:.0f} s",
        "score_gap": gap,
        "persona": persona_text,
        "commander_order": order.strip() or "none",
        "local_view": local_view(game, me) + "\nLegend: @ your head, o your body, A rival head, a rival body, "
                                              "* food, $ gold, > boost, + shield, # wall. Up is y-1.",
        "arena_map": arena_map(game, me),
    }


def build_questions(me: Snake, candidates: list[Intent], with_outlook: bool = True) -> dict:
    q = {
        "intent": {
            "type": "choice",
            "instructions": (f"You control the snake {me.name}. Your goal is to finish with the highest score. "
                             f"Following your persona's style and, above all, the commander order, which intent "
                             f"should {me.name} pursue right now?"),
            "criteria": {c.id: c.description for c in candidates},
        }
    }
    if with_outlook:
        q["outlook"] = {"type": "score", "instructions": f"How is {me.name} doing in this match right now?",
                        "criteria": OUTLOOK_LEVELS}
        q["danger"] = {"type": "noul",
                       "instructions": f"Is {me.name} in immediate danger of crashing or being cut off in the next 2 seconds?"}
    return q
