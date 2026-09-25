# Phase 1 — Research & Specification

**Project:** stockTradeBot — long-only US equity momentum/trend system for Trading 212 Invest / Stocks & Shares ISA
**Status:** DRAFT FOR REVIEW. No trading code has been written. No broker calls have been made.
**Date:** 2026-09-25
**Default mode:** PAPER. Live trading is out of scope until every gate in §10 and §12 has been passed.

> Nothing in this document says or implies that the strategy is, or will be, profitable. The document sets out a
> hypothesis and a way to test it that is designed to *reject* the hypothesis if the evidence is weak.

---

## 0. Executive summary

1. **The broker API can carry out this design, with some caveats.** Trading 212's Public API (v0, still labelled
   *beta*) works only on Invest and Stocks ISA accounts. It supports market, limit, stop and stop-limit orders on
   both demo and live, plus positions, pending orders, order history and instrument metadata. It **provides no
   historical price data**, **no bracket/OCO orders**, **no order idempotency** and **no field that reports the
   account type**.
2. **The costs are not zero.** The API cannot hold USD balances (multi-currency is not supported through the
   API), so every round trip in a USD stock from a GBP account pays the **0.15% FX fee twice (≈0.30%)**. Spread,
   slippage and US regulatory sell fees come on top. At swing-trading turnover this costs an estimated
   **2–4% a year**, which is large compared with any plausible edge.
3. **The evidence is mixed.** Cross-sectional momentum is one of the most replicated anomalies. It is also
   weaker in large caps, weaker after publication, weaker long-only, exposed to severe crashes and sensitive to
   costs. The evidence for *short-horizon* technical overlays (breakouts, pullbacks, fixed-R targets) is much
   weaker than the evidence for classic 12-1 momentum.
4. **Tension in the requested design.** The literature supports momentum held for **3–12 months**. A 2R fixed
   target with a 2–3×ATR stop on large caps usually exits within weeks, so it truncates the right tail of
   returns, which is where momentum profits historically came from. We therefore include a **classic monthly
   12-1 momentum benchmark with no stops or targets (Strategy 0)**. It tells us whether the swing overlay adds
   value or removes it.
5. **Mathematical note.** Ranked across stocks, *relative strength vs. a benchmark* gives exactly the same order
   as *raw momentum*, because the benchmark return is the same for every stock. RS only changes anything when it
   is used as an **absolute threshold** (RS > 0). §6 defines the A/B/C comparison on that basis.
6. **Data is the critical dependency.** A backtest free of survivorship bias needs a paid dataset with
   point-in-time constituents and delisted stocks. Free data (e.g. Yahoo) is survivorship-biased, and results
   built on it must be labelled as such. **Decision needed from you: see §13 Q1.**

---

## 1. Trading 212 API capabilities relevant to this project

### 1.1 Sources and verification status

- The official site `docs.trading212.com` is **blocked by this development environment's network proxy**, so I
  could not read it directly. I verified the API against:
  - a copy of the **official OpenAPI 3.0.1 spec** ("Trading 212 Public API", version `v0`) mirrored in the public
    repository `api-evangelist/trading212` (`openapi/_original/openapi.yaml`, last updated 2026-09-22);
  - search-indexed excerpts of the official pages `docs.trading212.com/api/...` and the Trading 212 Help Centre;
  - Trading 212's community announcement (January 2026) that **limit, stop and stop-limit orders are now live on
    real-money accounts**. Before that, live accounts supported market orders only.
- **Action before Phase 11:** open the official docs from your own browser and re-check every endpoint in §1.3.
  The broker adapter will hold every path in one module, so a change means a one-file edit.

### 1.2 Environments and authentication

| Item | Value |
|---|---|
| Demo (paper) base URL | `https://demo.trading212.com/api/v0` |
| Live base URL | `https://live.trading212.com/api/v0` |
| Auth | HTTP Basic: username = API key, password = API secret (`Authorization: Basic base64(key:secret)`) |
| Key scopes seen in 403 errors | `account`, `portfolio`, `metadata`, `orders:read`, `orders:execute`, `history:orders`, `history:dividends`, `history:transactions` |
| IP restriction | Optional per-key IP allow-list in the Trading 212 app |
| Rate limits | Per **account**, not per key or IP. Burst model. Headers: `x-ratelimit-limit/-period/-remaining/-reset/-used` |

Keys are generated in the app for **each environment separately**. A demo key cannot trade live, and the reverse
also holds. This is the strongest guarantee that paper mode stays paper.

### 1.3 Endpoints (from the official spec)

| Purpose | Method & path | Rate limit | Scope |
|---|---|---|---|
| Account summary (id, currency, cash, investments, totalValue) | `GET /api/v0/equity/account/summary` | 1 / 5 s | account |
| Open positions (optional `?ticker=`) | `GET /api/v0/equity/positions` | 1 / 1 s | portfolio |
| Pending orders | `GET /api/v0/equity/orders` | 1 / 5 s | orders:read |
| One pending order | `GET /api/v0/equity/orders/{id}` | 1 / 1 s | orders:read |
| Cancel order | `DELETE /api/v0/equity/orders/{id}` | 50 / 1 min | orders:execute |
| Market order | `POST /api/v0/equity/orders/market` | 50 / 1 min | orders:execute |
| Limit order | `POST /api/v0/equity/orders/limit` | 1 / 2 s | orders:execute |
| Stop order | `POST /api/v0/equity/orders/stop` | 1 / 2 s | orders:execute |
| Stop-limit order | `POST /api/v0/equity/orders/stop_limit` | 1 / 2 s | orders:execute |
| Historical orders + fills (cursor-paginated) | `GET /api/v0/equity/history/orders` | 6 / 1 min | history:orders |
| Dividends | `GET /api/v0/equity/history/dividends` | 6 / 1 min | history:dividends |
| Cash transactions | `GET /api/v0/equity/history/transactions` | 6 / 1 min | history:transactions |
| CSV export (async) | `POST` / `GET /api/v0/equity/history/exports` | 1 / 30 s, 1 / 1 min | — |
| Instruments | `GET /api/v0/equity/metadata/instruments` | 1 / 50 s | metadata |
| Exchanges & trading schedules | `GET /api/v0/equity/metadata/exchanges` | 1 / 30 s | metadata |
| Pies | `/api/v0/equity/pies...` | — | **Deprecated — will not be used** |

Pagination: `limit` (default 20, max 50) plus `cursor`. Follow `nextPagePath` until it is `null`.

### 1.4 Order semantics (from the spec)

- **Quantity-based only.** Orders are placed by `quantity`. Value-based orders are "not currently supported via
  the API". Position sizing must therefore produce a share quantity.
- **Buy = positive quantity, sell = negative quantity.**
- **Fractional quantities** appear in the official examples (`"quantity": 0.1`). The minimum quantity and decimal
  precision per instrument are **not documented** (`minTradeQuantity` is no longer in the instrument schema, and
  `maxOpenQuantity` is). **Default: whole shares** until a demo test confirms fractional precision (see §13).
- `timeValidity`: `DAY` (expires at midnight in the exchange's time zone) or `GOOD_TILL_CANCEL`.
- Market order has `extendedHours` (bool). "If placed when the market is closed, the order will be queued to
  execute when the market next opens." Market orders carry an explicit slippage warning.
- Stop and stop-limit orders trigger on the **Last Traded Price**. A stop becomes a *market* order, and a
  stop-limit becomes a *limit* order.
- **Every order-placement endpoint is documented as NOT idempotent**: "Sending the same request multiple times may
  result in duplicate orders." The spec defines no client order ID field, so we cannot deduplicate by tag.
- There are no bracket, OCO or attached stop/target orders. Each protective order is a separate sell order.
- Functional limit: at most 50 pending orders per ticker per account.
- Order status enum: `LOCAL, UNCONFIRMED, CONFIRMED, NEW, CANCELLING, CANCELLED, PARTIALLY_FILLED, FILLED,
  REJECTED, REPLACING, REPLACED`. `initiatedFrom` distinguishes `API` from `WEB/IOS/ANDROID/...`. We can use it
  to tell bot orders from manual ones.
- `Fill.walletImpact` exposes `fxRate`, `netValue` and `taxes[]` with names that include
  `CURRENCY_CONVERSION_FEE`, `FINRA_FEE` and `TRANSACTION_FEE`. This lets us **measure actual costs** in paper and
  live trading and compare them with the backtest's assumptions.
- `Position` gives `quantity`, `quantityAvailableForTrading`, `averagePricePaid` and `currentPrice` (both **in the
  instrument currency**), plus `walletImpact` in account currency, including `fxImpact`.

### 1.5 What the API does NOT provide (design consequences)

| Missing | Consequence |
|---|---|
| Historical OHLCV / quotes | A separate market-data provider is **required** (§8.1). Trading 212 is used for execution only. |
| Account-type field | The bot cannot read "Invest vs ISA vs CFD" from the API. See §2.1 for how it verifies the account. |
| Idempotency keys | Duplicate protection must be reconciliation-based (§7.6). |
| Bracket/OCO | We cannot rest a stop *and* a target on the same shares without checking how the broker reserves quantity. Design: rest the stop at the broker; check the target at the daily close (§5.6). |
| Earnings / corporate events | An external source is needed. If none is available, the earnings rule fails safe: no new entries (§5.9). |
| Real-time "market open" flag | Use `/metadata/exchanges` working schedules and an NYSE holiday calendar together. If they disagree, do not trade. |

---

## 2. Account and order limitations

### 2.1 Account type verification (the API cannot report it directly)

Official statements: the API "is enabled and usable **only for Invest and Stocks ISA** account types". CFD
accounts cannot use it. This holds for the demo environment as well. `AccountSummary` contains `id`, `currency`,
`cash`, `investments` and `totalValue`, and **has no account-type field**.

Startup check. The bot fails safe (no trading, exit with an error) unless **all** of these hold:

1. `ACCOUNT_TYPE` in config is exactly `INVEST` or `ISA`. `CFD`, `SIPP` or blank → refuse to start.
2. `GET /account/summary` succeeds. Authentication with a Public API key is itself evidence that the account is
   Invest or ISA, because CFD accounts are not served by this API.
3. The returned `id` equals `T212_EXPECTED_ACCOUNT_ID`. This stops a key for the wrong account, such as a SIPP
   if one is ever exposed to the API, from being used.
4. The returned `currency` equals `ACCOUNT_BASE_CURRENCY` (expected `GBP`).
5. The environment (demo/live) matches `TRADING_MODE` and the live double opt-in (§7.8).

### 2.2 Limitations

- The API is in **beta**. Endpoints and behaviour may change. The adapter treats any unexpected schema as
  "ambiguous response → no trade".
- **Primary-currency execution only; multi-currency accounts are not supported through the API.** Values are
  reported in the primary currency and each USD trade is converted, so the FX fee applies on **both** buy and
  sell.
- Limit, stop and stop-limit rate limits are **1 request per 2 s** per account. With ≤10 positions that is fine,
  but it rules out rapid order changes.
- Cancellation "is not guaranteed if the order is already in the process of being filled". After every cancel we
  must re-query.
- **Unknowns that must be checked in demo before any order logic is final** (§13):
  (a) whether a resting sell stop reduces `quantityAvailableForTrading` and blocks a second sell order;
  (b) fractional precision and minimum quantity per US stock;
  (c) that limit/stop prices are expressed in the instrument currency (USD), which the position schema implies but
  the request schema does not say;
  (d) whether stop orders trigger in extended hours;
  (e) how a queued market order placed outside hours is filled at the open.
- Demo fills are simulated and are probably more optimistic than live fills. Paper results measure **operational
  correctness**, not the edge.

---

## 3. Trading 212 costs relevant to US stocks (Invest / ISA, GBP base)

| Cost | Current value (as found) | Applies | Backtest default | Stress |
|---|---|---|---|---|
| FX conversion fee | **0.15%** of the converted value | Every buy **and** every sell of a USD stock from a GBP account | 0.15% per side | 0.30% per side |
| Commission | £0 (Invest/ISA) | — | 0 | — |
| Custody / inactivity / ISA fees | £0 | — | 0 | — |
| SEC transaction fee (Section 31) | Rate set by the SEC and changed periodically; passed through on **sells** | Sells | configurable, default 0.00278% of value | 2× |
| FINRA TAF | ≈ $0.000195 per share sold (per Trading 212 help page) | Sells | configurable | 2× |
| Bid–ask spread + market impact | Implicit in the fill price | Every trade | 5 bps per side at the open for large caps | 10 and 15 bps |
| Stamp duty | Not applicable to US shares | — | 0 | — |
| US dividend withholding | 15% with W-8BEN (Invest and ISA) | Dividends | 15% | 30% (no W-8BEN) |
| Interest on idle cash | Trading 212 pays interest on cash | — | **0** (conservative) | — |

**Round trip at base costs ≈ 0.30% FX + 0.10% spread/slippage + ~0.003% regulatory ≈ 0.40% of position value.**
With ~80% of the portfolio invested and an average hold of ~25 trading days, turnover is roughly 8× a year on the
invested sleeve, which gives **≈2.5–3.5% a year in cost drag**. This is the single biggest practical obstacle, and
the acceptance criteria test it explicitly.

Sources: Trading 212 Help Centre, "What is the FX fee? (Invest & Stocks ISA)" and "What are the fees in the Invest,
ISAs, and SIPP?"; "What are the applicable stock exchange fees?". All cost figures live in
`config/settings.yaml` and must be re-checked before paper and before live.

---

## 4. Evidence for and against equity momentum / trend

### 4.1 Supporting evidence

- **Jegadeesh & Titman (1993, 2001):** US stocks ranked on 3–12-month past returns kept outperforming losers over
  3–12 months. The effect persisted after the original sample.
- **Carhart (1997):** momentum (UMD) became a standard risk factor. **Fama & French (2012)** and
  **Asness, Moskowitz & Pedersen (2013, "Value and Momentum Everywhere")** found momentum across countries and
  asset classes.
- **Novy-Marx (2012):** the 7–12-month ("intermediate") past return carries much of the signal, which supports
  skipping the most recent month.
- **Jegadeesh (1990), Lehmann (1990):** 1-month *reversal* is why the classic signal skips the last month (12-1).
- **George & Hwang (2004):** closeness to the 52-week high predicts returns. This gives some support to
  breakout-style entries.
- **Barroso & Santa-Clara (2015), Daniel & Moskowitz (2016):** momentum risk is predictable, and **volatility
  scaling** substantially reduces crash exposure. This supports ATR-based sizing.
- **Han, Zhou & Zhu (2016); Kaminski & Lo (2014):** stop-loss rules can reduce momentum's left tail when returns are
  serially correlated.
- **Faber (2007) and time-series momentum (Moskowitz, Ooi & Pedersen 2012):** trend filters such as price >
  10-month SMA historically reduced drawdowns at the index level. The result is mainly about asset-class indices
  and futures, not single stocks.

### 4.2 Contradicting / cautionary evidence

- **Momentum crashes (Daniel & Moskowitz 2016):** the US long-short momentum portfolio lost more than 70% in a few
  months in 2009 and suffered catastrophic losses in 1932. Crashes cluster in rebounds after bear markets, which
  is exactly when a 200-DMA regime filter is slow to switch back.
- **Publication decay (McLean & Pontiff 2016):** anomaly returns are about a third lower after publication. US
  momentum was weak or negative for long stretches after 2000.
- **Replication (Hou, Xue & Zhang 2020):** many anomalies fail after microcaps are excluded. Momentum survives
  better than most, but it is weaker in large caps, and large caps are our universe.
- **Long-only captures only part of the premium (Israel & Moskowitz 2013):** a substantial part of the classic
  premium comes from the short leg, which we cannot trade.
- **Costs (Lesmond, Schill & Zhou 2004; Novy-Marx & Velikov 2016):** high-turnover momentum variants lose much or
  all of their paper profit after realistic costs. Our FX fee makes this worse.
- **Technical trading rules and data snooping (Sullivan, Timmermann & White 1999; Bajgrowicz & Scaillet 2012):**
  the apparent profitability of simple technical rules largely disappears out of sample and after
  multiple-testing corrections. MA timing results are highly period-dependent (Zakamulin 2014).
- **Short-horizon targets:** I found no credible evidence that fixed 2R/3R profit targets improve momentum
  returns. Momentum profits are concentrated in a minority of large winners, so capping them may destroy the
  edge. This is a hypothesis to test, not an assumption.
- **Backtest overfitting (Bailey & López de Prado 2014; Harvey, Liu & Zhu 2016):** testing many variants inflates
  the best Sharpe ratio. We must correct for the number of trials (Deflated Sharpe Ratio) and record every trial.

### 4.3 Honest prior

A realistic expectation for a long-only large-cap momentum strategy **after** our costs is a modest excess return
over the S&P 500 with a lower long-run drawdown **at best**. Underperforming the index, especially in fast
rebounds, is quite possible. The most likely failure mode is: "works in-sample; after FX and slippage the
out-of-sample edge is statistically indistinguishable from zero". If that happens, the correct output is
**do not deploy; hold a low-cost index fund instead**.

---

## 5. The exact strategy proposed for testing first

### 5.1 Pre-registered primary candidate: "MT-1" (Momentum + Trend, daily review)

This is the one strategy I propose we judge first. Everything else is a comparison or a sensitivity test.

| Component | Rule |
|---|---|
| Universe | Point-in-time US common stocks meeting the §5.2 filters and available on Trading 212 |
| Signal | 12-1 momentum percentile rank (`MOM_12_1`), ≥ 0.80 to enter |
| Absolute filter | `MOM_12_1 > 0` **and** `RS_12_1 > 0` (beats the benchmark) |
| Trend filter | `Close > SMA200` **and** `SMA50 > SMA200` |
| Market regime | New entries only when `SPY_Close > SPY_SMA200` |
| Volatility filter | Skip if `ATR14 / Close > 6%` |
| Entry | Decision after the close on day T; **buy limit order, DAY**, at `L = Close_T × (1 + 0.25 × ATR%_T)`; executes on T+1 |
| Initial stop | `S = Close_T − 3.0 × ATR14_T`, placed as a resting **GTC sell-stop** at the broker; never widened |
| Target | `Entry + 2.0 × R`, checked at the close; exit at the next open |
| Time exit | 40 trading days |
| Trend exit | Close < SMA50 → exit at the next open |
| Sizing | 0.5% equity risk at the worst-case entry price `L`, in GBP, including costs (§7) |
| Portfolio | ≤ 10 positions, ≤ 3 per sector, ≤ 5% total heat, correlation gate (§5.8) |
| Earnings | No new entry within 3 trading days before a scheduled earnings date |
| Rebalance | Daily review; entries only when a slot and a risk budget are free |

### 5.2 Universe

1. **Research universe (point-in-time).** Preferred: historical S&P 500 or Russell 1000 constituents on each date,
   including later-delisted names. If market-cap history exists, apply `MarketCap ≥ $5bn`. Otherwise
   index membership acts as the large-cap proxy, and this is documented.
2. **Security type:** common stock only. Exclude ETFs, ADRs (configurable), preferreds, warrants, units, SPACs
   before business combination, OTC, and leveraged or inverse products.
3. **Tradability filters, computed on data up to T only:**
   - `UnadjClose_T ≥ $10`
   - `ADV$_60 = mean(UnadjClose × Volume over the last 60 sessions) ≥ $20m`
   - at least 273 sessions of history (252 plus a buffer), no missing bars in the last 20 sessions, last bar dated T
4. **Broker availability:** the ticker maps to a Trading 212 instrument with `type == STOCK`, `currencyCode ==
   USD` and a US exchange working schedule. The mapping uses ISIN first, then ticker (`AAPL_US_EQ` format).
   **In the backtest this filter uses today's instrument list, which is a look-ahead: Trading 212 did not list
   everything historically.** This bias is small for large caps and is documented rather than hidden.

### 5.3 Execution timing

- The signal uses bars up to and including the close of day T, **after** the data vendor has published T's final
  bar. Default run time: 23:30 Europe/London, configurable. The code converts via `America/New_York`, because US
  and UK daylight-saving changes differ by a few weeks each year.
- The order is submitted after the close and before the T+1 open. The fill model (§8.3) never uses a price from
  day T to fill a day-T signal.

### 5.4 Variant definitions (all use the same universe, costs, risk and portfolio rules)

| ID | Name | Entry rule |
|---|---|---|
| **S0** | Classic benchmark | Monthly: hold the top 10 by `MOM_12_1` rank, equal risk, **no stops or targets**, exit when rank < 0.50 at month-end |
| **A** | Pure momentum | Rank ≥ 0.80 on `MOM_12_1`; no trend or regime filter; §5.5 exits |
| **B** | Momentum + trend | A + `Close > SMA200` and `SMA50 > SMA200` |
| **C** | Momentum + trend + regime | B + regime rule (B1 block / B2 reduce; see §6.6) = **MT-1** |
| **D** | Momentum + breakout | C + `Close_T > max(High_{T−N..T−1})`, N ∈ {20, 50, 60} |
| **E** | Momentum + pullback | C + pullback/confirmation (§6.8) |

### 5.5 Exit rules, each tested separately against a fixed base

Base exit set for A–E: initial stop + 2R target + 40-day time stop.
We then add **one** of these at a time and compare:

1. Trend deterioration: Close < SMA50.
2. Momentum deterioration: rank < 0.50.
3. Trailing (chandelier) stop instead of the target: `S_t = max(S_{t−1}, HighestClose_since_entry − 3 × ATR14_t)`.
4. Partial: sell 50% at 2R, move the stop on the remainder to breakeven, then trail as in (3).
5. Market regime: SPY < SMA200 → exit all at the next open (tested as an alternative, not as the default).
6. Portfolio risk reduction (§7.5).

### 5.6 How stop and target are carried out live (and modelled identically in the backtest)

- **Stop:** a resting GTC sell-stop at the broker (`POST /orders/stop`, negative quantity). It protects the
  position intraday even if the bot is down. It becomes a market order when triggered, so slippage and gap risk
  apply.
- **Target / time / trend exits:** checked after the close; a sell order goes out the next session. Before any
  exit sell, the bot cancels the resting stop, confirms the cancel by re-query, then sells. (Whether both can rest
  together is the verification item in §2.2(a).)
- The stop is only ever moved **up** (trailing variants), by cancel-then-replace with the same verification.

### 5.7 Earnings, news and corporate events

- `EARNINGS_BLACKOUT_DAYS = 3` trading days (test 1, 2, 3, 5): no new entry if the next confirmed earnings date is
  within the window. **If the earnings date is unknown, the stock is ineligible for entry.**
- Existing positions: no forced exit before earnings in the base case. "Exit before earnings" is tested as a
  separate variant.
- Corporate actions: if the vendor flags a pending merger, tender or delisting, or a trading halt → no entry.
  Stale price (last bar ≠ T) → no entry and no stop modification.

### 5.8 Portfolio construction

- Candidates that pass all filters are sorted by momentum rank (ties broken by lower ATR%).
- Accept in that order while:
  `positions < 10`, `sector_count < 3`, `heat + new_risk ≤ 5%`, `cash ≥ cost`, and the correlation gate passes.
- **Correlation gate:** ρ = Pearson correlation of 60-day daily log returns. Reject the candidate if ρ > 0.75 with
  **two or more** current holdings. Thresholds are fixed in advance and tested at ±0.1 only.
- **Sectors:** GICS if point-in-time data is available; otherwise vendor sector or SIC-derived groups. The source
  is documented.
- **Hysteresis:** enter at rank ≥ 0.80; the momentum exit (when enabled) triggers at < 0.50. Rank changes between
  those levels cause no trade.
- **Rebalance frequency:** daily vs weekly (Friday decision, Monday execution), both tested.

---

## 6. Exact mathematical definitions

Notation: trading-day index `t`. `C, H, L, O, V` are split- **and** dividend-adjusted OHLCV unless marked `Unadj`.
All look-backs use trading days: 1M = 21, 3M = 63, 6M = 126, 9M = 189, 12M = 252. Every quantity at `t` uses data
with index ≤ t only.

### 6.1 Returns and momentum

```
R(t; a, b)   = C[t−a] / C[t−b] − 1                       (a < b)
MOM_L(t)     = R(t; 0, L)          for L ∈ {63, 126, 189, 252}
MOM_12_1(t)  = R(t; 21, 252)       (classic; skips the most recent month)
```

### 6.2 Cross-sectional percentile rank

For the eligible set `U_t` with `N_t = |U_t|` (require `N_t ≥ 50`, otherwise no trading that day):

```
rank_pct_i(t) = (r_i − 1) / (N_t − 1)
```

`r_i` is the ascending rank of stock i's signal (1 = lowest), with ties given the average rank. So 1.0 = strongest
and 0.0 = weakest.

### 6.3 Relative strength

```
RS_L(t)     = MOM_L_stock(t)    − MOM_L_bench(t)
RS_12_1(t)  = MOM_12_1_stock(t) − MOM_12_1_bench(t)
```

Benchmark = SPY total-return series (configurable). **Note:** `rank_pct(RS_L) ≡ rank_pct(MOM_L)` because the
benchmark term is the same for every stock. The three comparisons are therefore defined as:

- **A. Absolute momentum:** rank ≥ θ **and** `MOM_12_1 > 0`
- **B. Relative strength:** rank ≥ θ **and** `RS_12_1 > 0`
- **C. Combined:** rank ≥ θ **and** `MOM_12_1 > 0` **and** `RS_12_1 > 0`

### 6.4 Moving averages

```
SMA_n(t) = (1/n) · Σ_{k=0}^{n−1} C[t−k]
```

Trend variants: **T0** none; **T1** `C > SMA200`; **T2** `SMA50 > SMA200`; **T3** T1 ∧ T2.

### 6.5 Average True Range (Wilder, n = 14)

```
TR(t)    = max(H[t] − L[t], |H[t] − C[t−1]|, |L[t] − C[t−1]|)
ATR(t)   = ATR(t−1) + (TR(t) − ATR(t−1)) / 14         (seeded with the 14-day simple mean of TR)
ATR%(t)  = ATR(t) / C[t]
```

Split-adjusted OHLC must be used, otherwise a split creates a false huge TR.

### 6.6 Market regime

```
BULL(t) = SPY_C[t] > SPY_SMA200(t);   BEAR otherwise
```

- R0: no regime filter
- R1: new entries only in BULL
- R2: in BEAR, risk per trade × 0.5 and max positions × 0.5

### 6.7 Breakout

```
BO_N(t) = C[t] > max(H[t−N], …, H[t−1])        N ∈ {20, 50, 60}
```

The comparison excludes today's high, and the close must exceed the prior highest high.

### 6.8 Pullback (P20 shown; P50 uses SMA50 for the touch and SMA200 as the floor)

A stock is in *setup* if it passes rank ≥ 0.80 and trend T3 on the day the pullback begins.

```
Touch(t)    = ∃ j ∈ [t−5, t−1] : L[j] ≤ SMA20(j)                      (pulled back to the 20-DMA)
Controlled  = min(C[t−5..t]) ≥ SMA50(t)  and  (maxH_20 − minL_5) ≤ 3·ATR(t)
Confirm(t)  = C[t] > H[t−1]  and  C[t] > SMA20(t)
Entry if    Setup ∧ Touch ∧ Controlled ∧ Confirm
```

Swing-low stop for the pullback: `S = min(L[t−5..t]) − 0.5 · ATR(t)`.

### 6.9 Stop methods (the stop is fixed at decision time T)

- **ATR:** `S = C[T] − m · ATR(T)`, m ∈ {2.0, 2.5, 3.0}
- **Percent:** `S = C[T] · (1 − p)`, p ∈ {8%, 10%, 12%}
- **Swing low:** `S = min(L[T−9..T]) − 0.5 · ATR(T)`
- **Validity:** if `S ≤ 0`, or `(L_entry − S)/L_entry > 20%`, or `(L_entry − S) < 0.5 · ATR` → **no trade**

### 6.10 Target and R:R

```
R_per_share   = E − S
Target        = E + k · R_per_share,      k ∈ {2.0, 2.25, 2.5, 3.0}
Planned R:R   = (Target − E_worst − c_rt) / (E_worst − S + c_rt) ≥ 2.0    else reject
```

Here `c_rt` is the round-trip cost per share. **Honest note:** because the target is placed mechanically, R:R ≥ 2
holds almost by construction. The rule prevents poorly specified trades; it does not create an edge. The edge
must come from the win rate: breakeven win rate for a k = 2 payoff is ≈ 1/(1+2) ≈ 33% before costs and higher
after costs. The backtest reports the realised R distribution so this can be checked.

---

## 7. Risk and position sizing (exact formulas)

### 7.1 Inputs at decision time

- `Eq` = account equity in GBP = `AccountSummary.totalValue` (live/paper) or the simulated equity (backtest)
- `fx` = USD per 1 GBP (mid, from the market-data provider, as of T's close)
- `f_fx` = 0.0015, `s` = slippage per side (fraction), `sec`, `finra` from config
- `L` = entry limit price (USD), the **worst possible entry**
- `S` = stop price (USD)

### 7.2 Risk per share in GBP, including costs

```
exit_worst      = S · (1 − s_stop)                           # stop slippage assumption (base 0.5%)
loss_px_usd     = L − exit_worst
cost_usd        = f_fx·L + f_fx·exit_worst + sec·exit_worst + finra
risk_ps_gbp     = (loss_px_usd + cost_usd) / (fx · (1 − fx_buffer))    # fx_buffer = 0.5% to cover FX moves before fill
```

### 7.3 Quantity

```
risk_budget     = min(0.005 · Eq,  heat_cap · Eq − current_heat)       # heat_cap = 0.05
q_risk          = risk_budget / risk_ps_gbp
q_weight        = (w_max · Eq) · fx / (L · (1 + f_fx))                  # w_max = 20% of equity (no concentration)
q_cash          = cash_available_gbp · fx / (L · (1 + f_fx + s))        # never borrow, no leverage
q_broker        = maxOpenQuantity − existing_qty
q               = floor_to_step(min(q_risk, q_weight, q_cash, q_broker), step)
```

- `step` = 1 share by default. The fractional step (e.g. 0.01 or 0.001) is enabled only after demo verification.
- **Always round down.** Rounding can never increase risk.
- If `q < step`, or `q · risk_ps_gbp > 0.005 · Eq` (a defensive re-check), → **no trade**.

Worked example (GBP account, `Eq` = £10,000, fx = 1.25): `C = $100`, ATR = $2, `S = 100 − 3·2 = $94`,
`L = 100·(1 + 0.25·0.02) = $100.50`, `s_stop` = 0.5% → exit_worst = $93.53.
`loss_px` = $6.97; `cost` ≈ 0.15 + 0.14 + ~0 = $0.29; `risk_ps` = 7.26 / (1.25·0.995) = £5.84.
`q_risk` = 50 / 5.84 = 8.56 → **8 shares** (whole-share mode). Position ≈ $804 ≈ £643 (6.4% of equity).
Planned risk ≈ £46.7 ≤ £50 ✔.

### 7.4 Portfolio heat (open risk)

```
heat = Σ_positions max(0, (P_t − S_t·(1 − s_stop)) · q) / fx  +  exit costs
```

`P_t` = latest close. This is conservative: it counts the risk from the current price down to the stop, so
unprotected winners consume budget until the stop is raised. A `heat_definition: initial` alternative is
available as a documented variant.

### 7.5 Limits

- Max positions: 10. Max risk per position: 0.5%. Max heat: 5%. Max weight per name: 20%. Sector cap: 3 positions.
- **Daily loss limit:** if `(Eq_now − Eq_prev_close) / Eq_prev_close ≤ −1.5%` → no new buys until the next trading
  day **and** an explicit reset, if configured. Existing stops stay in place. The event `DAILY_LIMIT_REACHED` is
  logged.
- **Kill switch:** `KILL_SWITCH=true` (env) **or** the file `./KILL_SWITCH` exists → no new orders of any kind
  except verified cancels of the bot's own *entry* orders. Resting stops remain. Monitoring continues. The event
  `KILL_SWITCH` is logged.
- **Gap risk disclosure:** 0.5% is the *planned* risk. A gap through the stop can produce a larger loss. The
  backtest reports the worst loss in R and in % of equity.

### 7.6 Duplicate-order protection (non-idempotent API)

1. Before submitting, write an `INTENT` row: uuid, ticker, side, qty, type, prices, and status `PENDING_SUBMIT`.
2. Submit **once**. A 2xx response → store the broker `id`, status `SUBMITTED`.
3. On timeout, 5xx, 408, network error or ambiguous body: status `UNKNOWN`. **Never retry immediately.**
4. Resolve `UNKNOWN`: query `GET /orders` (pending), `GET /history/orders?ticker=` (the most recent items since the
   intent timestamp, `initiatedFrom == API`, same side and quantity) and `GET /positions?ticker=`.
   - Match found → link it and continue.
   - No match after N polls, with the rate limits respected → mark `NOT_PLACED`. A new intent may be created
     **only** at the next decision cycle, never in the same cycle.
   - Evidence that conflicts → `NEEDS_REVIEW`, halt new orders for that ticker, alert.
5. One open entry intent per ticker, and one open protective stop per position. Enforced locally before every
   submit.

### 7.7 Reconciliation on startup (and before every decision cycle)

account → positions → pending orders → recent history (paginated) → compare with the local DB → classify each
difference:

| Case | Action |
|---|---|
| Broker position unknown to the DB (manual buy) | Flag `UNEXPECTED_POSITION`. **Do not close.** Default policy `IGNORE_AND_EXCLUDE`: do not manage it, and count it in heat and exposure. |
| DB position missing at the broker | Look for a fill in history (stop hit or manual sale) → record it. If none is found → `NEEDS_REVIEW`, halt. |
| Quantity mismatch | Adopt the broker quantity, re-verify the stop quantity, log it. |
| Position without a resting stop | Re-place the stop at the recorded stop price (never lower). If the price is already below the stop → exit at market. |
| Unknown pending order | Flag it and leave it. |

### 7.8 Mode safety

`TRADING_MODE=LIVE` **and** `ENABLE_LIVE_TRADING=YES` **and** a live base URL **and** matching account id → live.
If anything is missing or different → PAPER (demo URL). There is also a `DRY_RUN` mode, where orders are computed
and logged but never sent.

---

## 8. Backtest methodology

### 8.1 Data (decision needed — §13 Q1)

| Option | Point-in-time constituents | Delisted stocks | Earnings dates | Cost | Verdict |
|---|---|---|---|---|---|
| **Norgate Data (US Stocks, Platinum)** | Yes (S&P 500 / Russell history) | Yes | No | ~$ subscription | **Recommended for prices and universe** |
| Sharadar via Nasdaq Data Link (SEP/SF1/SP500) | S&P 500 history | Yes | Filing dates only | ~$ subscription | Good alternative |
| CRSP / Compustat (academic) | Yes | Yes (with delisting returns) | Via I/B/E/S | Institutional | Gold standard if you have access |
| Yahoo Finance (`yfinance`) | **No** | **No** | Limited | Free | Prototyping only; **survivorship-biased** |

Earnings dates: a historical earnings calendar (e.g. Zacks, EODHD, FMP or I/B/E/S). If none is available, the
earnings filter is disabled in the backtest, this is reported, and the live bot refuses entries with unknown
dates.

### 8.2 Engine (event-driven, daily bars)

For each session t, in this order:

1. **Open(t):** process orders created at close(t−1).
   - Buy limit L: if `O ≤ L` → fill at `min(L, O·(1+s))`; else if `Low ≤ L` → fill at `L`; else expire.
   - Market sells → `O·(1 − s)`.
2. **Intraday(t):** resting stops. If `O ≤ S` → fill at `O·(1 − s_stop)` (gap). Else if `Low ≤ S` → fill at
   `S·(1 − s_stop)`.
   - Same-day entry and stop: allowed only if `Low ≤ S` after the fill. Conservative assumption: the stop is hit.
3. **Corporate actions(t):** split-adjusted positions; dividends credited on the ex-date at 85%; delisting → exit at
   the last price with the vendor's delisting return, or −30% if that is unavailable (documented).
4. **Close(t):** mark to market in USD and GBP (fx at t); apply the daily loss limit; update trailing stops
   (effective t+1); compute signals; build orders for t+1.

Costs are applied on every fill (§3). Cash can never go negative. There is no shorting and no margin.

### 8.3 Look-ahead guards

- Signals at t use arrays sliced `[:t+1]`, and fills use `t+1` bars. There is a unit test that shifts future data
  and asserts that today's signals do not change.
- The universe is chosen as of t from point-in-time membership. Earnings dates use "as known at t" where the
  vendor supports it.
- Parameters are chosen only from training windows (§9).

### 8.4 Periods

Target: **2000-01-01 → latest**, if the data allows. This covers the dot-com bust, 2003–07 bull, 2008 GFC, 2009
momentum crash, 2011 drawdown, 2015–16 volatility, 2018 Q4, 2020 COVID crash and rebound, 2022 rate shock and bear
market, and 2023–26. Sub-period results are reported by regime: bull/bear (SPY vs 200-DMA), high/low volatility
(VIX above or below its median), and rising/falling rates (10y yield 12-month change).

### 8.5 Metrics reported

All the §31/§32 metrics from the brief, plus: exposure %, cost drag (gross − net), number of trials, Deflated
Sharpe Ratio, R-multiple histogram, and monthly and yearly return tables. Benchmarks: SPY total return
buy-and-hold (GBP and USD), equal-weight eligible universe (monthly rebalance), and S0.

### 8.6 Monte Carlo (a simulation, not a prediction)

- 10,000 bootstrap resamples of the OOS **trade R-multiples**: iid and block bootstrap (block = 20 trades, to keep
  clustering).
- Also resample of **daily portfolio returns** with a stationary bootstrap (mean block 20 days).
- Report the 5/50/95th percentiles of max drawdown, longest losing streak, final equity and time under water.

---

## 9. Out-of-sample methodology

1. **Final holdout (sealed):** the last 24 months (≈ 2024-10 → 2026-09) are **not touched** until one strategy
   configuration is frozen. It is run once, and the result is reported whatever it is.
2. **Walk-forward on the remaining data**, rolling:
   - TRAIN 5y → VALIDATE 1y → TEST 1y, step 1y.
   - Example first fold: train 2000–04, validate 2005, test 2006. Last fold ends before the holdout.
3. **Parameter grid (small and pre-registered)** per variant, ≤ 48 combinations:
   - momentum look-back {63, 126, 252, 12-1}
   - ATR multiple {2.0, 2.5, 3.0}
   - target k {2.0, 2.5, 3.0} (2.25 in the sensitivity pass only)
   - breakout N, pullback MA, and time stop {20, 40, 60} are tested one factor at a time around the base.
4. **Selection in TRAIN is not by the highest return.** Choose the parameter set that maximises the **median Sharpe
   of itself and its grid neighbours**, which rewards plateaus, subject to ≥ 100 train trades.
5. **VALIDATE** confirms the selection: expectancy > 0 and PF > 1 in the validation year, else fall back to the
   pre-registered base parameters (MT-1).
6. **TEST:** results are recorded only. Test years are concatenated into one OOS equity curve and trade list.
7. The **strategy variant choice** (S0/A–E) also uses only train and validation evidence. Every variant's OOS result
   is reported, including the losers.
8. Every run is logged to `reports/trials.csv` (config hash, window, metrics) so the trial count used in the
   Deflated Sharpe is honest.

---

## 10. Acceptance criteria (pre-registered; **all** must hold on concatenated OOS results after base costs)

| # | Criterion | Threshold |
|---|---|---|
| 1 | Sample size | ≥ 200 OOS trades and ≥ 15 per test year on average |
| 2 | Expectancy | Mean R > 0.10 **and** the block-bootstrap 95% CI lower bound > 0 |
| 3 | Profit factor | ≥ 1.20 at base costs; ≥ 1.00 at 2× slippage and 0.30% FX per side |
| 4 | Drawdown | OOS max DD ≤ 25%; Monte Carlo 95th-percentile DD ≤ 35% |
| 5 | Risk-adjusted vs passive | OOS Sharpe **or** Calmar > SPY buy-and-hold over the same OOS period, net of all costs; otherwise recommend the index instead |
| 6 | Overfitting | Deflated Sharpe Ratio probability ≥ 0.90, given the recorded trial count |
| 7 | Walk-forward stability | Positive net return in ≥ 60% of test years; no single year > 50% of total OOS profit |
| 8 | Parameter stability | ≥ 70% of grid neighbours have PF > 1 and expectancy > 0; otherwise **FRAGILE → reject** |
| 9 | Cost efficiency | Annual cost drag ≤ 33% of gross annual return |
| 10 | Regimes | No regime bucket (bull/bear, high/low vol, rising/falling rates) with expectancy < −0.15R across ≥ 30 trades |
| 11 | Holdout | Expectancy > 0 and max DD ≤ 25% in the sealed holdout |
| 12 | Tail | Worst single trade ≥ −3R; worst month ≥ −8% |

**If no variant passes:** do not deploy. The final report states that the research did not establish enough
evidence. Paper trading may still go ahead for **engineering validation only**, clearly labelled as such.

**Paper → live gate (later):** ≥ 3 months and ≥ 30 filled orders in demo; zero duplicate orders; zero unresolved
reconciliation breaks; measured fees and slippage within the modelled range; a shadow backtest of the paper period
matches the paper decisions ≥ 95%; and your explicit written sign-off.

---

## 11. Proposed project structure

The layout from the brief, with a few additions marked ✚.

```
/src
  main.py                     # CLI entry: backtest | walkforward | paper | live | reconcile | kill
  config.py                   # loads settings.yaml + env, validates, enforces mode safety
  broker/
    interface.py              # abstract Broker (execution only)
    trading212_client.py      # HTTP client: auth, rate-limit headers, pagination, typed errors
    models.py                 # dataclasses for Account, Instrument, Position, Order, Fill
    simulated_broker.py  ✚    # in-process broker for backtest/dry-run, same interface
  data/
    market_data.py            # provider interface + adapters (Norgate/Sharadar/yfinance)
    fundamentals.py           # market cap, sector
    earnings.py               # earnings calendar, unknown => ineligible
    universe.py               # research -> T212 availability -> tradable universe
    fx.py                ✚    # GBPUSD series and conversion helpers
    calendar.py          ✚    # NYSE sessions, US/UK DST-safe time handling
  strategy/
    interface.py  momentum.py  momentum_trend.py  momentum_breakout.py
    momentum_pullback.py  indicators.py  ranking.py  regime.py ✚
  portfolio/
    constructor.py  exposure.py  correlation.py  sector_limits.py
  risk/
    position_sizing.py  risk_manager.py  stops.py  daily_limits.py ✚
  execution/
    execution_engine.py  order_manager.py  reconciliation.py
  backtest/
    engine.py  fills.py ✚  costs.py  metrics.py  walk_forward.py  monte_carlo.py  deflated_sharpe.py ✚
  monitoring/
    logger.py  health.py  kill_switch.py
  storage/
    database.py  repositories.py  schema.sql ✚
  reporting/            ✚
    daily_report.py  research_report.py
/tests                   # pytest; mocked T212 responses; no network
/config/settings.yaml
/docs                    ✚ specification, assumptions, phase reports
/reports                 # generated (git-ignored except .gitkeep)
/scripts                 # data download, demo read-only probe
.env.example  .gitignore  README.md  requirements.txt
```

Libraries (proposed): `pandas`, `numpy`, `pyyaml`, `pydantic` (config validation), `httpx` (timeouts, retries off
for POST), `exchange_calendars`, `structlog`, `SQLAlchemy` (SQLite now, PostgreSQL later), `pytest`, `responses`
or `respx` (HTTP mocks), `matplotlib` (report charts). Python 3.11+.

Code convention (as requested): **each non-trivial line or block gets a comment that explains what it does and why.**

---

## 12. Phase plan

### 12.1 Completing Phase 1 (research/specification)

| Step | Owner | Status |
|---|---|---|
| Verify API capabilities, order types and limitations | Claude | ✔ Done (via mirrored official spec; the official site is blocked here, so you should re-check) |
| Verify costs | Claude | ✔ Done (FX 0.15% confirmed; SEC/FINRA are configurable and should be re-checked) |
| Literature review | Claude | ✔ Done (§4) |
| Strategy, signals, sizing, backtest, OOS and acceptance specs | Claude | ✔ Draft (§5–§10) |
| **Review this document and approve or amend** | **You** | ⏳ |
| **Choose the data source (§13 Q1)** | **You** | ⏳ |
| Create **demo** API key (read-only scopes first: account, portfolio, metadata, orders:read, history:*) and store it as an environment secret, **never in chat** | You | ⏳ |
| Read-only demo probe script (`scripts/probe_demo.py`): account summary shape, instrument list and US ticker format, exchange schedules | Claude | Next, after approval |

### 12.2 Following phases (none skipped)

2. **Data layer:** provider interface, chosen adapter, local Parquet cache, point-in-time universe, FX series,
   calendar, data-quality checks (gaps, stale bars, split sanity) and tests.
3. **Backtest engine:** event loop, fill model, costs, corporate actions, metrics, look-ahead tests.
4. **S0 + A** (pure momentum).
5. **B, C** (trend and regime).
6. **D, E** (breakout and pullback).
7. **Portfolio construction:** sector and correlation gates.
8. **Risk engine:** sizing, heat, daily limit, kill switch.
9. **Walk-forward, robustness, Monte Carlo, Deflated Sharpe → research report and go/no-go.**
10. **Paper trading** (simulated broker, same code path).
11. **Trading 212 demo integration:** order endpoints, and the §2.2 verifications done with real demo orders.
12. **Reconciliation and monitoring.**
13. **Live adapter:** disabled by default, double opt-in, and only if §10 passes.

Each phase ends with the report format from brief §56.

---

## 13. Open questions and decisions needed from you

1. **Data source.** Norgate (recommended), Sharadar, academic CRSP, or free Yahoo data. Yahoo means results are
   explicitly survivorship-biased and cannot count as evidence for deployment.
2. **Account:** Invest or Stocks ISA? Base currency GBP? (The ISA matters for tax: no CGT on gains. In an Invest
   account, frequent trading creates many CGT disposal events that you will need to report.)
3. **Earnings data source**, or accept "earnings filter disabled in the backtest, unknown dates are ineligible live".
4. Do you accept the **§10 acceptance criteria** as written? They are deliberately strict.
5. Fractional shares: fine to enable once the demo verification is done? This matters for small accounts, since
   whole shares of high-priced stocks may be impossible at 0.5% risk.

**Environment note:** this cloud development environment has a proxy that injects credentials for
`live.trading212.com` (the *live* host). I **made no calls to it**. For development, I recommend replacing it with
a **demo** key only (with read-only scopes first), or removing it, so that no code in this session can reach the
live account.

---

## Phase 1 report (per brief §56)

- **What was built:** this specification; repository scaffolding (`README.md`, `.gitignore`, `.env.example`,
  `config/settings.yaml` with pre-registered parameters). No trading logic.
- **Files created:** `docs/PHASE1_SPECIFICATION.md`, `README.md`, `.gitignore`, `.env.example`,
  `config/settings.yaml`, `reports/.gitkeep`.
- **Files changed:** none (new repository).
- **Tests performed:** none applicable (no code). YAML syntax of `settings.yaml` was validated by loading it with
  PyYAML.
- **Assumptions:** GBP base account; SPY as the benchmark; order prices in USD for US instruments (to verify); a
  paid point-in-time dataset will be chosen.
- **Limitations:** the official docs could not be fetched directly from this environment (proxy block). The API
  facts come from a mirror of the official OpenAPI spec updated 2026-09-22 plus indexed official excerpts. SEC and
  FINRA rates are approximate.
- **Risks:** the API is beta and may change; there is no idempotency; costs relative to the edge; momentum crash
  risk; survivorship bias if free data is used.
- **Unresolved issues:** §2.2 verification items (a)–(e); data-source choice; the earnings data source.
- **Next phase:** after your approval → read-only demo probe, then Phase 2 (data layer).

## References

Jegadeesh & Titman (1993) *J. Finance*; Jegadeesh & Titman (2001) *J. Finance*; Carhart (1997) *J. Finance*;
Asness, Moskowitz & Pedersen (2013) *J. Finance*; Novy-Marx (2012) *JFE*; Jegadeesh (1990) *J. Finance*;
Lehmann (1990) *QJE*; George & Hwang (2004) *J. Finance*; Barroso & Santa-Clara (2015) *JFE*;
Daniel & Moskowitz (2016) *JFE*; Han, Zhou & Zhu (2016) working paper "Taming Momentum Crashes";
Kaminski & Lo (2014) *J. Financial Markets*; Faber (2007) *J. Wealth Management*;
Moskowitz, Ooi & Pedersen (2012) *JFE*; McLean & Pontiff (2016) *J. Finance*; Hou, Xue & Zhang (2020) *RFS*;
Israel & Moskowitz (2013) *JFE*; Lesmond, Schill & Zhou (2004) *JFE*; Novy-Marx & Velikov (2016) *RFS*;
Sullivan, Timmermann & White (1999) *J. Finance*; Bajgrowicz & Scaillet (2012) *JFE*; Zakamulin (2014)
*J. Asset Management*; Bailey & López de Prado (2014) *J. Portfolio Management*; Harvey, Liu & Zhu (2016) *RFS*.

Broker/cost sources: Trading 212 Public API reference (`docs.trading212.com/api`, via the mirrored OpenAPI spec);
Trading 212 Help Centre FX-fee and fees articles; Trading 212 community API update (January 2026: limit/stop/stop-limit
enabled on live).
