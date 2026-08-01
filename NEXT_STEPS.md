# Confluence Paper Trading — Progress & Roadmap

Track progress, decisions, and planned improvements for the Book 2 paper-trading system.

## Current Status

**System:** Live paper trading on Oracle VM, automating EUR/USD 15-min trades via Oanda practice account.  
**Account:** $1,000 starting capital  
**Risk Per Trade:** 1% (recently fixed from 0.6%)  
**Deployment:** 2026-07-30  
**Last Updated:** 2026-08-01

## Validation Milestones

### ✅ Phase 1: Configuration & Deployment (COMPLETED)

- [x] Deploy Book 2 engine to Oracle VM
- [x] Set up Oanda practice account integration
- [x] Configure cron job for 15-min execution cycle
- [x] Test first trade execution (2026-07-30)
- [x] **FIX:** Correct position sizing to risk exactly 1% per trade (2026-08-01)
  - Changed `size_scale_by_score` from `{7: 0.6, 8: 0.75, 9: 0.9, 10: 1.0}` to all 1.0
  - Clears old logs; backtest restarts fresh
  - Live trading will validate on Monday 2026-08-05 when market opens

### 📊 Phase 2: Backtest Validation (IN PROGRESS)

- [ ] Run full historical backtest (24+ months of EUR/USD 15m data)
  - Current blocker: Historical CSV data needs to be acquired/formatted
  - Once available: `python3 backtest_book2.py` should show:
    - Win rate on historical data
    - Profit factor (should be >1.5 to be viable)
    - Max drawdown (should not exceed 20% on live account)
    - Daily loss streaks (monitor vs. 3% daily circuit breaker)
- [ ] Validate that 1% risk scaling is consistent across all score levels
- [ ] Stress test with tick data (if available) to catch slippage edge cases

### 🎯 Phase 3: Live Paper Trading Validation (PENDING)

**Start Date:** Monday 2026-08-05 (when forex market opens)

Targets:
- [ ] Complete 20+ trades over 4-6 weeks
- [ ] Achieve win rate ≥50% (break-even at 2:1 R:R is ~33%)
- [ ] Maintain daily loss <3% on any day
- [ ] Confirm no trade exceeds 1% loss (validates position sizing)
- [ ] Track and review any trade that breaks rules

**Success criteria:**
- Zero trades exceed 1% loss (position sizing is correct)
- Win rate stays above 40% (not going backwards on live data)
- No daily drawdown exceeds 3% (circuit breaker working)

### 💰 Phase 4: Graduated to Live Trading (FUTURE)

Once paper trading validates:
- [ ] Review 30+ trades and confirm edge still holds on live prices
- [ ] Document decision to move to live account
- [ ] Set up live Oanda account with $5,000 initial capital
- [ ] Deploy live trading runner (different from paper runner, with safeguards)
- [ ] Implement kill-switch and daily loss limit enforcement
- [ ] Run parallel: live trading + paper trading for validation

## Feature Backlog

### High Priority

1. **Backtest with real historical data**
   - Acquire 24+ months EUR/USD 15m data from Oanda or other source
   - Run `backtest_book2.py` to validate statistical edge
   - Compare backtest results to live paper trading (sanity check)

2. **Monitoring & Alerts**
   - Email alert on loss >2% in a day (warning before 3% circuit breaker)
   - Slack notification on trade fills + P&L
   - Daily email summary: trades, win rate, equity curve snapshot
   - Alert if cron job fails to run for >30 minutes

3. **Dashboard Improvements**
   - Real-time equity curve chart
   - Trade setup score distribution (how often triggering score-7 vs 10?)
   - Win rate rolling window (last 10 trades, last 100 trades)
   - Drawdown metrics + underwater plot
   - Trade details on click (entry/exit prices, market conditions)

### Medium Priority

4. **Trade Analysis & Logging**
   - Log market regime at time of entry (above/below 2000-bar SMA?)
   - Log VWAP confidence at entry
   - Log chart pattern type that triggered (flag, sweep, H&S, etc.)
   - Analyze correlation: does certain pattern or regime have higher win rate?

5. **Account Growth Strategy**
   - Define "graduation rules": when to grow account size from $1K → $5K → $25K
   - Plan risk ramp (e.g., stay at 1% risk-per-trade but larger account = larger dollar risk)
   - Document capital preservation targets (stop trading if equity drops below $X)

6. **Multi-Timeframe Analysis**
   - Currently uses 15m bars. Add optional daily/4H context:
     - Only trade if daily trend agrees with 15m signal (reduce false breakouts)
     - Avoid trading against 4H level (wait for conforming move)

### Low Priority (Nice-to-Have)

7. **Performance Optimization**
   - Cache indicator calculations (SMA, VWAP, Volume Profile) across runs
   - Parallelize backtest runs (batch multiple time windows)
   - Profile Python script for slow operations

8. **Live Account Prep**
   - Build live-trading-only runner (separate from paper)
   - Implement pre-trade risk checks (account equity, open positions, API rate limit)
   - Add manual kill-switch and daily loss limit hard stops
   - Integration with 2FA on Oanda login

## Decisions & Assumptions

### Fixed Decisions

1. **1% risk per trade, no score-based scaling** (Decided 2026-08-01)
   - Rationale: Small account ($1K) needs consistent risk sizing until it grows
   - Score-based scaling can be added later once account size supports it
   - All scores risk the same dollar amount, but position size adjusts with stop distance

2. **EUR/USD 15-minute bars only** (Decided 2026-07-30)
   - Rationale: Book 2 was validated on this instrument/timeframe
   - Multi-pair trading can wait until this single pair is proven
   - Adding new timeframes requires re-validation of edge

3. **Oanda practice account only** (Hardcoded, no live endpoint in code)
   - Rationale: Safety first — no real money until paper trading validates
   - Live runner will be deployed as separate codebase when ready

### Open Questions

1. **What historical backtest period should we use?**
   - Current plan: 24 months (2024-01 to 2026-01)
   - Trade-off: Longer = more data, but older data may not reflect current market regime
   - Decision needed: Use 24mo, or focus on more recent 12mo?

2. **When to graduate from paper to live?**
   - Current plan: 30+ live trades with >40% win rate and 0 rule breaks
   - Should we also require a minimum equity growth (e.g., $1K → $1.2K)?
   - Should we run parallel (paper + live) for a month before full transition?

3. **Single pair vs. multi-pair?**
   - Current: EUR/USD only
   - Future: Add GBP/USD, USD/JPY, or other forex pairs?
   - Each pair needs separate validation; when to add?

## Change Log

| Date | Change | Status |
|------|--------|--------|
| 2026-07-30 | Initial deployment to Oracle VM | ✅ |
| 2026-07-30 | First trade executed (5.8% loss) | ⚠️ Issue found |
| 2026-08-01 | Root cause analysis: score-based scaling reduced risk to 0.6% | ✅ Diagnosed |
| 2026-08-01 | Fix: update config to risk 1% across all scores | ✅ Fixed |
| 2026-08-01 | Reset backtest, waiting for forex market to open | ⏳ Pending |
| TBD | Validate first 20+ trades with 1% risk | ⏳ Pending |

## Related Docs

- **RUNBOOK.md** — Operational procedures (how to run, monitor, troubleshoot)
- **README.md** — System architecture and file structure
- **docs/oracle_and_vps_deployment_guide.md** — Server setup instructions
- **Book 2 source** — `engine/confluence_engine_book2.py` (the trading logic)
- **Paper journal** — `paper_trading/journal_book2.py` (trade log viewer)
