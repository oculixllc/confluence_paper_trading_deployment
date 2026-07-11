/**
 * worker/src/index.js
 *
 * Confluence paper-trading journal + dashboard, backed by D1
 * (database: confluence-paper-journal, uuid 01f828b0-c29f-473c-9f7d-29f1f25a914a).
 *
 * Endpoints:
 *   POST /api/event         -- called by oanda_paper_trading_book2.py for every
 *                              logged event (bar_evaluated, position_opened,
 *                              trade_closed, REVIEW_TRIGGER_FIRED,
 *                              daily_loss_halt_triggered, ERROR). Writes a raw
 *                              event row always; upserts into `trades` when the
 *                              event is position_opened/trade_closed; writes an
 *                              account_snapshots row when review-trigger state
 *                              is present in the payload.
 *   GET  /api/journal        -- Book 2 Ch. 11.2 journal fields per trade, plus
 *                              rolling win-rate/PF computed over closed trades.
 *   GET  /api/summary        -- headline metrics + equity curve points +
 *                              current review-trigger / daily-halt status.
 *   GET  /                   -- the dashboard itself (read-only; no config
 *                              editing here by design -- see chat for why).
 *
 * Auth: a shared secret (INGEST_TOKEN, set via `wrangler secret put`) gates
 * POST /api/event so a stray request can't pollute the journal. GET endpoints
 * are intentionally open (read-only dashboard, no sensitive data beyond your
 * own paper-trading P&L) -- tighten with Cloudflare Access if you want this
 * private.
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

async function handleEvent(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (env.INGEST_TOKEN && auth !== `Bearer ${env.INGEST_TOKEN}`) {
    return json({ error: "unauthorized" }, 401);
  }

  const body = await request.json();
  const { event, ...payload } = body;

  await env.DB.prepare("INSERT INTO events (event_type, payload) VALUES (?, ?)")
    .bind(event || "unknown", JSON.stringify(payload))
    .run();

  if (event === "position_opened") {
    await env.DB.prepare(
      `INSERT INTO trades (trade_id, entry_time, direction, setup_score, entry_price, stop_price, target_price, lots, rr_planned)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(trade_id) DO UPDATE SET
         entry_time=excluded.entry_time, direction=excluded.direction, setup_score=excluded.setup_score,
         entry_price=excluded.entry_price, stop_price=excluded.stop_price, target_price=excluded.target_price,
         lots=excluded.lots, rr_planned=excluded.rr_planned`
    ).bind(
      payload.trade_id, payload.entry_time, payload.direction, payload.score,
      payload.entry_price, payload.stop_price, payload.target_price, payload.lots,
      payload.stop_price && payload.entry_price && payload.target_price
        ? Math.abs((payload.target_price - payload.entry_price) / (payload.entry_price - payload.stop_price))
        : null
    ).run();
  }

  if (event === "trade_closed") {
    await env.DB.prepare(
      `UPDATE trades SET exit_time = ?, exit_price = ?, realized_pnl = ? WHERE trade_id = ?`
    ).bind(payload.close_time || null, payload.exit_price || null, payload.realized_pnl, payload.trade_id).run();
  }

  if (payload.review_state || event === "REVIEW_TRIGGER_FIRED" || event === "daily_loss_halt_triggered") {
    const rs = payload.review_state || {};
    await env.DB.prepare(
      `INSERT INTO account_snapshots (ts, equity, peak_equity, drawdown_pct, losing_streak, review_triggered, review_reason, day_loss_halted)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(
      new Date().toISOString(),
      payload.equity ?? null,
      rs.peak_equity ?? null,
      rs.peak_equity && payload.equity ? (rs.peak_equity - payload.equity) / rs.peak_equity : null,
      rs.current_losing_streak ?? null,
      rs.triggered ? 1 : 0,
      rs.triggered_reason ?? payload.reason ?? null,
      event === "daily_loss_halt_triggered" ? 1 : 0
    ).run();
  }

  return json({ ok: true });
}

async function getJournal(env) {
  const { results } = await env.DB.prepare(
    `SELECT * FROM trades ORDER BY entry_time DESC LIMIT 500`
  ).all();

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
    return { ...t, status: closed ? (t.realized_pnl > 0 ? "win" : t.realized_pnl < 0 ? "loss" : "flat") : "open", rr_actual: rrActual };
  });

  const totalClosed = wins + losses;
  return {
    trades: rows,
    metrics: {
      total_closed: totalClosed,
      wins, losses,
      win_rate: totalClosed ? +((wins / totalClosed) * 100).toFixed(1) : null,
      profit_factor: grossLoss > 0 ? +(grossWin / grossLoss).toFixed(2) : null,
      net_pnl: +(grossWin - grossLoss).toFixed(2),
    },
  };
}

async function getSummary(env) {
  const journal = await getJournal(env);
  const latestSnapshot = await env.DB.prepare(
    `SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1`
  ).first();

  const { results: snapshots } = await env.DB.prepare(
    `SELECT ts, equity FROM account_snapshots WHERE equity IS NOT NULL ORDER BY id ASC LIMIT 2000`
  ).all();

  const { results: recentEvents } = await env.DB.prepare(
    `SELECT event_type, payload, logged_at FROM events ORDER BY id DESC LIMIT 20`
  ).all();

  return {
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
    max-width: 980px; margin: 0 auto; padding: 24px 20px 60px;
    background: #fafaf9; color: #1a1a18;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #161614; color: #ececea; }
    .card { background: #201f1d !important; border-color: #34322f !important; }
    .muted { color: #9a9890 !important; }
    table th { color: #9a9890 !important; border-color: #34322f !important; }
    table td { border-color: #2a2926 !important; }
  }
  h1 { font-size: 20px; font-weight: 500; margin: 0 0 4px; }
  .muted { color: #6b6a64; font-size: 13px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 20px 0; }
  .card { background: #fff; border: 1px solid #e5e3dd; border-radius: 10px; padding: 14px 16px; }
  .card .label { font-size: 12px; color: #6b6a64; margin-bottom: 4px; }
  .card .value { font-size: 22px; font-weight: 500; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 12px; font-weight: 500; }
  .badge.ok { background: #e7f3e3; color: #2b6b1f; }
  .badge.warn { background: #fbe8e6; color: #a02e21; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eeece5; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: #6b6a64; font-weight: 500; }
  .win { color: #2b6b1f; } .loss { color: #a02e21; } .open { color: #8a6d1d; }
  section { margin-top: 32px; }
  h2 { font-size: 15px; font-weight: 500; margin: 0 0 4px; }
  #chartWrap { position: relative; height: 220px; margin-top: 12px; }
  .empty { padding: 24px; text-align: center; color: #6b6a64; font-size: 13px; }
</style>
</head>
<body>
  <h1>Confluence paper trading journal</h1>
  <p class="muted" id="asOf">Loading...</p>

  <div class="cards" id="cards"></div>

  <section>
    <h2>Equity curve</h2>
    <div id="chartWrap"><canvas id="equityChart" role="img" aria-label="Equity curve over the paper trading run">Equity curve chart</canvas></div>
  </section>

  <section>
    <h2>Trade journal</h2>
    <p class="muted">Book 2 Ch. 11.2 fields: entry/stop/target, realized exit, R:R planned vs. actual.</p>
    <div id="journalTable"></div>
  </section>

  <section>
    <h2>Recent activity</h2>
    <div id="eventsTable"></div>
  </section>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
async function load() {
  const [summaryRes, journalRes] = await Promise.all([
    fetch('/api/summary').then(r => r.json()),
    fetch('/api/journal').then(r => r.json()),
  ]);

  document.getElementById('asOf').textContent = 'Last updated ' + new Date().toLocaleString();

  const m = summaryRes.metrics;
  const rs = summaryRes.review_state;
  const cards = document.getElementById('cards');
  const fmtPct = (v) => v == null ? '--' : v.toFixed(1) + '%';
  const fmtNum = (v) => v == null ? '--' : v.toFixed(2);
  cards.innerHTML = [
    ['Closed trades', m.total_closed],
    ['Win rate', fmtPct(m.win_rate)],
    ['Profit factor', fmtNum(m.profit_factor)],
    ['Net PnL', m.net_pnl == null ? '--' : (m.net_pnl >= 0 ? '+' : '') + m.net_pnl.toFixed(2)],
  ].map(([label, value]) => \`<div class="card"><div class="label">\${label}</div><div class="value">\${value}</div></div>\`).join('')
  + (rs ? \`<div class="card">
      <div class="label">Review trigger</div>
      <div class="value"><span class="badge \${rs.triggered ? 'warn' : 'ok'}">\${rs.triggered ? 'FIRED' : 'clear'}</span></div>
    </div>\` : '');

  const curve = summaryRes.equity_curve || [];
  if (curve.length > 1) {
    new Chart(document.getElementById('equityChart'), {
      type: 'line',
      data: {
        labels: curve.map(p => new Date(p.ts).toLocaleDateString()),
        datasets: [{ data: curve.map(p => p.equity), borderColor: '#2a78d6', borderWidth: 2, pointRadius: 0, tension: 0.1 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { maxTicksLimit: 8 } } },
      },
    });
  } else {
    document.getElementById('chartWrap').innerHTML = '<div class="empty">No equity snapshots yet -- accumulates as trades close.</div>';
  }

  const trades = journalRes.trades || [];
  const journalDiv = document.getElementById('journalTable');
  if (trades.length === 0) {
    journalDiv.innerHTML = '<div class="empty">No trades logged yet.</div>';
  } else {
    journalDiv.innerHTML = \`<table>
      <tr><th>Entry time</th><th>Dir</th><th>Score</th><th>Entry</th><th>Stop</th><th>Target</th>
          <th>Exit</th><th>R:R planned</th><th>R:R actual</th><th>PnL</th><th>Status</th></tr>
      \${trades.map(t => \`<tr>
        <td>\${t.entry_time ? new Date(t.entry_time).toLocaleString() : '--'}</td>
        <td>\${t.direction || '--'}</td>
        <td>\${t.setup_score ?? '--'}</td>
        <td>\${t.entry_price ?? '--'}</td>
        <td>\${t.stop_price ?? '--'}</td>
        <td>\${t.target_price ?? '--'}</td>
        <td>\${t.exit_price ?? '--'}</td>
        <td>\${t.rr_planned != null ? t.rr_planned.toFixed(2) : '--'}</td>
        <td>\${t.rr_actual != null ? t.rr_actual.toFixed(2) : '--'}</td>
        <td class="\${t.status}">\${t.realized_pnl != null ? t.realized_pnl.toFixed(2) : '--'}</td>
        <td class="\${t.status}">\${t.status}</td>
      </tr>\`).join('')}
    </table>\`;
  }

  const events = summaryRes.recent_events || [];
  const eventsDiv = document.getElementById('eventsTable');
  eventsDiv.innerHTML = events.length === 0 ? '<div class="empty">No events yet.</div>' : \`<table>
    <tr><th>Time</th><th>Event</th><th>Detail</th></tr>
    \${events.map(e => \`<tr>
      <td>\${new Date(e.logged_at).toLocaleString()}</td>
      <td>\${e.event_type}</td>
      <td>\${e.payload.reason || e.payload.direction || e.payload.bar_time || ''}</td>
    </tr>\`).join('')}
  </table>\`;
}
load();
setInterval(load, 60000);
</script>
</body>
</html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    if (url.pathname === "/api/event" && request.method === "POST") {
      try {
        return await handleEvent(request, env);
      } catch (e) {
        return json({ error: String(e) }, 500);
      }
    }

    if (url.pathname === "/api/journal" && request.method === "GET") {
      return json(await getJournal(env));
    }

    if (url.pathname === "/api/summary" && request.method === "GET") {
      return json(await getSummary(env));
    }

    if (url.pathname === "/" || url.pathname === "") {
      return new Response(DASHBOARD_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }

    return json({ error: "not found" }, 404);
  },
};
