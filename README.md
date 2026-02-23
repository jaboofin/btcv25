<div align="center">

# ₿ BTC-15M-Oracle

**Autonomous multi-engine BTC prediction bot for Polymarket**

Chainlink-anchored · Drift-dominant strategy · 6 parallel engines · Live CLOB execution · Real-time dashboard

```
python bot.py --bankroll 500 --arb --late-window --5m --dashboard
```

**v2.6** — Strategy overhaul, late-window risk/reward fix, live dashboard heartbeat

</div>

---

## What This Does

Every 15 minutes, Polymarket runs a binary market: *"Will BTC go up or down?"*

These markets resolve against **Chainlink's BTC/USD data stream** — if the Chainlink price at the end of the window is greater than or equal to the price at the start, UP wins. Otherwise, DOWN wins.

This bot trades those markets autonomously across **six parallel engines**:

| Engine | What it does | Cycle |
|--------|-------------|-------|
| **Directional 15m** | Core strategy — predicts UP/DOWN for each 15-minute window | Every 15 min |
| **Directional 5m** | Same strategy on 5-minute windows (triples opportunity surface) | Every 5 min |
| **Late-Window** | High-conviction trades near window close when Chainlink has drifted | Continuous scan |
| **Arbitrage** | Captures mispricings when YES + NO < $1.00 across timeframes | Every 8 sec |
| **Market Maker** | Posts bid/ask on both sides to earn spread + Polymarket USDC rebates | Every 15 sec |
| **Hedge** | Protects open positions when the strategy signal flips | On signal flip |

The bot wakes 60 seconds before each 15-minute boundary, captures the **Chainlink opening price** (the exact number it needs to beat), waits 45 seconds for BTC to drift from the anchor, then analyzes the drift using a weighted signal engine. If the drift is meaningful and confidence exceeds 60%, it places a signed order via Polymarket's CLOB.

Between 15-minute cycles, the 5-minute loop trades independently at every non-overlapping 5m boundary, the late-window scanner checks for high-drift opportunities near close, and the arb scanner polls for mispricings.

---

## Quickstart

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Export your Polymarket wallet keys**

Go to [reveal.polymarket.com](https://reveal.polymarket.com) to get your private key, then grab your **funder address** from your Polymarket **profile page** (not the deposit address).

```bash
export POLY_PRIVATE_KEY="your_private_key"
export POLY_FUNDER="0xYourFunderFromProfilePage"
export POLY_SIG_TYPE=1
```

> **CRITICAL:** For email/Magic Link accounts, use the funder address from your **profile page**, NOT the deposit address. Using the deposit address will cause trades to fail silently.

> **SIG_TYPE:** `1` = email/Magic login (most common), `2` = browser wallet (MetaMask), `0` = direct EOA

**3. Run**

```bash
# Recommended: all engines + dashboard
python bot.py --bankroll 100 --arb --late-window --5m --dashboard

# Directional only (conservative)
python bot.py --bankroll 100

# Arb-only mode (no directional, reads live balance)
python bot.py --arb-only --dashboard
```

The bot is now live. Open `http://localhost:8765` to see the real-time dashboard with live BTC price, trade notifications, and heartbeat indicators.

---

## How It Works

### Timing

The bot is **clock-synced** to Polymarket's window boundaries. The 15-minute loop runs at :00, :15, :30, :45. The 5-minute loop runs at the 8 non-overlapping boundaries per hour (:05, :10, :20, :25, :35, :40, :50, :55).

```
11:54:05  →  [5m]  anchor capture (55s before :55)
11:54:50  →  [5m]  strategy fires (45s after anchor)
11:59:00  →  [15m] anchor capture (60s before :00)
11:59:45  →  [15m] strategy fires (45s after anchor)
12:04:05  →  [5m]  anchor capture
...
```

The 45-second **strategy delay** is critical: it lets BTC drift from the anchor so the `price_vs_open` signal (70% weight) has a meaningful value instead of ~0%.

At shared boundaries (:00, :15, :30, :45), the 5m loop detects the overlap and skips — the 15m loop handles it. No duplicate trades.

### Pipeline (each cycle)

```
Anchor → [45s delay] → Oracle → Strategy → Risk Check → Market Discovery → Execute → Resolve
```

**Anchor** — Captures the Chainlink BTC/USD price at the start of the window. This is the **price to beat** — Polymarket resolves UP if the closing Chainlink price ≥ this number.

**Oracle** — Three sources: Chainlink (resolution oracle, via persistent RTDS WebSocket), Binance, CoinGecko. Chainlink is primary. Rejects stale prices (>30s) and flags divergence >1%.

**Strategy** — Drift-dominant weighted signal engine (v2.6):

| Signal | Weight | What it does |
|--------|--------|-------------|
| **Price vs Open** | **70%** | **Where is BTC now vs the window open? Directly maps to resolution.** |
| Momentum | 9% | Price delta over 3 candles — raw directional pressure |
| RSI | 7.5% | 14-period Wilder's — overbought/oversold |
| MACD | 7.5% | 12/26/9 — trend momentum + histogram |
| EMA Cross | 6% | Fast(5) / Slow(15) — short-term trend |

**Key protections in the strategy:**

- **Dead zone**: Below 0.04% drift from open, the strategy returns HOLD. This is ~$26 on BTC $65k — below this threshold, BTC hasn't committed to a direction.
- **Agreement filter**: If price_vs_open says UP but 3+ indicators say DOWN, the trade is skipped. Prevents trading into conflicted signals.
- **Fee-adjusted edge check**: If expected edge < estimated Polymarket taker fee (~1.5%), the trade is skipped.

**Risk** — Daily trade cap (20), daily loss limit (25%), consecutive loss cooldown (5 losses → 60min pause), quarter-Kelly sizing with $25 hard cap. 5m and 15m have fully independent risk budgets.

**Execution** — EIP-712 signed order via Polymarket CLOB. Fill-or-Kill primary, auto-fallback to Good-Til-Cancelled. **Fill verification** catches phantom fills where the CLOB says "matched" but nothing settles on-chain.

---

## Six Engines

### Directional 15m *(core strategy)*

The primary trading engine. Fires 4 times per hour at 15-minute boundaries. Uses the full 5-signal strategy with 70% weight on drift from the Chainlink anchor.

### Directional 5m (`--5m`)

Independent parallel loop trading 5-minute windows. Same strategy engine but with its own timing (55s lead, 45s delay, 20s entry window), budget (30% of bankroll), trade limits (30/day), and loss streak cooldown (4 losses → 30min pause). Triples the number of trading opportunities from 4 to 12 per hour.

### Late-Window (`--late-window`)

Continuous scan of all active markets approaching their close (30–150 seconds remaining). When the Chainlink oracle shows BTC has drifted ≥0.08% from the window anchor, a high-conviction trade is placed in the drift direction. Pure drift signal — no technical indicators.

**v2.6 risk/reward fix:** Max entry price capped at $0.80 (down from $0.90). This ensures at least $0.20 upside per winning trade instead of $0.03–$0.07. Fewer trades but each one has real edge.

| Setting | Value | What it controls |
|---------|-------|-----------------|
| `min_drift_pct` | 0.08% | Minimum Chainlink drift to trigger |
| `max_entry_price` | $0.80 | Skip if token costs more than this |
| `max_confidence` | 0.95 | Cap confidence to limit oversizing |
| `max_trade_size_usd` | $8 | Max USD per late-window trade |
| `max_daily_trades` | 12 | Late-window trades per day |
| `budget_pct` | 25% | Bankroll allocated to late-window |

### Arbitrage (`--arb`)

Independent fast-polling scanner — runs every ~8 seconds, separate from directional cycles. Scans BTC markets across 5m, 15m, 30m, and 1h timeframes. When YES + NO < threshold (default 0.98), buys both sides for risk-free profit on resolution.

```
YES = $0.45  ·  NO = $0.48  ·  total = $0.93
Buy both → one always resolves to $1.00
Profit = $0.07 per share (7.5%) — zero risk
```

| Setting | Default | What it controls |
|---------|---------|-----------------|
| `arb_threshold` | 0.98 | Only arb if YES + NO < this |
| `arb_min_edge_pct` | 1.0% | Ignore tiny edges |
| `arb_size_usd` | $5 | USD per side |
| `arb_poll_secs` | 8.0 | Scan interval |
| `arb_max_daily_budget` | $20 | Max USD committed per day |
| `arb_timeframes` | 5m, 15m, 30m, 1h | Timeframes to scan |

### Market Maker (`--mm`)

Posts limit orders on both sides of BTC binary markets. Quotes bid (YES) and ask (NO) at prices summing to less than $1.00, capturing the spread when both fill. Includes inventory skew to prevent one-sided exposure, and cancels all quotes 60 seconds before window resolution.

### Hedge (`--hedge`)

Tracks open positions. If the strategy flips direction while you're holding, the hedge engine buys the opposite side to lock in a guaranteed outcome regardless of resolution.

---

## Dashboard

The bot includes a real-time web dashboard at `http://localhost:8765`.

```bash
python bot.py --bankroll 500 --dashboard
```

### Features

- **Live BTC price** — Updates every 2 seconds from the RTDS stream with green/red flash on direction change
- **Heartbeat indicator** — Pulsing green dot with expanding ring animation. Turns amber if no data for 5 seconds. Animated signal bars show stream activity.
- **Live clock** — Always ticking in the hero bar
- **Trade toast notifications** — Slide-in popups when trades open (amber), win (green), or lose (red) with P&L amount. Auto-dismiss after 6 seconds.
- **Hero banner** — Total profit with glow, BTC price, win rate, countdown to next entry
- **Stat cards** — Bankroll, Realized P&L, Window Open (anchor + drift), Strategy direction, Daily Risk
- **Equity curve** — Canvas chart with gradient fill
- **Arb scanner panel** — Markets live, trades, profit, best edge, live market table
- **Positions** — Tabbed open/closed with direction badges, entry price, confidence, P&L
- **Engine status** — All 6 engines with color-coded ACTIVE/OFF badges
- **Oracle sources** — Chainlink (amber), Binance, CoinGecko with spread %
- **Last Decision** — Raw strategy output: direction, confidence, drift, volatility, reason
- **Activity Feed** — Chronological trade entries, resolutions, cooldowns, anchors
- **RTDS Stream** — Streaming/Buffered status with live Chainlink price

The dashboard is **always alive** — BTC price moves between cycles, heartbeat pulses on every data tick, and toast notifications fire on every trade event.

---

## CLI Reference

```bash
# Full stack
python bot.py --bankroll 500 --arb --late-window --5m --mm --dashboard

# Conservative directional only
python bot.py --bankroll 100

# Directional + late-window + 5m (no arb/mm)
python bot.py --bankroll 200 --late-window --5m --dashboard

# Arb-only (auto-reads live balance)
python bot.py --arb-only --dashboard

# Fixed number of cycles
python bot.py --bankroll 100 --cycles 10 --late-window --5m
```

| Flag | Default | Description |
|------|---------|-------------|
| `--bankroll` | 500 | Starting capital in USD |
| `--cycles` | 0 | Max entry windows (0 = run forever) |
| `--arb` | off | Enable arb scanner alongside directional |
| `--arb-only` | off | Run ONLY arb scanner (reads live balance) |
| `--late-window` | off | Enable late-window conviction trades |
| `--5m` | off | Enable parallel 5-minute trading loop |
| `--mm` | off | Enable market maker engine |
| `--hedge` | off | Enable hedge engine |
| `--dashboard` | off | Start dashboard server on :8765 |
| `--sync-live-bankroll` | off | Sync bankroll from live Polymarket balance |

**Ctrl+C** → graceful shutdown. Saves performance, cancels orders, closes connections.

---

## Wallet Setup

### Polymarket Email Login *(most users)*

1. Log in at [polymarket.com](https://polymarket.com) with your email
2. Go to your **Profile page** → copy your **funder address** → this is `POLY_FUNDER`
3. Go to [reveal.polymarket.com](https://reveal.polymarket.com) → export your key → this is `POLY_PRIVATE_KEY`
4. Set `POLY_SIG_TYPE=1`
5. Make sure your wallet has USDC on Polygon

> **IMPORTANT:** Use the funder address from your **profile page**, NOT the deposit address. The deposit address will not work for trading.

### Browser Wallet (MetaMask, Coinbase Wallet)

1. Connect your wallet to Polymarket
2. Export your Polygon private key → `POLY_PRIVATE_KEY`
3. Copy your Polymarket proxy address → `POLY_FUNDER`
4. Set `POLY_SIG_TYPE=2`

### Direct EOA

1. Use any Polygon private key → `POLY_PRIVATE_KEY`
2. No funder needed
3. Set `POLY_SIG_TYPE=0`

---

## Configuration

All tuning parameters live in `config/settings.py`.

### Timing

| Param | 15m | 5m | What it does |
|-------|-----|-----|-------------|
| `entry_lead_secs` | 60 | 55 | How early to capture the anchor |
| `strategy_delay_secs` | 45 | 45 | Wait after anchor before strategy fires |
| `entry_window_secs` | 30 | 20 | How long to place the order |

### Strategy (v2.6)

| Param | Default | What it does |
|-------|---------|-------------|
| `confidence_threshold` | 0.60 | Minimum score to trade |
| `min_volatility_pct` | 0.03 | Skip if volatility below this |
| `max_volatility_pct` | 3.0 | Skip if volatility above this |
| `price_vs_open weight` | 70% | Drift from anchor (dominant signal) |
| `indicator weights` | 30% | RSI + MACD + momentum + EMA (tiebreakers) |
| `dead_zone` | 0.04% | Below this drift, strategy returns HOLD |
| `agreement_filter` | 3/4 | Skip if 3+ indicators oppose drift direction |

### Risk

| Param | 15m | 5m | What it does |
|-------|-----|-----|-------------|
| `max_trade_size_usd` | $25 | $10 | Hard cap per trade |
| `max_daily_trades` | 20 | 30 | Daily trade limit |
| `max_daily_loss_pct` | 25% | 15% | Daily drawdown circuit breaker |
| `max_consecutive_losses` | 5 | 4 | Triggers cooldown |
| `loss_streak_cooldown_mins` | 60 | 30 | Cooldown duration |
| `kelly_fraction` | 0.25 | 0.25 | Position sizing |

---

## Project Structure

```
btc-15m-oracle/
├── bot.py                        Main orchestrator — 6 engines, clock sync, dashboard
│
├── config/
│   └── settings.py               All parameters — strategy, risk, timing, engines
│
├── core/
│   ├── polymarket_client.py      CLOB SDK — orders, fees, fill verification
│   ├── risk_manager.py           Kelly sizing, 15m/5m/LW independent budgets
│   ├── arb_scanner.py            Independent arb engine (5m/15m/30m/1h)
│   ├── edge.py                   Hedge engine (signal-flip protection)
│   ├── dashboard_server.py       HTTP + WebSocket server + heartbeat + toasts
│   └── trade_logger.py           Structured JSONL logging
│
├── oracles/
│   └── price_feed.py             Persistent RTDS stream + Chainlink/Binance/CoinGecko
│
├── strategies/
│   └── signal_engine.py          Drift-dominant strategy (70/30 weight split)
│
├── logs/                         Runtime logs (trades, strategy, oracle, errors)
├── data/                         Performance snapshots
├── .env.example                  Wallet config template
└── requirements.txt              Python dependencies
```

---

## Logs

| File | What's in it |
|------|-------------|
| `logs/trades.jsonl` | Every order — direction, size, fill price, order ID, engine type, tx hashes |
| `logs/strategy.jsonl` | Every decision — signal values, confidence, drift, hold reasons |
| `logs/oracle.jsonl` | Every price fetch — Chainlink, sources, spread |
| `logs/errors.log` | Errors with stack traces |
| `data/performance.json` | Latest cumulative stats |

---

## Safety

**Chainlink-anchored** — Uses the same oracle Polymarket resolves against. The bot knows the exact price to beat.

**Persistent RTDS stream** — Single WebSocket connection to Polymarket's real-time data stream. Zero 429 rate limits. Automatic reconnection with exponential backoff (5s → 120s cap).

**Fill verification** — After CLOB returns success, re-checks order status after 3+2 seconds. Catches phantom fills where the CLOB matches but on-chain settlement fails. Prevents tracking positions that don't exist.

**Signal agreement filter** — Won't trade when drift and technical indicators conflict (3+ indicators opposing drift direction).

**Dead zone** — Ignores tiny drift (<0.04%) that's within bid-ask noise.

**Independent risk budgets** — 15m, 5m, and late-window each have separate daily budgets, trade limits, and loss streak cooldowns. A bad run in one engine cannot drain another's capital.

**Late-window entry cap** — Max $0.80 token price ensures at least 20¢ upside per win, preventing the "pennies in front of a steamroller" problem.

**Daily circuit breaker** — Stops trading when daily losses exceed the configured threshold.

**Loss streak cooldown** — Pauses after consecutive losses (5 for 15m → 60min, 4 for 5m → 30min).

**Conservative sizing** — Quarter-Kelly with hard caps ($25 for 15m, $10 for 5m, $8 for late-window).

**Full audit trail** — Every oracle query, strategy decision, risk check, and trade execution logged to JSONL.

---

## Dependencies

```
aiohttp>=3.9.0           Async HTTP + WebSocket for RTDS stream, APIs, dashboard
py-clob-client>=0.34.0   Polymarket CLOB SDK — order signing + execution
web3==6.14.0              Ethereum interaction (pinned for compatibility)
python-dotenv>=1.0.0      Environment variable loading
```

---

## Changelog

### v2.6 (February 23, 2026)
- **Strategy overhaul**: price_vs_open weight 35% → 70%, indicators 65% → 30%
- **Dead zone**: 0.01% → 0.04% minimum drift to trade
- **Signal agreement filter**: skip when 3+ indicators oppose drift
- **Late-window fix**: max entry $0.90 → $0.80, min drift 0.05% → 0.08%, budget reduced
- **Dashboard heartbeat**: live BTC price every 2s, pulsing indicators, tick counter
- **Trade toast notifications**: slide-in popups for trade opens and resolutions
- **Fill verification case fix**: .lower() normalization for CLOB status check

### v2.5 (February 22, 2026)
- Phase 1: Dynamic fees + arbitrage scanner
- Phase 2: Late-window conviction trading
- Phase 3: Parallel 5-minute trading loop
- Phase 4: Market maker engine
- Persistent RTDS WebSocket stream
- Late-window safety (30s floor, $0.90 cap, 5m skip)
- Fill verification system
- Dashboard v3 (GRIDPHANTOMDEV dark theme, 6 engines)

---

## Disclaimer

This bot places real orders with real money on Polymarket prediction markets. Start with a small bankroll you're comfortable losing. Cryptocurrency markets are volatile and prediction markets carry additional risks. Past performance does not guarantee future results. The authors assume no liability for financial losses.
