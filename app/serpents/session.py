"""Game sessions: real-time (async, for the app) and lockstep (headless, for evaluation)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from .brains import Decision, HeuristicBrain, OpenJevBrain
from .controller import Intent, choose_direction
from .engine import Game, GameConfig

log = logging.getLogger("serpents")

COLORS = ["#ff4d6d", "#4cc9f0", "#80ed99", "#ffd166", "#c77dff"]
AI_NAMES = {"viper": "Viper", "turtle": "Turtle", "glutton": "Glutton"}


@dataclass
class AIPlayer:
    snake_id: str
    persona: str
    order: str = ""
    order_kind: Optional[str] = None  # None = not classified yet
    brain_kind: str = "openjev"  # openjev | heuristic
    intent: Optional[Intent] = None
    last_decision: Optional[Decision] = None
    danger: float = 0.0
    thinking: bool = False
    invalidated: asyncio.Event = field(default_factory=asyncio.Event)
    decisions: int = 0
    errors: int = 0


def build_players(ais: list[dict], with_human: bool) -> tuple[list[dict], list[AIPlayer]]:
    players, ai_players = [], []
    color_i = 0
    if with_human:
        players.append({"id": "you", "name": "You", "color": COLORS[0], "kind": "human"})
        color_i = 1
    used = {}
    for a in ais:
        persona = a.get("persona", "viper")
        base = AI_NAMES.get(persona, (a.get("name") or "Custom")[:12])
        used[base] = used.get(base, 0) + 1
        name = base if used[base] == 1 else f"{base} {used[base]}"
        sid = name.lower().replace(" ", "_")
        players.append({"id": sid, "name": name, "color": COLORS[color_i % len(COLORS)], "kind": "ai", "persona": persona})
        ai_players.append(AIPlayer(snake_id=sid, persona=persona if persona in AI_NAMES else (a.get("custom") or persona),
                                   order=a.get("order", ""), brain_kind=a.get("brain", "openjev")))
        color_i += 1
    return players, ai_players


class GameSession:
    """Server-authoritative real-time match. Frames are pushed to subscriber queues."""

    def __init__(self, cfg: GameConfig, ais: list[dict], with_human: bool, client, logger=None,
                 decision_interval_s: float = 0.5, mode: str = "human_vs_ai"):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.mode = mode
        players, self.ai = build_players(ais, with_human)
        self.ai_by_id = {a.snake_id: a for a in self.ai}
        self.game = Game(cfg, players)
        self.client = client
        self.openjev = OpenJevBrain(client)
        self.heuristic = HeuristicBrain()
        self.logger = logger
        self.interval = decision_interval_s
        self.subscribers: set[asyncio.Queue] = set()
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.countdown = 3.0
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.created = time.time()

    # ------------------------------------------------------------ subscribers
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.subscribers.add(q)
        if not self._running and self.ended_at is None:
            self._running = True
            self._tasks.append(asyncio.create_task(self._run()))
        else:
            msg = self._frame([])
            if self.ended_at is not None:
                msg.update(type="game_over", winner=self.game.winner())
            q.put_nowait(json.dumps(msg, separators=(",", ":")))
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    def _broadcast(self, msg: dict):
        data = json.dumps(msg, separators=(",", ":"))
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest frame for slow clients
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)

    # ------------------------------------------------------------------ input
    def human_input(self, direction: str):
        self.game.queue_direction("you", direction)

    def set_order(self, snake_id: str, order: str) -> bool:
        a = self.ai_by_id.get(snake_id)
        if not a:
            return False
        a.order = order[:300]
        a.order_kind = None  # re-classified by the decision loop before the next decision
        a.invalidated.set()  # re-decide right away with the new order
        if self.logger:
            self.logger.event(self.id, self.game.tick, snake_id, "order", {"order": a.order})
        return True

    # ------------------------------------------------------------------- loop
    def _ai_direction(self, game: Game, snake) -> Optional[str]:
        a = self.ai_by_id.get(snake.id)
        if not a:
            return None
        d, valid = choose_direction(game, snake, a.intent, strict=a.danger > 0.6)
        if not valid:
            a.intent = None
            a.invalidated.set()
        return d

    def _frame(self, events: list[dict]) -> dict:
        snap = self.game.snapshot()
        snap["type"] = "frame"
        snap["events"] = events
        snap["countdown"] = round(self.countdown, 1) if self.countdown > 0 else 0
        snap["ai"] = {
            a.snake_id: {
                "persona": a.persona, "order": a.order, "order_kind": a.order_kind, "brain": a.brain_kind,
                "thinking": a.thinking,
                "intent": a.intent.id if a.intent else None,
                "decision": a.last_decision.public() if a.last_decision else None,
                "decisions": a.decisions, "errors": a.errors,
            } for a in self.ai
        }
        return snap

    async def _run(self):
        tick_s = 1.0 / self.cfg.tps
        # countdown
        while self.countdown > 0:
            self._broadcast(self._frame([]))
            await asyncio.sleep(0.25)
            self.countdown -= 0.25
        self.started_at = time.time()
        for a in self.ai:
            self._tasks.append(asyncio.create_task(self._decision_loop(a)))
        next_t = time.perf_counter()
        try:
            while not self.game.over:
                events = self.game.step(self._ai_direction)
                if self.logger and events:
                    for e in events:
                        self.logger.event(self.id, e["tick"], e.get("snake_id"), e["type"],
                                          {k: v for k, v in e.items() if k not in ("tick", "type", "snake_id")})
                self._broadcast(self._frame(events))
                next_t += tick_s
                await asyncio.sleep(max(0.0, next_t - time.perf_counter()))
                if not self.subscribers and time.time() - self.started_at > 5:
                    log.info("session %s abandoned", self.id)
                    break
        finally:
            self.ended_at = time.time()
            self._broadcast({**self._frame([]), "type": "game_over", "winner": self.game.winner()})
            if self.logger and self.game.over:  # abandoned matches would skew win rates
                self.logger.game(self)
            for t in self._tasks:
                if t is not asyncio.current_task():
                    t.cancel()

    async def _decision_loop(self, a: AIPlayer):
        while not self.game.over:
            snake = self.game.snakes[a.snake_id]
            if not snake.alive:
                a.intent, a.danger = None, 0.0
                await asyncio.sleep(0.2)
                continue
            a.invalidated.clear()
            a.thinking = True
            try:
                brain = self.openjev if a.brain_kind == "openjev" else self.heuristic
                if a.order_kind is None and a.brain_kind == "openjev":
                    try:
                        a.order_kind = await self.openjev.aclassify_order(a.order)
                    except Exception as exc:
                        a.order_kind = "none"
                        log.warning("order classification failed: %s", exc)
                    if self.logger and a.order:
                        self.logger.event(self.id, self.game.tick, a.snake_id, "order_kind",
                                          {"order": a.order, "kind": a.order_kind})
                dec = await brain.adecide(self.game, snake, a.persona, a.order, order_kind=a.order_kind)
                if self.game.snakes[a.snake_id].alive:
                    a.intent = dec.intent
                    a.danger = dec.danger or 0.0
                a.last_decision = dec
                a.decisions += 1
                if self.logger:
                    self.logger.decision(self.id, a, dec, mode=self.mode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep playing with the previous intent
                a.errors += 1
                log.warning("decision failed for %s: %s", a.snake_id, exc)
                await asyncio.sleep(1.0)
            finally:
                a.thinking = False
            try:
                await asyncio.wait_for(a.invalidated.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def summary(self) -> dict:
        return {
            "game_id": self.id, "mode": self.mode,
            "started_at": self.started_at, "ended_at": self.ended_at, "ticks": self.game.tick,
            "winner": self.game.winner(),
            "players": [{"snake_id": s.id, "name": s.name, "kind": s.kind,
                         "persona": self.ai_by_id[s.id].persona if s.id in self.ai_by_id else None,
                         "brain": self.ai_by_id[s.id].brain_kind if s.id in self.ai_by_id else "human",
                         "score": s.score, "kills": s.kills, "deaths": s.deaths}
                        for s in self.game.snakes.values()],
        }


# --------------------------------------------------------------------- lockstep
def run_lockstep(cfg: GameConfig, ais: list[dict], client=None, decision_every_ticks: int = 4,
                 on_decision: Optional[Callable[[str, AIPlayer, Decision], None]] = None,
                 on_event: Optional[Callable[[dict], None]] = None, max_workers: int = 4) -> dict:
    """Headless match: the simulation pauses while brains decide (deterministic given the answers).

    ``ais``: [{"persona": "viper", "brain": "openjev"|"heuristic", "order": ""}, ...]
    """
    players, ai_players = build_players(ais, with_human=False)
    game = Game(cfg, players)
    by_id = {a.snake_id: a for a in ai_players}
    openjev = OpenJevBrain(client) if client else None
    heuristic = HeuristicBrain()
    game_id = uuid.uuid4().hex[:12]

    def direction(g: Game, snake):
        a = by_id[snake.id]
        d, valid = choose_direction(g, snake, a.intent, strict=a.danger > 0.6)
        if not valid:
            a.intent = None
        return d

    def decide(a: AIPlayer) -> Optional[Decision]:
        snake = game.snakes[a.snake_id]
        if not snake.alive:
            return None
        brain = openjev if a.brain_kind == "openjev" and openjev else heuristic
        try:
            if a.order_kind is None:
                a.order_kind = openjev.classify_order(a.order) if (openjev and a.order) else "none"
            return brain.decide(game, snake, a.persona, a.order, order_kind=a.order_kind)
        except Exception as exc:
            a.errors += 1
            log.warning("lockstep decision failed: %s", exc)
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while not game.over:
            due = [a for a in ai_players if a.intent is None or game.tick % decision_every_ticks == 0]
            if due:
                for a, dec in zip(due, pool.map(decide, due)):
                    if dec:
                        a.intent, a.danger, a.last_decision = dec.intent, dec.danger or 0.0, dec
                        a.decisions += 1
                        if on_decision:
                            on_decision(game_id, a, dec)
            for e in game.step(direction):
                if on_event:
                    on_event({"game_id": game_id, **e})

    return {
        "game_id": game_id, "ticks": game.tick, "winner": game.winner(),
        "players": [{"snake_id": s.id, "name": s.name, "persona": by_id[s.id].persona, "brain": by_id[s.id].brain_kind,
                     "score": s.score, "kills": s.kills, "deaths": s.deaths, "decisions": by_id[s.id].decisions,
                     "errors": by_id[s.id].errors} for s in game.snakes.values()],
    }
