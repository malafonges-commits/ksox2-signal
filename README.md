# K-SOX2 Signal

Official KRX OPEN API based automated signal for ETF 423920.

Strategy:
- CASH -> LONG if finalized close > SMA260 * 1.05
- LONG -> CASH if finalized close < SMA260 * 0.95
- Otherwise preserve previous state
- Execute next KRX regular-session open

GitHub secret required:
- `KRX_AUTH_KEY`

Outputs:
- `ksox2_prices.csv`
- `ksox2_signal.json`
- `ksox2_history.csv`
