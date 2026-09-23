# K-SOX2 Signal

Automated signal calculation for KRX ETF **423920**.

## Strategy

Stateful SMA260 ±5% hysteresis:

- `CASH -> LONG` when finalized close > `SMA260 * 1.05`
- `LONG -> CASH` when finalized close < `SMA260 * 0.95`
- otherwise preserve prior state
- execute on the next KRX regular-session open

No confirmation-day rule and no trailing stop.

## Automation

GitHub Actions runs at **17:00 KST on weekdays** (`08:00 UTC`) and:

1. fetches 423920 ETF OHLCV using `pykrx`
2. recalculates the full strategy history from the first valid SMA260 date
3. writes `ksox2_signal.json`
4. writes the latest strategy history to `ksox2_history.csv`
5. commits changed outputs back to the repository

Manual runs are also available from **Actions -> Update K-SOX2 signal -> Run workflow**.

## Important data note

`pykrx.stock.get_etf_ohlcv_by_date()` is used for ETF OHLCV. The ETF API does not expose an explicit `adjusted=True` argument. The script therefore blocks the signal if it detects a split/reverse-split-like discontinuity (`>= +80%` ratio or `<= -45%` ratio between consecutive closes) rather than silently calculating on a potentially non-comparable series.

The generated JSON explicitly records the price basis and any data error.

## Output

`ksox2_signal.json` contains:

- latest finalized KRX date
- close
- SMA260
- +5% entry line
- -5% exit line
- previous/current state
- BUY/SELL/NONE signal
- most recent buy/sell transition
- next-action label
