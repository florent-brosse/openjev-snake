/* Playground: send any System One request to OpenJev and visualise the answer. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const TEMPLATES = {
    support: {
      state: { message: "Hi, my card was charged twice for order #4471 and I need the duplicate refunded today, this is the third time I ask!", customer_tier: "gold" },
      questions: {
        team: { type: "choice", instructions: "Which team should handle this ticket?", criteria: { billing: "Payments, charges, refunds and invoices.", shipping: "Delivery, tracking and lost parcels.", tech: "Bugs, login problems and app errors.", sales: "New purchases and pricing questions." } },
        urgency: { type: "score", instructions: "How urgent is this ticket?", criteria: ["can wait", "this week", "today", "immediately"] },
        angry: { type: "noul", instructions: "Is the customer angry or frustrated?" },
      },
    },
    game: {
      state: "You are a snake at (12,7) moving east, length 9. A rival longer snake's head is 3 steps away at (15,9). Gold (+5) is 11 steps away and the rival is closer to it. Food is 4 steps away and uncontested.",
      questions: {
        intent: { type: "choice", instructions: "Which intent should the snake pursue?", criteria: { eat_food: "Eat the uncontested food 4 steps away.", race_gold: "Race for the gold 11 steps away (rival is closer).", hunt: "Cut off the longer rival.", flee: "Move away from the rival into open space.", survive: "Circle safely and wait." } },
        danger: { type: "noul", instructions: "Is the snake in immediate danger?" },
      },
    },
    moderation: {
      state: { post: "Selling my old bike, DM me — or just come steal it lol, the lock is broken anyway", channel: "marketplace" },
      questions: {
        policy: { type: "choice", instructions: "Which policy category best fits this post?", criteria: { ok: "Allowed content.", spam: "Spam or scam.", violence: "Threats or incitement to violence.", illegal: "Encourages illegal activity." } },
        action: { type: "choice", instructions: "What should the moderation system do?", criteria: { publish: "Publish as is.", review: "Send to a human reviewer.", block: "Block immediately." } },
      },
    },
    sales: {
      state: { email: "We're a 4,000-person retailer evaluating a lakehouse to replace our on-prem Teradata by Q3. Can we get pricing and a POC plan?", source: "website form" },
      questions: {
        segment: { type: "choice", instructions: "Which sales segment fits this lead?", criteria: { smb: "Fewer than 200 employees.", mid_market: "200 to 2,000 employees.", enterprise: "More than 2,000 employees." } },
        intent: { type: "score", instructions: "How strong is the buying intent?", criteria: ["just curious", "researching", "evaluating", "ready to buy"] },
        competitive: { type: "noul", instructions: "Does the lead mention replacing an incumbent vendor?" },
      },
    },
  };

  function load(name) {
    const t = TEMPLATES[name];
    $("#pgState").value = typeof t.state === "string" ? t.state : JSON.stringify(t.state, null, 2);
    $("#pgQuestions").value = JSON.stringify(t.questions, null, 2);
  }
  $("#pgTemplate").addEventListener("change", (e) => load(e.target.value));
  load("support");

  function parseState(txt) { try { return JSON.parse(txt); } catch (e) { return txt; } }

  async function run() {
    const questions = JSON.parse($("#pgQuestions").value);
    const res = await fetch("/api/playground", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: parseState($("#pgState").value), questions }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    return { questions, body };
  }

  function bars(probs, top, labels) {
    return Object.entries(probs).sort((a, b) => b[1] - a[1]).map(([k, p]) => `
      <div class="bar ${k === top ? "top" : ""}" title="${esc(labels?.[k] ?? "")}"><span class="lbl">${esc(labels && Array.isArray(labels) ? labels[+k] : k)}</span>
        <span class="track"><span class="fill" style="width:${(p * 100).toFixed(1)}%;background:${k === top ? "#ff3621" : "#3a4a72"}"></span></span>
        <span class="val">${(p * 100).toFixed(1)}%</span></div>`).join("");
  }

  function render({ questions, body }) {
    const out = [];
    for (const [qid, ans] of Object.entries(body.answers || {})) {
      const q = questions[qid] || {};
      let html = `<div class="pg-q"><h4>${esc(qid)} <span class="muted">(${esc(ans.type)})</span></h4><div class="sub">${esc(q.instructions || "")}</div>`;
      if (ans.type === "choice") html += `<div class="sub">→ <b>${esc(ans.choice)}</b> · confidence ${(ans.confidence * 100).toFixed(0)}% · entropy ${ans.entropy_nats ?? "–"}</div>` + bars(ans.probabilities, ans.choice, q.criteria);
      else if (ans.type === "score") {
        const top = Object.entries(ans.probabilities).sort((a, b) => b[1] - a[1])[0][0];
        html += `<div class="sub">→ score <b>${ans.score.toFixed(2)}</b> (${esc(ans.legend?.[Math.round(ans.score)])}) · confidence ${(ans.confidence * 100).toFixed(0)}%</div>` + bars(ans.probabilities, top, q.criteria);
      } else if (ans.type === "noul") html += `<div class="sub">→ P(yes) = <b>${(ans.noul * 100).toFixed(1)}%</b></div>` + bars({ yes: ans.noul, no: 1 - ans.noul }, ans.noul >= 0.5 ? "yes" : "no");
      out.push(html + "</div>");
    }
    $("#pgResult").innerHTML = `<div class="sub muted">${esc(body.model)} · ${Math.round(body.latency_ms)} ms server-side · ${body.usage?.input_tokens ?? "?"} input tokens · ${body.usage?.output_tokens ?? 0} output tokens</div>` + out.join("");
    $("#pgRaw").textContent = JSON.stringify(body, null, 2);
  }

  $("#pgRun").addEventListener("click", async () => {
    $("#pgStatus").textContent = "deciding…";
    try { render(await run()); $("#pgStatus").textContent = ""; } catch (e) { $("#pgStatus").textContent = "Error: " + e.message; }
  });
  $("#pgRun10").addEventListener("click", async () => {
    const lat = [];
    try {
      for (let i = 0; i < 10; i++) { $("#pgStatus").textContent = `run ${i + 1}/10…`; const r = await run(); lat.push(r.body.latency_ms); if (i === 9) render(r); }
      lat.sort((a, b) => a - b);
      $("#pgStatus").textContent = `latency p50 ${Math.round(lat[4])} ms · min ${Math.round(lat[0])} · max ${Math.round(lat[9])} ms`;
    } catch (e) { $("#pgStatus").textContent = "Error: " + e.message; }
  });
})();
