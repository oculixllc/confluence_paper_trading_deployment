-- Stores the per-factor score breakdown (chart_pattern, candle, volume, vwap,
-- vol_profile, mtf_penalty, raw_total) as JSON for each trade.
ALTER TABLE trades ADD COLUMN score_components TEXT;

-- Backfill existing trades from the bar_evaluated event logged at entry time.
UPDATE trades SET score_components = (
  SELECT json_extract(e.payload, '$.signal.components')
  FROM events e
  WHERE e.event_type = 'bar_evaluated'
    AND json_extract(e.payload, '$.signal.direction') = trades.direction
    AND datetime(json_extract(e.payload, '$.bar_time')) = datetime(trades.entry_time)
  ORDER BY e.id DESC LIMIT 1
) WHERE score_components IS NULL;
