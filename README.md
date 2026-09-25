# stockTradeBot

A research-first, **long-only US stock momentum + trend** system that can paper-trade (and, only after
strict tests, live-trade) through the **Trading 212 Public API** on an **Invest** or **Stocks & Shares ISA** account.

> **Default mode is PAPER (Trading 212 practice/demo account). Live trading is OFF and requires two explicit
> switches.** No CFDs, no FX trading, no leverage, no shorting, no options.
>
> Nothing here promises profit. The research pipeline is designed to say **"do not deploy"** if the evidence
> is weak, and it will say so if that is the result.

Full design: [`docs/PHASE1_SPECIFICATION.md`](docs/PHASE1_SPECIFICATION.md) ·
Build status and findings: [`docs/IMPLEMENTATION_REPORT.md`](docs/IMPLEMENTATION_REPORT.md)

---

## 1. What the strategy does (plain English)

1. Every trading day it looks at roughly 500 large US companies (the S&P 500) that Trading 212 lets you trade.
2. It ranks them by how much they have risen over the past year, **skipping the most recent month**
   (the classic "12-1 momentum" measure). That month is skipped because very short-term moves tend to reverse.
3. It keeps only stocks that are:
   * in the **top 20%** of that ranking,
   * up over the year **and** beating the S&P 500 (relative strength),
   * in an **uptrend**: price above its 200-day average, and 50-day average above 200-day average,
   * not wildly volatile.
4. It only buys when the **overall market is healthy**: the S&P 500 is above its own 200-day average.
5. Each purchase gets a **stop-loss** (a sell order that rests at Trading 212) and a **profit target**.
   It also sells after at most 40 trading days.

### Why momentum?
Decades of academic research (Jegadeesh & Titman 1993, Carhart 1997, Asness et al. 2013) found that stocks that
have done well over the past 3–12 months have tended to keep doing relatively well for a few more months.
The same research also shows **momentum crashes** (e.g. 2009), weaker results in large caps, and sensitivity to
costs. That is why this project tests the idea rigorously instead of assuming it works.

### What "0.5% risk" means
If a trade hits its stop-loss, you lose **at most about 0.5% of the money the bot manages** (plus any gap if the
price jumps through the stop). With £10,000 that is £50. The bot never simply "puts 10% in each stock".
It works backwards from the stop:

```
risk per share (GBP) = (entry limit price − stop price after slippage + FX fee both ways + US sell fees) ÷ GBP/USD rate
shares               = (0.5% × equity) ÷ risk per share      → always rounded DOWN
```

Example: £10,000 account, GBP/USD 1.25, stock at $100, ATR $2 → stop $94, entry limit $100.50
→ about £5.84 at risk per share → **8 shares** (≈ £643 position, ≈ £47 at risk).
Further caps: at most 20% of equity in one stock, never more than the available cash (no borrowing), total
open risk ≤ 5%, at most 10 positions, and at most 3 per sector.

### What "1:2 risk:reward" means
The profit target is set so that, **after costs**, the planned gain is at least **2×** the planned loss
(risk £50 → target ≥ £100). Honest note: this rule alone creates no edge. The strategy only makes money if it
wins often enough (more than about 1 time in 3 at 2R, before costs).

---

## 2. Install (exact commands)

**Python 3.11 or newer** is required.
* Windows: install from <https://www.python.org/downloads/> and tick "Add Python to PATH".
* macOS: `brew install python@3.12`.
* Ubuntu: `sudo apt install python3 python3-venv`.

```bash
git clone https://github.com/daviddCode1/stockTradeBot.git
cd stockTradeBot
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                  # should print "68 passed" (no network, no broker needed)
```

## 3. Trading 212 API keys

1. In the Trading 212 app, **switch to your Practice (demo) account**.
2. Go to *Settings → API (Beta)* → *Generate API key*. Give it the permissions **Account data, Portfolio,
   Metadata, Orders (read), History**. Only add **Orders (execute)** when you are ready for paper orders.
3. Optionally restrict the key to your IP address.
4. Copy the key and secret (the secret is shown only once).

## 4. Configure `.env`

```bash
cp .env.example .env
```
Edit `.env`:
```
T212_API_KEY=your-demo-key
T212_API_SECRET=your-demo-secret
ACCOUNT_TYPE=INVEST            # or ISA. Anything else and the bot refuses to run
ACCOUNT_BASE_CURRENCY=GBP
TRADING_MODE=PAPER
ENABLE_LIVE_TRADING=NO
DRY_RUN=true                   # true = decide and log, but send no orders at all
```
`.env` is git-ignored and never printed. Check the connection (read-only):
```bash
python -m src.main status
```

## 5. Run the research (backtests)

```bash
python -m src.main download-data        # about 700 current + former S&P 500 stocks since 1998 (takes a while)
python -m src.main research             # all strategies, walk-forward, robustness, Monte Carlo
```
Output: `reports/research/<date>/report.md` (+ `oos_equity.png`, `summary.json`, `trials.jsonl`).
Single backtest: `python -m src.main backtest --strategy C`.

### How to read the results
* **Out-of-sample (OOS)** numbers are the ones that matter. In-sample numbers are optimistic by construction.
* The **Acceptance test** table lists every pre-registered criterion as PASS/FAIL. **If a strategy has any FAIL,
  it does not qualify.** The Verdict section says so plainly.
* **Deflated Sharpe** corrects for the number of variants tried. A low value means "could be luck".
* **Monte Carlo** shows how bad drawdowns and losing streaks could plausibly have been with a different ordering of
  the same trades. It is a simulation, not a forecast.
* Free Yahoo data lacks most delisted companies, so results are **survivorship-biased (too optimistic)**.
  The report states this.

## 6. Paper trading (Trading 212 practice account)

The paper bot runs the **same code** as the backtest: the same `decide()` function, the same risk engine and the
same sizing. Only the destination changes.

Order of operations each trading day (UK times; the code converts time zones itself):

| When | Command | What it does |
|---|---|---|
| ~13:00 UK (before the 14:30 US open) | `python -m src.main cycle` | reconcile → data → signals → risk → exits, stop raises, **DAY limit** entry orders |
| ~15:00 UK (after the open) | `python -m src.main protect` | places the resting **stop-loss** for any entry that filled |
| optional ~19:00 UK | `python -m src.main protect` | re-check that every position has its stop |

Why before the open? Trading 212 `DAY` orders **expire at midnight New York time**, so an order placed the evening
before would expire before the next session.

Steps to go from dry run to real paper orders:
1. Run with `DRY_RUN=true` for a few days and read `reports/daily/*.json` and `logs/bot.jsonl`.
2. Give the demo key the **Orders (execute)** permission and set `DRY_RUN=false`. Orders now go to the
   **practice** account only.

Scheduling with cron (Linux/macOS, times in UK local time; `crontab -e`):
```
0 13 * * 1-5  cd /path/to/stockTradeBot && ./scripts/run_daily.sh cycle
0 15 * * 1-5  cd /path/to/stockTradeBot && ./scripts/run_daily.sh protect
0 19 * * 1-5  cd /path/to/stockTradeBot && ./scripts/run_daily.sh protect
```
On Windows, create Task Scheduler tasks that run `.venv\Scripts\python.exe -m src.main cycle` (and `protect`) with
"Start in" set to the project folder. The bot itself skips US holidays using the NYSE calendar.

Your existing manual positions in the account are **never touched**. They are flagged as "unmanaged" and excluded
from the money the bot manages.

## 7. Live mode (not recommended until every gate is passed)

Live mode requires **all** of the following:
1. The research report shows a strategy that **passes every acceptance criterion**.
2. At least 3 months of clean paper trading (no duplicate orders, no unresolved reconciliation issues, fills
   and fees in line with the model).
3. A **live** API key in `.env`, plus:
```
TRADING_MODE=LIVE
ENABLE_LIVE_TRADING=YES
T212_EXPECTED_ACCOUNT_ID=<your live account number>
DRY_RUN=false
```
If any one of these is missing, the bot runs in PAPER mode.

## 8. Stopping the bot / emergencies

```bash
python -m src.main kill on       # creates ./KILL_SWITCH: no new orders; existing stop-losses stay at the broker
python -m src.main kill off
python -m src.main reset-daily-limit   # after a -1.5% day the bot stops buying until you reset it
```
Or set `KILL_SWITCH=true` in `.env`. To stop completely, remove the cron entries. Protective stops remain at
Trading 212 until you cancel them in the app.

## 9. Safety rules built in ("no trade beats a bad trade")

The bot does **not** trade when: data is missing or stale · the broker is unreachable · an order outcome is unknown
· reconciliation finds something unexplained · a stop cannot be set · R:R < 2 after costs · risk or size is
uncertain · the stock is not tradable on Trading 212 · portfolio limits are full · the daily loss limit or kill
switch is active · an earnings date is within 3 days (or unknown, in paper/live).

Order endpoints at Trading 212 are **not idempotent**. Every order is written to the database *before* it is sent,
sent exactly **once**, and an unclear result (timeout or 5xx) is resolved from broker state. It is never blindly
retried.

## 10. Risks and limitations

* Momentum can crash sharply, especially in rebounds after bear markets. Long-only portfolios cannot hedge this.
* Stop-losses become market orders. **Gaps can cause losses larger than 0.5%.**
* Costs: 0.15% FX fee on **both** buy and sell, plus spread and slippage, is a meaningful drag.
* Free data: survivorship bias, symbol re-use, current (not historical) sectors, incomplete earnings history.
* The Trading 212 API is in beta and may change. Practice-account fills are simulated and may be kinder than real
  fills.
* Past performance, backtests and simulations do not predict future results.

## Project layout

```
src/main.py                CLI                          src/config.py        settings + mode safety
src/broker/                Trading 212 adapter          src/data/            market data, universe, calendar
src/strategy/              signals S0/A/B/C/D/E         src/portfolio/       decide(), sector/correlation, heat
src/risk/                  stops, sizing, limits        src/execution/       orders, reconciliation, daily cycle
src/backtest/              engine, metrics, WF, MC      src/reporting/       research report
src/storage/               SQLite                       src/monitoring/      logs, kill switch, health
config/settings.yaml       pre-registered parameters    tests/               68 tests (mocked broker)
```
