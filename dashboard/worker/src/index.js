/**
 * worker/src/index.js
 *
 * Confluence paper-trading journal + control panel, backed by D1
 * (database: confluence-paper-journal, uuid 01f828b0-c29f-473c-9f7d-29f1f25a914a).
 *
 * Two simultaneous tests, one dashboard:
 *   Test 1  bot_id "book2"   the Book 2 5-pillar engine (running since 2026-07-30; parameters LOCKED)
 *   Test 2  bot_id "setups"  the 3-setup bot (ORB / Pullback / Reversal; parameters editable, starts PAUSED)
 *
 * Endpoints:
 *   POST /api/event          runner -> journal. Bearer INGEST_TOKEN. Raw event row always; upserts `trades`
 *                            on position_opened/trade_closed; equity snapshot when review state present;
 *                            `heartbeat` updates the bot's last-seen; `flatten_done` clears a flatten request.
 *   GET  /api/bot-config     runner <- control panel. Bearer INGEST_TOKEN. ?bot=<id>
 *   POST /api/admin/bot      browser -> control panel. Bearer ADMIN_TOKEN (separate secret).
 *                            {bot_id, action: start|stop|flatten|set_params, params?, confirm_restart?}
 *   GET  /api/bots           bot list + status (read-only, open like the other GETs)
 *   GET  /api/journal        trades + metrics + filter facets. ?bot= &setup= &instrument= &risk_pct= &tp_pct=
 *                            &trail_pct= &run_id=   (tp_pct / trail_pct / risk_pct accept "default" = NULL)
 *   GET  /api/summary        headline metrics + equity curve + review state + recent events. ?bot= + same filters
 *   GET  /                   the dashboard
 *
 * Auth: GET endpoints stay open (read-only; paper P&L only) exactly as before. Anything that changes
 * behaviour needs a secret and FAILS CLOSED if that secret is not configured:
 *   wrangler secret put ADMIN_TOKEN        (new; gates start/stop/params)
 *   wrangler secret put INGEST_TOKEN       (existing; now also gates /api/bot-config)
 * Consider putting Cloudflare Access in front of / and /api/admin/*.
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

const RISK_MAX = 1.0;       // the rules cap loss per trade at 1%; the panel can only lower it
const RISK_MIN = 0.1;
const TP_MIN = 0.05, TP_MAX = 5;
const TRAIL_MIN = 0.05, TRAIL_MAX = 2;
const RUN_DAYS = 30;
const MIN_TRADES_FOR_DECISION = 30;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

function timingSafeEqual(a, b) {
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

const nowIso = () => new Date().toISOString();
const botOf = (v) => (v && String(v)) || "book2";

async function audit(env, botId, action, detail) {
  await env.DB.prepare("INSERT INTO bot_config_audit (bot_id, ts, action, detail) VALUES (?, ?, ?, ?)")
    .bind(botId, nowIso(), action, detail ? JSON.stringify(detail) : null).run();
}

// ---------------------------------------------------------------- runner -> journal

async function handleEvent(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (env.INGEST_TOKEN && auth !== `Bearer ${env.INGEST_TOKEN}`) {
    return json({ error: "unauthorized" }, 401);
  }

  const body = await request.json();
  const { event, ...payload } = body;
  const botId = botOf(payload.bot_id);

  await env.DB.prepare("INSERT INTO events (event_type, payload) VALUES (?, ?)")
    .bind(event || "unknown", JSON.stringify(payload))
    .run();

  if (event === "heartbeat") {
    await env.DB.prepare("UPDATE bots SET last_seen_at = ?, config_source = ? WHERE bot_id = ?")
      .bind(nowIso(), payload.config_source ?? null, botId).run();
  }

  if (event === "bar_evaluated" && payload.bar_time) {
    await env.DB.prepare("UPDATE bots SET last_bar = ? WHERE bot_id = ?").bind(payload.bar_time, botId).run();
  }

  if (event === "flatten_done") {
    await env.DB.prepare("UPDATE bots SET flatten_requested = 0, updated_at = ? WHERE bot_id = ?")
      .bind(nowIso(), botId).run();
    await audit(env, botId, "flatten_done", null);
  }

  if (event === "position_opened") {
    const sizing = payload.sizing || {};
    await env.DB.prepare(
      `INSERT INTO trades (trade_id, entry_time, direction, instrument, setup_score, score_components, entry_price, stop_price, target_price, lots, rr_planned,
                           bot_id, setup, risk_pct, realized_risk_pct, tp_pct, trail_pct, config_version, run_id)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(trade_id) DO UPDATE SET
         entry_time=excluded.entry_time, direction=excluded.direction, instrument=excluded.instrument, setup_score=excluded.setup_score,
         score_components=excluded.score_components, entry_price=excluded.entry_price, stop_price=excluded.stop_price,
         target_price=excluded.target_price, lots=excluded.lots, rr_planned=excluded.rr_planned,
         bot_id=excluded.bot_id, setup=excluded.setup, risk_pct=excluded.risk_pct, realized_risk_pct=excluded.realized_risk_pct,
         tp_pct=excluded.tp_pct, trail_pct=excluded.trail_pct, config_version=excluded.config_version, run_id=excluded.run_id`
    ).bind(
      payload.trade_id, payload.entry_time, payload.direction, payload.instrument || "EUR_USD",
      payload.score ?? null,
      payload.components ? JSON.stringify(payload.components) : null,
      payload.entry_price, payload.stop_price, payload.target_price, payload.lots,
      payload.stop_price && payload.entry_price && payload.target_price
        ? Math.abs((payload.target_price - payload.entry_price) / (payload.entry_price - payload.stop_price))
        : null,
      botId, payload.setup ?? null, payload.risk_pct ?? null, sizing.realized_risk_pct ?? null,
      payload.tp_pct ?? null, payload.trail_pct ?? null, payload.config_version ?? null, payload.run_id ?? null
    ).run();
  }

  if (event === "trade_closed") {
    await env.DB.prepare(
      `UPDATE trades SET exit_time = ?, exit_price = ?, realized_pnl = ? WHERE trade_id = ?`
    ).bind(payload.close_time || null, payload.exit_price || null, payload.realized_pnl, payload.trade_id).run();
  }

  if (payload.review_state || payload.equity != null || event === "REVIEW_TRIGGER_FIRED" || event === "daily_loss_halt_triggered") {
    const rs = payload.review_state || {};
    await env.DB.prepare(
      `INSERT INTO account_snapshots (ts, equity, peak_equity, drawdown_pct, losing_streak, review_triggered, review_reason, day_loss_halted, bot_id)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(
      nowIso(),
      payload.equity ?? null,
      rs.peak_equity ?? null,
      rs.peak_equity && payload.equity ? (rs.peak_equity - payload.equity) / rs.peak_equity : null,
      rs.current_losing_streak ?? null,
      rs.triggered ? 1 : 0,
      rs.triggered_reason ?? payload.reason ?? null,
      event === "daily_loss_halt_triggered" ? 1 : 0,
      botId
    ).run();
  }

  return json({ ok: true });
}

// ---------------------------------------------------------------- control plane

async function getBotConfig(request, env, url) {
  if (!env.INGEST_TOKEN) return json({ error: "bot-config disabled: INGEST_TOKEN secret not set" }, 503);
  const auth = request.headers.get("Authorization") || "";
  if (!timingSafeEqual(auth, `Bearer ${env.INGEST_TOKEN}`)) return json({ error: "unauthorized" }, 401);
  const bot = await env.DB.prepare("SELECT * FROM bots WHERE bot_id = ?").bind(url.searchParams.get("bot") || "").first();
  if (!bot) return json({ error: "unknown bot" }, 404);
  return json({
    bot_id: bot.bot_id, enabled: !!bot.enabled, flatten_requested: !!bot.flatten_requested,
    risk_pct: bot.risk_pct, tp_pct: bot.tp_pct, trail_pct: bot.trail_pct,
    version: bot.version, run_id: bot.run_id,
  });
}

async function getBots(env) {
  const { results } = await env.DB.prepare("SELECT * FROM bots ORDER BY CASE bot_id WHEN 'book2' THEN 0 ELSE 1 END, bot_id").all();
  const out = [];
  for (const b of results) {
    const run = await env.DB.prepare(
      `SELECT COUNT(*) AS n, SUM(CASE WHEN exit_time IS NOT NULL THEN 1 ELSE 0 END) AS closed
       FROM trades WHERE COALESCE(bot_id,'book2') = ? AND COALESCE(run_id, 1) = ?`
    ).bind(b.bot_id, b.run_id).first();
    out.push({
      ...b, enabled: !!b.enabled, flatten_requested: !!b.flatten_requested, params_editable: !!b.params_editable,
      run_trades: run?.n || 0, run_closed: run?.closed || 0,
      run_days: b.run_started_at ? Math.floor((Date.now() - Date.parse(b.run_started_at)) / 86400000) : null,
      run_target_days: RUN_DAYS, run_target_trades: MIN_TRADES_FOR_DECISION,
      limits: { risk_max: RISK_MAX, risk_min: RISK_MIN, tp_min: TP_MIN, tp_max: TP_MAX, trail_min: TRAIL_MIN, trail_max: TRAIL_MAX },
    });
  }
  return { bots: out, server_time: nowIso() };
}

function parseParams(p) {
  const out = {}, errors = [];
  const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
  if ("risk_pct" in p) {
    const v = num(p.risk_pct);
    if (v === null || !Number.isFinite(v) || v < RISK_MIN || v > RISK_MAX) errors.push(`risk_pct must be ${RISK_MIN}-${RISK_MAX}`);
    else out.risk_pct = v;
  }
  if ("tp_pct" in p) {
    const v = num(p.tp_pct);
    if (v !== null && (!Number.isFinite(v) || v < TP_MIN || v > TP_MAX)) errors.push(`tp_pct must be blank or ${TP_MIN}-${TP_MAX}`);
    else out.tp_pct = v;
  }
  if ("trail_pct" in p) {
    const v = num(p.trail_pct);
    if (v !== null && v !== 0 && (!Number.isFinite(v) || v < TRAIL_MIN || v > TRAIL_MAX)) errors.push(`trail_pct must be blank, 0 (off) or ${TRAIL_MIN}-${TRAIL_MAX}`);
    else out.trail_pct = v;
  }
  return { out, errors };
}

async function adminBot(request, env) {
  if (!env.ADMIN_TOKEN) return json({ error: "admin disabled: ADMIN_TOKEN secret not set" }, 503);
  const auth = request.headers.get("Authorization") || "";
  if (!timingSafeEqual(auth, `Bearer ${env.ADMIN_TOKEN}`)) return json({ error: "unauthorized" }, 401);

  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }
  const { bot_id: botId, action } = body;
  const bot = await env.DB.prepare("SELECT * FROM bots WHERE bot_id = ?").bind(botId || "").first();
  if (!bot) return json({ error: "unknown bot" }, 404);
  const now = nowIso();

  if (action === "start") {
    await env.DB.prepare(
      "UPDATE bots SET enabled = 1, flatten_requested = 0, run_started_at = COALESCE(run_started_at, ?), updated_at = ? WHERE bot_id = ?"
    ).bind(now, now, botId).run();
    await audit(env, botId, "start", null);
  } else if (action === "stop") {
    await env.DB.prepare("UPDATE bots SET enabled = 0, updated_at = ? WHERE bot_id = ?").bind(now, botId).run();
    await audit(env, botId, "stop", null);
  } else if (action === "flatten") {
    await env.DB.prepare("UPDATE bots SET enabled = 0, flatten_requested = 1, updated_at = ? WHERE bot_id = ?").bind(now, botId).run();
    await audit(env, botId, "stop_and_flatten", null);
  } else if (action === "set_params") {
    if (!bot.params_editable) {
      return json({ error: "params_locked", message: "This test's parameters are locked so its validation run stays unmodified." }, 403);
    }
    const { out, errors } = parseParams(body.params || {});
    if (errors.length) return json({ error: "invalid_params", messages: errors }, 400);
    const next = { risk_pct: bot.risk_pct, tp_pct: bot.tp_pct, trail_pct: bot.trail_pct, ...out };
    const changed = ["risk_pct", "tp_pct", "trail_pct"].some((k) => next[k] !== bot[k]);
    if (!changed) return json({ ok: true, unchanged: true });

    const inRun = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM trades WHERE COALESCE(bot_id,'book2') = ? AND COALESCE(run_id, 1) = ?"
    ).bind(botId, bot.run_id).first();
    const restart = (inRun?.n || 0) > 0;
    if (restart && !body.confirm_restart) {
      return json({
        error: "restart_required",
        message: `Trades have already been taken in run #${bot.run_id}. Changing parameters starts run #${bot.run_id + 1} with a fresh 30-day clock and trade count; run #${bot.run_id} stays in the journal.`,
      }, 409);
    }
    await env.DB.prepare(
      `UPDATE bots SET risk_pct = ?, tp_pct = ?, trail_pct = ?, version = version + 1,
         run_id = run_id + ?, run_started_at = ?, updated_at = ? WHERE bot_id = ?`
    ).bind(next.risk_pct, next.tp_pct, next.trail_pct, restart ? 1 : 0,
           restart ? (bot.enabled ? now : null) : bot.run_started_at, now, botId).run();
    await audit(env, botId, restart ? "set_params_new_run" : "set_params", { before: { risk_pct: bot.risk_pct, tp_pct: bot.tp_pct, trail_pct: bot.trail_pct }, after: next });
  } else {
    return json({ error: "unknown action" }, 400);
  }
  const fresh = (await getBots(env)).bots.find((b) => b.bot_id === botId);
  return json({ ok: true, bot: fresh });
}

// ---------------------------------------------------------------- journal / summary

const FILTER_COLS = ["setup", "instrument", "risk_pct", "tp_pct", "trail_pct", "run_id"];
const NUMERIC_COLS = new Set(["risk_pct", "tp_pct", "trail_pct", "run_id"]);

function filterSql(url) {
  const bot = url.searchParams.get("bot") || "book2";
  const where = ["COALESCE(bot_id,'book2') = ?"], binds = [bot];
  for (const col of FILTER_COLS) {
    const v = url.searchParams.get(col);
    if (v === null || v === "" || v === "all") continue;
    if (v === "default") { where.push(`${col} IS NULL`); continue; }
    where.push(`${col} = ?`);
    binds.push(NUMERIC_COLS.has(col) ? Number(v) : v);
  }
  return { bot, where: where.join(" AND "), binds };
}

async function getFacets(env, bot) {
  const facets = {};
  for (const col of FILTER_COLS) {
    const { results } = await env.DB.prepare(
      `SELECT DISTINCT ${col} AS v FROM trades WHERE COALESCE(bot_id,'book2') = ? ORDER BY v`
    ).bind(bot).all();
    facets[col] = results.map((r) => (r.v === null ? "default" : r.v));
  }
  return facets;
}

async function getJournal(env, url) {
  const f = filterSql(url);
  const { results } = await env.DB.prepare(
    `SELECT * FROM trades WHERE ${f.where} ORDER BY entry_time DESC LIMIT 500`
  ).bind(...f.binds).all();

  let wins = 0, losses = 0, grossWin = 0, grossLoss = 0;
  const rows = results.map((t) => {
    const closed = t.exit_time != null;
    if (closed && t.realized_pnl != null) {
      if (t.realized_pnl > 0) { wins++; grossWin += t.realized_pnl; }
      else if (t.realized_pnl < 0) { losses++; grossLoss += Math.abs(t.realized_pnl); }
    }
    const riskDist = t.entry_price != null && t.stop_price != null ? Math.abs(t.entry_price - t.stop_price) : null;
    let rrActual = null;
    if (closed && riskDist && t.exit_price != null) {
      const realizedDist = t.direction === "long" ? t.exit_price - t.entry_price : t.entry_price - t.exit_price;
      rrActual = riskDist ? +(realizedDist / riskDist).toFixed(2) : null;
    }
    let components = null;
    try { components = t.score_components ? JSON.parse(t.score_components) : null; } catch (_) {}
    return { ...t, score_components: components, status: closed ? (t.realized_pnl > 0 ? "win" : t.realized_pnl < 0 ? "loss" : "flat") : "open", rr_actual: rrActual };
  });

  const totalClosed = wins + losses;
  return {
    bot: f.bot,
    trades: rows,
    facets: await getFacets(env, f.bot),
    metrics: {
      total_closed: totalClosed,
      wins, losses,
      win_rate: totalClosed ? +((wins / totalClosed) * 100).toFixed(1) : null,
      profit_factor: grossLoss > 0 ? +(grossWin / grossLoss).toFixed(2) : null,
      net_pnl: +(grossWin - grossLoss).toFixed(2),
    },
  };
}

async function getSummary(env, url) {
  const journal = await getJournal(env, url);
  const bot = journal.bot;
  const latestSnapshot = await env.DB.prepare(
    `SELECT * FROM account_snapshots WHERE COALESCE(bot_id,'book2') = ? ORDER BY id DESC LIMIT 1`
  ).bind(bot).first();

  const { results: snapshots } = await env.DB.prepare(
    `SELECT ts, equity FROM account_snapshots WHERE equity IS NOT NULL AND COALESCE(bot_id,'book2') = ? ORDER BY id ASC LIMIT 2000`
  ).bind(bot).all();

  const { results: recentEvents } = await env.DB.prepare(
    `SELECT event_type, payload, logged_at FROM events
     WHERE COALESCE(json_extract(payload, '$.bot_id'), 'book2') = ? AND event_type NOT IN ('heartbeat')
     ORDER BY id DESC LIMIT 20`
  ).bind(bot).all();

  return {
    bot,
    metrics: journal.metrics,
    equity_curve: snapshots,
    review_state: latestSnapshot
      ? {
          triggered: !!latestSnapshot.review_triggered,
          reason: latestSnapshot.review_reason,
          drawdown_pct: latestSnapshot.drawdown_pct,
          losing_streak: latestSnapshot.losing_streak,
          day_loss_halted: !!latestSnapshot.day_loss_halted,
          as_of: latestSnapshot.ts,
        }
      : null,
    recent_events: recentEvents.map((e) => ({ ...e, payload: JSON.parse(e.payload || "{}") })),
  };
}

// ---------------------------------------------------------------- dashboard
// NOTE: the client script below deliberately uses no template literals, so this outer template literal needs no escaping.

const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Confluence paper trading journal</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 1080px; margin: 0 auto; padding: 24px 20px 60px;
    background: #fafaf9; color: #1a1a18;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #161614; color: #ececea; }
    .card, .panel { background: #201f1d !important; border-color: #34322f !important; }
    .muted { color: #9a9890 !important; }
    table th { color: #9a9890 !important; border-color: #34322f !important; }
    table td { border-color: #2a2926 !important; }
    input, select { background: #161614 !important; color: #ececea !important; border-color: #4a4843 !important; }
    .seg button { background: #201f1d !important; color: #ececea !important; border-color: #34322f !important; }
    .seg button.on { background: #2a78d6 !important; color: #fff !important; }
    button.act { background: #201f1d !important; color: #ececea !important; border-color: #4a4843 !important; }
  }
  h1 { font-size: 20px; font-weight: 500; margin: 0 0 4px; }
  .muted { color: #6b6a64; font-size: 13px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 20px 0; }
  .card { background: #fff; border: 1px solid #e5e3dd; border-radius: 10px; padding: 14px 16px; }
  .card .label { font-size: 12px; color: #6b6a64; margin-bottom: 4px; }
  .card .value { font-size: 22px; font-weight: 500; }
  .panel { background: #fff; border: 1px solid #e5e3dd; border-radius: 10px; padding: 14px 16px; margin-top: 14px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 12px; font-weight: 500; }
  .badge.ok { background: #e7f3e3; color: #2b6b1f; }
  .badge.warn { background: #fbe8e6; color: #a02e21; }
  .badge.idle { background: #eeece5; color: #55534d; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eeece5; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: #6b6a64; font-weight: 500; }
  .win { color: #2b6b1f; } .loss { color: #a02e21; } .open { color: #8a6d1d; }
  section { margin-top: 32px; }
  h2 { font-size: 15px; font-weight: 500; margin: 0 0 4px; }
  #chartWrap { position: relative; height: 220px; margin-top: 12px; }
  .factors { font-size: 12px; white-space: nowrap; } .factors .f { display: inline-block; padding: 1px 6px; margin: 1px 2px 1px 0; border-radius: 4px; background: #eeece5; color: #3a3935; }
  .factors .f.pen { background: #fbe8e6; color: #a02e21; }
  @media (prefers-color-scheme: dark) { .factors .f { background: #2a2926; color: #d8d6cf; } .factors .f.pen { background: #3a1f1c; color: #f0a59b; } .badge.idle { background: #2a2926; color: #bdbbb3; } }
  #journalTable, #eventsTable { overflow-x: auto; }
  .empty { padding: 24px; text-align: center; color: #6b6a64; font-size: 13px; }
  .seg { display: flex; gap: 8px; margin: 16px 0 0; flex-wrap: wrap; }
  .seg button { flex: 1 1 260px; text-align: left; padding: 10px 14px; border: 1px solid #e5e3dd; background: #fff; border-radius: 10px; cursor: pointer; font-size: 14px; color: inherit; }
  .seg button.on { background: #2a78d6; color: #fff; border-color: #2a78d6; }
  .seg .sub { display: block; font-size: 12px; opacity: .8; margin-top: 2px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
  .field { display: flex; flex-direction: column; font-size: 12px; color: #6b6a64; gap: 3px; }
  input, select { font: inherit; font-size: 13px; padding: 6px 8px; border: 1px solid #cfccc3; border-radius: 6px; background: #fff; color: inherit; min-width: 110px; }
  input:disabled { opacity: .6; }
  button.act { font: inherit; font-size: 13px; padding: 7px 14px; border: 1px solid #cfccc3; border-radius: 6px; background: #fff; cursor: pointer; color: inherit; }
  button.act.go { background: #2b6b1f; color: #fff; border-color: #2b6b1f; }
  button.act.stop { background: #a02e21; color: #fff; border-color: #a02e21; }
  button.act:disabled { opacity: .5; cursor: default; }
  #msg { font-size: 13px; margin-top: 8px; min-height: 18px; }
  #msg.err { color: #a02e21; } #msg.good { color: #2b6b1f; }
</style>
</head>
<body>
  <h1>Confluence paper trading journal</h1>
  <p class="muted" id="asOf">Loading...</p>

  <div class="seg" id="tabs"></div>

  <div class="panel" id="controlPanel"></div>

  <div class="panel">
    <div class="row">
      <div class="field"><span>Setup</span><select id="f_setup"></select></div>
      <div class="field"><span>Instrument</span><select id="f_instrument"></select></div>
      <div class="field"><span>Risk % profile</span><select id="f_risk_pct"></select></div>
      <div class="field"><span>Target % profile</span><select id="f_tp_pct"></select></div>
      <div class="field"><span>Trailing stop % profile</span><select id="f_trail_pct"></select></div>
      <div class="field"><span>Run</span><select id="f_run_id"></select></div>
      <button class="act" id="resetFilters">Reset filters</button>
    </div>
    <p class="muted" style="margin:8px 0 0">Filters show only trades taken with the selected settings (every trade records the settings it used). The equity curve is account-level and is not filtered.</p>
  </div>

  <div class="cards" id="cards"></div>

  <section>
    <h2>Equity curve</h2>
    <div id="chartWrap"><canvas id="equityChart" role="img" aria-label="Equity curve over the paper trading run">Equity curve chart</canvas></div>
  </section>

  <section>
    <h2>Trade journal</h2>
    <p class="muted" id="journalNote"></p>
    <div id="journalTable"></div>
  </section>

  <section>
    <h2>Recent activity</h2>
    <div id="eventsTable"></div>
  </section>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
var FILTERS = ['setup', 'instrument', 'risk_pct', 'tp_pct', 'trail_pct', 'run_id'];
var TEST_NAMES = { book2: 'Test 1', setups: 'Test 2' };
var state = { bot: 'book2', bots: [], chart: null };
try { state.bot = localStorage.getItem('journal_bot') || 'book2'; } catch (e) {}

function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
function $(id) { return document.getElementById(id); }
function fmtPct(v) { return v == null ? '--' : v.toFixed(1) + '%'; }
function fmtNum(v) { return v == null ? '--' : v.toFixed(2); }
function getToken() { try { return sessionStorage.getItem('admin_token') || ''; } catch (e) { return ''; } }
function ago(iso) {
  if (!iso) return null;
  var m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (m < 1) return 'just now';
  if (m < 90) return m + ' min ago';
  if (m < 2880) return Math.round(m / 60) + ' h ago';
  return Math.round(m / 1440) + ' d ago';
}
function currentBot() { return state.bots.filter(function (b) { return b.bot_id === state.bot; })[0]; }
function filterQuery() {
  var q = 'bot=' + encodeURIComponent(state.bot);
  FILTERS.forEach(function (f) { var el = $('f_' + f); if (el && el.value && el.value !== 'all') q += '&' + f + '=' + encodeURIComponent(el.value); });
  return q;
}
function setMsg(text, kind) { state.msg = { text: text, kind: kind }; var m = $('msg'); if (m) { m.textContent = text || ''; m.className = kind || ''; } }

function renderTabs() {
  $('tabs').innerHTML = state.bots.map(function (b) {
    var seen = b.last_seen_at ? 'runner seen ' + ago(b.last_seen_at) : 'no runner heartbeat yet';
    var st = b.flatten_requested ? 'FLATTENING' : (b.enabled ? 'RUNNING' : 'PAUSED');
    return '<button data-bot="' + esc(b.bot_id) + '" class="' + (b.bot_id === state.bot ? 'on' : '') + '">' +
      esc(TEST_NAMES[b.bot_id] || b.bot_id) + ' &middot; ' + esc(b.label) +
      '<span class="sub">' + st + ' &middot; ' + esc(seen) + ' &middot; run #' + b.run_id + ': ' + b.run_closed + ' closed trades</span></button>';
  }).join('');
  Array.prototype.forEach.call($('tabs').querySelectorAll('button'), function (btn) {
    btn.onclick = function () {
      state.bot = btn.getAttribute('data-bot');
      try { localStorage.setItem('journal_bot', state.bot); } catch (e) {}
      FILTERS.forEach(function (f) { var el = $('f_' + f); if (el) el.value = 'all'; });
      state.msg = null; renderTabs(); renderControl(); load();
    };
  });
}

function renderControl() {
  var b = currentBot();
  if (!b) { $('controlPanel').innerHTML = ''; return; }
  var status = b.flatten_requested ? '<span class="badge warn">FLATTEN PENDING</span>'
    : (b.enabled ? '<span class="badge ok">RUNNING</span>' : '<span class="badge idle">PAUSED</span>');
  var stale = b.last_seen_at && (Date.now() - Date.parse(b.last_seen_at)) > 45 * 60000;
  var seen = b.last_seen_at ? 'Runner last seen ' + ago(b.last_seen_at) + (stale ? ' &mdash; <b class="loss">no heartbeat, is the cron job running?</b>' : '')
    : 'Runner has not reported yet';
  var run = 'Run #' + b.run_id + (b.run_started_at ? ' &middot; started ' + new Date(b.run_started_at).toLocaleDateString() + ' &middot; day ' + (b.run_days + 1) + (b.run_days + 1 > b.run_target_days ? ' (30-day minimum met)' : ' of ' + b.run_target_days) : ' &middot; clock starts when you press Start') +
    ' &middot; ' + b.run_closed + ' of ' + b.run_target_trades + ' closed trades needed before a go-live decision';
  var ed = b.params_editable;
  var v = function (x) { return x == null ? '' : x; };
  var html = '<div class="row" style="justify-content:space-between"><div><b>' + esc(TEST_NAMES[b.bot_id] || b.bot_id) + '</b> ' + status +
    '<div class="muted" style="margin-top:4px">' + seen + '</div><div class="muted">' + run + '</div></div>' +
    '<div class="row">' +
    '<div class="field"><span>Admin token</span><input type="password" id="adminToken" placeholder="ADMIN_TOKEN" value="' + esc(getToken()) + '"></div>' +
    '<button class="act go" id="btnStart"' + (b.enabled && !b.flatten_requested ? ' disabled' : '') + '>Start</button>' +
    '<button class="act stop" id="btnStop"' + (!b.enabled ? ' disabled' : '') + '>Stop (no new trades)</button>' +
    '<button class="act" id="btnFlat">Stop &amp; close open trades</button></div></div>' +
    '<div class="row" style="margin-top:14px">' +
    '<div class="field"><span>Risk % per trade (max ' + b.limits.risk_max + ')</span><input id="p_risk" type="number" step="0.05" min="' + b.limits.risk_min + '" max="' + b.limits.risk_max + '" value="' + v(b.risk_pct) + '"' + (ed ? '' : ' disabled') + '></div>' +
    '<div class="field"><span>Target % of price (blank = 1:2 R:R)</span><input id="p_tp" type="number" step="0.05" value="' + v(b.tp_pct) + '" placeholder="rules default"' + (ed ? '' : ' disabled') + '></div>' +
    '<div class="field"><span>Trailing stop % (blank = 1%, 0 = off)</span><input id="p_trail" type="number" step="0.05" value="' + v(b.trail_pct) + '" placeholder="1% default"' + (ed ? '' : ' disabled') + '></div>' +
    (ed ? '<button class="act" id="btnApply">Apply parameters</button>' : '<span class="muted">Parameters are locked for this test so its validation run stays unmodified.</span>') +
    '</div><div id="msg"></div>' +
    (ed ? '<p class="muted" style="margin:6px 0 0">Parameter changes affect new trades only. After trades exist in the current run, a change starts a new run (fresh 30-day clock) and asks you to confirm.</p>' : '');
  $('controlPanel').innerHTML = html;
  $('adminToken').onchange = function () { try { sessionStorage.setItem('admin_token', $('adminToken').value); } catch (e) {} };
  $('btnStart').onclick = function () { act('start'); };
  $('btnStop').onclick = function () { act('stop'); };
  $('btnFlat').onclick = function () { if (confirm('Stop this test and close its open trades at market on the next runner pass (within ~15 min)?')) act('flatten'); };
  if (ed) $('btnApply').onclick = function () { applyParams(false); };
  if (state.msg) setMsg(state.msg.text, state.msg.kind);
}

function post(body) {
  try { sessionStorage.setItem('admin_token', $('adminToken').value); } catch (e) {}
  return fetch('/api/admin/bot', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + $('adminToken').value }, body: JSON.stringify(body) })
    .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
}
function act(action) {
  setMsg('Working...');
  post({ bot_id: state.bot, action: action }).then(function (r) {
    if (r.status === 200) { setMsg(action === 'start' ? 'Started. The runner picks this up on its next pass (within ~15 min).' : 'Done. The runner picks this up on its next pass (within ~15 min).', 'good'); refreshBots(true); }
    else setMsg(r.body.error + (r.body.message ? ': ' + r.body.message : ''), 'err');
  }).catch(function (e) { setMsg(String(e), 'err'); });
}
function applyParams(confirmRestart) {
  var params = { risk_pct: $('p_risk').value, tp_pct: $('p_tp').value, trail_pct: $('p_trail').value };
  setMsg('Working...');
  post({ bot_id: state.bot, action: 'set_params', params: params, confirm_restart: !!confirmRestart }).then(function (r) {
    if (r.status === 200) { setMsg(r.body.unchanged ? 'No change.' : 'Saved. Applies to new trades from the next runner pass.', 'good'); refreshBots(true); }
    else if (r.status === 409) { if (confirm(r.body.message + '\\n\\nContinue?')) applyParams(true); else setMsg('Cancelled.', ''); }
    else setMsg(r.body.error + (r.body.messages ? ': ' + r.body.messages.join('; ') : (r.body.message ? ': ' + r.body.message : '')), 'err');
  }).catch(function (e) { setMsg(String(e), 'err'); });
}

function fillFilters(facets) {
  var labels = { setup: 'All setups', instrument: 'All instruments', risk_pct: 'All risk %', tp_pct: 'All targets', trail_pct: 'All trailing', run_id: 'All runs' };
  var suffix = { risk_pct: '%', tp_pct: '%', trail_pct: '%' };
  var defLabel = { risk_pct: 'strategy default', tp_pct: 'rules default (1:2 R:R / 2R)', trail_pct: 'rules default (1% / none)' };
  FILTERS.forEach(function (f) {
    var el = $('f_' + f); var cur = el.value || 'all';
    var opts = ['<option value="all">' + labels[f] + '</option>'];
    (facets[f] || []).forEach(function (val) {
      if (val === null || val === 'default' && f === 'setup') return;
      var text = val === 'default' ? defLabel[f] || 'default' : (f === 'run_id' ? 'Run #' + val : val + (suffix[f] || ''));
      opts.push('<option value="' + esc(val) + '">' + esc(text) + '</option>');
    });
    el.innerHTML = opts.join('');
    el.value = Array.prototype.some.call(el.options, function (o) { return o.value === String(cur); }) ? cur : 'all';
  });
}

function fmtFactors(c) {
  if (!c) return '--';
  var LABELS = { chart_pattern: 'Pattern', candle: 'Candle', volume: 'Volume', vwap: 'VWAP', vol_profile: 'Vol profile' };
  if (c.raw_total !== undefined) {
    var chips = Object.keys(LABELS).filter(function (k) { return c[k] > 0; }).map(function (k) { return '<span class="f">' + LABELS[k] + ' +' + c[k] + '</span>'; });
    if (c.mtf_penalty > 0) chips.push('<span class="f pen">MTF -' + c.mtf_penalty + '</span>');
    return chips.join('') || '--';
  }
  return Object.keys(c).filter(function (k) { return c[k] === true; }).map(function (k) { return '<span class="f">' + esc(k.replace(/_/g, ' ')) + '</span>'; }).join('') || '--';
}

function load() {
  var q = filterQuery();
  return Promise.all([fetch('/api/summary?' + q).then(function (r) { return r.json(); }), fetch('/api/journal?' + q).then(function (r) { return r.json(); })]).then(function (res) {
    var summary = res[0], journal = res[1];
    $('asOf').textContent = 'Last updated ' + new Date().toLocaleString();
    fillFilters(journal.facets || {});

    var m = summary.metrics, rs = summary.review_state;
    $('cards').innerHTML = [
      ['Closed trades', m.total_closed], ['Win rate', fmtPct(m.win_rate)], ['Profit factor', fmtNum(m.profit_factor)],
      ['Net PnL', m.net_pnl == null ? '--' : (m.net_pnl >= 0 ? '+' : '') + m.net_pnl.toFixed(2)]
    ].map(function (p) { return '<div class="card"><div class="label">' + p[0] + '</div><div class="value">' + p[1] + '</div></div>'; }).join('') +
      (rs ? '<div class="card"><div class="label">Review trigger</div><div class="value"><span class="badge ' + (rs.triggered ? 'warn' : 'ok') + '">' + (rs.triggered ? 'FIRED' : 'clear') + '</span></div></div>' : '');

    if (state.chart) { state.chart.destroy(); state.chart = null; }
    var curve = summary.equity_curve || [];
    if (!$('equityChart')) $('chartWrap').innerHTML = '<canvas id="equityChart" role="img" aria-label="Equity curve over the paper trading run">Equity curve chart</canvas>';
    if (curve.length > 1) {
      state.chart = new Chart($('equityChart'), {
        type: 'line',
        data: { labels: curve.map(function (p) { return new Date(p.ts).toLocaleDateString(); }),
                datasets: [{ data: curve.map(function (p) { return p.equity; }), borderColor: '#2a78d6', borderWidth: 2, pointRadius: 0, tension: 0.1 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { maxTicksLimit: 8 } } } }
      });
    } else {
      $('chartWrap').innerHTML = '<div class="empty">No equity snapshots yet -- accumulates as trades close.</div>';
    }

    var isBook2 = state.bot === 'book2';
    $('journalNote').textContent = isBook2
      ? 'Book 2 Ch. 11.2 fields: entry/stop/target, realized exit, R:R planned vs. actual.'
      : '3-setup rules: structural stop, 1:2 R:R target, 1% trailing stop. Risk shows requested vs. actually taken after the 5% margin cap.';
    var trades = journal.trades || [];
    if (trades.length === 0) {
      $('journalTable').innerHTML = '<div class="empty">No trades logged yet.</div>';
    } else {
      $('journalTable').innerHTML = '<table><tr><th>Entry time</th><th>Pair</th><th>Dir</th><th>' + (isBook2 ? 'Score' : 'Setup') + '</th><th>Factors</th><th>Entry</th><th>Stop</th><th>Target</th>' +
        '<th>Exit</th><th>R:R planned</th><th>R:R actual</th><th>Risk %</th><th>Trail %</th><th>PnL</th><th>Status</th></tr>' +
        trades.map(function (t) {
          var risk = t.risk_pct == null ? '--' : (t.risk_pct + (t.realized_risk_pct != null ? ' &rarr; ' + t.realized_risk_pct.toFixed(2) : ''));
          return '<tr><td>' + (t.entry_time ? new Date(t.entry_time).toLocaleString() : '--') + '</td>' +
            '<td>' + esc((t.instrument || '').replace('_', '/')) + '</td><td>' + esc(t.direction || '--') + '</td>' +
            '<td>' + (isBook2 ? esc(t.setup_score == null ? '--' : t.setup_score) : esc(t.setup || '--')) + '</td>' +
            '<td class="factors">' + fmtFactors(t.score_components) + '</td>' +
            '<td>' + esc(t.entry_price == null ? '--' : t.entry_price) + '</td><td>' + esc(t.stop_price == null ? '--' : t.stop_price) + '</td><td>' + esc(t.target_price == null ? '--' : t.target_price) + '</td>' +
            '<td>' + esc(t.exit_price == null ? '--' : t.exit_price) + '</td>' +
            '<td>' + (t.rr_planned != null ? t.rr_planned.toFixed(2) : '--') + '</td><td>' + (t.rr_actual != null ? t.rr_actual.toFixed(2) : '--') + '</td>' +
            '<td>' + risk + '</td><td>' + (t.trail_pct == null ? '--' : t.trail_pct) + '</td>' +
            '<td class="' + t.status + '">' + (t.realized_pnl != null ? t.realized_pnl.toFixed(2) : '--') + '</td><td class="' + t.status + '">' + t.status + '</td></tr>';
        }).join('') + '</table>';
    }

    var events = summary.recent_events || [];
    $('eventsTable').innerHTML = events.length === 0 ? '<div class="empty">No events yet.</div>' :
      '<table><tr><th>Time</th><th>Event</th><th>Detail</th></tr>' + events.map(function (e) {
        var p = e.payload || {};
        return '<tr><td>' + new Date(e.logged_at.replace(' ', 'T') + (e.logged_at.indexOf('Z') < 0 && e.logged_at.indexOf('T') < 0 ? 'Z' : '')).toLocaleString() + '</td><td>' + esc(e.event_type) + '</td><td>' +
          esc((p.instrument ? p.instrument.replace('_', '/') + ' ' : '') + (p.reason || p.setup || p.direction || p.bar_time || '')) + '</td></tr>';
      }).join('') + '</table>';
  });
}

function refreshBots(rerenderControl) {
  return fetch('/api/bots').then(function (r) { return r.json(); }).then(function (j) {
    state.bots = j.bots;
    if (!currentBot() && state.bots.length) state.bot = state.bots[0].bot_id;
    renderTabs();
    var active = document.activeElement && document.activeElement.closest && document.activeElement.closest('#controlPanel');
    if (rerenderControl || !active) renderControl();
  });
}

FILTERS.forEach(function (f) { $('f_' + f).onchange = load; });
$('resetFilters').onclick = function () { FILTERS.forEach(function (f) { $('f_' + f).value = 'all'; }); load(); };
refreshBots(true).then(load);
setInterval(function () { refreshBots(false); load(); }, 60000);
</script>
</body>
</html>`;

// ---------------------------------------------------------------- router

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    try {
      if (url.pathname === "/api/event" && request.method === "POST") return await handleEvent(request, env);
      if (url.pathname === "/api/bot-config" && request.method === "GET") return await getBotConfig(request, env, url);
      if (url.pathname === "/api/admin/bot" && request.method === "POST") return await adminBot(request, env);
      if (url.pathname === "/api/bots" && request.method === "GET") return json(await getBots(env));
      if (url.pathname === "/api/journal" && request.method === "GET") return json(await getJournal(env, url));
      if (url.pathname === "/api/summary" && request.method === "GET") return json(await getSummary(env, url));
    } catch (e) {
      return json({ error: String(e) }, 500);
    }

    if (url.pathname === "/" || url.pathname === "") {
      return new Response(DASHBOARD_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }

    return json({ error: "not found" }, 404);
  },
};
