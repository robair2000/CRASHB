# Crash-rebound paper bot (KuCoin Futures)

Paper trading only: public KuCoin data, **no API keys, no real orders**.

## What it does
Every hour it checks the ~50 most traded KuCoin USDT perpetuals. When a coin has dropped more than **4 times its
normal daily move** over the last 24 hours **and** BTC also moved hard (a market-wide shake-out rather than news
about one coin), it buys on paper with 7.5% of equity. It holds up to 24 hours, and sells early if an hourly close
falls 15% below the price at the signal. Most hours nothing happens: these events are rare and come in clusters on
liquidation-cascade days.

## Backtest it came from
Run 2026-10-24c, strategy `c04_crash_coin_vs_market` (42 coins, 3x, taker fees, slippage, funding):
- **Train (Oct 2024 – Jan 2026, mostly a bull market):** Sharpe 1.69, about +13% average per trade, 98% of trades won.
- **Validation (Jan – May 2026, a bear market):** Sharpe 1.35, +4%, max drawdown −3%, profit factor 2.58, still
  profitable at double costs and with ±20% settings.
- **It did not pass every gate:** only 20 validation trades (the bar is 30), so the newest 5 months stay locked as a
  final test. This paper run is the forward test.
- **Checked:** replaying this bot hour by hour over the validation period gives exactly the backtest's 20 trades.

## Known risks
- **Few independent events:** many trades happen on the same cascade day, so one bad day can wipe out several wins.
- **Wicks:** a single hour can spike 30–90% on thin coins. In the backtest, a full-size 3x isolated position was
  liquidated by such a wick (ARC). For live trading: small size and cross margin.
- **Survivorship:** the backtest used today's top coins; coins that collapsed and were delisted are missing.

## Render
Background worker, Starter plan, Frankfurt, Python 3.12.8, disk at `/data` (all set in `render.yaml`).

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `MODE` | `paper` | only paper is supported |
| `UNIVERSE_SIZE` / `PAIRS` / `EXCLUDE` | `50` / empty / stock tokens | which coins are watched |
| `MIN_AGE_DAYS` | `30` | minimum history before a coin can be traded |
| `CRASH_Z` | `4.0` | how extreme the 24h drop must be (x normal daily move) |
| `HOLD_HOURS` | `24` | maximum holding time |
| `STOP_PCT` | `0.15` | early exit below the signal price |
| `POSITION_FRAC` | `0.075` | size per trade / equity |
| `MAX_POSITIONS` | `20` | |
| `FEE` / `SLIPPAGE` | `0.0006` / `0.0005` | per side |
| `START_CASH` | `1000` | paper balance |
| `DATA_DIR` | `.` | state, trades, every crash event, equity |

`crash_events.csv` logs every crash it sees, including coin-specific ones it skips, so later we can check whether
those should be traded too. Run `python bot.py --report` for results. Kill switch: a file named `STOP`.
