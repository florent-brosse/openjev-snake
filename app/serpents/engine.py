"""Delta Serpents — pure, deterministic snake-arena simulation (no I/O).

Coordinates are (x, y) with (0, 0) top-left. A snake's body is a list with the
head at index 0. One call to ``Game.step`` advances one tick; boosted snakes
move twice per tick (two sub-steps).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Optional

Cell = tuple[int, int]

DIRS: dict[str, Cell] = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

ITEM_POINTS = {"food": 1, "gold": 5, "boost": 0, "shield": 0}
KILL_POINTS = 10
START_LEN = 3


@dataclass
class Snake:
    id: str
    name: str
    color: str
    kind: str  # "human" | "ai"
    persona: Optional[str] = None
    body: list[Cell] = field(default_factory=list)
    direction: str = "right"
    alive: bool = True
    respawn_at: Optional[int] = None
    score: int = 0
    grow: int = 0
    shield_until: int = 0
    boost_until: int = 0
    kills: int = 0
    deaths: int = 0
    input_queue: list[str] = field(default_factory=list)

    @property
    def head(self) -> Cell:
        return self.body[0]

    def shielded(self, tick: int) -> bool:
        return self.shield_until > tick

    def boosted(self, tick: int) -> bool:
        return self.boost_until > tick


@dataclass
class Item:
    id: str
    kind: str  # food | gold | boost | shield
    pos: Cell
    expires_at: Optional[int] = None


@dataclass
class GameConfig:
    width: int = 32
    height: int = 24
    tps: int = 8
    duration_s: int = 120
    seed: int = 7
    food_base: int = 5
    gold_every_s: float = 12.0
    gold_ttl_s: float = 10.0
    powerup_every_s: float = 9.0
    powerup_ttl_s: float = 15.0
    boost_s: float = 3.0
    shield_s: float = 5.0
    respawn_s: float = 3.0


class Game:
    def __init__(self, config: GameConfig, players: list[dict]):
        self.cfg = config
        self.w, self.h = config.width, config.height
        self.rng = random.Random(config.seed)
        self.tick = 0
        self.items: dict[str, Item] = {}
        self._item_seq = 0
        self.snakes: dict[str, Snake] = {}
        self.events: list[dict] = []  # events produced by the last step()
        for p in players:
            s = Snake(id=p["id"], name=p["name"], color=p["color"], kind=p["kind"], persona=p.get("persona"))
            self.snakes[s.id] = s
            self._spawn(s)
        self._next_gold = self._s(config.gold_every_s * 0.5)
        self._next_powerup = self._s(config.powerup_every_s * 0.6)
        self._refill_food()

    # ------------------------------------------------------------------ helpers
    def _s(self, seconds: float) -> int:
        return max(1, int(round(seconds * self.cfg.tps)))

    @property
    def total_ticks(self) -> int:
        return self.cfg.duration_s * self.cfg.tps

    @property
    def time_left_s(self) -> float:
        return max(0.0, (self.total_ticks - self.tick) / self.cfg.tps)

    @property
    def over(self) -> bool:
        return self.tick >= self.total_ticks

    def in_bounds(self, c: Cell) -> bool:
        return 0 <= c[0] < self.w and 0 <= c[1] < self.h

    def occupied(self) -> dict[Cell, str]:
        """Cell -> snake id for every live body cell."""
        occ: dict[Cell, str] = {}
        for s in self.snakes.values():
            if s.alive:
                for c in s.body:
                    occ.setdefault(c, s.id)
        return occ

    def item_at(self, c: Cell) -> Optional[Item]:
        for it in self.items.values():
            if it.pos == c:
                return it
        return None

    def free_cells(self) -> list[Cell]:
        occ = self.occupied()
        items = {it.pos for it in self.items.values()}
        return [(x, y) for y in range(self.h) for x in range(self.w) if (x, y) not in occ and (x, y) not in items]

    def winner(self) -> Optional[str]:
        """Highest score wins; None on a tie."""
        ranked = sorted(self.snakes.values(), key=lambda s: s.score, reverse=True)
        if len(ranked) > 1 and ranked[0].score == ranked[1].score:
            return None
        return ranked[0].id if ranked else None

    def _emit(self, type_: str, snake_id: Optional[str] = None, **detail):
        self.events.append({"tick": self.tick, "type": type_, "snake_id": snake_id, **detail})

    # ----------------------------------------------------------------- spawning
    def _new_item(self, kind: str, pos: Cell, ttl_s: Optional[float] = None) -> Item:
        self._item_seq += 1
        prefix = {"food": "f", "gold": "g", "boost": "b", "shield": "s"}[kind]
        it = Item(id=f"{prefix}{self._item_seq}", kind=kind, pos=pos,
                  expires_at=(self.tick + self._s(ttl_s)) if ttl_s else None)
        self.items[it.id] = it
        return it

    def _random_free(self, margin: int = 1) -> Optional[Cell]:
        cells = [c for c in self.free_cells()
                 if margin <= c[0] < self.w - margin and margin <= c[1] < self.h - margin]
        heads = [s.head for s in self.snakes.values() if s.alive]
        cells = [c for c in cells if all(abs(c[0] - h[0]) + abs(c[1] - h[1]) > 2 for h in heads)] or cells
        return self.rng.choice(cells) if cells else None

    def _refill_food(self):
        target = self.cfg.food_base + len(self.snakes)
        food = sum(1 for it in self.items.values() if it.kind == "food")
        for _ in range(max(0, target - food)):
            pos = self._random_free()
            if pos:
                self._new_item("food", pos)

    def _spawn(self, s: Snake):
        """Place a snake (length 3) on a safe straight run, far from other heads."""
        occ = self.occupied()
        items = {it.pos for it in self.items.values()}
        heads = [o.head for o in self.snakes.values() if o.alive and o.id != s.id and o.body]
        options = []
        for y in range(2, self.h - 2):
            for x in range(2, self.w - 2):
                for d, (dx, dy) in DIRS.items():
                    cells = [(x - dx * i, y - dy * i) for i in range(START_LEN)]
                    ahead = [(x + dx * i, y + dy * i) for i in range(1, 4)]
                    if not all(self.in_bounds(c) and c not in occ and c not in items for c in cells + ahead):
                        continue
                    dist = min((abs(x - h[0]) + abs(y - h[1]) for h in heads), default=99)
                    if dist >= 6:
                        options.append((cells, d))
        if not options:  # crowded arena: accept anything free
            for y in range(1, self.h - 1):
                for x in range(1, self.w - 1):
                    for d, (dx, dy) in DIRS.items():
                        cells = [(x - dx * i, y - dy * i) for i in range(START_LEN)]
                        if all(self.in_bounds(c) and c not in occ for c in cells):
                            options.append((cells, d))
        if not options:  # board full: try again in a second
            s.alive, s.body, s.respawn_at = False, [], self.tick + self.cfg.tps
            return
        cells, d = self.rng.choice(options)
        s.body, s.direction = list(cells), d
        s.alive, s.respawn_at, s.grow = True, None, 0
        s.input_queue.clear()
        s.shield_until = self.tick + self._s(1.5)  # brief spawn protection
        if self.tick > 0:
            self._emit("respawn", s.id, x=cells[0][0], y=cells[0][1])

    # ------------------------------------------------------------------- input
    def queue_direction(self, snake_id: str, direction: str):
        s = self.snakes.get(snake_id)
        if s and direction in DIRS and len(s.input_queue) < 3:
            s.input_queue.append(direction)

    def _take_input(self, s: Snake):
        while s.input_queue:
            d = s.input_queue.pop(0)
            if d != OPPOSITE[s.direction] and d != s.direction:
                s.direction = d
                return

    # -------------------------------------------------------------------- step
    def step(self, ai_direction: Optional[Callable[["Game", Snake], Optional[str]]] = None) -> list[dict]:
        """Advance one tick. ``ai_direction(game, snake)`` steers AI snakes each sub-step."""
        self.events = []
        if self.over:
            return self.events
        for sub in range(2):
            movers = [s for s in self.snakes.values() if s.alive and (sub == 0 or s.boosted(self.tick))]
            if not movers:
                continue
            for s in movers:
                if s.kind == "ai" and ai_direction:
                    d = ai_direction(self, s)
                    if d in DIRS and d != OPPOSITE[s.direction]:
                        s.direction = d
                else:
                    self._take_input(s)
            self._move(movers)
        self._housekeeping()
        self.tick += 1
        if self.over:
            self._emit("game_over", None, winner=self.winner(),
                       scores={s.id: s.score for s in self.snakes.values()})
        return self.events

    def _move(self, movers: list[Snake]):
        """Resolve one simultaneous sub-step.

        Rules: walls and bodies kill; in a head-on crash (same target cell, or two heads swapping
        places) the strictly longer snake survives, equal lengths all die. A shield *bounces* its
        owner: the move is skipped and the shield consumed, so snakes never overlap. A kill is
        credited only if the killer survives the sub-step.
        """
        t = self.tick
        nh = {s.id: (s.head[0] + DIRS[s.direction][0], s.head[1] + DIRS[s.direction][1]) for s in movers}
        mover_ids = {s.id for s in movers}
        dead: dict[str, Optional[str]] = {}  # snake id -> killer id (or None)
        cause: dict[str, tuple[str, Optional[str]]] = {}
        bounced: set[str] = set()

        # 1) Head-on crashes: several heads into one cell, or two heads swapping places.
        groups: dict[Cell, list[Snake]] = {}
        for s in movers:
            groups.setdefault(nh[s.id], []).append(s)
        clashes = [g for g in groups.values() if len(g) > 1]
        clashes += [[a, b] for a in movers for b in movers
                    if a.id < b.id and nh[a.id] == b.head and nh[b.id] == a.head]
        for group in clashes:
            contenders = []
            for s in group:
                if s.id in dead or s.id in bounced:
                    continue
                if s.shielded(t):
                    bounced.add(s.id)
                else:
                    contenders.append(s)
            if len(contenders) < 2:
                continue
            top = max(len(s.body) for s in contenders)
            winners = [s for s in contenders if len(s.body) == top]
            winner = winners[0].id if len(winners) == 1 else None
            for s in contenders:
                if s.id != winner:
                    dead[s.id], cause[s.id] = winner, ("head_on", winner)
        head_on_dead = set(dead)

        # 2) Walls and bodies. Bounced snakes stay put (their tail no longer vacates), which can
        #    create new collisions, so iterate until no new bounce happens.
        for _ in range(len(movers) + 1):
            occ: dict[Cell, str] = {}
            for o in self.snakes.values():
                if not o.alive or o.id in head_on_dead:
                    continue
                vacates = o.id in mover_ids and o.id not in bounced and o.grow == 0 and len(o.body) > 1
                for c in (o.body[:-1] if vacates else o.body):
                    occ.setdefault(c, o.id)
            new_bounce = False
            for s in movers:
                if s.id in dead or s.id in bounced:
                    continue
                h = nh[s.id]
                owner = occ.get(h) if self.in_bounds(h) else "#wall"
                if owner is None:
                    continue
                if s.shielded(t):
                    bounced.add(s.id)
                    new_bounce = True
                elif owner == "#wall":
                    dead[s.id], cause[s.id] = None, ("wall", None)
                else:
                    killer = owner if owner != s.id else None
                    dead[s.id], cause[s.id] = killer, ("self" if owner == s.id else "body", owner)
            if not new_bounce:
                break

        # 3) Apply.
        for sid in bounced:
            self.snakes[sid].shield_until = 0
            self._emit("shield_saved", sid)
        for sid, (why, by) in cause.items():
            self._emit("death", sid, cause=why, by=by)
        for s in movers:
            if s.id in dead or s.id in bounced:
                continue
            s.body.insert(0, nh[s.id])
            if s.grow > 0:
                s.grow -= 1
            else:
                s.body.pop()
            it = self.item_at(s.head)
            if it:
                self._pickup(s, it)
        for sid, killer in dead.items():
            self._kill(self.snakes[sid], killer if killer not in dead else None)

    def _pickup(self, s: Snake, it: Item):
        del self.items[it.id]
        s.score += ITEM_POINTS[it.kind]
        if it.kind in ("food", "gold"):
            s.grow += 1
        elif it.kind == "boost":
            s.boost_until = self.tick + self._s(self.cfg.boost_s)
        elif it.kind == "shield":
            s.shield_until = self.tick + self._s(self.cfg.shield_s)
        self._emit("eat", s.id, item=it.kind, item_id=it.id, points=ITEM_POINTS[it.kind])

    def _kill(self, s: Snake, killer: Optional[str]):
        s.alive, s.deaths = False, s.deaths + 1
        s.respawn_at = self.tick + self._s(self.cfg.respawn_s)
        cells = [c for c in s.body[1:] if self.in_bounds(c)]
        s.body, s.input_queue = [], []
        s.boost_until = s.shield_until = 0
        occ = self.occupied()
        for c in cells[::2][:10]:  # body turns into food (every other cell, capped)
            if c not in occ and not self.item_at(c):
                self._new_item("food", c, ttl_s=20)
        if killer and killer in self.snakes and killer != s.id:
            k = self.snakes[killer]
            k.score += KILL_POINTS
            k.kills += 1
            self._emit("kill", killer, victim=s.id, points=KILL_POINTS)

    def _housekeeping(self):
        for it in list(self.items.values()):
            if it.expires_at is not None and self.tick >= it.expires_at:
                del self.items[it.id]
                if it.kind == "gold":
                    self._emit("gold_expired", None, item_id=it.id)
        for s in self.snakes.values():
            if not s.alive and s.respawn_at is not None and self.tick >= s.respawn_at:
                self._spawn(s)
        if self.tick >= self._next_gold:
            if not any(it.kind == "gold" for it in self.items.values()):
                pos = self._random_free(margin=2)
                if pos:
                    g = self._new_item("gold", pos, ttl_s=self.cfg.gold_ttl_s)
                    self._emit("gold_spawn", None, item_id=g.id, x=pos[0], y=pos[1])
            self._next_gold = self.tick + self._s(self.cfg.gold_every_s)
        if self.tick >= self._next_powerup:
            kind = self.rng.choice(["boost", "shield"])
            if not any(it.kind == kind for it in self.items.values()):
                pos = self._random_free(margin=1)
                if pos:
                    self._new_item(kind, pos, ttl_s=self.cfg.powerup_ttl_s)
            self._next_powerup = self.tick + self._s(self.cfg.powerup_every_s)
        self._refill_food()

    # --------------------------------------------------------------- snapshots
    def snapshot(self) -> dict:
        return {
            "tick": self.tick,
            "tps": self.cfg.tps,
            "w": self.w,
            "h": self.h,
            "time_left": round(self.time_left_s, 1),
            "over": self.over,
            "snakes": [
                {
                    "id": s.id, "name": s.name, "color": s.color, "kind": s.kind, "persona": s.persona,
                    "alive": s.alive, "body": s.body, "dir": s.direction, "score": s.score,
                    "kills": s.kills, "deaths": s.deaths,
                    "shield": s.shielded(self.tick), "boost": s.boosted(self.tick),
                    "respawn_in": (round((s.respawn_at - self.tick) / self.cfg.tps, 1)
                                   if not s.alive and s.respawn_at is not None else None),
                }
                for s in self.snakes.values()
            ],
            "items": [
                {"id": it.id, "kind": it.kind, "x": it.pos[0], "y": it.pos[1],
                 "ttl": (round((it.expires_at - self.tick) / self.cfg.tps, 1) if it.expires_at else None)}
                for it in self.items.values()
            ],
        }
