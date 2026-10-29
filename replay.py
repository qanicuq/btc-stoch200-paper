#!/usr/bin/env python3
"""
Replay test for BTC stoch200 paper bot.
Fetches Binance BTCUSDT 1h from 2017-08-17 to now and runs the exact logic
used in bot.py (200-bar window INCLUDING current bar).
Prints period stats vs targets.
"""
import json
import requests
import time
from datetime import datetime, timezone

LOOKBACK = 200
ENTRY = 0.8
EXIT = 0.5
FEE = 0.0015

def fetch_all():
    url = "https://data-api.binance.vision/api/v3/klines"
    start = int(datetime(2017, 8, 17, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    bars = []
    current = start
    while current < end:
        params = {"symbol": "BTCUSDT", "interval": "1h", "startTime": current, "limit": 1000}
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for k in data:
            bars.append({
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
            })
        current = data[-1][0] + 1
        time.sleep(0.05)
    return bars

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

def simulate_period(bars, start_iso, end_iso):
    s_ms = int(datetime.fromisoformat(start_iso + "T00:00:00+00:00").timestamp() * 1000)
    e_ms = int(datetime.fromisoformat(end_iso + "T23:59:59+00:00").timestamp() * 1000)
    cash = 10000.0
    btc = 0.0
    position = "flat"
    trade_count = 0
    peak = 10000.0
    max_dd = 0.0
    entry_price = 0.0
    gw = 0.0
    gl = 0.0
    started = False
    last_i = 0
    for i in range(len(bars) - 1):
        t = bars[i]["open_time"]
        if t < s_ms:
            continue
        if not started:
            started = True
            cash = 10000.0
            btc = 0.0
            position = "flat"
            peak = 10000.0
        if t > e_ms:
            break
        last_i = i
        stoch = compute_stoch(bars, i)
        if stoch is None:
            continue
        next_open = bars[i + 1]["open"]
        action = None
        if position == "flat" and stoch > ENTRY:
            action = "BUY"
            trade_count += 1
        elif position == "long" and stoch < EXIT:
            action = "SELL"
            trade_count += 1
        if action:
            if action == "BUY":
                fee = cash * FEE
                btc = (cash - fee) / next_open
                cash = 0.0
                position = "long"
                entry_price = next_open
            else:
                gross = btc * next_open
                fee = gross * FEE
                cash = gross - fee
                btc = 0.0
                position = "flat"
                if entry_price:
                    pnl = next_open / entry_price - 1
                    if pnl > 0:
                        gw += pnl
                    else:
                        gl += abs(pnl)
        value = cash + btc * bars[i]["close"]
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak else 0
        if dd > max_dd:
            max_dd = dd
    final = cash + btc * bars[last_i]["close"]
    ret = (final / 10000 - 1) * 100
    pf = gw / gl if gl > 0 else 999
    round_trips = trade_count // 2
    return round_trips, ret, max_dd * 100, pf

if __name__ == "__main__":
    print("Fetching full history (this takes a few minutes)...")
    bars = fetch_all()
    print(f"Loaded {len(bars)} bars")
    print("\nPeriod results (window includes current bar):")
    targets = [
        ("2017-08-17 to 2024-01-15", "2017-08-17", "2024-01-15", 159, 1803.3, 45.0, 2.32),
        ("2024-01-15 to 2026-10-12", "2024-01-15", "2026-10-12", 75, 53.9, 39.1, 1.61),
    ]
    for label, s, e, tgt_tr, tgt_ret, tgt_dd, tgt_pf in targets:
        tr, ret, dd, pf = simulate_period(bars, s, e)
        print(f"{label}:")
        print(f"  Got:  {tr} trades, {ret:+.1f}%, {dd:.1f}% DD, PF {pf:.2f}")
        print(f"  Tgt:  {tgt_tr} trades, {tgt_ret:+.1f}%, {tgt_dd:.1f}% DD, PF {tgt_pf:.2f}")
