#!/usr/bin/env python3
"""
BTC Stochastic 200 paper bot.
Logic matches Round 2 replay: stoch over last 200 bars INCLUDING current,
long >0.8, exit <0.5. Fill at next bar open (or current price for live newest).
0.15% fee per side. Paper only. GitHub Actions.
"""
import json
import os
import sys
import csv
from datetime import datetime, timezone
import requests
from zoneinfo import ZoneInfo

LOOKBACK = 200
ENTRY_THRESH = 0.8
EXIT_THRESH = 0.5
FEE_RATE = 0.0015
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"

def ntfy(msg, title="BTC Paper Bot"):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Priority": "default"},
            timeout=10,
        )
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)

def fetch_binance(limit=250):
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1h", "limit": limit}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    bars = []
    for k in data:
        bars.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
        })
    return bars, "binance"

def fetch_coinbase(limit=250):
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    params = {"granularity": 3600}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    data = data[:limit]
    data.reverse()
    bars = []
    for c in data:
        bars.append({
            "open_time": int(c[0]) * 1000,
            "open": float(c[3]),
            "high": float(c[2]),
            "low": float(c[1]),
            "close": float(c[4]),
        })
    return bars, "coinbase"

def get_bars(limit=250):
    try:
        bars, src = fetch_binance(limit)
        if len(bars) < LOOKBACK + 5:
            raise ValueError("insufficient bars")
        return bars, src
    except Exception as e:
        print(f"Binance failed: {e}, falling back to Coinbase")
        try:
            bars, src = fetch_coinbase(limit)
            return bars, src
        except Exception as e2:
            ntfy(f"Data fetch failed both sources: {e} | {e2}", "ERROR")
            raise

def compute_stoch(bars, idx):
    start = idx - LOOKBACK + 1
    if start < 0:
        return None
    window = bars[start:idx+1]
    if len(window) < LOOKBACK:
        return None
    max_h = max(b["high"] for b in window)
    min_l = min(b["low"] for b in window)
    close = bars[idx]["close"]
    if max_h <= min_l:
        return 0.5
    return (close - min_l) / (max_h - min_l)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "cash": 10000.0,
            "btc": 0.0,
            "position": "flat",
            "entry_price": 0.0,
            "benchmark_cash": 5000.0,
            "benchmark_btc": 0.0,
            "benchmark_init_price": 0.0,
            "last_processed_time": 0,
            "trade_count": 0,
            "peak_value": 10000.0,
            "max_drawdown": 0.0,
            "initialized": False,
            "last_weekly_sent": "1970-01-01",
        }
    with open(STATE_FILE) as f:
        s = json.load(f)
    if "last_weekly_sent" not in s:
        s["last_weekly_sent"] = "1970-01-01"
    return s

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def append_trade(row):
    file_exists = os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["time", "action", "price", "btc_amount", "fee", "cash_after", "btc_after", "value", "reason"])
        writer.writerow(row)

def portfolio_value(cash, btc, price):
    return cash + btc * price

def update_drawdown(state, value):
    if value > state["peak_value"]:
        state["peak_value"] = value
    dd = (state["peak_value"] - value) / state["peak_value"] if state["peak_value"] > 0 else 0
    if dd > state["max_drawdown"]:
        state["max_drawdown"] = dd

def main():
    now = datetime.now(timezone.utc)
    print(f"Run at {now.isoformat()}")

    try:
        bars, source = get_bars(300)
    except Exception as e:
        print(f"Fatal data error: {e}")
        sys.exit(1)

    last_bar = bars[-1]
    last_time = last_bar["open_time"]
    age_hours = (now.timestamp() * 1000 - last_time) / (1000 * 3600)
    if age_hours > 3:
        ntfy(f"Data is {age_hours:.1f} hours old (source={source}). Last bar {datetime.fromtimestamp(last_time/1000, timezone.utc)}", "STALE DATA")

    print(f"Source: {source}, bars: {len(bars)}, last: {datetime.fromtimestamp(last_time/1000, timezone.utc)}")

    state = load_state()

    if not state.get("initialized"):
        price = bars[-1]["close"]
        btc_bought = 5000.0 / price
        state["benchmark_btc"] = btc_bought
        state["benchmark_cash"] = 5000.0
        state["benchmark_init_price"] = price
        state["initialized"] = True
        state["last_processed_time"] = last_time - 3600*1000
        print(f"Initialized benchmark: bought {btc_bought:.6f} BTC at {price}")

    current_price = bars[-1]["close"]

    to_process = [b for b in bars if b["open_time"] > state["last_processed_time"] and b["open_time"] <= last_time]
    if not to_process:
        print("No new bars to process")
    else:
        print(f"Processing {len(to_process)} bars")

    for j, bar in enumerate(to_process):
        idx = next(i for i, b in enumerate(bars) if b["open_time"] == bar["open_time"])
        stoch = compute_stoch(bars, idx)
        if stoch is None:
            state["last_processed_time"] = bar["open_time"]
            continue
        reason = ""
        action = None
        if state["position"] == "flat" and stoch > ENTRY_THRESH:
            action = "BUY"
            reason = f"stoch {stoch:.4f} > {ENTRY_THRESH}"
        elif state["position"] == "long" and stoch < EXIT_THRESH:
            action = "SELL"
            reason = f"stoch {stoch:.4f} < {EXIT_THRESH}"

        if action:
            is_newest = (j == len(to_process) - 1)
            if not is_newest and idx + 1 < len(bars):
                fill_price = bars[idx + 1]["open"]
            else:
                fill_price = current_price
            fee = 0.0
            if action == "BUY":
                fee = state["cash"] * FEE_RATE
                state["cash_at_entry"] = state["cash"]
                net = state["cash"] - fee
                btc_amt = net / fill_price
                state["btc"] = btc_amt
                state["cash"] = 0.0
                state["position"] = "long"
                state["entry_price"] = fill_price
                state["trade_count"] += 1
            else:
                gross = state["btc"] * fill_price
                fee = gross * FEE_RATE
                state["cash"] = gross - fee
                state.setdefault("closed_pl", []).append(round(state["cash"] - state.get("cash_at_entry", 0.0), 2))
                btc_amt = state["btc"]
                state["btc"] = 0.0
                state["position"] = "flat"
                state["entry_price"] = 0.0
                state["trade_count"] += 1

            value = portfolio_value(state["cash"], state["btc"], fill_price)
            update_drawdown(state, value)
            append_trade([
                datetime.fromtimestamp(bar["open_time"]/1000, timezone.utc).isoformat(),
                action,
                f"{fill_price:.2f}",
                f"{btc_amt:.6f}",
                f"{fee:.2f}",
                f"{state['cash']:.2f}",
                f"{state['btc']:.6f}",
                f"{value:.2f}",
                reason,
            ])
            ntfy(
                f"{action} at ${fill_price:.2f}\nReason: {reason}\nBot value: ${value:.2f}\nSource: {source}",
                f"BTC {action}",
            )
            print(f"{action} at {fill_price} reason {reason} value {value}")

        state["last_processed_time"] = bar["open_time"]

    value = portfolio_value(state["cash"], state["btc"], current_price)
    update_drawdown(state, value)
    bench_value = state["benchmark_cash"] + state["benchmark_btc"] * current_price
    print(f"Bot value: {value:.2f} (pos={state['position']}) | Bench: {bench_value:.2f}")

    et_now = now.astimezone(ZoneInfo("America/New_York"))
    if et_now.weekday() == 6 and et_now.hour >= 18:
        this_sunday = et_now.date().isoformat()
        if state.get("last_weekly_sent", "1970-01-01") < this_sunday:
            bot_ret = (value - 10000) / 10000 * 100
            bench_ret = (bench_value - 10000) / 10000 * 100
            pls = state.get("closed_pl", [])
            exp_txt = f"${sum(pls) / len(pls):.2f}/round trip ({len(pls)} closed)" if pls else "n/a"
            msg = (
                f"Weekly Summary\n"
                f"Bot: ${value:.2f} ({bot_ret:+.2f}%) DD {state['max_drawdown']*100:.2f}% trades {state['trade_count']}\n"
                f"Expectancy: {exp_txt}\n"
                f"Bench: ${bench_value:.2f} ({bench_ret:+.2f}%)"
            )
            ntfy(msg, "Weekly Paper Bot")
            state["last_weekly_sent"] = this_sunday
            print("Sent weekly summary")

    save_state(state)
    print("State saved")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        ntfy(f"Bot error: {str(e)}", "ERROR")
        raise
