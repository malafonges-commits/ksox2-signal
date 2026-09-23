from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pykrx import stock

TICKER = "423920"
TZ = ZoneInfo("Asia/Seoul")
SMA_WINDOW = 260
UPPER_MULTIPLIER = 1.05
LOWER_MULTIPLIER = 0.95
MIN_BARS = 262
LISTING_DATE = "20220414"

SIGNAL_PATH = Path("ksox2_signal.json")
HISTORY_PATH = Path("ksox2_history.csv")


def finite(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite numeric value: {value}")
    return value


def query_end_date(now: datetime) -> str:
    # Manual runs before KRX close must not use the ongoing day's bar.
    if now.time() < time(15, 40):
        day = now.date() - timedelta(days=1)
    else:
        day = now.date()
    return day.strftime("%Y%m%d")


def fetch_krx_history(now: datetime) -> pd.DataFrame:
    end_date = query_end_date(now)
    df = stock.get_etf_ohlcv_by_date(LISTING_DATE, end_date, TICKER)

    if df is None or df.empty:
        raise RuntimeError("pykrx returned no ETF OHLCV data for 423920")

    df = df.copy().sort_index()

    if "종가" not in df.columns:
        raise RuntimeError(f"Unexpected pykrx ETF columns: {list(df.columns)}")

    df = df.loc[~df.index.duplicated(keep="last")]
    df["종가"] = pd.to_numeric(df["종가"], errors="coerce")
    df = df[df["종가"].notna() & (df["종가"] > 0)]

    if len(df) < MIN_BARS:
        raise RuntimeError(
            f"DATA_INSUFFICIENT: only {len(df)} finalized bars, need >= {MIN_BARS}"
        )

    # pykrx ETF OHLCV does not expose an explicit adjusted=True parameter.
    # Guard against split/reverse-split-like discontinuities rather than silently
    # producing a signal from a potentially non-comparable price series.
    ratio = df["종가"].pct_change().add(1.0)
    suspicious = ratio[(ratio >= 1.80) | (ratio <= 0.55)]
    if not suspicious.empty:
        dates = [d.strftime("%Y-%m-%d") for d in suspicious.index]
        raise RuntimeError(
            "CORPORATE_ACTION_REVIEW_REQUIRED: split/reverse-split-like "
            f"price discontinuity detected at {dates}"
        )

    return df


def build_signal(df: pd.DataFrame, now: datetime) -> tuple[dict, pd.DataFrame]:
    work = pd.DataFrame(index=df.index)
    work["close"] = df["종가"].astype(float)
    work["sma260"] = work["close"].rolling(
        SMA_WINDOW, min_periods=SMA_WINDOW
    ).mean()
    work["upper_5pct"] = work["sma260"] * UPPER_MULTIPLIER
    work["lower_5pct"] = work["sma260"] * LOWER_MULTIPLIER

    state = "CASH"
    recent_buy = None
    recent_sell = None
    records = []

    for date, row in work.dropna(subset=["sma260"]).iterrows():
        close = finite(row["close"])
        sma = finite(row["sma260"])
        upper = finite(row["upper_5pct"])
        lower = finite(row["lower_5pct"])

        previous_state = state
        signal = "NONE"

        if state == "CASH" and close > upper:
            state = "LONG"
            signal = "BUY"
            recent_buy = {
                "date": date.strftime("%Y-%m-%d"),
                "close": close,
                "sma260": sma,
                "upper_5pct": upper,
            }

        elif state == "LONG" and close < lower:
            state = "CASH"
            signal = "SELL"
            recent_sell = {
                "date": date.strftime("%Y-%m-%d"),
                "close": close,
                "sma260": sma,
                "lower_5pct": lower,
            }

        records.append(
            {
                "date": date,
                "close": close,
                "sma260": sma,
                "upper_5pct": upper,
                "lower_5pct": lower,
                "previous_state": previous_state,
                "state": state,
                "signal": signal,
            }
        )

    if not records:
        raise RuntimeError("DATA_INSUFFICIENT: SMA260 could not be calculated")

    latest = records[-1]

    close = latest["close"]
    sma = latest["sma260"]
    upper = latest["upper_5pct"]
    lower = latest["lower_5pct"]

    if latest["signal"] == "BUY":
        action = "NEXT_KRX_OPEN_BUY"
    elif latest["signal"] == "SELL":
        action = "NEXT_KRX_OPEN_SELL"
    elif latest["state"] == "LONG":
        action = "LONG_HOLD"
    else:
        action = "CASH_HOLD"

    payload = {
        "ticker": TICKER,
        "strategy": "SMA260 +/-5% hysteresis",
        "generated_at_kst": now.isoformat(timespec="seconds"),
        "status": "OK",
        "data_source": "KRX ETF OHLCV via pykrx",
        "price_basis": (
            "KRX ETF close; pykrx ETF API has no explicit adjusted=True flag"
        ),
        "corporate_action_guard": (
            "block if consecutive close ratio >=1.80 or <=0.55"
        ),
        "data_rows": int(len(df)),
        "latest_finalized_date": latest["date"].strftime("%Y-%m-%d"),
        "close": close,
        "sma260": sma,
        "upper_5pct": upper,
        "lower_5pct": lower,
        "close_vs_sma_pct": (close / sma - 1.0) * 100.0,
        "close_vs_upper_pct": (close / upper - 1.0) * 100.0,
        "close_vs_lower_pct": (close / lower - 1.0) * 100.0,
        "previous_state": latest["previous_state"],
        "state": latest["state"],
        "signal": latest["signal"],
        "action": action,
        "recent_buy_transition": recent_buy,
        "recent_sell_transition": recent_sell,
        "rules": {
            "buy": "CASH and close > SMA260 * 1.05",
            "sell": "LONG and close < SMA260 * 0.95",
            "inside_band": "preserve previous state",
            "execution": "next KRX regular-session open",
        },
    }

    history = pd.DataFrame(records).set_index("date")
    return payload, history


def write_outputs(payload: dict, history: pd.DataFrame) -> None:
    SIGNAL_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    history.tail(500).to_csv(
        HISTORY_PATH,
        encoding="utf-8-sig",
        float_format="%.6f",
    )


def main() -> None:
    now = datetime.now(TZ)

    try:
        df = fetch_krx_history(now)
        payload, history = build_signal(df, now)
        write_outputs(payload, history)
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    except Exception as exc:
        error_payload = {
            "ticker": TICKER,
            "strategy": "SMA260 +/-5% hysteresis",
            "generated_at_kst": now.isoformat(timespec="seconds"),
            "status": "DATA_ERROR",
            "error": str(exc),
        }
        SIGNAL_PATH.write_text(
            json.dumps(error_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(error_payload, ensure_ascii=False, indent=2))
        raise


if __name__ == "__main__":
    main()
