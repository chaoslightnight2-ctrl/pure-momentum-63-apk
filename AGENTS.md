## Pure Momentum 63 Safety Notes

This repository contains the Android APK/source and research reports for the Pure Momentum 63 Alpaca paper strategy.

Hard rules:

- Do not commit real Alpaca keys or broker credentials.
- Do not enable live trading by default.
- Keep Alpaca paper trading as the only default broker endpoint.
- Do not remove market-open checks, duplicate order checks, buying power checks, protective stop handling, or wash-trade protection.
- Treat backtest and Monte Carlo results as research only, not guaranteed profit.
- Any strategy change must refresh the backtest report and explain the change in risk controls.
