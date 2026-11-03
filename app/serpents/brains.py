"""Brains choose an Intent for an AI snake.

* OpenJevBrain  — asks the OpenJev decision model (3 questions in one call).
* HeuristicBrain — greedy rule-based baseline used for evaluation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .controller import Intent
from .engine import Game, Snake
from .intents import ORDER_KINDS, PERSONAS, build_candidates, build_questions, build_state, order_question


@dataclass
class Decision:
    snake_id: str
    tick: int
    choice: str
    intent: Intent
    candidates: list[dict]  # [{id, description, probability}]
    confidence: Optional[float] = None
    entropy_nats: Optional[float] = None
    outlook: Optional[float] = None  # 0 (losing) .. 2 (winning)
    outlook_probs: Optional[dict] = None
    danger: Optional[float] = None
    latency_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    model: Optional[str] = None
    brain: str = "openjev"
    state: Optional[dict] = None
    error: Optional[str] = None
    ts: float = field(default_factory=time.time)

    def public(self) -> dict:
        """Compact form sent to the browser."""
        return {
            "snake_id": self.snake_id, "tick": self.tick, "choice": self.choice, "brain": self.brain,
            "candidates": self.candidates, "confidence": self.confidence, "entropy": self.entropy_nats,
            "outlook": self.outlook, "outlook_probs": self.outlook_probs, "danger": self.danger,
            "latency_ms": self.latency_ms, "input_tokens": self.input_tokens, "error": self.error,
        }


def persona_text(persona: str, custom: str = "") -> str:
    return PERSONAS.get(persona, custom or persona or "A balanced snake player.")


class OpenJevBrain:
    name = "openjev"

    def __init__(self, client, model: Optional[str] = None):
        self.client = client
        self.model = model

    def classify_order(self, order: str) -> str:
        if not order.strip():
            return "none"
        ans = self.client.call({"commander_order": order}, order_question(), self.model)["answers"]["kind"]
        return ans.get("choice") if ans.get("choice") in ORDER_KINDS else "none"

    async def aclassify_order(self, order: str) -> str:
        if not order.strip():
            return "none"
        resp = await self.client.acall({"commander_order": order}, order_question(), self.model)
        choice = resp["answers"]["kind"].get("choice")
        return choice if choice in ORDER_KINDS else "none"

    def prepare(self, game: Game, me: Snake, persona: str, order: str, with_outlook: bool = True,
                order_kind: Optional[str] = None):
        cands = build_candidates(game, me, order_kind)
        state = build_state(game, me, persona_text(persona), order)
        return cands, state, build_questions(me, cands, with_outlook)

    def interpret(self, game: Game, me: Snake, cands: list[Intent], state: dict, resp: dict) -> Decision:
        ans = resp.get("answers", {})
        intent_ans = ans.get("intent", {})
        by_id = {c.id: c for c in cands}
        probs = {k: v for k, v in (intent_ans.get("probabilities") or {}).items() if k in by_id}
        chosen = intent_ans.get("choice") if intent_ans.get("choice") in by_id else max(probs, key=probs.get, default=cands[0].id)
        out = ans.get("outlook") or {}
        danger = (ans.get("danger") or {}).get("noul")
        return Decision(
            snake_id=me.id, tick=game.tick, choice=chosen, intent=by_id[chosen],
            candidates=sorted(({"id": c.id, "description": c.description, "probability": round(probs.get(c.id, 0.0), 4)}
                               for c in cands), key=lambda c: -c["probability"]),
            confidence=intent_ans.get("confidence"), entropy_nats=intent_ans.get("entropy_nats"),
            outlook=out.get("score"), outlook_probs=out.get("probabilities"), danger=danger,
            latency_ms=resp.get("latency_ms"), input_tokens=(resp.get("usage") or {}).get("input_tokens"),
            model=resp.get("model") or self.model, brain=self.name, state=state,
        )

    def decide(self, game: Game, me: Snake, persona: str, order: str, with_outlook: bool = True,
               order_kind: Optional[str] = None) -> Decision:
        cands, state, questions = self.prepare(game, me, persona, order, with_outlook, order_kind)
        resp = self.client.call(state, questions, self.model)
        return self.interpret(game, me, cands, state, resp)

    async def adecide(self, game: Game, me: Snake, persona: str, order: str, with_outlook: bool = True,
                      order_kind: Optional[str] = None) -> Decision:
        cands, state, questions = self.prepare(game, me, persona, order, with_outlook, order_kind)
        resp = await self.client.acall(state, questions, self.model)
        return self.interpret(game, me, cands, state, resp)


class HeuristicBrain:
    """Greedy baseline: closest uncontested item, else the closest item, else survive."""

    name = "heuristic"

    def decide(self, game: Game, me: Snake, persona: str = "", order: str = "", with_outlook: bool = False,
               order_kind: Optional[str] = None) -> Decision:
        cands = build_candidates(game, me)
        pick = None
        for c in cands:  # candidates list specials first, then foods by distance
            if c.kind in ("eat", "gold") and "lose the race" not in c.description and "Dead-end" not in c.description:
                pick = c if pick is None or _steps(c) < _steps(pick) else pick
        pick = pick or next((c for c in cands if c.kind == "eat"), None) or next(c for c in cands if c.kind == "survive")
        return Decision(
            snake_id=me.id, tick=game.tick, choice=pick.id, intent=pick, brain=self.name, latency_ms=0.0,
            candidates=[{"id": c.id, "description": c.description, "probability": 1.0 if c is pick else 0.0} for c in cands],
        )

    async def adecide(self, *args, **kwargs) -> Decision:
        return self.decide(*args, **kwargs)


def _steps(c: Intent) -> int:
    import re

    m = re.search(r"(\d+) steps away", c.description)
    return int(m.group(1)) if m else 999
