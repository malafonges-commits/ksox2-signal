from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

TICKER = "423920"
TZ = ZoneInfo("Asia/Seoul")

API_URL = "https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd"
AUTH_ENV = "KRX_AUTH_KEY"
BACKFILL_START = date(2022, 4, 14)

SMA_WINDOW = 260
UPPER_MULTIPLIER = 1.05
LOWER_MULTIPLIER = 0.95

HISTORY_PATH = Path("ksox2_prices.csv")
SIGNAL_PATH = Path("ksox2_signal.json")
STRATEGY_HISTORY_PATH = Path("ksox2_history.csv")

REQUEST_INTERVAL_SECONDS = 0.15
MAX_RETRIES = 4


def clean_number(value) -> float:
    if value is None:
        raise ValueError("missing numeric value")
    s = str(value).replace(",", "").strip()
    if s in {"", "-"}:
        raise ValueError(f"invalid numeric value: {value!r}")
    x = float(s)
    if not math.isfinite(x):
        raise ValueError(f"non-finite numeric value: {value!r}")
    return x


def target_end_date(now: datetime) -> date:
    if now.time() < dtime(15, 40):
        return now.date() - timedelta(days=1)
    return now.date()


def iter_weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def get_auth_key() -> str:
    key = os.getenv(AUTH_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"{AUTH_ENV} is missing. Add the approved KRX OPEN API key "
            "as a GitHub Actions repository secret."
        )
    return key


def request_etf_day(session: requests.Session, auth_key: str, d: date) -> dict | None:
    params = {"basDd": d.strftime("%Y%m%d")}
    headers = {"AUTH_KEY": auth_key}

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(API_URL, headers=headers, params=params, timeout=30)

            if r.status_code in {401, 403}:
                raise RuntimeError(
                    f"KRX OPEN API authorization failed ({r.status_code}). "
                    "Check KRX_AUTH_KEY and ETF API approval."
                )

            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            r.raise_for_status()
            payload = r.json()

            rows = payload.get("OutBlock_1")
            if rows is None:
                for key in ("outBlock_1", "output", "data"):
                    if isinstance(payload.get(key), list):
                        rows = payload[key]
                        break

            if rows is None:
                raise RuntimeError(
                    f"Unexpected KRX response schema for {params['basDd']}: "
                    f"keys={list(payload.keys())}"
                )

            if not rows:
                return None

            for row in rows:
                code = str(row.get("ISU_CD", "")).strip()

                if code == TICKER or code.endswith(TICKER):
                    raw_close = row.get("TDD_CLSPRC")

                    # KRX 휴장일에는 종가가 빈 문자열/“-”로 반환될 수 있음
                    close_text = str(raw_close or "").replace(",", "").strip()
                    if close_text in {"", "-", "0"}:
                        return None

                    close = clean_number(raw_close)

                    raw_date = str(row.get("BAS_DD", "")).strip()
                    parsed_date = pd.to_datetime(raw_date, errors="coerce")
                    actual_date = d if pd.isna(parsed_date) else parsed_date.date()

                    return {
                        "date": actual_date,
                        "close": close,
                    }

            return None

        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(2 ** attempt)

    raise RuntimeError(f"KRX API request failed for {params['basDd']}: {last_error}")


def load_price_history() -> pd.DataFrame:
    if not HISTORY_PATH.exists():
        return pd.DataFrame(columns=["date", "close"])

    df = pd.read_csv(HISTORY_PATH)
    if df.empty:
        return pd.DataFrame(columns=["date", "close"])

    df["date"] = pd.to_datetime(df["date"], errors="raise")
    df["close"] = pd.to_numeric(df["close"], errors="raise")
    return (
        df.drop_duplicates("date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def update_price_history(now: datetime) -> pd.DataFrame:
    auth_key = get_auth_key()
    history = load_price_history()
    end = target_end_date(now)

    if history.empty:
        start = BACKFILL_START
    else:
        start = history["date"].max().date() + timedelta(days=1)

    new_rows = []
    if start <= end:
        with requests.Session() as session:
            for d in iter_weekdays(start, end):
                row = request_etf_day(session, auth_key, d)
                if row is not None:
                    new_rows.append(row)
                time.sleep(REQUEST_INTERVAL_SECONDS)

    if new_rows:
        add = pd.DataFrame(new_rows)
        add["date"] = pd.to_datetime(add["date"])
        history = pd.concat([history, add], ignore_index=True)

    history = (
        history.drop_duplicates("date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )

    if len(history) < SMA_WINDOW:
        raise RuntimeError(
            f"DATA_INSUFFICIENT: only {len(history)} trading-day closes; "
            f"need at least {SMA_WINDOW}"
        )

    ratio = history["close"].pct_change().add(1.0)
    suspicious = history.loc[(ratio >= 1.80) | (ratio <= 0.55), "date"]
    if not suspicious.empty:
        dates = [x.strftime("%Y-%m-%d") for x in suspicious]
        raise RuntimeError(
            "CORPORATE_ACTION_REVIEW_REQUIRED: split/reverse-split-like "
            f"discontinuity detected at {dates}"
        )

    history.to_csv(
        HISTORY_PATH,
        index=False,
        encoding="utf-8",
        date_format="%Y-%m-%d",
        float_format="%.6f",
    )
    return history


def replay_strategy(prices: pd.DataFrame, now: datetime) -> tuple[dict, pd.DataFrame]:
    df = prices.copy()
    df["sma260"] = df["close"].rolling(
        SMA_WINDOW, min_periods=SMA_WINDOW
    ).mean()
    df["upper_5pct"] = df["sma260"] * UPPER_MULTIPLIER
    df["lower_5pct"] = df["sma260"] * LOWER_MULTIPLIER

    state = "CASH"
    recent_buy = None
    recent_sell = None
    records = []

    for _, row in df.dropna(subset=["sma260"]).iterrows():
        d = pd.Timestamp(row["date"])
        close = float(row["close"])
        sma = float(row["sma260"])
        upper = float(row["upper_5pct"])
        lower = float(row["lower_5pct"])
        previous_state = state
        signal = "NONE"

        if state == "CASH" and close > upper:
            state = "LONG"
            signal = "BUY"
            recent_buy = {
                "date": d.strftime("%Y-%m-%d"),
                "close": close,
                "sma260": sma,
                "upper_5pct": upper,
            }
        elif state == "LONG" and close < lower:
            state = "CASH"
            signal = "SELL"
            recent_sell = {
                "date": d.strftime("%Y-%m-%d"),
                "close": close,
                "sma260": sma,
                "lower_5pct": lower,
            }

        records.append(
            {
                "date": d,
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
        "data_source": "KRX OPEN API / ETF daily trading information",
        "api_id": "etf_bydd_trd",
        "price_basis": (
            "Official KRX market close. API does not expose a separate adjusted-close "
            "field; signal is blocked if a split/reverse-split-like discontinuity is detected."
        ),
        "corporate_action_guard": "block if consecutive close ratio >=1.80 or <=0.55",
        "data_rows": int(len(prices)),
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

    strategy_history = pd.DataFrame(records)
    return payload, strategy_history


def write_outputs(payload: dict, strategy_history: pd.DataFrame) -> None:
    SIGNAL_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    strategy_history.tail(600).to_csv(
        STRATEGY_HISTORY_PATH,
        index=False,
        encoding="utf-8-sig",
        date_format="%Y-%m-%d",
        float_format="%.6f",
    )


def write_error(now: datetime, exc: Exception) -> None:
    payload = {
        "ticker": TICKER,
        "strategy": "SMA260 +/-5% hysteresis",
        "generated_at_kst": now.isoformat(timespec="seconds"),
        "status": "DATA_ERROR",
        "error": str(exc),
    }
    SIGNAL_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    now = datetime.now(TZ)
    try:
        prices = update_price_history(now)
        payload, strategy_history = replay_strategy(prices, now)
        write_outputs(payload, strategy_history)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:
        write_error(now, exc)
        raise


if __name__ == "__main__":
    main()
