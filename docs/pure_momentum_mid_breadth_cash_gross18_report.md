# Pure Momentum Mid-Breadth SPY20 Sleeve Gross 1.8

Research date: 2026-05-26

This is research-only and paper-trading only. It is not a guarantee of profit and does not enable live trading.

## Strategy

- Universe: `TQQQ, TECL, SOXL, UPRO, SPXL, MSTR, COIN, MARA, RIOT, NVDA, AMD, PLTR, SMCI, TSLA, CVNA, APP, HOOD`
- Normal mode:
  - rank by 63 trading-day momentum
  - require 63-day momentum `>= 0`
  - require 5-day symbol momentum `>= 0`
  - select top 7
  - target gross exposure: `1.8`
  - rebalance every 7 trading days
- Mid-breadth mode:
  - if `50% <= breadth20 < 66%`, usually hold cash instead of the normal momentum basket
  - if `50% <= breadth20 < 66%` and SPY 20-day return is at least `+2.5%`, open a 63-day top-3 sleeve at `1.3` gross
- Loss-regime sleeves:
  - SPY 20-day return `< -5%`: 5-day top 7, momentum `>= 0`
  - SPY 63-day drawdown `-2%` to `-5%`: top 2 by `20-day momentum - 63-day momentum`, with 63-day, 20-day, and 5-day momentum `>= 0`
  - breadth20 `< 33%`: top 5 by composite score `40% 63-day momentum + 40% 20-day momentum + 20% 5-day momentum`, with 63-day momentum `>= 0`
  - SPY 20-day return `-2%` to `0%`: 63-day top 5, momentum `>= 0`

## Backtest Summary

Dataset: Alpaca IEX 5-minute bars aggregated to completed daily regular-session bars, 2023-01-01 through 2026-05-26.

Costs: 8 bps slippage per side in the base test.

Benchmark: SPY total return over the same period: `105.9%`.

| Candidate | Return | Sharpe | Max DD | Profit Factor | Trades | Worst Month | Avg Gross | Max Gross |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| previous final switch | 300.10x | 2.25 | -57.0% | 2.21 | 646 | -27.7% | 1.64 | 2.47 |
| normal mom5 filter | 458.88x | 2.35 | -58.0% | 2.37 | 580 | -25.5% | 1.63 | 2.47 |
| mid-breadth cash gross 1.8 | 532.01x | 2.46 | -45.5% | 3.17 | 511 | -25.5% | 1.42 | 2.11 |

## Alpaca Effective-Gross Variant

After paper-trading buying-power checks, the Alpaca basket filled closer to `1.2` effective gross than the nominal `1.8` target. A broker-realistic 1.2-gross research pass tested a smaller mid-breadth sleeve:

| Candidate | Return | Sharpe | Max DD | Profit Factor | Trades | Worst Month | Avg Gross | Max Gross |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| current mid-breadth cash, 1.2 gross | 88.61x | 2.45 | -32.3% | 3.01 | 511 | -16.6% | 0.95 | 1.24 |
| mid-breadth SPY20 >= +2.5%, top3 sleeve at 1.3 gross | 225.49x | 2.83 | -32.3% | 3.53 | 526 | -16.6% | 1.00 | 1.31 |
| weak-breadth composite 40/40/20, mid sleeve still 1.3 gross | 244.58x | 2.86 | -32.3% | 3.72 | 526 | -16.6% | 1.00 | 1.31 |
| SPY DD accel top2, weak composite, mid sleeve still 1.3 gross | 322.52x | 3.13 | -24.3% | 5.46 | 454 | -13.2% | 0.92 | 1.31 |

## Stress Results

| Fill | Slippage | Return | Sharpe | Max DD | Profit Factor |
|---|---:|---:|---:|---:|---:|
| open0 | 8 bps | 532.01x | 2.46 | -45.5% | 3.17 |
| open0 | 16 bps | 405.14x | 2.37 | -46.0% | 2.99 |
| open1 delayed | 8 bps | 523.45x | 2.45 | -45.3% | 3.02 |
| open1 delayed | 16 bps | 398.72x | 2.37 | -45.8% | 2.85 |

## Risk Notes

- Max drawdown improved materially but is still high at `-45.5%`.
- Backtest max gross reached about `2.11` because mark-to-market drift can raise exposure between rebalances.
- Paper runner caps target gross at `1.8` when sizing orders and keeps existing market-open, duplicate-order, buying-power, daily-loss, and paper-only controls.
- Paper runner submits flatten orders first, waits for order completion, then submits new buys in proportional rounds with buying-power checks before each order. This prevents the first symbols from consuming all buying power while later symbols are starved.
- The strategy was discovered in-sample and still needs walk-forward parameter stability before any promotion beyond paper.

