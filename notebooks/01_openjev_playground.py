# Databricks notebook source
# MAGIC %md
# MAGIC # OpenJev playground on Databricks
# MAGIC
# MAGIC **OpenJev** is a *decision model*: instead of generating text, it reads a **state** and answers
# MAGIC **typed questions** with probabilities — in one fast call, with `output_tokens = 0`.
# MAGIC
# MAGIC | type | answers | example |
# MAGIC |---|---|---|
# MAGIC | `choice` | one of N options + a probability per option + confidence | which team should handle this ticket? |
# MAGIC | `score` | a level on an ordered scale (probability-weighted) | how urgent is it? |
# MAGIC | `noul` | probability of *yes* | is the customer angry? |
# MAGIC
# MAGIC On Databricks it is served through the Unity AI Gateway **System One** route:
# MAGIC `POST /ai-gateway/typesafe/v1/systemone` with `model = system.ai.openjev-qwen35-4b`
# MAGIC (TypeSafe's `system.ai.typesafeai-jev-1-14-0` uses the same API once its preview is enabled).

# COMMAND ----------

# MAGIC %pip install -q "databricks-sdk>=0.40"
# MAGIC %restart_python

# COMMAND ----------

import os
import sys

sys.path.append(os.path.abspath("../app"))
import pandas as pd
from systemone import SystemOneClient, choice, noul, score

dbutils.widgets.text("catalog", "")  # required: your Unity Catalog catalog
dbutils.widgets.text("schema", "openjev_demo")
dbutils.widgets.text("model", "system.ai.openjev-qwen35-4b")
CATALOG, SCHEMA, MODEL = (dbutils.widgets.get(k) for k in ("catalog", "schema", "model"))
assert CATALOG, "Set the `catalog` widget (or job parameter) to a catalog you can write to."

jev = SystemOneClient(model=MODEL)  # authenticates as you (notebook identity)


def show(resp):
    """Pretty-print a System One response as one row per (question, option)."""
    rows = []
    for qid, a in resp["answers"].items():
        if a["type"] == "noul":
            rows.append({"question": qid, "option": "yes", "probability": a["noul"], "picked": a["noul"] >= 0.5})
        else:
            top = a.get("choice") or max(a["probabilities"], key=a["probabilities"].get)
            labels = a.get("legend") or {}
            for k, p in sorted(a["probabilities"].items(), key=lambda kv: -kv[1]):
                rows.append({"question": qid, "option": labels.get(k, k), "probability": round(p, 4), "picked": k == top})
    print(f"{resp.get('model')} · {resp['latency_ms']:.0f} ms · {resp['usage']['input_tokens']} input tokens")
    return pd.DataFrame(rows)

# COMMAND ----------

# MAGIC %md ## 1. Three decisions in one call: ticket triage

# COMMAND ----------

ticket = {"message": "My card was charged twice for order #4471, refund the duplicate today please — third time I ask!",
          "customer_tier": "gold"}
questions = {
    "team": choice("Which team should handle this ticket?", {
        "billing": "Payments, charges, refunds and invoices.",
        "shipping": "Delivery, tracking and lost parcels.",
        "tech": "Bugs, login problems and app errors.",
        "sales": "New purchases and pricing questions."}),
    "urgency": score("How urgent is this ticket?", ["can wait", "this week", "today", "immediately"]),
    "angry": noul("Is the customer angry or frustrated?"),
}
display(show(jev.call(ticket, questions)))

# COMMAND ----------

# MAGIC %md ## 2. Steering: the same situation judged under different instructions (personas / policies)

# COMMAND ----------

situation = ("A snake of length 9 sees gold (+5) 11 steps away, but a longer rival is closer to it. "
             "Uncontested food is 4 steps away. The rival's head is 3 steps away.")
options = {"eat_food": "Eat the uncontested food 4 steps away.",
           "race_gold": "Race for the gold even though the rival is closer.",
           "hunt": "Cut off the longer rival to make it crash.",
           "flee": "Move away from the rival into open space.",
           "survive": "Circle safely and wait."}
rows = []
for persona in ["an aggressive hunter that loves risky kills", "a cautious survivor", "a greedy eater obsessed with gold"]:
    r = jev.call({"situation": situation, "persona": persona},
                 {"intent": choice("Following the persona, which intent should the snake pursue?", options)})
    rows.append({"persona": persona, **{k: round(v, 3) for k, v in r["answers"]["intent"]["probabilities"].items()}})
display(pd.DataFrame(rows))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Sensitivity test: option order and wording
# MAGIC Probabilities are **relative to the options you supply** and uncalibrated. A good habit is to check
# MAGIC that reordering options doesn't matter and to see how much rewording does.

# COMMAND ----------

import random

base = {"intent": choice("Which intent should the snake pursue?", options)}
shuffled_opts = dict(random.Random(1).sample(list(options.items()), len(options)))
reworded = dict(options, race_gold="Go for the +5 gold — the rival is nearer, so you may lose the race.")
variants = {"original": base,
            "shuffled order": {"intent": choice("Which intent should the snake pursue?", shuffled_opts)},
            "reworded race_gold": {"intent": choice("Which intent should the snake pursue?", reworded)}}
display(pd.DataFrame([{"variant": n, **{k: round(v, 3) for k, v in jev.call({"situation": situation}, q)["answers"]["intent"]["probabilities"].items()}}
                      for n, q in variants.items()]))

# COMMAND ----------

# MAGIC %md ## 4. Batch decisions on a DataFrame → Delta (decisions become data)

# COMMAND ----------

from concurrent.futures import ThreadPoolExecutor

tickets = [
    "I was charged twice this month, please refund.",
    "The app crashes every time I open my invoices.",
    "Where is my parcel? Tracking hasn't moved in 6 days.",
    "Do you offer a discount for 500 seats?",
    "I can't log in since the last update, password reset doesn't work.",
    "My package arrived damaged, I want a replacement.",
    "Can I switch from monthly to yearly billing?",
    "Your website is down for me, error 502.",
    "We want to buy the enterprise plan, who do I talk to?",
    "Refund still not received after 3 weeks!!!",
    "The delivery driver left my parcel at the wrong address.",
    "How do I export my data to CSV?",
]


def triage(text):
    r = jev.call({"message": text}, {k: v for k, v in questions.items()})
    a = r["answers"]
    return {"message": text, "team": a["team"]["choice"], "team_confidence": a["team"]["confidence"],
            "urgency": a["urgency"]["score"], "angry": a["angry"]["noul"], "latency_ms": r["latency_ms"]}


with ThreadPoolExecutor(6) as pool:
    results = pd.DataFrame(list(pool.map(triage, tickets)))
display(results)
spark.createDataFrame(results).write.mode("overwrite").saveAsTable(f"{CATALOG}.{SCHEMA}.ticket_triage")
print(f"saved to {CATALOG}.{SCHEMA}.ticket_triage")

# COMMAND ----------

# MAGIC %md ## 5. Latency benchmark (sequential and parallel)

# COMMAND ----------

import statistics
import time


def bench(n, parallel):
    def one(_):
        return jev.call(ticket, questions)["latency_ms"]

    t = time.time()
    with ThreadPoolExecutor(parallel) as pool:
        lat = sorted(pool.map(one, range(n)))
    return {"parallel": parallel, "calls": n, "p50_ms": statistics.median(lat), "p90_ms": lat[int(0.9 * (n - 1))],
            "max_ms": lat[-1], "throughput_per_s": round(n / (time.time() - t), 1)}


jev.call(ticket, questions)  # warm-up
display(pd.DataFrame([bench(10, 1), bench(24, 4), bench(32, 8)]))
