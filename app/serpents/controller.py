"""Deterministic low-level control: turn a high-level intent into a safe next step.

OpenJev picks *what* to do (an intent) every ~0.5 s; this module decides *how*
(one grid step) on every tick using BFS distances and flood-fill safety checks.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .engine import DIRS, OPPOSITE, Cell, Game, Snake


@dataclass
class Intent:
    id: str
    kind: str  # eat | gold | boost | shield | hunt | flee | survive | explore
    description: str = ""
    item_id: Optional[str] = None
    rival_id: Optional[str] = None
    target: Optional[Cell] = None
    meta: dict = field(default_factory=dict)


def blocked_cells(game: Game, me: Snake) -> set[Cell]:
    """Cells unsafe to enter next sub-step (bodies, minus tails that will move)."""
    blocked: set[Cell] = set()
    for s in game.snakes.values():
        if not s.alive:
            continue
        body = s.body[:-1] if s.grow == 0 and len(s.body) > 1 else s.body
        blocked.update(body)
    return blocked


def neighbors(game: Game, c: Cell):
    for d, (dx, dy) in DIRS.items():
        n = (c[0] + dx, c[1] + dy)
        if game.in_bounds(n):
            yield d, n


def bfs(game: Game, start: Cell, blocked: set[Cell]) -> dict[Cell, int]:
    """Distance from ``start`` to every reachable cell (start itself may be blocked)."""
    dist = {start: 0}
    q = deque([start])
    while q:
        c = q.popleft()
        for _, n in neighbors(game, c):
            if n not in dist and n not in blocked:
                dist[n] = dist[c] + 1
                q.append(n)
    return dist


def flood_size(game: Game, start: Cell, blocked: set[Cell], cap: int = 400) -> int:
    if start in blocked or not game.in_bounds(start):
        return 0
    seen = {start}
    q = deque([start])
    while q and len(seen) < cap:
        c = q.popleft()
        for _, n in neighbors(game, c):
            if n not in seen and n not in blocked:
                seen.add(n)
                q.append(n)
    return len(seen)


def rival_threat_cells(game: Game, me: Snake) -> dict[Cell, Snake]:
    """Cells a rival head could enter next step (straight or turning; never its reverse/neck)."""
    cells: dict[Cell, Snake] = {}
    for s in game.snakes.values():
        if s.alive and s.id != me.id:
            for d, n in neighbors(game, s.head):
                if d != OPPOSITE[s.direction]:
                    # keep the most dangerous rival per cell (the longest one)
                    if n not in cells or len(s.body) > len(cells[n].body):
                        cells[n] = s
    return cells


def hunt_target(game: Game, rival: Snake) -> Cell:
    """A cell two steps ahead of the rival head (clamped), to cut it off."""
    dx, dy = DIRS[rival.direction]
    x = min(max(rival.head[0] + 2 * dx, 0), game.w - 1)
    y = min(max(rival.head[1] + 2 * dy, 0), game.h - 1)
    return (x, y)


def resolve_target(game: Game, intent: Intent) -> tuple[Optional[Cell], bool]:
    """Return (target cell, still_valid)."""
    if intent.kind in ("eat", "gold", "boost", "shield"):
        it = game.items.get(intent.item_id or "")
        return (it.pos, True) if it else (None, False)
    if intent.kind == "hunt":
        r = game.snakes.get(intent.rival_id or "")
        if not (r and r.alive):
            return None, False
        return (intent.target or hunt_target(game, r)), True
    if intent.kind == "flee":
        r = game.snakes.get(intent.rival_id or "")
        return (r.head, True) if r and r.alive else (None, False)
    if intent.kind == "explore":
        return intent.target, intent.target is not None
    return None, True  # survive


def choose_direction(game: Game, me: Snake, intent: Optional[Intent], strict: bool = False) -> tuple[str, bool]:
    """Pick the next direction for ``me``. Returns (direction, intent_still_valid)."""
    intent = intent or Intent(id="survive", kind="survive")
    target, valid = resolve_target(game, intent)
    if not valid:
        intent, target = Intent(id="survive", kind="survive"), None
    if intent.kind in ("explore", "hunt") and intent.target and me.head == intent.target:
        valid = False  # reached: ask for a new decision

    blocked = blocked_cells(game, me)
    threats = rival_threat_cells(game, me)
    tail = me.body[-1]
    target_dist = bfs(game, target, blocked - {target}) if target and intent.kind != "flee" else None

    best, best_key = None, None
    for d, n in neighbors(game, me.head):
        if d == OPPOSITE[me.direction] or n in blocked:
            continue
        nb = blocked | {me.head}
        space = flood_size(game, n, nb)
        safe = space >= len(me.body) + 2
        threat = threats.get(n)
        # A head-on crash with a rival at least as long kills us (unless our shield bounces us):
        # treat it as near-fatal. Shorter rivals are only "risky" (we would win), except when hunting.
        deadly = threat is not None and len(threat.body) >= len(me.body) and not me.shielded(game.tick)
        risky = threat is not None and not deadly and intent.kind != "hunt"
        if intent.kind == "flee" and target:
            progress = abs(n[0] - target[0]) + abs(n[1] - target[1])  # maximise distance from the rival head
        elif intent.kind == "survive" or target is None:
            # prefer open space, then staying close to our own tail (classic safe loop)
            progress = space * 0.05 - (abs(n[0] - tail[0]) + abs(n[1] - tail[1])) * 0.5
        else:
            progress = -target_dist.get(n, 999) if target_dist is not None else 0
        # Lexicographic preference: never trap ourselves, never risk a losing head-on; in strict
        # mode (OpenJev says "danger" > 0.6) also avoid any rival-adjacent cell; otherwise penalise it.
        key = (safe, not deadly, (not risky) if strict else True, progress - 8 * risky, space)
        if best_key is None or key > best_key:
            best, best_key = d, key
    return (best or me.direction), valid
