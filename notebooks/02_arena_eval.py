# Databricks notebook source
# MAGIC %md
# MAGIC # Delta Serpents — arena evaluation
# MAGIC Headless **lockstep** matches: the simulation pauses while the brains decide, so results only depend on the
# MAGIC seed and the model's answers. OpenJev personas play against a rule-based heuristic bot. Every decision and
# MAGIC match is appended to the same Delta tables as the app, and summary metrics go to MLflow.

# COMMAND ----------

# MAGIC %pip install -q "databricks-sdk>=0.40" "mlflow-skinny[databricks]>=2.16"
# MAGIC %restart_python

# COMMAND ----------

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.append(os.path.abspath("../app"))
import mlflow
import pandas as pd
from decision_log import array_schema
from serpents.engine import GameConfig
from serpents.session import run_lockstep
from systemone import SystemOneClient

dbutils.widgets.text("catalog", "")  # required: your Unity Catalog catalog
dbutils.widgets.text("schema", "openjev_demo")
dbutils.widgets.text("matches", "6")
dbutils.widgets.text("duration_s", "60")
dbutils.widgets.text("model", "system.ai.openjev-qwen35-4b")
dbutils.widgets.text("experiment_id", "")  # set by the bundle; falls back to /Users/<you>/delta_serpents_arena
CATALOG, SCHEMA, MODEL = (dbutils.widgets.get(k) for k in ("catalog", "schema", "model"))
MATCHES, DURATION = int(dbutils.widgets.get("matches")), int(dbutils.widgets.get("duration_s"))
PREFIX = f"{CATALOG}.{SCHEMA}"
assert CATALOG, "Set the `catalog` widget (or job parameter) to a catalog you can write to."

LINEUP = [{"persona": "viper", "brain": "openjev"}, {"persona": "turtle", "brain": "openjev"},
          {"persona": "glutton", "brain": "openjev"}, {"persona": "glutton", "brain": "heuristic"}]
client = SystemOneClient(model=MODEL)

# COMMAND ----------

iso = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
decisions, events, games = [], [], []


def on_decision(game_id, ai, d):
    decisions.append({"ts": iso(d.ts), "game_id": game_id, "mode": "arena", "tick": d.tick, "snake_id": ai.snake_id,
                      "brain": d.brain, "persona": ai.persona, "order_text": None, "model": d.model,
                      "state": json.dumps(d.state) if d.state else None, "candidates": d.candidates, "choice": d.choice,
                      "confidence": d.confidence, "entropy_nats": d.entropy_nats, "outlook_score": d.outlook,
                      "danger": d.danger, "input_tokens": d.input_tokens, "latency_ms": d.latency_ms})


def on_event(e):
    events.append({"ts": iso(time.time()), "game_id": e["game_id"], "tick": e["tick"], "snake_id": e.get("snake_id"),
                   "type": e["type"], "detail": json.dumps({k: v for k, v in e.items() if k not in ("game_id", "tick", "type", "snake_id")})})


rows = []
for m in range(MATCHES):
    t0 = time.time()
    res = run_lockstep(GameConfig(seed=1000 + m, duration_s=DURATION), LINEUP, client=client,
                       on_decision=on_decision, on_event=on_event)
    t1 = time.time()
    games.append({"game_id": res["game_id"], "mode": "arena", "started_at": iso(t0), "ended_at": iso(t1),
                  "ticks": res["ticks"], "winner": res["winner"],
                  "players": [{**{k: p[k] for k in ("snake_id", "name", "persona", "brain", "score", "kills", "deaths")},
                               "kind": "ai"} for p in res["players"]]})
    for p in res["players"]:
        rows.append({"match": m, "winner": res["winner"] == p["snake_id"], **p})
    print(f"match {m + 1}/{MATCHES}: winner={res['winner']} in {t1 - t0:.0f}s")

df = pd.DataFrame(rows)
df["label"] = df["persona"] + " · " + df["brain"]
summary = (df.groupby("label").agg(win_rate=("winner", "mean"), avg_score=("score", "mean"), avg_kills=("kills", "mean"),
                                   avg_deaths=("deaths", "mean"), decisions=("decisions", "sum"), errors=("errors", "sum"))
           .sort_values("avg_score", ascending=False).reset_index())
display(summary)

# COMMAND ----------

# MAGIC %md ## Persist to Delta (same tables as the app)

# COMMAND ----------

for table, data in (("decisions", decisions), ("events", events), ("games", games)):
    if data:
        (spark.sql(f"SELECT inline(from_json(:rows, '{array_schema(table)}'))", args={"rows": json.dumps(data, default=str)})
         .write.mode("append").saveAsTable(f"{PREFIX}.{table}"))
        print(f"appended {len(data)} rows to {PREFIX}.{table}")

# COMMAND ----------

# MAGIC %md ## Log to MLflow

# COMMAND ----------

user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_tracking_uri("databricks")
if dbutils.widgets.get("experiment_id"):
    mlflow.set_experiment(experiment_id=dbutils.widgets.get("experiment_id"))
else:
    mlflow.set_experiment(f"/Users/{user}/delta_serpents_arena")
lat = pd.Series([d["latency_ms"] for d in decisions if d["latency_ms"]])
with mlflow.start_run(run_name=f"arena-{MATCHES}x{DURATION}s"):
    mlflow.log_params({"model": MODEL, "matches": MATCHES, "duration_s": DURATION, "lineup": json.dumps(LINEUP)})
    mlflow.log_metrics({"latency_p50_ms": float(lat.median()), "latency_p90_ms": float(lat.quantile(0.9)),
                        "openjev_decisions": int(len(lat))})
    for r in summary.itertuples():
        key = r.label.replace(" · ", "_")
        mlflow.log_metrics({f"{key}_win_rate": float(r.win_rate), f"{key}_avg_score": float(r.avg_score),
                            f"{key}_avg_deaths": float(r.avg_deaths)})
    mlflow.log_table(summary, "summary.json")
print("logged MLflow run")
