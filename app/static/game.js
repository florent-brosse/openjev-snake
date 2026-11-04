/* Delta Serpents — browser client: lobby, transport (WS → SSE fallback), canvas renderer, mind cards. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------------ tabs
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab-page").forEach((p) => p.classList.toggle("active", p.id === `tab-${b.dataset.tab}`));
  }));

  let modelInfo = {};
  async function refreshModel() {
    try {
      modelInfo = await (await fetch("/api/model")).json();
      $("#modelPill").innerHTML = `model: <b>${esc(modelInfo.model)}</b> · ${modelInfo.calls} calls`;
      $("#aboutModel").textContent = modelInfo.model;
      const dl = $("#dashLink");
      if (modelInfo.dashboard_url) { dl.href = modelInfo.dashboard_url; dl.classList.remove("hidden"); } else dl.classList.add("hidden");
    } catch (e) { $("#modelPill").textContent = "model: unavailable"; }
  }
  refreshModel(); setInterval(refreshModel, 10000);

  // ----------------------------------------------------------------- lobby
  const PERSONAS = { viper: "Viper — aggressive hunter", turtle: "Turtle — cautious survivor", glutton: "Glutton — greedy eater", custom: "Custom persona…" };
  // same palette order as the server (session.COLORS): the human takes the first colour when playing
  const SERVER_COLORS = ["#ff4d6d", "#4cc9f0", "#80ed99", "#ffd166", "#c77dff"];
  const previewColor = (i) => SERVER_COLORS[(i + (spectating() ? 0 : 1)) % SERVER_COLORS.length];
  let aiRows = [{ persona: "viper", order: "", custom: "", brain: "openjev" }, { persona: "turtle", order: "", custom: "", brain: "openjev" }];

  function renderLobby() {
    const list = $("#aiList");
    list.innerHTML = "";
    aiRows.forEach((r, i) => {
      const row = document.createElement("div");
      row.className = "ai-row";
      row.innerHTML = `
        <span class="dot" style="background:${previewColor(i)}"></span>
        <select data-k="persona">${Object.entries(PERSONAS).map(([k, v]) => `<option value="${k}" ${k === r.persona ? "selected" : ""}>${v.split(" —")[0]}</option>`).join("")}</select>
        <input type="text" data-k="order" placeholder="Commander order (optional), e.g. hunt the red snake" value="${esc(r.order)}" />
        <select data-k="brain"><option value="openjev" ${r.brain === "openjev" ? "selected" : ""}>OpenJev</option><option value="heuristic" ${r.brain === "heuristic" ? "selected" : ""}>Heuristic bot</option></select>
        <button class="x" title="remove">✕</button>
        ${r.persona === "custom" ? `<input class="custom" type="text" data-k="custom" placeholder="Describe the personality, e.g. a coward that only eats gold" value="${esc(r.custom)}" />` : ""}`;
      row.querySelectorAll("[data-k]").forEach((el) => el.addEventListener("input", () => {
        r[el.dataset.k] = el.value;
        if (el.dataset.k === "persona") renderLobby();
      }));
      row.querySelector(".x").addEventListener("click", () => { if (aiRows.length > 1) { aiRows.splice(i, 1); renderLobby(); } });
      list.appendChild(row);
    });
    $("#addAi").classList.toggle("hidden", aiRows.length >= 3 + (spectating() ? 1 : 0));
  }
  const spectating = () => document.querySelector('input[name="mode"]:checked').value === "spectate";
  document.querySelectorAll('input[name="mode"]').forEach((el) => el.addEventListener("change", renderLobby));
  $("#addAi").addEventListener("click", () => {
    const next = ["viper", "turtle", "glutton", "custom"][aiRows.length % 4];
    aiRows.push({ persona: next, order: "", custom: "", brain: "openjev" });
    renderLobby();
  });
  renderLobby();

  // ------------------------------------------------------------- transport
  let gameId = null, ws = null, es = null, transport = "none";
  let cur = null, prev = null, curAt = 0;
  const minds = {};
  const feed = [];

  async function startMatch() {
    $("#startBtn").disabled = true;
    try {
      const body = {
        ais: aiRows.slice(0, spectating() ? 4 : 3).map((r) => ({ persona: r.persona, custom: r.custom, order: r.order, brain: r.brain })),
        spectator: spectating(),
        duration_s: +$("#duration").value, tps: +$("#tps").value, decision_interval_s: +$("#interval").value,
      };
      const res = await fetch("/api/game", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const g = await res.json();
      gameId = g.game_id; cur = prev = null; feed.length = 0; renderFeed(); matchOver = false; reconnects = 0;
      Object.keys(minds).forEach((k) => delete minds[k]);
      $("#minds").innerHTML = "";
      $("#lobby").classList.add("hidden"); $("#gameover").classList.add("hidden");
      connect();
    } catch (e) {
      alert("Could not start the match: " + e.message);
    } finally { $("#startBtn").disabled = false; }
  }
  $("#startBtn").addEventListener("click", startMatch);
  $("#againBtn").addEventListener("click", () => { $("#gameover").classList.add("hidden"); $("#lobby").classList.remove("hidden"); });

  let reconnects = 0, matchOver = false;
  function connect() {
    closeTransport();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let opened = false;
    try {
      const sock = ws = new WebSocket(`${proto}://${location.host}/ws/game/${gameId}`);
      sock.onopen = () => { opened = true; reconnects = 0; transport = "websocket"; };
      sock.onmessage = (ev) => onMessage(JSON.parse(ev.data));
      sock.onerror = () => { if (!opened && sock === ws) fallbackSSE(); };
      sock.onclose = () => {
        if (sock !== ws || matchOver) return;          // replaced on purpose, or match finished
        if (!opened) return fallbackSSE();
        transport = "none";                           // dropped mid-match: reconnect, then fall back to SSE
        if (reconnects++ < 5) setTimeout(() => { if (!matchOver) connect(); }, 400 * reconnects);
        else fallbackSSE();
      };
      setTimeout(() => { if (!opened && sock === ws && transport !== "sse") fallbackSSE(); }, 3000);
    } catch (e) { fallbackSSE(); }
  }
  function fallbackSSE() {
    if (transport === "sse" || !gameId) return;
    if (ws) { try { ws.close(); } catch (e) { } ws = null; }
    transport = "sse";
    es = new EventSource(`/sse/game/${gameId}`);
    es.onmessage = (ev) => onMessage(JSON.parse(ev.data));
  }
  function closeTransport() {
    const oldWs = ws, oldEs = es;
    ws = es = null; transport = "none";             // clear first so onclose handlers ignore this close
    if (oldWs) { try { oldWs.close(); } catch (e) { } }
    if (oldEs) oldEs.close();
  }
  function send(msg) {
    if (transport === "websocket" && ws && ws.readyState === 1) return ws.send(JSON.stringify(msg));
    if (!gameId) return;
    if (msg.type === "dir") fetch(`/api/game/${gameId}/input`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dir: msg.dir }) });
    if (msg.type === "order") fetch(`/api/game/${gameId}/order`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ snake_id: msg.snake_id, order: msg.order }) });
  }

  // ------------------------------------------------------------- messages
  function onMessage(m) {
    prev = cur; cur = m; curAt = performance.now();
    const cd = $("#countdown");
    if (m.countdown > 0) { cd.classList.remove("hidden"); $("#countdownNum").textContent = Math.ceil(m.countdown); }
    else cd.classList.add("hidden");
    (m.events || []).forEach(addEvent);
    renderScorebar(m);
    renderMinds(m);
    if (m.type === "game_over") { matchOver = true; showGameOver(m); closeTransport(); refreshModel(); }
  }

  const nameOf = (id) => (cur?.snakes.find((s) => s.id === id) || {}).name || id;
  function addEvent(e) {
    const n = `<b>${esc(nameOf(e.snake_id))}</b>`;
    let t = null;
    if (e.type === "eat" && e.item === "gold") t = `${n} grabbed ★ gold +5`;
    else if (e.type === "eat" && e.item === "boost") t = `${n} picked up a ◆ speed boost`;
    else if (e.type === "eat" && e.item === "shield") t = `${n} picked up a ✚ shield`;
    else if (e.type === "kill") t = `${n} +10 — ${esc(nameOf(e.victim))} crashed into it`;
    else if (e.type === "death") t = `${n} died (${esc(e.cause)}${e.by && e.by !== e.snake_id ? " → " + esc(nameOf(e.by)) : ""})`;
    else if (e.type === "shield_saved") t = `${n}'s shield saved it!`;
    else if (e.type === "gold_spawn") t = `★ gold appeared at (${e.x},${e.y})`;
    if (t) { feed.unshift(t); feed.length = Math.min(feed.length, 6); renderFeed(); }
  }
  function renderFeed() { $("#feed").innerHTML = feed.map((t) => `<div>${t}</div>`).join(""); }

  function renderScorebar(m) {
    const chips = [...m.snakes].sort((a, b) => b.score - a.score).map((s) => `
      <div class="chip ${s.alive ? "" : "dead"}"><span class="dot" style="background:${s.color};color:${s.color}"></span>
        <span>${esc(s.name)}</span><span class="score">${s.score}</span>
        <span class="muted">${s.kills}K/${s.deaths}D${s.alive ? "" : " · respawn " + (s.respawn_in ?? "") + "s"}</span></div>`).join("");
    const t = Math.max(0, Math.ceil(m.time_left));
    $("#scorebar").innerHTML = chips + `<div class="timer">${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}</div>`;
  }

  // ------------------------------------------------------------ mind cards
  function intentLabel(id) {
    if (!id) return "—";
    const [k, rest] = [id.split("_")[0], id.split("_").slice(1).join("_")];
    return ({ eat: "eat food", cluster: "food cluster", gold: "★ race for gold", boost: "◆ grab boost", shield: "✚ grab shield", hunt: `ambush ${nameOf(rest)}`, flee: `flee ${nameOf(rest)}`, survive: "survive (circle)", explore: "explore centre" })[k] || id;
  }
  const steps = (d) => { const m = /(\d+) steps/.exec(d || ""); return m ? ` · ${m[1]}` : ""; };

  function renderMinds(m) {
    const box = $("#minds");
    for (const s of m.snakes.filter((x) => x.kind === "ai")) {
      const ai = m.ai[s.id];
      let card = minds[s.id];
      if (!card) {
        const el = document.createElement("div");
        el.className = "card mind";
        el.innerHTML = `
          <div class="head"><span class="pulse"></span><span class="name" style="color:${s.color}">${esc(s.name)}</span>
            <span class="persona"></span><span class="badge"></span></div>
          <div class="intent-now">Intent: <b class="intent">thinking…</b></div>
          <div class="bars"></div><button class="more hidden"></button>
          <div class="gauges">
            <div class="gauge"><div class="k"><span>outlook</span><span class="ov"></span></div><div class="g outlook"><span class="marker"></span></div></div>
            <div class="gauge"><div class="k"><span>danger</span><span class="dv"></span></div><div class="g danger"><span class="marker"></span></div></div>
          </div>
          <div class="metrics">
            <div class="metric"><div class="k">latency</div><div class="v lat">–</div></div>
            <div class="metric"><div class="k">entropy</div><div class="v ent">–</div></div>
            <div class="metric"><div class="k">confidence</div><div class="v conf">–</div></div>
            <div class="metric"><div class="k">decisions</div><div class="v nd">0</div></div>
          </div>
          <div class="order"><input type="text" placeholder="Live commander order… (Enter)" /><button class="ghost">Send</button></div>
          <div class="understood muted"></div>`;
        if (box.querySelector(".minds-empty")) box.innerHTML = "";
        box.appendChild(el);
        card = minds[s.id] = { el, lastTick: -1, showAll: false };
        const input = el.querySelector(".order input");
        const go = () => { send({ type: "order", snake_id: s.id, order: input.value }); input.blur(); addEventText(`Commander → <b>${esc(s.name)}</b>: “${esc(input.value || "(cleared)")}”`); };
        input.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") go(); });
        el.querySelector(".order button").addEventListener("click", go);
        el.querySelector(".more").addEventListener("click", () => { card.showAll = !card.showAll; card.lastTick = -1; });
        input.value = ai.order || "";
      }
      const el = card.el;
      el.querySelector(".pulse").classList.toggle("on", ai.thinking);
      el.querySelector(".persona").textContent = ai.persona.length > 30 ? "custom" : ai.persona;
      el.querySelector(".badge").textContent = ai.brain === "openjev" ? "OpenJev" : "heuristic";
      el.querySelector(".nd").textContent = ai.decisions + (ai.errors ? ` (${ai.errors} err)` : "");
      el.querySelector(".understood").textContent = ai.order ? (ai.order_kind ? `order understood as: ${ai.order_kind}` : "classifying order…") : "";
      const d = ai.decision;
      if (!d || d.tick === card.lastTick) continue;
      card.lastTick = d.tick;
      el.querySelector(".intent").textContent = s.alive ? intentLabel(d.choice) : "respawning…";
      const shown = card.showAll ? d.candidates : d.candidates.slice(0, 6);
      el.querySelector(".bars").innerHTML = shown.map((c, i) => `
        <div class="bar ${c.id === d.choice ? "top" : ""}" title="${esc(c.description)}">
          <span class="lbl">${esc(intentLabel(c.id) + steps(c.description))}${(c.description || "").includes("BEST VALUE") ? ' <span class="best">best</span>' : ""}${(c.description || "").includes("ORDERED BY YOUR COMMANDER") ? ' <span class="ordered">order</span>' : ""}</span>
          <span class="track"><span class="fill" style="width:${(c.probability * 100).toFixed(1)}%;background:${c.id === d.choice ? s.color : "#3a4a72"}"></span></span>
          <span class="val">${(c.probability * 100).toFixed(0)}%</span></div>`).join("");
      const more = el.querySelector(".more");
      more.classList.toggle("hidden", d.candidates.length <= 6);
      more.textContent = card.showAll ? "show top 6" : `show all ${d.candidates.length} options`;
      el.querySelector(".lat").textContent = d.latency_ms != null ? `${Math.round(d.latency_ms)} ms` : "–";
      el.querySelector(".ent").textContent = d.entropy != null ? d.entropy.toFixed(2) : "–";
      el.querySelector(".conf").textContent = d.confidence != null ? `${Math.round(d.confidence * 100)}%` : "–";
      const ol = d.outlook != null ? d.outlook / 2 : null;
      el.querySelector(".outlook .marker").style.left = ol != null ? `calc(${ol * 100}% - 2px)` : "-10px";
      el.querySelector(".ov").textContent = ol != null ? ["losing", "even", "winning"][Math.round(d.outlook)] : "";
      el.querySelector(".danger .marker").style.left = d.danger != null ? `calc(${d.danger * 100}% - 2px)` : "-10px";
      el.querySelector(".dv").textContent = d.danger != null ? `${Math.round(d.danger * 100)}%${d.danger > 0.6 ? " · strict mode" : ""}` : "";
    }
  }
  function addEventText(t) { feed.unshift(t); feed.length = Math.min(feed.length, 6); renderFeed(); }

  function showGameOver(m) {
    const w = m.snakes.find((s) => s.id === m.winner);
    $("#winnerTitle").textContent = w ? `${w.name} wins!` : "Draw!";
    $("#resultsTable").innerHTML = `<tr><th>Snake</th><th>Brain</th><th>Score</th><th>Kills</th><th>Deaths</th></tr>` +
      [...m.snakes].sort((a, b) => b.score - a.score).map((s) => `<tr><td style="color:${s.color};font-weight:700">${esc(s.name)}</td>
        <td>${s.kind === "human" ? "human" : esc((m.ai[s.id] || {}).brain)}</td><td>${s.score}</td><td>${s.kills}</td><td>${s.deaths}</td></tr>`).join("");
    $("#gameover").classList.remove("hidden");
  }

  // --------------------------------------------------------------- input
  const KEYS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", w: "up", s: "down", a: "left", d: "right", z: "up", q: "left" };
  window.addEventListener("keydown", (e) => {
    if (!$("#tab-arena").classList.contains("active")) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    const dir = KEYS[e.key] || KEYS[e.key?.toLowerCase?.()];
    if (dir && gameId && cur && !cur.over) { e.preventDefault(); send({ type: "dir", dir }); }
  });

  // ------------------------------------------------------------- renderer
  const cv = $("#board"), ctx = cv.getContext("2d");
  const W = 32, H = 24, CS = cv.width / W;

  function lerpBody(sNow, sPrev, t) {
    if (!sPrev || !sPrev.alive || !sNow.alive) return sNow.body.map(([x, y]) => [x, y]);
    return sNow.body.map(([x, y], i) => {
      const p = sPrev.body[i];
      if (!p || Math.abs(p[0] - x) + Math.abs(p[1] - y) > 2) return [x, y];
      return [p[0] + (x - p[0]) * t, p[1] + (y - p[1]) * t];
    });
  }
  const cx = (x) => x * CS + CS / 2, cy = (y) => y * CS + CS / 2;

  function drawGrid() {
    ctx.fillStyle = "#050810"; ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = "rgba(76,201,240,0.10)";
    for (let x = 0; x < W; x++) for (let y = 0; y < H; y++) ctx.fillRect(cx(x) - 1, cy(y) - 1, 2, 2);
    ctx.strokeStyle = "rgba(76,201,240,0.25)"; ctx.lineWidth = 2; ctx.strokeRect(1, 1, cv.width - 2, cv.height - 2);
  }

  function star(x, y, r, n = 5) {
    ctx.beginPath();
    for (let i = 0; i < n * 2; i++) {
      const a = (Math.PI / n) * i - Math.PI / 2, rr = i % 2 ? r * 0.45 : r;
      ctx.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr);
    }
    ctx.closePath();
  }

  function drawItems(items, now) {
    for (const it of items) {
      const x = cx(it.x), y = cy(it.y);
      const blink = it.ttl != null && it.ttl < 3 && Math.floor(now / 150) % 2 === 0;
      if (blink) continue;
      ctx.save();
      if (it.kind === "food") {
        ctx.shadowColor = "#80ed99"; ctx.shadowBlur = 10; ctx.fillStyle = "#b9fbc0";
        ctx.beginPath(); ctx.arc(x, y, CS * 0.18, 0, Math.PI * 2); ctx.fill();
      } else if (it.kind === "gold") {
        const p = 1 + Math.sin(now / 180) * 0.12;
        ctx.shadowColor = "#ffd166"; ctx.shadowBlur = 22; ctx.fillStyle = "#ffd166";
        star(x, y, CS * 0.48 * p); ctx.fill();
      } else if (it.kind === "boost") {
        ctx.shadowColor = "#4cc9f0"; ctx.shadowBlur = 16; ctx.strokeStyle = "#4cc9f0"; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(x - 6, y - 7); ctx.lineTo(x + 2, y); ctx.lineTo(x - 6, y + 7); ctx.moveTo(x + 1, y - 7); ctx.lineTo(x + 9, y); ctx.lineTo(x + 1, y + 7); ctx.stroke();
      } else if (it.kind === "shield") {
        ctx.shadowColor = "#c77dff"; ctx.shadowBlur = 16; ctx.strokeStyle = "#c77dff"; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(x, y, CS * 0.38, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.moveTo(x, y - 5); ctx.lineTo(x, y + 5); ctx.stroke();
      }
      ctx.restore();
    }
  }

  function intentTarget(m, s) {
    const d = m.ai?.[s.id]?.decision; if (!d || !s.alive) return null;
    const [k, ...rest] = d.choice.split("_"); const ref = rest.join("_");
    if (["eat", "cluster", "gold", "boost", "shield"].includes(k)) { const it = m.items.find((i) => i.id === ref); return it ? [it.x, it.y] : null; }
    if (k === "hunt" || k === "flee") { const r = m.snakes.find((x) => x.id === ref); return r && r.alive ? r.body[0] : null; }
    if (k === "explore") return [W / 2, H / 2];
    return null;
  }

  function drawSnake(s, body, m, now) {
    if (!s.alive || body.length === 0) return;
    ctx.save();
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.shadowColor = s.color; ctx.shadowBlur = s.boost ? 24 : 12;
    ctx.strokeStyle = s.color; ctx.lineWidth = CS * 0.62;
    ctx.beginPath(); ctx.moveTo(cx(body[0][0]), cy(body[0][1]));
    for (const [x, y] of body.slice(1)) ctx.lineTo(cx(x), cy(y));
    if (body.length === 1) ctx.lineTo(cx(body[0][0]) + 0.1, cy(body[0][1]));
    ctx.stroke();
    ctx.shadowBlur = 0; ctx.strokeStyle = "rgba(255,255,255,0.18)"; ctx.lineWidth = CS * 0.18; ctx.stroke();
    // head
    const [hx, hy] = body[0];
    ctx.fillStyle = "#fff"; ctx.shadowColor = s.color; ctx.shadowBlur = 16;
    ctx.beginPath(); ctx.arc(cx(hx), cy(hy), CS * 0.38, 0, Math.PI * 2); ctx.fill();
    const dv = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] }[s.dir];
    ctx.fillStyle = "#050810"; ctx.shadowBlur = 0;
    for (const side of [-1, 1]) {
      const ex = cx(hx) + dv[0] * 4 + -dv[1] * side * 4, ey = cy(hy) + dv[1] * 4 + dv[0] * side * 4;
      ctx.beginPath(); ctx.arc(ex, ey, 2.2, 0, Math.PI * 2); ctx.fill();
    }
    if (s.shield) {
      ctx.strokeStyle = "#c77dff"; ctx.lineWidth = 2; ctx.shadowColor = "#c77dff"; ctx.shadowBlur = 12;
      ctx.beginPath(); ctx.arc(cx(hx), cy(hy), CS * 0.7 + Math.sin(now / 120) * 1.5, 0, Math.PI * 2); ctx.stroke();
    }
    // label + uncertainty bubble
    ctx.shadowBlur = 0; ctx.font = "bold 11px Inter, sans-serif"; ctx.textAlign = "center";
    const ly = hy < 1.2 ? cy(hy) + CS * 1.15 : cy(hy) - CS * 0.8;  // keep the label inside the arena
    ctx.lineWidth = 3; ctx.strokeStyle = "#050810"; ctx.strokeText(s.name, cx(hx), ly);
    ctx.fillStyle = s.color; ctx.fillText(s.name, cx(hx), ly);
    const d = m.ai?.[s.id]?.decision;
    if (d && d.entropy != null && d.candidates.length > 1 && d.entropy / Math.log(d.candidates.length) > 0.7) {
      ctx.fillStyle = "#ffd166"; ctx.font = "bold 16px Inter, sans-serif"; ctx.fillText("?", cx(hx) + CS * 0.8, cy(hy) - CS * 0.6);
    }
    ctx.restore();
  }

  function drawIntents(m, bodies) {
    for (const s of m.snakes) {
      if (s.kind !== "ai" || !s.alive) continue;
      const tgt = intentTarget(m, s); const b = bodies[s.id];
      if (!tgt || !b?.length) continue;
      ctx.save();
      ctx.setLineDash([4, 6]); ctx.lineWidth = 1.5; ctx.strokeStyle = s.color; ctx.globalAlpha = 0.55;
      ctx.beginPath(); ctx.moveTo(cx(b[0][0]), cy(b[0][1])); ctx.lineTo(cx(tgt[0]), cy(tgt[1])); ctx.stroke();
      ctx.setLineDash([]); ctx.beginPath(); ctx.arc(cx(tgt[0]), cy(tgt[1]), CS * 0.55, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
    }
  }

  function frame(now) {
    drawGrid();
    if (cur) {
      const tickMs = 1000 / (cur.tps || 8);
      const t = Math.min(1, (now - curAt) / tickMs);
      drawItems(cur.items, now);
      const bodies = {};
      for (const s of cur.snakes) bodies[s.id] = lerpBody(s, prev?.snakes.find((p) => p.id === s.id), t);
      drawIntents(cur, bodies);
      for (const s of cur.snakes) drawSnake(s, bodies[s.id], cur, now);
    } else {
      ctx.fillStyle = "rgba(230,236,255,0.25)"; ctx.font = "bold 28px Inter, sans-serif"; ctx.textAlign = "center";
      ctx.fillText("DELTA SERPENTS", cv.width / 2, cv.height / 2);
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
