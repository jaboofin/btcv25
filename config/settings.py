"""
╔══════════════════════════════════════════════════════════════════════╗
║  BTC-15M-Oracle — CONFIGURATION                                     ║
║                                                                      ║
║  v2.5 — Active 5m parallel trading loop (Phase 3)                   ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
from dataclasses import dataclass, field
from enum import Enum


class MarketDirection(Enum):
    UP = "up"
    DOWN = "down"
    HOLD = "hold"


@dataclass
class OracleConfig:
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    binance_base_url: str = "https://api.binance.com/api/v3"
    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
    coincap_base_url: str = "https://api.coincap.io/v2"
    poll_interval: int = 10
    max_price_age: int = 30
    min_oracle_consensus: int = 2
    history_candle_count: int = 100
    candle_interval: str = "15m"


@dataclass
class PolymarketConfig:
    clob_api_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    rpc_url: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    funder: str = os.getenv("POLY_FUNDER", "")
    sig_type: int = int(os.getenv("POLY_SIG_TYPE", "0"))
    market_slug_pattern: str = "btc-price"
    market_interval_minutes: int = 15
    order_type: str = "market"
    max_slippage_pct: float = 2.0
    min_liquidity_usd: float = 50.0
    sync_live_bankroll: bool = False
    live_bankroll_poll_secs: int = 60
    # ── Fee handling (Phase 1) ──
    fee_cache_ttl_secs: int = 60         # how long to cache fee lookups per token
    fee_fallback_pct: float = 1.56       # fallback fee % if API lookup fails (worst-case at 50c)


@dataclass
class StrategyConfig:
    confidence_threshold: float = 0.60
    strong_signal_threshold: float = 0.75
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    ema_fast: int = 5
    ema_slow: int = 15
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    momentum_lookback: int = 3
    min_volatility_pct: float = 0.03
    max_volatility_pct: float = 3.0
    weight_momentum: float = 0.30
    weight_rsi: float = 0.25
    weight_macd: float = 0.25
    weight_ema_cross: float = 0.20


@dataclass
class RiskConfig:
    max_trade_pct: float = 5.0
    max_daily_trades: int = 20
    max_daily_loss_pct: float = 25.0
    max_consecutive_losses: int = 5
    loss_streak_cooldown_mins: int = 60
    kelly_fraction: float = 0.25
    min_trade_size_usd: float = 1.0
    max_trade_size_usd: float = 25.0


@dataclass
class EdgeConfig:
    """Arbitrage + Hedge toggles."""
    # ── Arbitrage (independent scanner) ──
    enable_arb: bool = False             # --arb to turn on
    arb_threshold: float = 0.98          # buy both if YES+NO < this
    arb_min_edge_pct: float = 1.0        # skip tiny edges below 1%
    arb_size_usd: float = 5.0           # USD per side on arb trades
    arb_poll_secs: float = 8.0           # scan every N seconds
    arb_max_daily_trades: int = 50       # daily arb trade pair limit
    arb_max_daily_budget: float = 20.0  # max USD committed per day
    arb_cooldown_secs: float = 120.0     # don't re-arb same market within 2min
    arb_timeframes: list = field(default_factory=lambda: ["5m", "15m", "30m", "1h"])
    # ── Hedge ──
    enable_hedge: bool = False           # --hedge to turn on
    hedge_min_confidence: float = 0.65   # only hedge if flip signal is strong


@dataclass
class LoggingConfig:
    log_dir: str = "logs"
    trade_log_file: str = "logs/trades.jsonl"
    strategy_log_file: str = "logs/strategy.jsonl"
    oracle_log_file: str = "logs/oracle.jsonl"
    error_log_file: str = "logs/errors.log"
    performance_file: str = "data/performance.json"
    alert_on_loss_streak: int = 3
    alert_on_oracle_downtime_secs: int = 60


@dataclass
class LateWindowConfig:
    """Phase 2: Late-window conviction trading based on Chainlink drift."""
    enabled: bool = False                 # --late-window to turn on
    lead_secs: int = 150                  # seconds before window close to check (2.5 min)
    min_drift_pct: float = 0.08           # minimum Chainlink drift % from anchor to trigger
    # Confidence scaling: drift → confidence
    # 0.08% drift → 0.80 confidence, 0.15% → 0.88, 0.25%+ → 0.95
    base_confidence: float = 0.80         # confidence at min_drift_pct
    max_confidence: float = 0.95          # cap confidence (lowered — don't oversize)
    drift_scale_pct: float = 0.25         # drift % at which confidence hits max
    max_entry_price: float = 0.80         # skip if token > 80¢ (need 20¢+ edge per win)
    # Budget: separate from main directional trades
    max_daily_trades: int = 12            # fewer but higher quality
    budget_pct: float = 25.0              # % of daily budget reserved for late-window (reduced)
    max_trade_size_usd: float = 8.0       # max USD per late-window trade (reduced from 10)


@dataclass
class MarketMakerConfig:
    """Phase 4: Market making — post-only orders, zero fees, earn rebates."""
    enabled: bool = False                 # --mm to turn on
    # ── Quoting ──
    spread_bps: int = 400                 # half-spread in basis points (200 bps = 2 cents each side)
    order_size_usd: float = 3.0           # USD per side per quote
    num_levels: int = 1                   # number of price levels each side
    level_spacing_bps: int = 100          # spacing between levels in bps (1 cent)
    refresh_secs: float = 15.0            # re-quote interval
    # ── Inventory ──
    max_inventory_imbalance: float = 10.0 # max $ net position before widening
    skew_bps_per_dollar: int = 10         # widen the heavy side by N bps per $ of imbalance
    # ── Safety ──
    pull_before_close_secs: int = 60      # cancel all quotes N secs before window resolution
    max_daily_budget: float = 50.0        # max USD committed across all maker orders per day
    max_open_orders: int =4             # max simultaneous open orders
    timeframes: list = field(default_factory=lambda: ["15m", "5m"])


@dataclass
class Active5mConfig:
    """Phase 3: Parallel 5-minute directional trading loop."""
    enabled: bool = False                 # --5m to turn on
    # ── Budget (separate from 15m) ──
    budget_pct: float = 30.0              # % of bankroll allocated to 5m trades
    max_daily_trades: int = 30            # 5m trades per day (more windows = higher limit)
    max_trade_size_usd: float = 10.0      # max USD per 5m trade
    max_daily_loss_pct: float = 15.0      # separate loss circuit breaker for 5m
    max_consecutive_losses: int = 4       # tighter than 15m since faster feedback
    loss_streak_cooldown_mins: int = 30   # shorter cooldown (30min vs 60min for 15m)
    # ── Timing ──
    strategy_delay_secs: int = 45         # wait 45s after anchor → fires at boundary
    entry_lead_secs: int = 55             # capture anchor 55s before 5m boundary
    entry_window_secs: int = 20           # 20s entry window


@dataclass
class BotConfig:
    oracle: OracleConfig = field(default_factory=OracleConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    late_window: LateWindowConfig = field(default_factory=LateWindowConfig)
    market_maker: MarketMakerConfig = field(default_factory=MarketMakerConfig)
    active_5m: Active5mConfig = field(default_factory=Active5mConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    bot_name: str = "BTC-15M-Oracle"
    version: str = "2.5.0"
    # Bankroll — set via CLI: python bot.py --bankroll 500
    bankroll: float = 500.0
    # Clock-sync timing
    entry_lead_secs: int = 60 
    entry_window_secs: int = 30 
    sleep_poll_secs: int = 5
    # ── NEW: Strategy delay ──
    # Seconds to wait AFTER capturing the anchor price before running the
    # strategy. This lets BTC drift away from the open so that price_vs_open
    # (35% weight) has a meaningful signal instead of always being ~0.
    # Recommended: 30-60 seconds. Set to 0 to disable (old behavior).
    strategy_delay_secs: int = 45
