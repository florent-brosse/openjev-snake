# Databricks notebook source
# MAGIC %md
# MAGIC # Delta Serpents — export selected decisions as MLflow traces
# MAGIC The app picks the decisions worth inspecting (a random sample, the uncertain ones, and every decision of the
# MAGIC 2 s before a death) and records them in `decision_traces`. This job turns each one into an MLflow trace with
# MAGIC its real timestamps: `snake_decision` → `perception (code)` + `openjev.systemone`, tagged with `reason` and
# MAGIC `outcome` (died_within_2s / survived / game_over). It runs automatically at the end of each match.

# COMMAND ----------

# MAGIC %pip install -q "mlflow-skinny[databricks]>=3.1"
# MAGIC %restart_python

# COMMAND ----------

import json
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.abspath("../app"))
import mlflow
from decision_log import array_schema
from tracing import write_trace

dbutils.widgets.text("catalog", "")  # required: your Unity Catalog catalog
dbutils.widgets.text("schema", "openjev_demo")
dbutils.widgets.text("experiment_id", "")
dbutils.widgets.text("max_traces", "500")
CATALOG, SCHEMA, EXPERIMENT = (dbutils.widgets.get(k).strip() for k in ("catalog", "schema", "experiment_id"))
assert CATALOG and EXPERIMENT, "Set the `catalog` and `experiment_id` widgets (or job parameters)."
P = f"{CATALOG}.{SCHEMA}"
spark.conf.set("spark.sql.session.timeZone", "UTC")
mlflow.set_tracking_uri("databricks")

# COMMAND ----------

rows = spark.sql(f"""
  SELECT t.ts, t.game_id, t.mode, t.tick, t.snake_id, t.reason, t.outcome, t.persona, t.order_text, t.order_kind,
         d.model, d.state, d.candidates, d.choice, d.confidence, d.entropy_nats, d.outlook_score, d.danger,
         d.input_tokens, d.latency_ms
  FROM {P}.decision_traces t
  JOIN {P}.decisions d ON d.game_id = t.game_id AND d.snake_id = t.snake_id AND d.ts = t.ts
  LEFT ANTI JOIN {P}.trace_exports x ON x.game_id = t.game_id AND x.snake_id = t.snake_id AND x.ts = t.ts
  ORDER BY t.ts
  LIMIT {int(dbutils.widgets.get("max_traces"))}
""").collect()
print(f"{len(rows)} decisions to export")

exported, failed = [], 0
for r in rows:
    d = r.asDict(recursive=True)
    d["state"] = json.loads(d["state"]) if isinstance(d.get("state"), str) else d.get("state")
    try:
        trace_id = write_trace(mlflow, EXPERIMENT, d)
        exported.append({"ts": d["ts"].replace(tzinfo=timezone.utc).isoformat(), "game_id": d["game_id"],
                         "snake_id": d["snake_id"], "tick": d["tick"], "trace_id": trace_id,
                         "exported_at": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        failed += 1
        print("failed:", d["game_id"], d["snake_id"], d["tick"], exc)

if hasattr(mlflow, "flush_trace_async_logging"):
    mlflow.flush_trace_async_logging()
if exported:
    (spark.sql(f"SELECT inline(from_json(:rows, '{array_schema('trace_exports')}'))", args={"rows": json.dumps(exported)})
     .write.mode("append").saveAsTable(f"{P}.trace_exports"))
print(f"exported {len(exported)} traces to experiment {EXPERIMENT} ({failed} failed)")
