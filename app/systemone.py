"""Tiny client for the Databricks Unity AI Gateway System One route (OpenJev / Jev).

POST /ai-gateway/typesafe/v1/systemone
{"model": "system.ai.openjev-qwen35-4b", "state": ..., "questions": {id: {type, instructions, criteria}}}

Auth goes through the Databricks SDK (app service principal inside a Databricks
App, the notebook/CLI user elsewhere) — no tokens are handled here.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any, Optional

ROUTE = "/ai-gateway/typesafe/v1/systemone"
DEFAULT_MODEL = os.getenv("OPENJEV_MODEL", "system.ai.openjev-qwen35-4b")
RETRYABLE = (429, 500, 502, 503, 504)


class SystemOneError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------- question builders
def choice(instructions: str, options: dict[str, Optional[str]]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


def noul(instructions: str, true: Optional[str] = None, false: Optional[str] = None) -> dict:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "yes", "false": false or "no"}
    return q


def entropy_nats(probabilities: dict[str, float]) -> float:
    ps = [p for p in probabilities.values() if p and p > 0]
    total = sum(ps) or 1.0
    return -sum((p / total) * math.log(p / total) for p in ps)


class SystemOneClient:
    """Calls the System One route with SDK-managed auth headers.

    We use ``requests`` directly (instead of ``api_client.do``) so that the gateway's real HTTP
    status and validation details (e.g. 422 ``detail``) are preserved, and so retries/timeouts are
    tuned for a real-time game: a late decision is worthless.
    """

    def __init__(self, model: Optional[str] = None, workspace_client=None, max_concurrency: int = 6,
                 retries: int = 2, profile: Optional[str] = None, timeout_s: float = 10.0):
        self.model = model or DEFAULT_MODEL
        self.retries = retries
        self.timeout_s = timeout_s
        self._w = workspace_client
        self._profile = profile
        self._session = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._max_concurrency = max_concurrency
        self.calls = 0
        self.errors = 0

    @property
    def w(self):
        if self._w is None:
            from databricks.sdk import WorkspaceClient

            self._w = WorkspaceClient(profile=self._profile) if self._profile else WorkspaceClient()
        return self._w

    def _post(self, body: dict) -> tuple[int, Any]:
        """One HTTP attempt. Returns (status_code, parsed JSON or text)."""
        import requests

        if self._session is None:
            self._session = requests.Session()
        headers = {"Content-Type": "application/json", **self.w.config.authenticate()}
        r = self._session.post(self.w.config.host.rstrip("/") + ROUTE, json=body, headers=headers,
                               timeout=self.timeout_s)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text

    def call(self, state: Any, questions: dict, model: Optional[str] = None) -> dict:
        """Synchronous call. Returns the response plus ``latency_ms`` and per-choice ``entropy_nats``."""
        body = {"model": model or self.model, "state": state, "questions": questions}
        status: Optional[int] = None
        detail: Any = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                status, payload = self._post(body)
            except Exception as exc:  # network error / timeout: retryable
                status, payload = None, str(exc)
            if status == 200 and isinstance(payload, dict):
                self.calls += 1
                payload["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                for ans in (payload.get("answers") or {}).values():
                    if isinstance(ans, dict) and ans.get("probabilities"):
                        ans["entropy_nats"] = round(entropy_nats(ans["probabilities"]), 4)
                return payload
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            if (status is not None and status not in RETRYABLE) or attempt == self.retries:
                break
            time.sleep(0.15 * (2 ** attempt))
        self.errors += 1
        raise SystemOneError(f"System One call failed ({status}): {str(detail)[:500]}", status)

    async def acall(self, state: Any, questions: dict, model: Optional[str] = None) -> dict:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrency)
        async with self._sem:
            return await asyncio.to_thread(self.call, state, questions, model)
