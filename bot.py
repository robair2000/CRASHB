"""Crash-rebound paper bot for KuCoin Futures (public data only, no API keys, no real orders).

Every hour, right after the candle closes, it checks the most traded USDT perpetuals. When a coin has just dropped far
more than its normal daily move during a market-wide shake-out (BTC also moved hard), it buys on paper, holds up to
24 hours, and sells early if the price falls a further 15%. Funding is charged or credited hourly.

    python bot.py            # runs forever
    python bot.py --once     # one cycle, then exits
    python bot.py --report   # paper results so far
    touch STOP               # kill switch
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

import kucoin_data as kd
from strategy import crash, market_wide


def _f(name, default):
    return float(os.getenv(name, default))


MODE = os.getenv("MODE", "paper").lower()
UNIVERSE_SIZE = int(os.getenv("UNIVERSE_SIZE", "50"))
PAIRS = [p.strip().upper() for p in os.getenv("PAIRS", "").split(",") if p.strip()]
EXCLUDE = {s.strip().upper() for s in os.getenv(
    "EXCLUDE", "TSLA,SOXL,SPCX,SNDK,MU,PAXG,XAUT,NVDA,AAPL,AMZN,GOOGL,META,MSFT,COIN,MSTR,HOOD,CRCL,QQQ,SPY").split(",")}
MIN_AGE_DAYS = _f("MIN_AGE_DAYS", "30")          # coin needs this much history (backtest: listed 30+ days)
Z = _f("CRASH_Z", "4.0")                        # 24h drop beyond Z x the coin's normal daily move
HOLD_H = int(os.getenv("HOLD_HOURS", "24"))
STOP = _f("STOP_PCT", "0.15")                   # sell early on an hourly close this far below the signal close
POSITION_FRAC = _f("POSITION_FRAC", "0.075")    # notional per trade / equity (backtest: 3x on 1/41 of equity)
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "20"))
FEE = _f("FEE", "0.0006")
SLIPPAGE = _f("SLIPPAGE", "0.0005")
START_CASH = _f("START_CASH", "1000")
FUNDING = os.getenv("FUNDING", "true").lower() in ("1", "true", "yes")
DATA_DIR = os.getenv("DATA_DIR", ".")
STATE_PATH = os.path.join(DATA_DIR, os.getenv("STATE_FILE", "crash_state.json"))
TRADES_CSV = os.path.join(DATA_DIR, "crash_trades.csv")
EVENTS_CSV = os.path.join(DATA_DIR, "crash_events.csv")
EQUITY_CSV = os.path.join(DATA_DIR, "crash_equity.csv")
H = 3600
HISTORY = 760                                    # hourly candles kept per coin (720 for the volatility + 25 + margin)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"cash": START_CASH, "pos": {}, "trades": 0, "wins": 0, "fees": 0.0, "funding": 0.0,
            "last_cycle": None, "universe": [], "universe_at": 0, "started": time.time()}


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)


def equity(st, prices):
    return st["cash"] + sum(p["qty"] * (prices.get(s, p["entry"]) - p["entry"]) for s, p in st["pos"].items())


def universe(st, now):
    if PAIRS:
        return [p if p.endswith("USDTM") else ("XBT" if p == "BTC" else p) + "USDTM" for p in PAIRS]
    if st["universe"] and now - st["universe_at"] < 86400:
        return st["universe"]
    rows = [r for r in kd.active_contracts() if r["base"].upper() not in EXCLUDE and r["symbol"].replace("USDTM", "") not in EXCLUDE]
    rows.sort(key=lambda r: -r["turnover"])
    st["universe"], st["universe_at"] = [r["symbol"] for r in rows[:UNIVERSE_SIZE]], now
    return st["universe"]


class Candles:
    """Keeps the last HISTORY hourly candles per coin; tops up with a small request each hour."""

    def __init__(self, fetch=None):
        self.fetch = fetch or (lambda s, n, end: kd.candles_1h(s, n, end_ts=end))
        self.c = {}

    def get(self, sym, now):
        have = self.c.get(sym)
        want_last = (int(now) // H) * H - H                      # open time of the newest closed candle
        if have is None or int(have.t_ms.iloc[-1] // 1000) < want_last - 4 * H:
            df = self.fetch(sym, HISTORY, now)
        elif int(have.t_ms.iloc[-1] // 1000) < want_last:
            new = self.fetch(sym, 8, now)
            import pandas as pd
            df = pd.concat([have, new]).drop_duplicates("t_ms").sort_values("t_ms").tail(HISTORY).reset_index(drop=True)
        else:
            df = have
        self.c[sym] = df
        return df


def cycle(st, candles, now=None):
    now = now or time.time()
    close_hour = int(now // H) * H
    now_iso = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    syms = universe(st, now)
    data, prices = {}, {}
    for s in sorted(set(syms) | set(st["pos"]) | {"XBTUSDTM"}):
        try:
            df = candles.get(s, now)
            data[s] = df
            prices[s] = float(df.close.iloc[-1])
        except Exception as e:
            log(f"{s}: data error, skipped this cycle ({e})")

    # funding for the hour that passed
    if FUNDING and st["pos"] and st["last_cycle"]:
        hours = min((close_hour - st["last_cycle"]) / H, 3)
        for s, p in st["pos"].items():
            pay = p["qty"] * prices.get(s, p["entry"]) * kd.funding_rate(s) * hours / 8.0
            st["cash"] -= pay
            st["funding"] -= pay
    st["last_cycle"] = close_hour

    btc = data.get("XBTUSDTM")
    wide, b24, bsd = market_wide(btc.close.values) if btc is not None else (False, float("nan"), float("nan"))
    signals = {}
    for s in syms:
        df = data.get(s)
        if df is None or len(df) < MIN_AGE_DAYS * 24:
            continue
        hit, m, sd = crash(df.close.values, Z)
        if hit:
            signals[s] = (m, sd)
            append_csv(EVENTS_CSV, ["time", "symbol", "move_24h", "normal_daily_move", "btc_move_24h", "btc_normal", "market_wide", "traded"],
                       [now_iso, s, round(m, 4), round(sd, 4), round(b24, 4), round(bsd, 4), wide, wide])

    # exits: hold time over or stop hit (a position whose crash signal is still on is simply renewed, as backtested)
    for s, p in list(st["pos"].items()):
        if s not in prices:
            continue
        px = prices[s]
        over = close_hour - p["signal_at"] >= HOLD_H * H
        stopped = px / p["signal_close"] - 1 < -STOP
        if not (over or stopped):
            continue
        if wide and s in signals:
            p.update(signal_at=close_hour, signal_close=px)
            log(f"{s}: still crashing in a market-wide drop - hold renewed")
            continue
        fill = px * (1 - SLIPPAGE)
        fee = p["qty"] * fill * FEE
        pnl = p["qty"] * (fill - p["entry"]) - fee
        st["cash"] += pnl
        st["fees"] += fee
        ret = fill / p["entry"] - 1 - 2 * FEE
        st["trades"] += 1
        st["wins"] += ret > 0
        st["pos"].pop(s)
        why = "stop" if stopped else f"{HOLD_H}h exit"
        append_csv(TRADES_CSV, ["closed", "symbol", "opened", "entry", "exit", "return_after_fees", "reason"],
                   [now_iso, s, p["opened"], p["entry"], round(fill, 10), round(ret, 5), why])
        log(f"{s}: SOLD ({why}) {ret:+.2%} after fees")

    # entries
    if signals and not wide:
        log("crash signals but BTC is calm (coin-specific drops) - not traded: " + ", ".join(s.replace("USDTM", "") for s in signals))
    if wide:
        eq = equity(st, prices)
        for s, (m, sd) in sorted(signals.items(), key=lambda x: x[1][0]):
            if s in st["pos"] or len(st["pos"]) >= MAX_POSITIONS:
                continue
            px = prices[s]
            fill = px * (1 + SLIPPAGE)
            qty = eq * POSITION_FRAC / fill
            fee = qty * fill * FEE
            st["cash"] -= fee
            st["fees"] += fee
            st["pos"][s] = dict(qty=qty, entry=fill, signal_close=px, signal_at=close_hour, opened=now_iso)
            log(f"{s}: BOUGHT after a {m:+.1%} 24h drop ({m / sd:.1f}x its normal daily move) during a market-wide move "
                f"(BTC {b24:+.1%}) | {qty * fill:.2f} USDT")

    eq = equity(st, prices)
    wr = st["wins"] / st["trades"] if st["trades"] else float("nan")
    log(f"equity {eq:.2f} ({eq / START_CASH - 1:+.2%}) | open {len(st['pos'])} | trades {st['trades']} | win rate {wr:.0%} "
        f"| BTC 24h {b24:+.1%} ({'market-wide' if wide else 'calm'}) | fees {st['fees']:.2f} | funding {st['funding']:+.2f}")
    append_csv(EQUITY_CSV, ["time", "equity", "open", "trades", "wins", "fees", "funding"],
               [now_iso, round(eq, 4), len(st["pos"]), st["trades"], st["wins"], round(st["fees"], 4), round(st["funding"], 4)])
    save_state(st)


def report():
    if not os.path.exists(STATE_PATH):
        print("no results yet")
        return
    st = load_state()
    wr = st["wins"] / st["trades"] if st["trades"] else float("nan")
    print(f"cash {st['cash']:.2f} | closed trades {st['trades']} | win rate {wr:.0%} | open {len(st['pos'])} | fees {st['fees']:.2f} | funding {st['funding']:+.2f}")


def main():
    if "--report" in sys.argv:
        return report()
    if MODE != "paper":
        raise SystemExit("This bot is paper-only for now (MODE=paper). It never places real orders.")
    log(f"CRASH-REBOUND PAPER BOT | universe {'fixed ' + str(len(PAIRS)) if PAIRS else 'top ' + str(UNIVERSE_SIZE)} | buy on a 24h drop > "
        f"{Z:g}x normal during market-wide moves | hold {HOLD_H}h | stop {STOP:.0%} | {POSITION_FRAC:.1%} of equity per trade")
    st, candles = load_state(), Candles()
    while True:
        if os.path.exists("STOP"):
            log("STOP file found, exiting")
            break
        try:
            cycle(st, candles)
        except Exception as e:
            log(f"cycle error: {e}")
        if "--once" in sys.argv:
            break
        time.sleep(H - time.time() % H + 90)


if __name__ == "__main__":
    main()
