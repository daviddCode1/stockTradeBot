# stockTradeBot

A research project to test, and only if the evidence supports it, later automate, a **long-only US equity
momentum + trend** strategy executed through the **Trading 212 Public API** (Invest or Stocks & Shares ISA only).

> **Status: Phase 1 — research & specification. There is no trading code yet.**
> The full specification is in [`docs/PHASE1_SPECIFICATION.md`](docs/PHASE1_SPECIFICATION.md) and is awaiting review.

## Ground rules

- **Paper/demo by default.** Live trading requires `TRADING_MODE=LIVE` **and** `ENABLE_LIVE_TRADING=YES`, plus
  passing the pre-registered acceptance criteria and a paper-trading period.
- No CFDs, no FX trading, no leverage, no shorting, no options.
- Credentials come only from environment variables (`T212_API_KEY`, `T212_API_SECRET`). `.env` is git-ignored.
  See `.env.example`.
- Nothing here claims or implies the strategy is profitable. If the research does not support it, the answer is
  **do not deploy**.

## Repository layout (so far)

```
docs/PHASE1_SPECIFICATION.md   # research findings, exact rules, backtest & acceptance methodology
config/settings.yaml           # pre-registered parameters (fixed before any backtest)
.env.example                   # template for local secrets and safety switches
reports/                       # generated reports (git-ignored)
```

A beginner-friendly guide (installing Python, getting API keys, running backtests and paper trading, shutting the
bot down) will be written as those parts are built.
