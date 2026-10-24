"""Market-wide crash rebound, exactly as backtested (run 2026-10-24c, c04_crash_coin_vs_market, which=0):

  * a coin's 24h move = close now / close 24 hours ago - 1
  * its normal daily move = std of its last 720 hourly returns (at least 168) x sqrt(24)
  * BUY when the coin fell more than Z x its normal daily move AND BTC's own 24h move was at least 1.5 x BTC's
    normal daily move in either direction (a market-wide shake-out, not coin-specific news)
  * hold up to HOLD hours; sell early if an hourly close is STOP below the close at the signal
"""
import numpy as np


def daily_sd(close):
    r = np.diff(close) / close[:-1]
    r = r[-720:]
    return float(np.std(r, ddof=1) * np.sqrt(24)) if len(r) >= 168 else float("nan")


def move24(close):
    return float(close[-1] / close[-25] - 1) if len(close) >= 25 else float("nan")


def market_wide(btc_close):
    b, sd = move24(btc_close), daily_sd(btc_close)
    return bool(np.isfinite(b) and np.isfinite(sd) and abs(b) >= 1.5 * sd), b, sd


def crash(close, z):
    m, sd = move24(close), daily_sd(close)
    return bool(np.isfinite(m) and np.isfinite(sd) and m < -z * sd), m, sd
