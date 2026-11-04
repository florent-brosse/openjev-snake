"""Delta Serpents — a Databricks App where OpenJev drives AI snakes in real time."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from decision_log import DecisionLogger
from serpents.engine import DIRS, GameConfig
from serpents.session import GameSession
from systemone import DEFAULT_MODEL, SystemOneClient, SystemOneError
from tracing import DecisionTracer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC = Path(__file__).parent / "static"
MODEL = os.getenv("OPENJEV_MODEL", DEFAULT_MODEL)
ALLOWED_MODELS = {m.strip() for m in os.getenv("OPENJEV_ALLOWED_MODELS", MODEL).split(",") if m.strip()} | {MODEL}
TABLE_PREFIX = os.getenv("OPENJEV_TABLE_PREFIX", "")
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
DASHBOARD_URL = os.getenv("OPENJEV_DASHBOARD_URL", "")
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "6"))

client = SystemOneClient(model=MODEL, max_concurrency=int(os.getenv("OPENJEV_MAX_CONCURRENCY", "6")),
                         profile=os.getenv("DATABRICKS_CONFIG_PROFILE") or None, retries=1, timeout_s=4.0)
dlog = DecisionLogger(TABLE_PREFIX, WAREHOUSE_ID)
tracer = DecisionTracer(dlog, export_job_id=os.getenv("TRACE_EXPORT_JOB_ID"), experiment_id=os.getenv("MLFLOW_EXPERIMENT_ID"),
                        sample_rate=float(os.getenv("TRACE_SAMPLE_RATE", "0.05")))
sessions: dict[str, GameSession] = {}

app = FastAPI(title="Delta Serpents — OpenJev on Databricks")


# ------------------------------------------------------------------- models
class AIConfig(BaseModel):
    persona: str = Field("viper", max_length=40)
    custom: str = Field("", max_length=400)
    order: str = Field("", max_length=300)
    brain: str = Field("openjev", pattern="^(openjev|heuristic)$")


class NewGame(BaseModel):
    ais: list[AIConfig] = Field(default_factory=lambda: [AIConfig()], min_length=1, max_length=4)
    spectator: bool = False
    duration_s: int = Field(120, ge=30, le=600)
    tps: int = Field(8, ge=4, le=14)
    seed: Optional[int] = None
    decision_interval_s: float = Field(0.5, ge=0.2, le=3.0)


class Order(BaseModel):
    snake_id: str = Field(..., max_length=40)
    order: str = Field("", max_length=300)


class PlaygroundRequest(BaseModel):
    state: object
    questions: dict
    model: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _gc_sessions():
    now = time.time()
    for sid, s in list(sessions.items()):
        if (s.ended_at and now - s.ended_at > 300) or (not s.started_at and now - s.created > 60):
            sessions.pop(sid, None)


def _get(game_id: str) -> GameSession:
    s = sessions.get(game_id)
    if not s:
        raise HTTPException(404, "game not found")
    return s


# ------------------------------------------------------------------- routes
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/model")
def model_info():
    return {
        "model": MODEL, "allowed_models": sorted(ALLOWED_MODELS), "route": "/ai-gateway/typesafe/v1/systemone",
        "calls": client.calls, "errors": client.errors, "logging": dlog.enabled, "table_prefix": TABLE_PREFIX,
        "rows_written": dlog.written, "log_error": dlog.last_error, "dashboard_url": DASHBOARD_URL,
        "active_sessions": sum(1 for s in sessions.values() if s.started_at and not s.ended_at),
        "tracing": {"enabled": tracer.enabled, "experiment_id": tracer.experiment_id, "selected": tracer.selected,
                    "export_job_id": tracer.export_job_id, "export_runs": tracer.export_runs,
                    "sample_rate": tracer.sample_rate, "error": tracer.last_error},
    }


@app.post("/api/game")
async def new_game(body: NewGame):
    _gc_sessions()
    if sum(1 for s in sessions.values() if not s.ended_at) >= MAX_SESSIONS:
        raise HTTPException(429, "Too many matches running right now, try again in a minute.")
    cfg = GameConfig(duration_s=body.duration_s, tps=body.tps,
                     seed=body.seed if body.seed is not None else int(time.time() * 1000) % 100000)
    mode = "spectator" if body.spectator else "human_vs_ai"
    s = GameSession(cfg, [a.model_dump() for a in body.ais], with_human=not body.spectator, client=client,
                    logger=dlog, decision_interval_s=body.decision_interval_s, mode=mode, tracer=tracer)
    sessions[s.id] = s
    return {"game_id": s.id, "seed": cfg.seed, "snakes": [sn.id for sn in s.game.snakes.values()]}


@app.post("/api/game/{game_id}/order")
async def set_order(game_id: str, body: Order):
    if not _get(game_id).set_order(body.snake_id, body.order):
        raise HTTPException(404, "unknown AI snake")
    return {"ok": True}


@app.post("/api/game/{game_id}/input")
async def human_input(game_id: str, body: dict):
    d = body.get("dir")
    if d not in DIRS:
        raise HTTPException(422, "dir must be up/down/left/right")
    _get(game_id).human_input(d)
    return {"ok": True}


@app.websocket("/ws/game/{game_id}")
async def ws_game(ws: WebSocket, game_id: str):
    s = sessions.get(game_id)
    await ws.accept()
    if not s:
        await ws.close(code=4404)
        return
    q = s.subscribe()

    async def sender():
        while True:
            await ws.send_text(await q.get())

    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "dir" and msg.get("dir") in DIRS:
                s.human_input(msg["dir"])
            elif msg.get("type") == "order":
                s.set_order(str(msg.get("snake_id", ""))[:40], str(msg.get("order", ""))[:300])
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        send_task.cancel()
        s.unsubscribe(q)


@app.get("/sse/game/{game_id}")
async def sse_game(game_id: str, request: Request):
    """Fallback transport when WebSockets are unavailable: frames via Server-Sent Events."""
    s = _get(game_id)
    q = s.subscribe()

    async def stream():
        try:
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(q.get(), timeout=10)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            s.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/playground")
async def playground(body: PlaygroundRequest):
    model = body.model or MODEL
    if model not in ALLOWED_MODELS:
        raise HTTPException(422, f"model must be one of {sorted(ALLOWED_MODELS)}")
    if not body.questions or len(body.questions) > 8:
        raise HTTPException(422, "provide 1 to 8 questions")
    if len(json.dumps(body.state)) > 20000 or len(json.dumps(body.questions)) > 20000:
        raise HTTPException(413, "state and questions are limited to 20,000 characters each")
    try:
        return await client.acall(body.state, body.questions, model, tags={"app": "delta-serpents", "kind": "playground"})
    except SystemOneError as exc:
        raise HTTPException(exc.status or 502, str(exc))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("DATABRICKS_APP_PORT", "8000")), log_level="info")
