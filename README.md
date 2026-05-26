# Pure Momentum 63 APK

Android Alpaca paper trading app for the Pure Momentum 63 strategy.

## Latest APK

- APK: `apk/PureMomentum63-debug.apk`
- Application ID: `com.quant.puremomentum63`
- Strategy: `pure_momentum_mid_breadth_spy20_sleeve_gross18`
- Alpaca endpoint: paper trading only

The app asks for the Alpaca paper API key and secret on the phone. Do not commit real keys.

## Strategy

Pure Momentum 63 ranks this universe by 63 trading-day momentum:

`TQQQ, TECL, SOXL, UPRO, SPXL, MSTR, COIN, MARA, RIOT, NVDA, AMD, PLTR, SMCI, TSLA, CVNA, APP, HOOD`

It normally selects the strongest 7 symbols by 63 trading-day momentum, requires positive 63-day and 5-day symbol momentum, targets 1.8 gross exposure split equally, and rebalances every 7 trading days. If breadth20 is between 50% and 66%, it usually holds cash instead of opening the normal momentum basket; if SPY 20-day return is at least +3% in that mid-breadth regime, it opens a 1.2 gross top-3 sleeve. In loss regimes it uses the tested defensive sleeves: SPY 20-day crash uses 5-day top 7; SPY 63-day drawdown -2% to -5%, weak breadth, and mild negative SPY20 use 63-day top 5.

## Paper Safety

The Android bot uses:

- Alpaca paper API URL
- open-order duplicate protection
- buying power checks
- market-open checks
- whole-share order sizing
- protective stop synchronization
- daily/session buy blocking after equity loss or drawdown
- wash-trade stop cancellation checks before opposite orders

This repo does not enable live trading.

## GitHub Actions Schedule

`.github/workflows/pure-momentum-paper.yml` runs the paper order runner every 3 hours all day:

- UTC cron: `17 */3 * * *`

The workflow still checks Alpaca market-open status, existing open orders, buying power, and the 7-trading-day rebalance gate before sending paper orders. If the market is closed or rebalance is not due, it exits without orders.

Rebalance state is kept in `state/pure_momentum_state.json`. After a successful paper rebalance run, GitHub Actions commits the latest rebalance date back to the repository so later 3-hour runs do not repeat the same rebalance.

Required GitHub secrets:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

## Local Paper Runner

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python scripts/run_pure_momentum_paper.py --config config/settings.yaml --execute
```

It will only send paper orders when Alpaca says the market is open and the rebalance gate is due.

Paper runs and uploaded artifacts are execution logs, not guaranteed profit.
