# Implementation report — Phases 2–13

**Date:** 2026-09-25 · **Mode:** PAPER (Trading 212 practice account, GBP) · **Live:** disabled

## Decisions taken (from review of the Phase 1 spec)

| Question | Decision | Why |
|---|---|---|
| Q1 Data source | **Yahoo Finance (yfinance) + point-in-time S&P 500 membership** now; adapter interface ready for a paid vendor | fiscal.ai was requested. Its connector is a chat connector (not callable from the bot's code) and it was not authorised in this session. Its REST API host is also blocked here. Norgate needs a Windows desktop app, so it cannot run in a Linux cloud container. Yahoo is free and adequate for prototyping, but it is **survivorship-biased**. A vendor with delisted stocks (e.g. EODHD or Sharadar) is the recommended upgrade before any live decision. |
| Q2 Account | Trading 212 **Invest, Practice**, base currency **GBP** | Verified via API: `currency = GBP`. |
| Q3 Earnings | **Block new entries within 3 trading days before earnings; do not force-sell existing positions.** Source: Yahoo earnings calendar (cached weekly). Live/paper: unknown date ⇒ no entry. Backtest: unknown dates allowed (free history is incomplete) and reported. | Earnings gaps are the main way a single trade loses far more than its planned 0.5% (the stop is jumped). Avoiding *new* entries right before the event protects the risk budget cheaply. Research on the earnings-announcement premium (e.g. Frazzini & Lamont 2007; Barber et al. 2013) suggests holding *through* earnings has historically been compensated, so we don't force exits. The research pipeline tests 1/2/3/5-day blackouts rather than assuming 3 is best. |
| Q4 Acceptance criteria | Adopted as written (spec §10) | — |
| Q5 Fractional shares | **Enabled**, quantity step 0.01 | Demo verified: 0.55 shares accepted. |

## Broker facts verified on the practice account (read-only, plus non-filling test orders cancelled at once)

* Demo API reachable. Credentials are injected by this environment's proxy (no secret handled by code or chat).
  **The live host is blocked** from this environment.
* Account: GBP. 15 existing **manual** positions. The bot flags them as unmanaged and never touches them.
* 18,472 instruments. 6,952 are USD stocks. **502 of 503 current S&P 500 members are tradable** on Trading 212.
* Trading 212's internal tickers are not always exchange symbols (`DMYI_US_EQ` = IonQ). Mapping uses `shortName`,
  and OTC listings are excluded.
* **Fractional quantity accepted** (0.55 AAPL limit order). Limit prices are in USD.
* **Minimum order value ≈ £1** (`min-value-exceeded`).
* **A resting stop reserves shares.** A second sell on the same shares is rejected ("Selling more equities than
  owned"). So targets and exits must cancel the stop and verify the cancel before selling. This is implemented.
* Limit, stop and stop-limit placements **share one rate-limit bucket** (a 429 was observed). The client now spaces
  them 3 s apart.
* FX fee on a real demo fill: £6.81 on £4,545.68 = **0.15%**, as modelled.
* Test orders were all cancelled. Verified: 0 pending orders afterwards.

## What was built (by phase)

| Phase | Built | Key files |
|---|---|---|
| 2 Data | Provider interface, Yahoo adapter, parquet cache, point-in-time S&P 500 membership, split un-adjustment for the $10 filter, NYSE calendar (US/UK DST-safe), earnings blackout, synthetic data for tests | `src/data/*` |
| 3 Backtester | Event-driven daily engine: next-open fills, limit entries (no chasing), gap-aware stops, FX fee on both legs, SEC/FINRA, dividends net of 15% withholding, no leverage | `src/backtest/engine.py`, `costs.py`, `metrics.py` |
| 4–6 Strategies | S0 classic 12-1 monthly (benchmark), A pure momentum, B + trend, **C + regime (primary)**, D breakout, E pullback. One shared, backward-looking feature pipeline | `src/strategy/*` |
| 7 Portfolio | Shared `decide()` used by **both** backtest and paper/live; sector cap; 60-day correlation gate; heat; daily/weekly rebalance | `src/portfolio/*` |
| 8 Risk | ATR/percent/swing stops, never widened; cost-aware target (R:R ≥ 2 **after** costs); GBP sizing with FX buffer, always rounded down; 5% heat cap; 20% weight cap; cash cap; −1.5% daily limit; kill switch; final pre-submission risk gate | `src/risk/*` |
| 9 Validation | Walk-forward (5y train / 1y validate / 1y test, neighbourhood-median selection), sealed 24-month holdout, sensitivity, cost stress, Monte Carlo (trade and daily bootstrap), Deflated Sharpe, regime buckets, automatic acceptance test, report | `src/backtest/walk_forward.py`, `monte_carlo.py`, `deflated_sharpe.py`, `src/reporting/research_report.py` |
| 10–11 Paper + T212 | Trading 212 v0 adapter: single-attempt POST, ambiguous outcome ⇒ UNKNOWN, paced GETs with retry, cursor pagination | `src/broker/*` |
| 12 Reconciliation | Intent-before-send duplicate protection; UNKNOWN resolution from broker state; fills → positions; stop fills → closures; unexplained differences ⇒ halt; manual positions ignored; **restart recovery** (re-adopts bot positions from API buy + API stop) | `src/execution/*`, `src/storage/*` |
| 13 Live adapter | Same code path. LIVE only with `TRADING_MODE=LIVE` + `ENABLE_LIVE_TRADING=YES` + pinned account id; otherwise PAPER | `src/config.py` |

## Tests

`python -m pytest -q` → **68 passed** (no network; mocked Trading 212 responses). They cover momentum, ranking,
trend, breakout, pullback subsets, regime, ATR, stops, 0.5% risk, R:R ≥ 2, heat, sector and correlation limits,
earnings blackout, FX costs (including the demo-fill check), daily loss limit, kill switch, duplicate-order
protection, ambiguous timeouts (exactly one POST), 429/5xx handling, pagination, reconciliation, restart recovery,
time zones (UK/US DST), split un-adjustment, dividends, gap stops, no-look-ahead (truncating future data does not
change today's signals), and YAML type sanity.

Bug caught during testing: YAML 1.1 parsed `2.0e7` (the $20m liquidity filter) as a **string**. Fixed, and a test
now guards all numeric settings.

End-to-end checks:
* `status` against demo: OK.
* `cycle` in DRY_RUN against demo: reconciliation OK. The cycle **aborted safely** at the market-data step (see
  blocker).
* The full research pipeline ran on 12 years of synthetic data (report, charts, acceptance table). As intended,
  the verdict was "DO NOT DEPLOY" because the criteria are strict. Synthetic results carry no information about
  real markets.

## BLOCKER: market data is not reachable from this cloud environment

The environment's network policy blocks every market-data host tried (Yahoo, Stooq, Nasdaq, SEC, FMP, Tiingo,
EODHD, fiscal.ai, FRED). Without prices the bot **correctly refuses to trade**, and no real backtest can run yet.

**To unblock:** in this environment's settings, add these hosts to the allowed domains:
`query1.finance.yahoo.com`, `query2.finance.yahoo.com`, `fc.yahoo.com`, `finance.yahoo.com`, `guce.yahoo.com`.
Then run `python -m src.main download-data && python -m src.main research`.

Alternatively, run the bot on your own computer or a VPS (see README §2–§6), where these hosts are normally
reachable.

## Assumptions / limitations / risks

* Yahoo data: survivorship bias (most delisted names are missing), symbol re-use, current sectors only, incomplete
  earnings history.
* Today's Trading 212 instrument list is not applied historically.
* Practice-account fills are simulated.
* Before 2003 the GBPUSD series is back-filled.
* The API is beta. The official docs site is blocked here; endpoints were verified against the official OpenAPI spec
  mirror and live demo responses.
* A cloud container is temporary. The local SQLite state can be lost; restart recovery mitigates this, and a
  persistent machine is recommended for paper trading.

## Next steps

1. Allow the data hosts (above) → run `download-data` and `research`.
2. If any strategy passes: run paper trading in DRY_RUN for a few days, then with `DRY_RUN=false` on the
   **practice** account for ≥ 3 months.
3. If none passes: do not deploy. Paper trading can still continue as software validation.
