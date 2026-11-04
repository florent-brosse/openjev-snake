# Delta Serpents: OpenJev on Databricks

A real-time snake arena running as a **Databricks App**. You play against AI snakes, and each one decides what to do with **OpenJev**, a *decision model*. OpenJev doesn't generate text. You give it a situation and some typed questions, and it returns a probability for every possible answer.

It's a deployable demo (a Declarative Automation Bundle) containing the app, two jobs, a playground notebook and an AI/BI dashboard.

**Prerequisites:**
- A Databricks workspace with Unity Catalog, serverless compute and Databricks Apps.
- The Unity AI Gateway **System One** route, with the `databricks-openjev-qwen35-4b` endpoint available.
- A catalog you can create a schema in.

## How OpenJev is called on Databricks

OpenJev is served natively through the Unity AI Gateway **System One** route. The serving endpoint is `databricks-openjev-qwen35-4b`.

```
POST /ai-gateway/typesafe/v1/systemone
{ "model": "system.ai.openjev-qwen35-4b",
  "state": { ...anything describing the situation... },
  "questions": {
    "intent":  {"type": "choice", "instructions": "...", "criteria": {"eat_f3": "Eat food 4 steps away...", "hunt_red": "..."}},
    "outlook": {"type": "score",  "instructions": "...", "criteria": ["losing", "even", "winning"]},
    "danger":  {"type": "noul",   "instructions": "Is it about to die?"} } }
→ { "answers": {"intent": {"choice": "...", "probabilities": {...}, "confidence": 0.62}, ...},
    "usage": {"input_tokens": 1200, "output_tokens": 0} }
```

TypeSafe's `system.ai.typesafeai-jev-1-14-0` uses the same API. It's a one-line swap (`--var openjev_model=...`) once an account admin enables the **TypeSafe AI Models** preview.

## How the game works

1. **The game runs on the app's server.** `app/serpents/engine.py` ticks the arena 8 times a second (FastAPI + asyncio). Frames are sent to the browser over a WebSocket, with an SSE fallback. The browser only draws the board and sends your key presses.
2. **Perception is code** (`app/serpents/intents.py`). Every ~0.5 s each AI snake describes its situation in English:
   - its head and the rivals, the score gap, a 9×9 local view and the **full 32×24 arena map**;
   - its persona and any live commander order;
   - up to 12 candidate intents, each with computed facts. For example: *"Race for GOLD at (20,4): +5 points, 9 steps (0.56 points/step); Turtle is closer, you will probably lose the race"*. Candidates also include food clusters.
   - *Ambush* is only offered when an interception is geometrically possible, and *escape* only when a longer rival is really close.
   - Code does the arithmetic and tags the best points-per-step option **BEST VALUE**. OpenJev keeps the judgement: style, orders, trade-offs.
   - **Commander orders** are first classified by OpenJev with a small, separate question (eat / gold / hunt / safe / power-up / explore / none), works in any language. Code then tags the matching options **ORDERED BY YOUR COMMANDER**. Splitting the task this way made orders reliable. Before, a small model would ignore "never fight" when a +10 ambush was on offer.
3. **The decision is OpenJev.** One call answers `intent`, `outlook` and `danger` together (`app/systemone.py`, `app/serpents/brains.py`). Calls are async, so the snake keeps following its current intent while the next decision is in flight.
4. **Action is code** (`app/serpents/controller.py`). Collision rules: walls and bodies kill; in a head-on crash, including two heads swapping places, the longer snake survives; a shield *bounces* you (you skip that move and lose the shield), so snakes never overlap. A BFS pathfinder with a flood-fill anti-trap check carries out the intent on every tick. When `danger > 0.6` it switches to strict safety mode, which is a small example of code combining several decisions.
5. **Data goes to Delta** (`app/decision_log.py`). Every decision, game event and match result is queued in memory and written **asynchronously in batches** through the SQL warehouse, every 4 s or 40 rows. The game never waits on the database, and it plays exactly the same if logging is off (empty `OPENJEV_TABLE_PREFIX`).

The **Playground** tab sends any System One request (ticket triage, moderation, lead routing…) and shows the probabilities and latency.

## Measured results (one workspace, 2026-11-03)

| What | Result |
|---|---|
| Latency from a laptop, game payload (1.2k tokens, 10 options, 3 questions) | p50 ~240 ms, p90 ~330 ms |
| Latency from Databricks (app / serverless job), ~2,800 decisions | **p50 105–137 ms, p90 149–174 ms** |
| Persona effect (arena) | Viper picks *hunt* 53% of the time; Turtle picks *flee* 29% and dies the least (0.3 deaths per match); Glutton mostly eats |
| Is `danger` predictive? | Death within 2 s: 7.3% when danger is 60–80%, about 2% when it's below 60% |
| 6 × 60 s arena matches, average score (after perception rework) | Glutton·OpenJev 60.3 · Turtle·OpenJev 54.5 · Viper·OpenJev 52.3 · Heuristic bot 63.0. Before: OpenJev average 49.9 → after 55.7; Viper 35.7 → 52.3 |
| Commander orders ("only eat food, never fight", "play it safe", "tue le serpent rouge"…) | Obeyed in 7/7 test orders after the classify-then-tag change; 1/4 before |
| Load: 2 concurrent matches, 6 OpenJev snakes | 0 errors, p50 ~120 ms, p90 ≤ 225 ms, no tick lag |

## Repository layout

```
app/                      Databricks App (FastAPI + vanilla JS canvas)
  main.py                 routes: /api/game, /ws/game/{id}, /sse/game/{id}, /api/playground, /api/model
  systemone.py            System One client (SDK auth, retries, semaphore, entropy)
  decision_log.py         async batched Delta writer + table DDL
  serpents/               engine, controller, intents (perception), brains, sessions (real-time + lockstep)
  static/                 index.html, game.js (renderer + mind cards), playground.js, style.css
notebooks/
  00_setup.py             schema/tables + grants to the app service principal (job)
  01_openjev_playground.py  learn System One: triage, steering, wording/order sensitivity, batch → Delta, latency
  02_arena_eval.py        headless lockstep tournament → Delta + MLflow (job)
dashboards/openjev_arena.lvdash.json   AI/BI dashboard
databricks.yml            bundle: app (+ its env config), 2 jobs, dashboard; targets.local.yml.example
tests/                    pytest: engine rules, controller, intents, client, brains, lockstep
```

## Run, test, deploy

```bash
uv sync && uv run pytest                            # 43 unit tests (no network): rules, collisions fuzz, controller, client, sessions

# Deploy to your workspace (uses the CLI profile's host; the warehouse defaults to "Serverless Starter Warehouse")
databricks bundle deploy -p <profile> --var catalog=<your_catalog>
databricks bundle run serpents_setup -p <profile> --var catalog=<your_catalog>   # tables + grants for the app SP
databricks bundle run delta_serpents -p <profile> --var catalog=<your_catalog>   # start / redeploy the app
databricks bundle run serpents_arena -p <profile> --var catalog=<your_catalog>   # tournament -> Delta + MLflow
```

To avoid passing `--var` every time, copy `targets.local.yml.example` to `targets.local.yml`. That file is git-ignored; set your workspace host and catalog in it, then use `-t <your_target>`.

Run the app locally with your own CLI identity:

```bash
cd app && DATABRICKS_CONFIG_PROFILE=<profile> DATABRICKS_WAREHOUSE_ID=<warehouse_id> \
  OPENJEV_TABLE_PREFIX=<catalog>.openjev_demo uv run --project .. python main.py   # http://localhost:8000
```

The app's environment (model, table prefix, dashboard link) is defined in `databricks.yml` under `resources.apps.delta_serpents.config`. Leave `OPENJEV_TABLE_PREFIX` empty to turn off Delta logging.

## What else you could do with this

- **Game AI and NPCs.** Personas, difficulty set through instructions, live orders from the player, automated QA bots that play through levels.
- **Routing and triage.** Tickets, claims, alerts and leads, with the full probability distribution rather than a single label. Several questions cost one call.
- **Human in the loop.** Escalate to a person when confidence is low or entropy is high.
- **Agent guardrails.** "Which tool next?", "Should I escalate?" and "Is this answer safe?" as fast typed decisions inside an agent loop.
- **Batch decisions on data.** Score tables from notebooks or jobs. Every decision becomes a governed, auditable row in Unity Catalog.
- **Evaluation.** Arenas and A/B tests of personas, instructions and models (OpenJev vs Jev 1.14.0), plus wording and ordering sensitivity tests.

## Caveats

- Probabilities are **uncalibrated** and relative to the options you supply. Rewording an option can change its score, so test for sensitivity (notebook 01, section 3).
- Under parallel load from a laptop, p90 latency rose to ~0.7–0.8 s. Both the decision cadence and the number of AI snakes are configurable.
- Game sessions live in memory on one app instance. That's fine for a demo but not multi-tenant hardened. Up to 6 matches can run at once.
- Logging is best-effort: rows land in Delta 4–10 s later, and the last few seconds can be lost if the app restarts. For guaranteed delivery, use Zerobus Ingest or Lakebase.

## Observability

Three complementary layers, each answering a different question:

| Layer | Where | Answers |
|---|---|---|
| **Unity AI Gateway usage** (automatic, no code) | `system.ai_gateway.usage`, filter `api_type = 'typesafe/v1/systemone'` | Who calls OpenJev, how often, input tokens, status codes, gateway latency, for cost and governance |
| **Game decision log** (this app) | `<catalog>.openjev_demo.decisions / events / games` | What each snake saw, the probability of every option, what it chose, and what happened next. Joinable with deaths and scores; this is what the dashboard uses |
| **MLflow** | experiment `/Users/<you>/delta_serpents_arena` | Arena runs, i.e. comparisons of personas, models or prompt variants, with metrics and a summary table |

## License

MIT, see [LICENSE](LICENSE).
